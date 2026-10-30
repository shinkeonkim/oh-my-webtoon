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


GL = frozenset({"BL", "[GL]"})  # config: EXCLUDED_GENRES=BL + EXCLUDED_TITLE_TAGS=GL


def test_title_tag_is_excluded_even_when_genre_is_not():
    """GL 작품은 장르가 로맨스 등으로 달려 있고 제목에만 [GL] 이 붙는다 — 제목 꼬리표로도 거른다."""
    assert db.excluded_reason("로맨스", "우리의 계절 [GL]", GL) == "[GL]"
    assert (
        db.excluded_reason("로맨스", "[ gl ] 우리의 계절", GL) == "[GL]"
    )  # 대소문자·괄호 안 공백 무시
    assert db.excluded_reason("BL", "아무 제목", GL) == "BL"
    assert db.excluded_reason("로맨스", "GLORY 의 계절", GL) is None  # 꼬리표가 아니면 그대로
    assert db.excluded_reason("로맨스", "우리의 계절 [GL]", BL) is None  # 설정에 없으면 그대로


def test_config_folds_title_tags_into_exclusions(monkeypatch):
    from my_webtoon.config import load_settings

    monkeypatch.delenv("EXCLUDED_TITLE_TAGS", raising=False)
    assert "[GL]" in load_settings().excluded_genres  # 기본값
    monkeypatch.setenv("EXCLUDED_TITLE_TAGS", "gl, [19금] ,")
    assert {"[gl]", "[19금]"} <= load_settings().excluded_genres
    monkeypatch.setenv("EXCLUDED_TITLE_TAGS", "")
    assert not any(e.startswith("[") for e in load_settings().excluded_genres)


def test_existing_gl_titles_are_deleted_and_restored(library):
    with db.session(library) as conn:
        db.upsert_comic(conn, comic_data(51, "우리의 계절 [GL]", genre="로맨스"))
        assert db.apply_genre_exclusion(conn, GL) == (1, 0)
        assert db.get_comic(conn, 51) is None
        state = conn.execute("SELECT last_error FROM crawl_queue WHERE product_no = 51").fetchone()
        assert state[0] == "제외 장르:[GL]"
        assert db.apply_genre_exclusion(conn, BL) == (0, 1)  # 꼬리표 설정을 빼면 다시 가져온다


def test_fetch_one_rejects_gl_title():
    import pytest

    client = FakeClient("로맨스")
    title = parser.parse_detail(11724795, client.html)["title"]
    client.html = client.html.replace(title, title + " [GL]")
    with pytest.raises(parser.ExcludedGenre, match=r"\[GL\]"):
        runner.fetch_one(client, 11724795, GL)
