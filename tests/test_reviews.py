import sqlite3

import pytest

from my_webtoon import backup, db

ME = 1  # conftest 의 DB 를 만들 때 생기는 관리자 singun11


def test_score_and_status_coexist(library):
    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=8)
        db.update_review(conn, ME, 1, status="seen")  # 상태를 눌러도 점수는 그대로
        db.update_review(conn, ME, 1, memo=" 재밌다 ")
        review = db.get_review(conn, ME, 1)
    assert (review.score, review.status, review.memo) == (8, "seen", "재밌다")


def test_clearing_every_field_deletes_row(library):
    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=8)
        assert db.update_review(conn, ME, 1, score=None) is None
        assert db.get_review(conn, ME, 1) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"score": 11},
        {"score": -1},
        {"status": "liked"},
        {"aspects": {"art": 12}},
        {"aspects": {"smell": 3}},
        {"episodes_read": -2},
        {"stars": 3},
    ],
)
def test_invalid_values_are_rejected(library, changes):
    with db.session(library) as conn, pytest.raises(ValueError):
        db.update_review(conn, ME, 1, **changes)


def test_zero_is_a_real_score(library):
    with db.session(library) as conn:
        review = db.update_review(conn, ME, 1, score=0)
    assert review.score == 0 and review.overall == 0.0


def test_migrates_v1_ratings(db_path):
    """예전 1~5점 ratings 테이블이 있으면 reviews 로 옮기고 원본은 백업 이름으로 남긴다."""
    with db.session(db_path) as conn:
        conn.execute("DROP TABLE reviews")
        conn.execute(
            "CREATE TABLE ratings (product_no INTEGER PRIMARY KEY, kind TEXT, score INTEGER,"
            " memo TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)"
        )
        from .conftest import comic_data

        for no in (1, 2, 3):
            db.upsert_comic(conn, comic_data(no, f"작품{no}"))
        conn.executemany(
            "INSERT INTO ratings VALUES (?, ?, ?, '', '2026-10-24T00:00:00+00:00')",
            [(1, "rated", 5), (2, "seen", None), (3, "dismissed", None)],
        )
    with db.session(db_path) as conn:
        db.init_schema(conn)
        reviews = db.all_reviews(conn, ME)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert (reviews[1].status, reviews[1].score) == ("seen", 10)
    assert (reviews[2].status, reviews[2].score) == ("seen", None)
    assert reviews[3].status == "dismissed"
    assert "ratings" not in tables and "ratings_v1_backup" in tables


def test_export_import_roundtrip(library, tmp_path):
    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=9, status="seen", aspects={"art": 7}, memo="좋음")
        db.update_review(conn, ME, 5, status="wish")
        me = db.get_user(conn, ME)
        payload = backup.export_reviews(conn, me)
        db.delete_review(conn, ME, 1)
        db.delete_review(conn, ME, 5)
        payload["reviews"].append({"product_no": 999, "score": 3})  # 아직 수집 안 된 작품
        imported, missing = backup.import_reviews(conn, me, payload)
        review = db.get_review(conn, ME, 1)
        queued = conn.execute("SELECT 1 FROM crawl_queue WHERE product_no = 999").fetchone()
    assert imported == 2 and missing == [999] and queued
    assert (review.score, review.aspects, review.memo) == (9, {"art": 7}, "좋음")


def test_backup_writes_consistent_copy_and_rotates(library, tmp_path):
    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=7)
    target = backup.run_backup(library, tmp_path / "bk", keep=2)
    copy = sqlite3.connect(target)
    assert copy.execute("SELECT score FROM reviews WHERE product_no = 1").fetchone() == (7,)
    for _ in range(3):
        (tmp_path / "bk" / f"my-webtoon-2000010{_}T000000Z.db").write_text("")
        (tmp_path / "bk" / f"reviews-2000010{_}T000000Z.json").write_text("{}")
    backup.run_backup(library, tmp_path / "bk", keep=2)
    assert len(list((tmp_path / "bk").glob("my-webtoon-*.db"))) == 2
    assert len(list((tmp_path / "bk").glob("reviews-*.json"))) == 2


def test_migrates_v2_single_user_reviews_to_admin(db_path):
    """운영 DB 모양(v2): user_id 없는 reviews + /settings 에서 바꾼 meta.password_hash.
    → 관리자 singun11 이 그 비밀번호를 갖고, 기존 평가는 모두 singun11 의 것이 된다."""
    from .conftest import comic_data

    fresh = db_path.with_name("v2.db")
    with db.session(fresh) as conn:
        conn.executescript(
            """
            CREATE TABLE comics (product_no INTEGER PRIMARY KEY, title TEXT NOT NULL,
              authors TEXT NOT NULL DEFAULT '[]', publisher TEXT, genre TEXT,
              tags TEXT NOT NULL DEFAULT '[]', synopsis TEXT NOT NULL DEFAULT '', status TEXT,
              age_rating TEXT, unit TEXT NOT NULL DEFAULT '화', total_episodes INTEGER,
              free_episodes INTEGER, is_all_free INTEGER NOT NULL DEFAULT 0, naver_rating REAL,
              popularity INTEGER, thumbnail_url TEXT, crawled_at TEXT NOT NULL);
            CREATE TABLE reviews (product_no INTEGER PRIMARY KEY REFERENCES comics(product_no),
              status TEXT, score INTEGER, aspects TEXT NOT NULL DEFAULT '{}', episodes_read INTEGER,
              memo TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('password_hash', 'pbkdf2_sha256$1$00$legacy');
            """
        )
        db.upsert_comic(conn, comic_data(1, "작품1"))
        conn.execute(
            "INSERT INTO reviews (product_no, status, score, aspects, memo, created_at, updated_at)"
            " VALUES (1, 'seen', 9, '{\"art\": 8}', '메모', 't', 't')"
        )
    with db.session(fresh) as conn:
        db.init_schema(conn, "singun11", "pbkdf2_sha256$1$00$from-env")
        admin = db.get_user_by_name(conn, "singun11")
        review = db.get_review(conn, admin.id, 1)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        db.init_schema(conn, "singun11", None)  # 두 번 불러도 그대로
        assert len(db.all_reviews(conn, admin.id)) == 1
    assert admin.is_admin and admin.password_hash == "pbkdf2_sha256$1$00$legacy"
    assert (review.status, review.score, review.aspects, review.memo) == (
        "seen",
        9,
        {"art": 8},
        "메모",
    )
    assert "reviews_v2_backup" in tables


def test_reviews_are_per_user(library):
    with db.session(library) as conn:
        other = db.create_user(conn, "alice", None)
        db.update_review(conn, ME, 1, score=10)
        db.update_review(conn, other.id, 1, score=2, status="dropped")
        mine, hers = db.get_review(conn, ME, 1), db.get_review(conn, other.id, 1)
        assert (mine.score, mine.status) == (10, None)
        assert (hers.score, hers.status) == (2, "dropped")
        db.delete_review(conn, other.id, 1)
        assert db.get_review(conn, ME, 1) is not None


@pytest.mark.parametrize("name", ["ab", "Has Space", "한글", "x" * 33])
def test_invalid_usernames(library, name):
    with db.session(library) as conn, pytest.raises(ValueError):
        db.create_user(conn, name, None)


def test_duplicate_username(library):
    with db.session(library) as conn, pytest.raises(ValueError):
        db.create_user(conn, "SINGUN11", None)  # 대소문자 구분 없이 같은 아이디
