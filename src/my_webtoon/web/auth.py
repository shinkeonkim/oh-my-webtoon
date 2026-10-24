"""비밀번호 로그인.

- 처음에는 환경변수 APP_PASSWORD(쿠버네티스 시크릿)로 로그인한다.
- /settings 에서 비밀번호를 바꾸면 PBKDF2 해시를 DB(meta.password_hash)에 저장하고,
  그때부터는 DB 해시가 우선한다. 평문은 어디에도 남지 않는다.
- 비밀번호를 바꾸면 session_epoch 를 올려서 다른 기기의 로그인 세션을 모두 끊는다.
- 비밀번호를 잊었으면 meta.password_hash 행을 지우면 APP_PASSWORD 로 돌아간다 (docs/04_배포.md).
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


def verify_hash(password: str, stored: str) -> bool:
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


def auth_enabled(conn: sqlite3.Connection, env_password: str | None) -> bool:
    return bool(env_password) or db.get_meta(conn, "password_hash") is not None


def check_password(conn: sqlite3.Connection, env_password: str | None, password: str) -> bool:
    stored = db.get_meta(conn, "password_hash")
    if stored:
        return verify_hash(password, stored)
    return bool(env_password) and hmac.compare_digest(password.encode(), env_password.encode())


def change_password(conn: sqlite3.Connection, new_password: str) -> str:
    """새 비밀번호 저장 + 세션 epoch 증가. 새 epoch 를 돌려준다 (지금 세션은 유지시키려고)."""
    if len(new_password) < MIN_LENGTH:
        raise ValueError(f"비밀번호는 {MIN_LENGTH}자 이상이어야 해요")
    db.set_meta(conn, "password_hash", hash_password(new_password))
    epoch = str(int(session_epoch(conn)) + 1)
    db.set_meta(conn, "session_epoch", epoch)
    return epoch


def session_epoch(conn: sqlite3.Connection) -> str:
    return db.get_meta(conn, "session_epoch") or "0"
