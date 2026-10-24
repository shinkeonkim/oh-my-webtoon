"""크롤링 한 번(run) = 아래 3단계.

    1. 시드 갱신 (하루 한 번): TOP100/무료관 페이지에서 productNo 를 모아 큐에 넣는다.
    2. 재수집 예약: 오래된 작품(기본 7일)을 다시 pending 으로 — 무료 회차 수가 자주 바뀐다.
    3. 큐 처리: pending 인 작품의 상세 페이지를 최대 batch_size 개 가져와 저장하고,
       그 페이지 안에서 발견한 다른 작품 링크를 다시 큐에 넣는다 (BFS).

몇 번만 돌려도 시드 수백 개 → 연관작을 타고 수천 개로 불어난다.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import db
from . import parser, seeds
from .client import PoliteClient

log = logging.getLogger(__name__)

SEED_REFRESH = timedelta(hours=24)


@dataclass
class RunResult:
    fetched: int = 0
    discovered: int = 0
    skipped: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"수집 {self.fetched}, 신규 발견 {self.discovered}, "
            f"건너뜀 {self.skipped}, 실패 {self.failed}"
        )


def fetch_one(client: PoliteClient, product_no: int) -> tuple[dict, set[int]]:
    """상세 페이지 하나 → (작품 dict, 페이지 안의 다른 작품 번호들)."""
    page = client.get(f"/comic/detail.series?productNo={product_no}")
    if parser.is_login_page(page.url, page.text):
        raise parser.LoginRequired(product_no)
    if page.status != 200:
        raise RuntimeError(f"HTTP {page.status}")
    return parser.parse_detail(product_no, page.text), parser.extract_product_links(
        page.text, exclude=product_no
    )


def refresh_seeds(conn, client: PoliteClient, result: RunResult, stop: threading.Event) -> None:
    promos: dict[int, str] = {}
    free_paths = set(seeds.free_list_paths())
    for path in seeds.seed_paths():
        if stop.is_set():
            return
        try:
            page = client.get(path)
        except Exception as exc:  # noqa: BLE001 — 시드 하나 실패(robots 차단 포함)로 전체를 멈추지 않는다
            result.notes.append(f"시드 실패 {path}: {exc}")
            continue
        result.discovered += db.enqueue(conn, parser.extract_product_links(page.text), None)
        if path in free_paths:
            promos.update(parser.extract_promos(page.text))
        conn.commit()
    db.replace_promos(conn, promos)
    db.set_meta(conn, "seeds_refreshed_at", db.now_iso())
    conn.commit()


def _seeds_due(conn) -> bool:
    last = db.get_meta(conn, "seeds_refreshed_at")
    return last is None or datetime.now(UTC) - datetime.fromisoformat(last) > SEED_REFRESH


def run_once(
    database_path: Path,
    *,
    user_agent: str,
    delay_seconds: float,
    batch_size: int,
    recrawl_after_days: int,
    force_seeds: bool = False,
    stop: threading.Event | None = None,
) -> RunResult:
    stop = stop or threading.Event()
    result = RunResult()
    with db.session(database_path) as conn, PoliteClient(user_agent, delay_seconds) as client:
        run_id = db.start_run(conn)
        try:
            if force_seeds or _seeds_due(conn):
                log.info("시드 갱신 중")
                refresh_seeds(conn, client, result, stop)

            db.requeue(conn, db.stale_product_nos(conn, recrawl_after_days, batch_size // 3))
            conn.commit()

            for product_no in db.next_pending(conn, batch_size):
                if stop.is_set():
                    result.notes.append("중간에 멈춤")
                    break
                try:
                    data, links = fetch_one(client, product_no)
                except parser.LoginRequired:
                    db.mark_crawled(conn, product_no, "skipped", "로그인 필요(19금)")
                    result.skipped += 1
                except Exception as exc:  # noqa: BLE001 — 한 작품 실패로 전체를 멈추지 않는다
                    log.warning("productNo=%s 실패: %s", product_no, exc)
                    db.mark_crawled(conn, product_no, "failed", str(exc)[:500])
                    result.failed += 1
                else:
                    db.upsert_comic(conn, data)
                    db.mark_crawled(conn, product_no, "done")
                    result.discovered += db.enqueue(conn, links, product_no)
                    result.fetched += 1
                conn.commit()
        finally:
            db.finish_run(
                conn,
                run_id,
                result.fetched,
                result.discovered,
                result.failed,
                "; ".join([result.summary(), *result.notes[:5]]),
            )
    log.info("크롤링 완료: %s", result.summary())
    return result


def import_one(database_path: Path, product_no: int, *, user_agent: str) -> dict:
    """화면에서 'URL 로 추가' 할 때 쓰는 단건 수집. 연관 작품도 큐에 넣어 둔다."""
    with db.session(database_path) as conn, PoliteClient(user_agent, delay_seconds=0) as client:
        data, links = fetch_one(client, product_no)
        db.upsert_comic(conn, data)
        db.enqueue(conn, [product_no], None)
        db.mark_crawled(conn, product_no, "done")
        db.enqueue(conn, links, product_no)
    return data


class BackgroundCrawler:
    """웹 서버 프로세스 안에서 주기적으로 run_once() 를 돌리는 스레드.

    CronJob 으로 따로 띄우지 않는 이유: SQLite 파일(PVC, ReadWriteOnce)을 쓰는 주체를
    하나로 두는 게 가장 단순하기 때문. /crawl 화면의 버튼으로 즉시 실행도 할 수 있다.
    """

    def __init__(self, database_path: Path, settings) -> None:
        self.database_path = database_path
        self.settings = settings
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.last_result: RunResult | None = None
        self.on_finish = None  # 크롤링이 끝나면 부를 콜백 (추천 모델 캐시 무효화)

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="crawler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        interval = self.settings.crawl_interval_hours * 3600
        while not self._stop.is_set():
            self.run_now()
            self._wake.wait(timeout=interval)
            self._wake.clear()

    def run_now(self) -> RunResult | None:
        if not self._lock.acquire(blocking=False):
            return None  # 이미 돌고 있다
        try:
            s = self.settings
            self.last_result = run_once(
                self.database_path,
                user_agent=s.user_agent,
                delay_seconds=s.crawl_delay_seconds,
                batch_size=s.crawl_batch_size,
                recrawl_after_days=s.recrawl_after_days,
                stop=self._stop,
            )
            if self.on_finish:
                self.on_finish()
            return self.last_result
        except Exception:
            log.exception("크롤링 실행 실패")
            return None
        finally:
            self._lock.release()
