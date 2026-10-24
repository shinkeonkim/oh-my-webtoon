"""SQLite 접근 계층.

ORM 을 쓰지 않고 SQL 을 그대로 둔다 — 어떤 쿼리가 나가는지 코드에서 바로 보이게 하려고.
연결은 짧게 열고 닫는다 (요청마다 하나, 크롤러 실행마다 하나). WAL 모드라
웹 요청(읽기)과 크롤러(쓰기)가 동시에 돌아도 서로 막지 않는다.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def session(path: Path) -> Iterator[sqlite3.Connection]:
    """with db.session(path) as conn: ... — 끝나면 커밋하고 닫는다."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _schema_sql() -> str:
    return resources.files("my_webtoon").joinpath("schema.sql").read_text(encoding="utf-8")


def init_schema(
    conn: sqlite3.Connection,
    admin_username: str = "singun11",
    admin_password_hash: str | None = None,
) -> None:
    """스키마를 만들고 옛 버전 데이터를 옮긴다. 몇 번 불러도 안전하다.

    마이그레이션 순서 (오래된 것부터):
      v1  ratings(kind, 1~5점)            → reviews
      v2  reviews(product_no 단일 키)      → reviews(user_id, product_no) — 관리자 계정 소유로
    """
    conn.executescript(_schema_sql())
    admin_id = ensure_admin(conn, admin_username, admin_password_hash)
    _migrate_reviews_v2(conn, admin_id)
    _migrate_ratings_v1(conn, admin_id)
    conn.commit()


# ---------------------------------------------------------------------------
# comics
# ---------------------------------------------------------------------------


@dataclass
class Comic:
    product_no: int
    title: str
    authors: list[str]
    publisher: str | None
    genre: str | None
    tags: list[str]
    synopsis: str
    status: str | None
    age_rating: str | None
    unit: str
    total_episodes: int | None
    free_episodes: int | None
    is_all_free: bool
    naver_rating: float | None
    popularity: int | None
    thumbnail_url: str | None
    crawled_at: str
    promo_label: str | None = None  # free_promos 와 조인했을 때만 채워진다

    @property
    def url(self) -> str:
        return f"https://series.naver.com/comic/detail.series?productNo={self.product_no}"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Comic:
        keys = row.keys()
        return cls(
            product_no=row["product_no"],
            title=row["title"],
            authors=json.loads(row["authors"]),
            publisher=row["publisher"],
            genre=row["genre"],
            tags=json.loads(row["tags"]),
            synopsis=row["synopsis"],
            status=row["status"],
            age_rating=row["age_rating"],
            unit=row["unit"],
            total_episodes=row["total_episodes"],
            free_episodes=row["free_episodes"],
            is_all_free=bool(row["is_all_free"]),
            naver_rating=row["naver_rating"],
            popularity=row["popularity"],
            thumbnail_url=row["thumbnail_url"],
            crawled_at=row["crawled_at"],
            promo_label=row["promo_label"] if "promo_label" in keys else None,
        )


_COMIC_SELECT = """
    SELECT c.*, p.label AS promo_label
    FROM comics c
    LEFT JOIN free_promos p ON p.product_no = c.product_no
"""


def upsert_comic(conn: sqlite3.Connection, data: dict) -> None:
    """parser.parse_detail() 결과 dict 를 그대로 저장한다."""
    conn.execute(
        """
        INSERT INTO comics (
            product_no, title, authors, publisher, genre, tags, synopsis, status,
            age_rating, unit, total_episodes, free_episodes, is_all_free,
            naver_rating, popularity, thumbnail_url, crawled_at
        ) VALUES (
            :product_no, :title, :authors, :publisher, :genre, :tags, :synopsis, :status,
            :age_rating, :unit, :total_episodes, :free_episodes, :is_all_free,
            :naver_rating, :popularity, :thumbnail_url, :crawled_at
        )
        ON CONFLICT(product_no) DO UPDATE SET
            title=excluded.title, authors=excluded.authors, publisher=excluded.publisher,
            genre=excluded.genre, tags=excluded.tags, synopsis=excluded.synopsis,
            status=excluded.status, age_rating=excluded.age_rating, unit=excluded.unit,
            total_episodes=excluded.total_episodes, free_episodes=excluded.free_episodes,
            is_all_free=excluded.is_all_free, naver_rating=excluded.naver_rating,
            popularity=excluded.popularity, thumbnail_url=excluded.thumbnail_url,
            crawled_at=excluded.crawled_at
        """,
        {
            **data,
            "authors": json.dumps(data["authors"], ensure_ascii=False),
            "tags": json.dumps(data["tags"], ensure_ascii=False),
            "is_all_free": int(data["is_all_free"]),
            "crawled_at": data.get("crawled_at") or now_iso(),
        },
    )


