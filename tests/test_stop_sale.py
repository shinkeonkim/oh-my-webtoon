"""판매 중지 작품: 실패로 세지 않고, 매번 갱신 대상 맨 앞으로 다시 뽑히지도 않는다."""

import pytest

from my_webtoon import db
from my_webtoon.crawler import parser, runner
from my_webtoon.crawler.client import Page

from .conftest import comic_data
from .test_genre_exclusion import _Ctx

STOP_SALE_URL = "https://series.naver.com/error/stopSale.series"


class StopSaleClient:
    def get(self, path):
        return Page(STOP_SALE_URL, 200, "<html>판매가 중지된 작품입니다</html>")


def test_is_stop_sale_page():
    assert parser.is_stop_sale_page(STOP_SALE_URL)
    assert not parser.is_stop_sale_page("https://series.naver.com/comic/detail.series?productNo=1")


def test_fetch_one_raises_stop_sale():
    with pytest.raises(parser.StopSale):
        runner.fetch_one(StopSaleClient(), 50)


def test_crawl_skips_stop_sale_and_keeps_comic_out_of_stale(library, monkeypatch):
    monkeypatch.setattr(runner, "PoliteClient", lambda *a, **k: _Ctx(StopSaleClient()))
    with db.session(library) as conn:
        db.upsert_comic(
            conn, comic_data(50, "판매 중지된 작품", crawled_at="2020-01-01T00:00:00+00:00")
        )
        db.update_review(conn, 1, 50, score=8)
        db.enqueue(conn, [50], None)
        db.set_meta(conn, "seeds_refreshed_at", db.now_iso())  # 시드 단계 건너뛰기
        assert db.stale_product_nos(conn, 7, 10) == [50]
    result = runner.run_once(
        library, user_agent="t", delay_seconds=0, batch_size=10, recrawl_after_days=7,
    )  # fmt: skip
    assert (result.skipped, result.failed) == (1, 0)
    with db.session(library) as conn:
        assert db.stale_product_nos(conn, 7, 10) == []  # 다음 수집 때 또 뽑히지 않는다
        assert db.get_comic(conn, 50) is not None and db.get_review(conn, 1, 50) is not None
        state = conn.execute(
            "SELECT state, last_error FROM crawl_queue WHERE product_no = 50"
        ).fetchone()
        assert tuple(state) == ("skipped", "판매 중지")
