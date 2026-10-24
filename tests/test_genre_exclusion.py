"""EXCLUDED_GENRES 플래그 (기본 BL): 수집하지 않고, 이미 있던 건 지우고, 플래그를 풀면 다시 모은다."""

from pathlib import Path

from my_webtoon import db
from my_webtoon.crawler import parser, runner, seeds

from .conftest import comic_data

BL = frozenset({"BL"})
FIXTURE = (Path(__file__).parent / "fixtures" / "detail_sample.html").read_text(encoding="utf-8")


def test_seed_paths_skip_excluded_genre_ranking():
    assert not any("categoryCode=107" in p for p in seeds.seed_paths(BL))
    assert any("categoryCode=107" in p for p in seeds.seed_paths(frozenset()))
    assert any("categoryCode=ALL" in p for p in seeds.seed_paths(BL))


def test_existing_bl_is_deleted_and_restored_when_flag_cleared(library):
    with db.session(library) as conn:
        db.upsert_comic(conn, comic_data(50, "BL 작품", genre="BL"))
        db.update_review(conn, 1, 50, score=8)

        removed, restored = db.apply_genre_exclusion(conn, BL)
        assert (removed, restored) == (1, 0)
        assert db.get_comic(conn, 50) is None and db.get_review(conn, 1, 50) is None
        state = conn.execute(
            "SELECT state, last_error FROM crawl_queue WHERE product_no = 50"
        ).fetchone()
        assert tuple(state) == ("skipped", "제외 장르:BL")
        assert db.apply_genre_exclusion(conn, BL) == (0, 0)  # 다시 불러도 그대로
        assert 50 not in db.next_pending(conn, 100)

        assert db.apply_genre_exclusion(conn, frozenset()) == (0, 1)  # 플래그를 풀면
        assert 50 in db.next_pending(conn, 100)  # 다음 수집 때 다시 가져온다


class FakeClient:
    """네트워크 없이 상세 페이지를 돌려주는 가짜 클라이언트."""

    def __init__(self, genre: str):
        self.html = FIXTURE.replace(">무협</a>", f">{genre}</a>")

    def get(self, path):
        from my_webtoon.crawler.client import Page

        return Page("https://series.naver.com" + path, 200, self.html)


def test_fetch_one_rejects_excluded_genre():
    import pytest

    with pytest.raises(parser.ExcludedGenre):
        runner.fetch_one(FakeClient("BL"), 11724795, BL)
    data, links = runner.fetch_one(FakeClient("무협"), 11724795, BL)
    assert data["genre"] == "무협" and links


def test_crawl_skips_bl_without_storing_or_following_links(library, monkeypatch):
    monkeypatch.setattr(runner, "PoliteClient", lambda *a, **k: _Ctx(FakeClient("BL")))
    with db.session(library) as conn:
        db.enqueue(conn, [11724795], None)
        db.set_meta(conn, "seeds_refreshed_at", db.now_iso())  # 시드 단계 건너뛰기
    result = runner.run_once(
        library, user_agent="t", delay_seconds=0, batch_size=10, recrawl_after_days=7,
        excluded_genres=BL,
    )  # fmt: skip
    with db.session(library) as conn:
        assert db.get_comic(conn, 11724795) is None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM crawl_queue WHERE discovered_from = 11724795"
            ).fetchone()[0]
            == 0
        )
    assert result.skipped == 1 and result.fetched == 0


class _Ctx:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *exc):
        return False
