"""백업.

SQLite 파일은 Longhorn 볼륨(복제본 1개)에 있다. 파드가 죽거나 재배포돼도 남지만,
그 디스크가 고장 나면 사라진다. 그래서 하루에 한 번 다른 기계(Mac 의 NFS, BACKUP_DIR)로 복사한다.

    BACKUP_DIR/
      my-webtoon-20261024T120000Z.db      전체 DB (sqlite3 backup API — 쓰는 중에도 일관된 사본)
      reviews-20261024T120000Z.json       사용자별 평가 (사람이 읽을 수 있는 형태, /settings 에서 가져오기 가능)

최근 KEEP 개만 남기고 오래된 건 지운다.

백업은 "되살릴 수 있어야" 백업이다. 그래서 만들 때마다 verify_backup() 으로 사본을 열어 본다:
    - PRAGMA integrity_check 가 ok 인지 (파일이 깨지지 않았는지)
    - 사본의 작품·평가·사용자 수가 원본과 맞는지 (백업 직후라 같아야 한다)
결과는 meta 'last_backup_check' 에 남기고 /settings 에 보여 준다. 실제 복구 절차는 docs/04.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from . import db

log = logging.getLogger(__name__)
KEEP = 14


def export_reviews(conn: sqlite3.Connection, user: db.User) -> dict:
    """한 사용자의 평가를 JSON 으로. 제목도 같이 넣어 사람이 읽을 수 있게 한다 (가져올 땐 product_no 만 쓴다)."""
    items = []
    for review, comic in db.reviews_with_comics(conn, user.id):
        items.append(
            {
                "product_no": review.product_no,
                "title": comic.title,
                "status": review.status,
                "score": review.score,
                "aspects": review.aspects,
                "episodes_read": review.episodes_read,
                "memo": review.memo,
                "created_at": review.created_at,
                "updated_at": review.updated_at,
            }
        )
    return {
        "format": "my-webtoon-reviews",
        "version": 3,
        "username": user.username,
        "exported_at": db.now_iso(),
        "reviews": items,
    }


def import_reviews(conn: sqlite3.Connection, user: db.User, payload: dict) -> tuple[int, list[int]]:
    """export_reviews() 결과(또는 자동 백업 JSON)를 user 의 평가로 넣는다.
    (가져온 개수, 아직 수집 안 돼서 건너뛴 product_no 들).

    자동 백업 파일(여러 사용자)이면 같은 아이디의 몫만 가져온다.
    건너뛴 작품은 크롤링 큐에 넣어 두니, 수집된 뒤 한 번 더 가져오면 들어간다.
    """
    if payload.get("format") == "my-webtoon-backup":
        mine = [u for u in payload.get("users", []) if u.get("username") == user.username]
        if not mine:
            raise ValueError(f"백업 파일에 {user.username} 의 평가가 없어요")
        payload = mine[0]
    if payload.get("format") != "my-webtoon-reviews":
        raise ValueError("my-webtoon 평가 내보내기 파일이 아니에요")
    imported, missing = 0, []
    for item in payload.get("reviews", []):
        no = int(item["product_no"])
        if db.get_comic(conn, no) is None:
            missing.append(no)
            continue
        db.update_review(
            conn,
            user.id,
            no,
            status=item.get("status"),
            score=item.get("score"),
            aspects=item.get("aspects") or {},
            episodes_read=item.get("episodes_read"),
            memo=item.get("memo") or "",
        )
        imported += 1
    if missing:
        db.enqueue(conn, missing, None)
    return imported, missing


def run_backup(database_path: Path, backup_dir: Path, keep: int = KEEP) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"my-webtoon-{stamp}.db"

    with db.session(database_path) as src:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
        everyone = {
            "format": "my-webtoon-backup",
            "exported_at": db.now_iso(),
            "users": [export_reviews(src, user) for user, _ in db.list_users(src)],
        }
        (backup_dir / f"reviews-{stamp}.json").write_text(
            json.dumps(everyone, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        db.set_meta(src, "last_backup_at", db.now_iso())

    for pattern in ("my-webtoon-*.db", "reviews-*.json"):
        for old in sorted(backup_dir.glob(pattern))[:-keep]:
            old.unlink()
    log.info("백업 완료: %s", target)
    return target


def verify_backup(backup_path: Path, live: sqlite3.Connection | None = None) -> dict:
    """백업 파일을 읽기 전용으로 열어 검사한다. live 를 주면 행 수를 원본과 비교한다."""
    tables = ("comics", "reviews", "users")
    result: dict = {"file": backup_path.name, "checked_at": db.now_iso()}
    conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        result["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        # 옛 스키마 백업(예: users 가 생기기 전)도 검사할 수 있게, 있는 테이블만 센다.
        # 그런 백업도 복구는 된다 — 앱이 뜰 때 init_schema() 가 마이그레이션한다 (docs/04 복구 연습).
        present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result["counts"] = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in tables
            if t in present
        }
    except sqlite3.DatabaseError as exc:
        result.update(integrity=f"열 수 없음: {exc}", counts={})
    finally:
        conn.close()
    if live is not None:
        result["live"] = {
            t: live.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables
        }
    result["ok"] = result["integrity"] == "ok" and (
        live is None or result["counts"] == result["live"]
    )
    return result


def backup_and_verify(database_path: Path, backup_dir: Path) -> dict:
    """백업 → 바로 검사 → 결과를 meta 에 남긴다. 정기 작업과 '지금 백업' 버튼이 이걸 부른다."""
    target = run_backup(database_path, backup_dir)
    with db.session(database_path) as conn:
        check = verify_backup(target, conn)
        db.set_meta(conn, "last_backup_check", json.dumps(check, ensure_ascii=False))
    if not check["ok"]:
        log.error("백업 검사 실패: %s", check)
    return check


def last_check(conn: sqlite3.Connection) -> dict | None:
    raw = db.get_meta(conn, "last_backup_check")
    return json.loads(raw) if raw else None


def backup_due(conn: sqlite3.Connection, hours: float = 24) -> bool:
    last = db.get_meta(conn, "last_backup_at")
    if last is None:
        return True
    return (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds() > hours * 3600
