"""시연용(데모) 계정.

누구에게나 아이디/비밀번호를 알려 주는 계정이라 망가뜨려도 되게 만든다.
  - 막힘: 비밀번호 변경, 링크로 가져오기(네이버에 요청), 평가 JSON 가져오기 — web/app.py 의 forbid_demo()
  - 매일 밤 평가를 '기준 평가'로 되돌린다 (다음 사람이 같은 상태에서 시작하도록).

기준 평가는 meta 에 JSON 으로 둔다 (demo_baseline:<user_id>). 지정하는 법:
    my-webtoon user demo test                        지금 test 의 평가를 기준으로 데모 지정
    my-webtoon user demo test --copy-from singun11   singun11 의 평가(메모 제외)를 복사해 기준으로
    my-webtoon user demo test --off                  데모 해제
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime

from . import db

log = logging.getLogger(__name__)
RESET_EVERY_HOURS = 24


def _baseline_key(user_id: int) -> str:
    return f"demo_baseline:{user_id}"


def _snapshot(conn: sqlite3.Connection, user_id: int, keep_memo: bool) -> list[dict]:
    return [
        {
            "product_no": r.product_no,
            "status": r.status,
            "score": r.score,
            "aspects": r.aspects,
            "episodes_read": r.episodes_read,
            "memo": r.memo if keep_memo else "",
        }
        for r in db.all_reviews(conn, user_id).values()
    ]


def make_demo(conn: sqlite3.Connection, user: db.User, copy_from: db.User | None = None) -> int:
    """user 를 데모 계정으로 지정하고 기준 평가를 저장한 뒤 바로 그 상태로 되돌린다. 기준 평가 수를 돌려준다.

    copy_from 이 있으면 그 사람의 평가를 메모 없이 복사해 기준으로 쓴다(공개 화면에 이미 보이는 정보만).
    """
    baseline = (
        _snapshot(conn, copy_from.id, keep_memo=False)
        if copy_from
        else _snapshot(conn, user.id, keep_memo=True)
    )
    db.set_meta(conn, _baseline_key(user.id), json.dumps(baseline, ensure_ascii=False))
    db.set_demo(conn, user.id, True)
    reset(conn, user)
    return len(baseline)


def unmake_demo(conn: sqlite3.Connection, user: db.User) -> None:
    db.set_demo(conn, user.id, False)
    conn.execute(
        "DELETE FROM meta WHERE key IN (?, ?)", (_baseline_key(user.id), _reset_key(user.id))
    )


def _reset_key(user_id: int) -> str:
    return f"demo_reset_at:{user_id}"


def reset(conn: sqlite3.Connection, user: db.User) -> int:
    """평가를 지우고 기준 평가로 되돌린다. (아직 수집 안 된 작품은 건너뛴다)"""
    baseline = json.loads(db.get_meta(conn, _baseline_key(user.id)) or "[]")
    conn.execute("DELETE FROM reviews WHERE user_id = ?", (user.id,))
    conn.execute("DELETE FROM quick_skips WHERE user_id = ?", (user.id,))
    restored = 0
    for item in baseline:
        if db.get_comic(conn, item["product_no"]) is None:
            continue
        no = item.pop("product_no")
        db.update_review(conn, user.id, no, **item)
        restored += 1
    db.set_meta(conn, _reset_key(user.id), db.now_iso())
    return restored


def reset_due(conn: sqlite3.Connection) -> list[str]:
    """마지막 초기화가 RESET_EVERY_HOURS 넘은 데모 계정을 되돌린다. 되돌린 아이디 목록."""
    done = []
    for user, _ in db.list_users(conn):
        if not user.is_demo:
            continue
        last = db.get_meta(conn, _reset_key(user.id))
        if (
            last
            and (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds()
            < RESET_EVERY_HOURS * 3600
        ):
            continue
        reset(conn, user)
        done.append(user.username)
    if done:
        log.info("데모 계정 초기화: %s", done)
    return done
