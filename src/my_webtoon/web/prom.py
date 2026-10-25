"""Prometheus 메트릭 — oh-my-homelab 의 Prometheus/Grafana 가 긁어 간다 (deploy/monitoring.yaml).

metrics.py(화면용 최근 2000건)와 따로 두는 이유: 그쪽은 파드 안 메모리에서 바로 그리는 '지금 상태'이고,
이쪽은 Prometheus 가 30초마다 가져가 시간축으로 쌓는 '추이'다.

    HTTP      요청 수·지연 (라우트 패턴별)        — 매 요청 미들웨어에서 센다
    앱 상태    작품·평가·수집 큐·마지막 수집·백업   — 긁을 때마다 SQLite 에서 읽는다 (몇 ms)
    프로세스   메모리·CPU·열린 파일               — prometheus_client 기본 수집기

공개 포트(8000)가 아니라 METRICS_PORT(배포에선 9464)에서만 연다. 공개 도메인(터널)은 8000 에만 붙어 있어서
/metrics 가 바깥에 보이지 않는다. NetworkPolicy 도 이 포트는 monitoring 네임스페이스만 허용한다.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from prometheus_client import (
    CollectorRegistry,
    Counter,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
    start_http_server,
)
from prometheus_client.core import GaugeMetricFamily

from .. import db

log = logging.getLogger(__name__)
PREFIX = "mywebtoon"
SKIP = (
    "/static",
    "/healthz",
    "/readyz",
)  # 프로브·정적 파일은 빼야 실제 화면 지표가 보인다 (metrics.py 와 같은 기준)
BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)


def _ts(iso: str | None) -> float | None:
    return datetime.fromisoformat(iso).timestamp() if iso else None


class AppCollector:
    """긁을 때마다 DB 에서 현재 상태를 읽어 게이지로 내보낸다. 실패해도 HTTP 메트릭은 나가야 하니 삼킨다."""

    def __init__(self, database_path: Path, app_state) -> None:
        self.database_path = database_path
        self.app_state = app_state  # ready(워밍업 끝), crawler.running 을 본다

    def collect(self):
        up = GaugeMetricFamily(f"{PREFIX}_db_up", "DB 를 읽을 수 있으면 1")
        try:
            with db.session(self.database_path) as conn:
                yield from self._from_db(conn)
            up.add_metric([], 1)
        except Exception:  # noqa: BLE001
            log.exception("메트릭 수집 중 DB 읽기 실패")
            up.add_metric([], 0)
        yield up
        ready = GaugeMetricFamily(f"{PREFIX}_ready", "추천 모델 워밍업이 끝났으면 1")
        ready.add_metric([], 1 if self.app_state.ready.is_set() else 0)
        yield ready
        running = GaugeMetricFamily(f"{PREFIX}_crawl_running", "지금 수집 중이면 1")
        running.add_metric([], 1 if self.app_state.crawler.running else 0)
        yield running

    def _from_db(self, conn: sqlite3.Connection):
        def one(sql: str, *params):
            return conn.execute(sql, params).fetchone()[0]

        comics = GaugeMetricFamily(
            f"{PREFIX}_comics", "수집한 작품 수 (연재 상태별)", labels=["status"]
        )
        for status, n in conn.execute(
            "SELECT COALESCE(status, '알 수 없음'), COUNT(*) FROM comics GROUP BY 1"
        ):
            comics.add_metric([status], n)
        yield comics

        free = GaugeMetricFamily(
            f"{PREFIX}_free_episodes", "무료로 볼 수 있는 회차 합 (권은 8화로 환산)"
        )
        free.add_metric(
            [],
            one(
                "SELECT COALESCE(SUM(CASE WHEN unit = '권' THEN free_episodes * 8"
                " ELSE free_episodes END), 0) FROM comics"
            ),
        )
        yield free

        users = GaugeMetricFamily(f"{PREFIX}_users", "사용자 수")
        users.add_metric([], one("SELECT COUNT(*) FROM users"))
        yield users

        reviews = GaugeMetricFamily(
            f"{PREFIX}_reviews", "평가 수 (상태별, 상태 없으면 none)", labels=["status"]
        )
        for status, n in conn.execute(
            "SELECT COALESCE(status, 'none'), COUNT(*) FROM reviews GROUP BY 1"
        ):
            reviews.add_metric([status], n)
        yield reviews

        queue = GaugeMetricFamily(f"{PREFIX}_crawl_queue", "수집 큐 (상태별)", labels=["state"])
        for state, n in conn.execute("SELECT state, COUNT(*) FROM crawl_queue GROUP BY state"):
            queue.add_metric([state], n)
        yield queue

        changes = GaugeMetricFamily(
            f"{PREFIX}_comic_changes_24h",
            "지난 24시간 동안 기록된 작품 변경 (항목별)",
            labels=["field"],
        )
        since = datetime.fromtimestamp(time.time() - 86400, UTC).isoformat(
            timespec="seconds"
        )  # db.now_iso() 형식
        for field, n in conn.execute(
            "SELECT field, COUNT(*) FROM comic_changes WHERE changed_at >= ? GROUP BY field",
            (since,),
        ):
            changes.add_metric([field], n)
        yield changes

        # 마지막으로 '끝난' 수집 한 번의 결과. 수집이 멈췄는지는 finished 시각으로 본다 (알림: 12시간).
        last = conn.execute(
            "SELECT * FROM crawl_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last is not None:
            finished = GaugeMetricFamily(
                f"{PREFIX}_crawl_last_finished_timestamp_seconds", "마지막 수집이 끝난 시각"
            )
            finished.add_metric([], _ts(last["finished_at"]))
            yield finished
            duration = GaugeMetricFamily(
                f"{PREFIX}_crawl_last_duration_seconds", "마지막 수집에 걸린 시간"
            )
            duration.add_metric([], _ts(last["finished_at"]) - _ts(last["started_at"]))
            yield duration
            result = GaugeMetricFamily(
                f"{PREFIX}_crawl_last_pages",
                "마지막 수집 결과 (가져옴/새로 발견/실패/건너뜀)",
                labels=["result"],
            )
            for key in ("fetched", "discovered", "failed", "skipped"):
                result.add_metric([key], last[key] or 0)
            yield result

        backup_at = _ts(db.get_meta(conn, "last_backup_at"))
        if backup_at is not None:
            g = GaugeMetricFamily(f"{PREFIX}_backup_last_timestamp_seconds", "마지막 백업 시각")
            g.add_metric([], backup_at)
            yield g
        from ..backup import last_check  # 순환 import 피하기

        check = last_check(conn)
        if check is not None:
            g = GaugeMetricFamily(f"{PREFIX}_backup_last_ok", "마지막 백업 검사가 통과했으면 1")
            g.add_metric([], 1 if check.get("ok") else 0)
            yield g

        size = GaugeMetricFamily(f"{PREFIX}_db_size_bytes", "SQLite 파일 크기 (WAL 제외)")
        size.add_metric([], self.database_path.stat().st_size)
        yield size


class Metrics:
    """앱 하나에 레지스트리 하나 (테스트가 앱을 여러 번 만들어도 '이미 등록됨' 오류가 안 나게)."""

    def __init__(self, database_path: Path, app_state) -> None:
        self.registry = CollectorRegistry()
        ProcessCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)
        GCCollector(registry=self.registry)
        self.requests = Counter(
            f"{PREFIX}_http_requests",
            "HTTP 요청 수",
            ["route", "method", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            f"{PREFIX}_http_request_duration_seconds",
            "HTTP 응답 시간",
            ["route"],
            buckets=BUCKETS,
            registry=self.registry,
        )
        self.registry.register(AppCollector(database_path, app_state))
        self._server = None

    def observe(self, route: str, method: str, status: int, seconds: float) -> None:
        self.requests.labels(route, method, str(status)).inc()
        self.latency.labels(route).observe(seconds)

    def text(self) -> bytes:
        return generate_latest(self.registry)

    def serve(self, port: int) -> None:
        self._server, _ = start_http_server(port, registry=self.registry)
        log.info("메트릭: :%d/metrics", port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
