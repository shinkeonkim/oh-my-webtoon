import sqlite3

import pytest

from my_webtoon import backup, db


def test_score_and_status_coexist(library):
    with db.session(library) as conn:
        db.update_review(conn, 1, score=8)
        db.update_review(conn, 1, status="seen")  # 상태를 눌러도 점수는 그대로
        db.update_review(conn, 1, memo=" 재밌다 ")
        review = db.get_review(conn, 1)
    assert (review.score, review.status, review.memo) == (8, "seen", "재밌다")


def test_clearing_every_field_deletes_row(library):
    with db.session(library) as conn:
        db.update_review(conn, 1, score=8)
        assert db.update_review(conn, 1, score=None) is None
        assert db.get_review(conn, 1) is None


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
        db.update_review(conn, 1, **changes)


def test_zero_is_a_real_score(library):
    with db.session(library) as conn:
        review = db.update_review(conn, 1, score=0)
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
        reviews = db.all_reviews(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert (reviews[1].status, reviews[1].score) == ("seen", 10)
    assert (reviews[2].status, reviews[2].score) == ("seen", None)
    assert reviews[3].status == "dismissed"
    assert "ratings" not in tables and "ratings_v1_backup" in tables


def test_export_import_roundtrip(library, tmp_path):
    with db.session(library) as conn:
        db.update_review(conn, 1, score=9, status="seen", aspects={"art": 7}, memo="좋음")
        db.update_review(conn, 5, status="wish")
        payload = backup.export_reviews(conn)
        db.delete_review(conn, 1)
        db.delete_review(conn, 5)
        payload["reviews"].append({"product_no": 999, "score": 3})  # 아직 수집 안 된 작품
        imported, missing = backup.import_reviews(conn, payload)
        review = db.get_review(conn, 1)
        queued = conn.execute("SELECT 1 FROM crawl_queue WHERE product_no = 999").fetchone()
    assert imported == 2 and missing == [999] and queued
    assert (review.score, review.aspects, review.memo) == (9, {"art": 7}, "좋음")


def test_backup_writes_consistent_copy_and_rotates(library, tmp_path):
    with db.session(library) as conn:
        db.update_review(conn, 1, score=7)
    target = backup.run_backup(library, tmp_path / "bk", keep=2)
    copy = sqlite3.connect(target)
    assert copy.execute("SELECT score FROM reviews WHERE product_no = 1").fetchone() == (7,)
    for _ in range(3):
        (tmp_path / "bk" / f"my-webtoon-2000010{_}T000000Z.db").write_text("")
        (tmp_path / "bk" / f"reviews-2000010{_}T000000Z.json").write_text("{}")
    backup.run_backup(library, tmp_path / "bk", keep=2)
    assert len(list((tmp_path / "bk").glob("my-webtoon-*.db"))) == 2
    assert len(list((tmp_path / "bk").glob("reviews-*.json"))) == 2