def get_comic(conn: sqlite3.Connection, product_no: int) -> Comic | None:
    row = conn.execute(_COMIC_SELECT + " WHERE c.product_no = ?", (product_no,)).fetchone()
    return Comic.from_row(row) if row else None


def all_comics(conn: sqlite3.Connection) -> list[Comic]:
    rows = conn.execute(_COMIC_SELECT + " ORDER BY c.product_no").fetchall()
    return [Comic.from_row(r) for r in rows]


def search_comics(conn: sqlite3.Connection, query: str, limit: int = 40) -> list[Comic]:
    """제목/작가 부분 일치 검색. 몇 천 건 규모라 LIKE 로 충분하다."""
    like = f"%{query.strip()}%"
    rows = conn.execute(
        _COMIC_SELECT
        + """
        WHERE c.title LIKE ? OR c.authors LIKE ?
        ORDER BY (c.title = ?) DESC, c.popularity DESC NULLS LAST
        LIMIT ?
        """,
        (like, like, query.strip(), limit),
    ).fetchall()
    return [Comic.from_row(r) for r in rows]


COMIC_SORTS = {
    "popular": ("인기순", "c.popularity DESC NULLS LAST"),
    "free": ("무료 많은 순", "c.is_all_free DESC, c.free_episodes DESC NULLS LAST"),
    "episodes": ("회차 많은 순", "c.total_episodes DESC NULLS LAST"),
    "rating": ("네이버 평점순", "c.naver_rating DESC NULLS LAST, c.popularity DESC NULLS LAST"),
    "recent": ("최근 수집순", "c.crawled_at DESC"),
}


def list_comics(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    genre: str = "",
    sort: str = "popular",
    limit: int = 40,
    offset: int = 0,
) -> tuple[list[Comic], int]:
    """수집 목록 화면용. (현재 페이지 작품들, 조건에 맞는 전체 개수)."""
    where, params = [], []
    if query.strip():
        where.append("(c.title LIKE ? OR c.authors LIKE ?)")
        params += [f"%{query.strip()}%"] * 2
    if genre:
        where.append("c.genre = ?")
        params.append(genre)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    order = COMIC_SORTS.get(sort, COMIC_SORTS["popular"])[1]
    total = conn.execute(f"SELECT COUNT(*) FROM comics c{clause}", params).fetchone()[0]
    rows = conn.execute(
        _COMIC_SELECT + clause + f" ORDER BY {order} LIMIT ? OFFSET ?", [*params, limit, offset]
    ).fetchall()
    return [Comic.from_row(r) for r in rows], total


def genres(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT genre FROM comics WHERE genre IS NOT NULL GROUP BY genre ORDER BY COUNT(*) DESC"
    ).fetchall()
    return [r[0] for r in rows]


def comics_signature(conn: sqlite3.Connection) -> tuple:
    """작품 데이터가 바뀌었는지 싸게 확인하는 값. 추천 모델 캐시 무효화에 쓴다."""
    row = conn.execute("SELECT COUNT(*), MAX(crawled_at) FROM comics").fetchone()
    return (row[0], row[1])


def stale_product_nos(conn: sqlite3.Connection, older_than_days: int, limit: int) -> list[int]:
    cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT product_no FROM comics WHERE crawled_at < ? ORDER BY crawled_at LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# crawl_queue / free_promos / crawl_runs
# ---------------------------------------------------------------------------


def enqueue(conn: sqlite3.Connection, product_nos: Iterable[int], source: int | None) -> int:
    """처음 보는 productNo 만 pending 으로 넣는다. 새로 넣은 개수를 돌려준다."""
    ts = now_iso()
    before = conn.total_changes
    conn.executemany(
        """
        INSERT INTO crawl_queue (product_no, discovered_from, discovered_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(product_no) DO NOTHING
        """,
        [(no, source, ts, ts) for no in set(product_nos)],
    )
    return conn.total_changes - before


