"""사용자 로그인.

- 사용자마다 아이디 + 비밀번호(PBKDF2 해시)를 가진다. 평문은 어디에도 남지 않는다.
- 첫 관리자 계정(ADMIN_USERNAME, 기본 singun11)은 처음 뜰 때 APP_PASSWORD(쿠버네티스 시크릿)로 만든다.
  관리자는 /settings 에서 다른 사용자를 추가할 수 있다 (공개 가입은 없다).
- 세션에는 user_id 와 epoch 를 넣는다. 비밀번호를 바꾸면 그 사용자의 epoch 가 올라가서
  다른 기기에 남아 있던 세션이 모두 끊긴다.
- 비밀번호를 잊었으면: kubectl exec ... my-webtoon user set-password <아이디> (docs/04_배포.md)
- 아무도 비밀번호가 없으면(로컬 개발) '열린 모드' — 로그인 없이 관리자로 동작한다.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3

from .. import db

ITERATIONS = 310_000  # OWASP 권장치 (PBKDF2-HMAC-SHA256)
MIN_LENGTH = 8


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_hash(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


def validate_new_password(password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"비밀번호는 {MIN_LENGTH}자 이상이어야 해요")


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> db.User | None:
    user = db.get_user_by_name(conn, username)
    if user and verify_hash(password, user.password_hash):
        return user
    return None


def open_mode(conn: sqlite3.Connection) -> bool:
    """비밀번호가 있는 사용자가 하나도 없으면 로그인 없이 쓴다 (로컬 개발용)."""
    row = conn.execute("SELECT 1 FROM users WHERE password_hash IS NOT NULL LIMIT 1").fetchone()
    return row is None


def current_user(conn: sqlite3.Connection, session: dict) -> db.User | None:
    if open_mode(conn):
        row = conn.execute("SELECT id FROM users WHERE is_admin = 1 ORDER BY id").fetchone()
        return db.get_user(conn, row[0]) if row else None
    user_id = session.get("user_id")
    if user_id is None:
        return None
    user = db.get_user(conn, int(user_id))
    if user is None or session.get("epoch") != user.session_epoch:
        return None
    return user


def change_password(conn: sqlite3.Connection, user: db.User, new_password: str) -> int:
    """새 비밀번호 저장 + 세션 epoch 증가. 새 epoch 를 돌려준다 (지금 세션은 유지시키려고)."""
    validate_new_password(new_password)
    return db.set_password_hash(conn, user.id, hash_password(new_password))
