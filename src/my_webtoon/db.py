"""SQLite 접근 계층.

ORM 을 쓰지 않고 SQL 을 그대로 둔다 — 어떤 쿼리가 나가는지 코드에서 바로 보이게 하려고.
연결은 짧게 열고 닫는다 (요청마다 하나, 크롤러 실행마다 하나). WAL 모드라
웹 요청(읽기)과 크롤러(쓰기)가 동시에 돌아도 서로 막지 않는다.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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


def init_schema(conn: sqlite3.Connection) -> None:
    sql = resources.files("my_webtoon").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(sql)


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


def requeue(conn: sqlite3.Connection, product_nos: Iterable[int]) -> None:
    """이미 수집한 작품을 다시 pending 으로 (재수집)."""
    ts = now_iso()
    conn.executemany(
        """
        INSERT INTO crawl_queue (product_no, state, discovered_at, updated_at)
        VALUES (?, 'pending', ?, ?)
        ON CONFLICT(product_no) DO UPDATE SET state='pending', attempts=0, updated_at=excluded.updated_at
        """,
        [(no, ts, ts) for no in product_nos],
    )


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


def queue_stats(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT state, COUNT(*) FROM crawl_queue GROUP BY state").fetchall()
    stats = {r[0]: r[1] for r in rows}
    stats["comics"] = conn.execute("SELECT COUNT(*) FROM comics").fetchone()[0]
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
# ratings
# ---------------------------------------------------------------------------


@dataclass
class Rating:
    product_no: int
    kind: str  # rated / seen / dismissed
    score: int | None
    memo: str
    updated_at: str


def set_rating(
    conn: sqlite3.Connection,
    product_no: int,
    kind: str,
    score: int | None = None,
    memo: str | None = None,
) -> None:
    if kind == "rated" and not (score and 1 <= score <= 5):
        raise ValueError("rated 는 1~5 점수가 필요하다")
    if kind != "rated":
        score = None
    conn.execute(
        """
        INSERT INTO ratings (product_no, kind, score, memo, updated_at)
        VALUES (?, ?, ?, COALESCE(?, ''), ?)
        ON CONFLICT(product_no) DO UPDATE SET
            kind=excluded.kind, score=excluded.score,
            memo=COALESCE(?, ratings.memo), updated_at=excluded.updated_at
        """,
        (product_no, kind, score, memo, now_iso(), memo),
    )


def delete_rating(conn: sqlite3.Connection, product_no: int) -> None:
    conn.execute("DELETE FROM ratings WHERE product_no = ?", (product_no,))


def get_rating(conn: sqlite3.Connection, product_no: int) -> Rating | None:
    row = conn.execute("SELECT * FROM ratings WHERE product_no = ?", (product_no,)).fetchone()
    return Rating(**dict(row)) if row else None


def all_ratings(conn: sqlite3.Connection) -> dict[int, Rating]:
    rows = conn.execute("SELECT * FROM ratings").fetchall()
    return {r["product_no"]: Rating(**dict(r)) for r in rows}


def ratings_with_comics(conn: sqlite3.Connection) -> list[tuple[Rating, Comic]]:
    rows = conn.execute(
        """
        SELECT r.kind, r.score, r.memo, r.updated_at AS rated_at, c.*, p.label AS promo_label
        FROM ratings r
        JOIN comics c ON c.product_no = r.product_no
        LEFT JOIN free_promos p ON p.product_no = c.product_no
        ORDER BY r.updated_at DESC
        """
    ).fetchall()
    return [
        (
            Rating(r["product_no"], r["kind"], r["score"], r["memo"], r["rated_at"]),
            Comic.from_row(r),
        )
        for r in rows
    ]


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