def next_pending(conn: sqlite3.Connection, limit: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT product_no FROM crawl_queue
        WHERE state = 'pending' OR (state = 'failed' AND attempts < 3)
        ORDER BY discovered_from IS NOT NULL, discovered_at
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [r[0] for r in rows]


def mark_crawled(
    conn: sqlite3.Connection, product_no: int, state: str, error: str | None = None
) -> None:
    conn.execute(
        """
        UPDATE crawl_queue
        SET state = ?, last_error = ?, attempts = attempts + 1, updated_at = ?
        WHERE product_no = ?
        """,
        (state, error, now_iso(), product_no),
    )


def replace_promos(conn: sqlite3.Connection, promos: dict[int, str]) -> None:
    """무료관 이벤트는 매번 통째로 바뀌므로 지우고 다시 넣는다."""
    conn.execute("DELETE FROM free_promos")
    ts = now_iso()
    conn.executemany(
        "INSERT INTO free_promos (product_no, label, seen_at) VALUES (?, ?, ?)",
        [(no, label, ts) for no, label in promos.items()],
    )


def queue_stats(conn: sqlite3.Connection, recrawl_after_days: int = 7) -> dict[str, int]:
    rows = conn.execute("SELECT state, COUNT(*) FROM crawl_queue GROUP BY state").fetchall()
    stats = {r[0]: r[1] for r in rows}
    stats["comics"] = conn.execute("SELECT COUNT(*) FROM comics").fetchone()[0]
    cutoff = (datetime.now(UTC) - timedelta(days=recrawl_after_days)).isoformat(timespec="seconds")
    stats["stale"] = conn.execute(
        "SELECT COUNT(*) FROM comics WHERE crawled_at < ?", (cutoff,)
    ).fetchone()[0]
    return stats


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO crawl_runs (started_at) VALUES (?)", (now_iso(),))
    conn.commit()
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection, run_id: int, fetched: int, discovered: int, failed: int, note: str
) -> None:
    conn.execute(
        """
        UPDATE crawl_runs SET finished_at=?, fetched=?, discovered=?, failed=?, note=?
        WHERE id=?
        """,
        (now_iso(), fetched, discovered, failed, note, run_id),
    )


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# ---------------------------------------------------------------------------
# reviews (내 평가)
# ---------------------------------------------------------------------------

# 상태는 하나만 고른다 (봤음이면서 하차일 수는 없으니까). 점수·세부 점수·메모는 상태와 별개로 자유롭게.
STATUSES = {
    "seen": "봤음",
    "watching": "보는 중",
    "dropped": "하차",
    "wish": "보고 싶음",
    "dismissed": "관심 없음",
}
# 세부 평가 항목. 여기에 한 줄 추가하면 화면·저장·내보내기에 바로 반영된다 (DB 는 JSON 이라 마이그레이션 불필요).
ASPECTS = {
    "story": "스토리",
    "art": "작화",
    "character": "캐릭터",
    "pacing": "전개",
    "immersion": "몰입도",
}
SCORE_RANGE = range(0, 11)  # 0~10


@dataclass
class Review:
    product_no: int
    status: str | None = None
    score: int | None = None
    aspects: dict[str, int] = field(default_factory=dict)
    episodes_read: int | None = None
    memo: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def overall(self) -> float | None:
        """종합 점수. 직접 준 점수가 없으면 세부 점수 평균으로 대신한다. 둘 다 없으면 None."""
        if self.score is not None:
            return float(self.score)
        if self.aspects:
            return sum(self.aspects.values()) / len(self.aspects)
        return None

    @property
    def status_label(self) -> str | None:
        return STATUSES.get(self.status) if self.status else None

    def is_empty(self) -> bool:
        return (
            self.status is None
            and self.score is None
            and not self.aspects
            and self.episodes_read is None
            and not self.memo
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Review:
        return cls(
            product_no=row["product_no"],
            status=row["status"],
            score=row["score"],
            aspects=json.loads(row["aspects"] or "{}"),
            episodes_read=row["episodes_read"],
            memo=row["memo"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _validate_score(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    value = int(value)
    if value not in SCORE_RANGE:
        raise ValueError(f"{name} 는 0~10 이어야 한다 (받은 값: {value})")
    return value


def update_review(
    conn: sqlite3.Connection, user_id: int, product_no: int, **changes
) -> Review | None:
    """주어진 칸만 바꾼다 (부분 업데이트). 값이 None 이면 그 칸을 비운다.

    update_review(conn, me, 1, score=8)              → 점수만 8, 상태·메모는 그대로
    update_review(conn, me, 1, status="seen")        → 상태만 '봤음', 점수는 그대로
    update_review(conn, me, 1, aspects={"art": 9})   → 세부 점수 통째로 교체
    모든 칸이 비면 행을 지우고 None 을 돌려준다.
    """
    unknown = set(changes) - {"status", "score", "aspects", "episodes_read", "memo"}
    if unknown:
        raise ValueError(f"모르는 칸: {unknown}")
    if changes.get("status") is not None and changes["status"] not in STATUSES:
        raise ValueError(f"모르는 상태: {changes['status']}")
    if "score" in changes:
        changes["score"] = _validate_score("score", changes["score"])
    if "aspects" in changes:
        aspects = changes["aspects"] or {}
        bad = set(aspects) - set(ASPECTS)
        if bad:
            raise ValueError(f"모르는 세부 항목: {bad}")
        changes["aspects"] = {k: _validate_score(k, v) for k, v in aspects.items() if v is not None}
    if changes.get("episodes_read") is not None and int(changes["episodes_read"]) < 0:
        raise ValueError("episodes_read 는 0 이상")
    if "memo" in changes:
        changes["memo"] = (changes["memo"] or "").strip()

    current = get_review(conn, user_id, product_no) or Review(product_no, created_at=now_iso())
    for key, value in changes.items():
        setattr(current, key, value)
    if current.is_empty():
        delete_review(conn, user_id, product_no)
        return None

    current.updated_at = now_iso()
    conn.execute(
        """
        INSERT INTO reviews (user_id, product_no, status, score, aspects, episodes_read, memo,
                             created_at, updated_at)
        VALUES (:user_id, :product_no, :status, :score, :aspects, :episodes_read, :memo,
                :created_at, :updated_at)
        ON CONFLICT(user_id, product_no) DO UPDATE SET
            status=excluded.status, score=excluded.score, aspects=excluded.aspects,
            episodes_read=excluded.episodes_read, memo=excluded.memo,
            updated_at=excluded.updated_at
        """,
        {
            **current.__dict__,
            "user_id": user_id,
            "aspects": json.dumps(current.aspects, ensure_ascii=False),
        },
    )
    return current


def delete_review(conn: sqlite3.Connection, user_id: int, product_no: int) -> None:
    conn.execute("DELETE FROM reviews WHERE user_id = ? AND product_no = ?", (user_id, product_no))


def get_review(conn: sqlite3.Connection, user_id: int, product_no: int) -> Review | None:
    row = conn.execute(
        "SELECT * FROM reviews WHERE user_id = ? AND product_no = ?", (user_id, product_no)
    ).fetchone()
    return Review.from_row(row) if row else None


def all_reviews(conn: sqlite3.Connection, user_id: int | None) -> dict[int, Review]:
    """한 사용자의 평가 전부 {product_no: Review}. user_id 가 None(로그인 안 함)이면 빈 dict."""
    if user_id is None:
        return {}
    rows = conn.execute("SELECT * FROM reviews WHERE user_id = ?", (user_id,)).fetchall()
    return {r["product_no"]: Review.from_row(r) for r in rows}


def reviews_with_comics(conn: sqlite3.Connection, user_id: int) -> list[tuple[Review, Comic]]:
    rows = conn.execute(
        """
        SELECT r.status, r.score, r.aspects, r.episodes_read, r.memo,
               r.created_at AS r_created_at, r.updated_at AS r_updated_at,
               c.*, p.label AS promo_label
        FROM reviews r
        JOIN comics c ON c.product_no = r.product_no
        LEFT JOIN free_promos p ON p.product_no = c.product_no
        WHERE r.user_id = ?
        ORDER BY r.updated_at DESC
        """,
        (user_id,),
    ).fetchall()
    return [
        (
            Review(
                product_no=r["product_no"],
                status=r["status"],
                score=r["score"],
                aspects=json.loads(r["aspects"] or "{}"),
                episodes_read=r["episodes_read"],
                memo=r["memo"],
                created_at=r["r_created_at"],
                updated_at=r["r_updated_at"],
            ),
            Comic.from_row(r),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

USERNAME_RE = r"^[a-z0-9_-]{3,32}$"


@dataclass
class User:
    id: int
    username: str
    password_hash: str | None
    is_admin: bool
    session_epoch: int
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> User:
        return cls(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            is_admin=bool(row["is_admin"]),
            session_epoch=row["session_epoch"],
            created_at=row["created_at"],
        )


def create_user(
    conn: sqlite3.Connection, username: str, password_hash: str | None, is_admin: bool = False
) -> User:
    username = username.strip().lower()
    if not re.fullmatch(USERNAME_RE, username):
        raise ValueError("아이디는 영문 소문자·숫자·-·_ 3~32자")
    if get_user_by_name(conn, username):
        raise ValueError(f"이미 있는 아이디: {username}")
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
        (username, password_hash, int(is_admin), now_iso()),
    )
    return get_user(conn, cur.lastrowid)


def get_user(conn: sqlite3.Connection, user_id: int) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return User.from_row(row) if row else None


def get_user_by_name(conn: sqlite3.Connection, username: str) -> User | None:
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username.strip().lower(),)
    ).fetchone()
    return User.from_row(row) if row else None


def list_users(conn: sqlite3.Connection) -> list[tuple[User, int]]:
    """(사용자, 평가 수) 목록."""
    rows = conn.execute(
        """
        SELECT u.*, COUNT(r.product_no) AS review_count
        FROM users u LEFT JOIN reviews r ON r.user_id = u.id
        GROUP BY u.id ORDER BY u.id
        """
    ).fetchall()
    return [(User.from_row(r), r["review_count"]) for r in rows]


def set_password_hash(conn: sqlite3.Connection, user_id: int, password_hash: str) -> int:
    """비밀번호 해시를 바꾸고 세션 epoch 를 올린다. 새 epoch 를 돌려준다."""
    conn.execute(
        "UPDATE users SET password_hash = ?, session_epoch = session_epoch + 1 WHERE id = ?",
        (password_hash, user_id),
    )
    return get_user(conn, user_id).session_epoch


def ensure_admin(conn: sqlite3.Connection, username: str, password_hash: str | None) -> int:
    """사용자가 아무도 없으면 관리자 계정을 만든다. 관리자 id 를 돌려준다.

    비밀번호는 (1) 예전 단일 사용자 시절 /settings 에서 바꾼 해시(meta.password_hash),
    (2) 없으면 넘겨받은 해시(APP_PASSWORD), (3) 둘 다 없으면 NULL — 로컬 개발용 '열린 모드'.
    관리자가 있는데 비밀번호가 NULL 이고 해시가 넘어오면 그때 채운다.
    """
    row = conn.execute(
        "SELECT id, password_hash FROM users WHERE is_admin = 1 ORDER BY id"
    ).fetchone()
    if row:
        if row["password_hash"] is None and password_hash:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, row["id"])
            )
        return row["id"]
    legacy = get_meta(conn, "password_hash")
    return create_user(conn, username, legacy or password_hash, is_admin=True).id


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_reviews_v2(conn: sqlite3.Connection, admin_id: int) -> int:
    """v2 reviews(user_id 없음) → v3 reviews(user_id, product_no). 옛 테이블은 reviews_v2_backup."""
    if "user_id" in _columns(conn, "reviews"):
        return 0
    conn.execute("ALTER TABLE reviews RENAME TO reviews_v2_backup")
    conn.executescript(_schema_sql())  # 새 모양의 reviews 를 만든다
    cur = conn.execute(
        """
        INSERT INTO reviews (user_id, product_no, status, score, aspects, episodes_read, memo,
                             created_at, updated_at)
        SELECT ?, product_no, status, score, aspects, episodes_read, memo, created_at, updated_at
        FROM reviews_v2_backup
        """,
        (admin_id,),
    )
    return cur.rowcount


def _migrate_ratings_v1(conn: sqlite3.Connection, admin_id: int) -> int:
    """v1(ratings: kind + 1~5점) → reviews. 옛 테이블은 ratings_v1_backup 으로 남겨 둔다.

    rated ★s → status='seen', score=s×2   (★5 → 10점, ★3 → 6점)
    seen     → status='seen'
    dismissed→ status='dismissed'
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ratings'"
    ).fetchone()
    if not exists:
        return 0
    rows = conn.execute("SELECT * FROM ratings").fetchall()
    for r in rows:
        status = "dismissed" if r["kind"] == "dismissed" else "seen"
        score = r["score"] * 2 if r["kind"] == "rated" and r["score"] else None
        conn.execute(
            """
            INSERT OR IGNORE INTO reviews (user_id, product_no, status, score, memo,
                                           created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (admin_id, r["product_no"], status, score, r["memo"], r["updated_at"], r["updated_at"]),
        )
    conn.execute("ALTER TABLE ratings RENAME TO ratings_v1_backup")
    return len(rows)


# ---------------------------------------------------------------------------
# meta (키-값)
# ---------------------------------------------------------------------------


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
