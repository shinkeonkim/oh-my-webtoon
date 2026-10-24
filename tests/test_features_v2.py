"""빠른 평가 · 관심 없음 · 소식(변경 기록) · 이어 보기 · 취향 · 필터 · 운영 · 수집 우선순위 · 스냅숏."""

from my_webtoon import backup, db
from my_webtoon.recommender import Engine, ScoringOptions

from .conftest import comic_data
from .test_web import HX, ME, review

# -- 빠른 평가 -----------------------------------------------------------------


def test_quick_rating_saves_and_moves_to_next(client, library):
    page = client.get("/quick").text
    assert "빠른 평가" in page and 'id="quick"' in page

    first = _quick_product(page)
    resp = client.post(
        f"/quick/{first}", data={"action": "score", "score": "8", "done": "0"}, headers=HX
    )
    assert resp.status_code == 200 and "<html" not in resp.text  # 다음 장 조각만
    r = review(library, first)
    assert (r.score, r.status) == (8, "seen")  # 별점을 누르면 '봤음'까지
    assert _quick_product(resp.text) != first
    assert "이번에 1장" in resp.text and "되돌리기" in resp.text


def test_quick_skip_is_not_a_review_and_undo_restores(client, library):
    first = _quick_product(client.get("/quick").text)
    client.post(f"/quick/{first}", data={"action": "skip", "done": "0"}, headers=HX)
    assert review(library, first) is None  # '안 봤음'은 취향에 안 넣는다
    with db.session(library) as conn:
        assert first in db.quick_skips(conn, ME)
    resp = client.post(f"/quick/{first}/undo", data={"done": "1"}, headers=HX)
    assert _quick_product(resp.text) == first  # 되돌리면 그 작품을 다시 묻는다
    with db.session(library) as conn:
        assert first not in db.quick_skips(conn, ME)


def test_quick_queue_skips_reviewed_works_and_adult(library):
    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=10)
        queue = Engine().quick_queue(conn, ME, limit=50)
    nos = [c.product_no for c in queue]
    assert 1 not in nos and 7 not in nos  # 평가한 작품, 성인 작품은 안 묻는다


def _quick_product(html: str) -> int:
    import re

    return int(re.search(r'hx-post="/quick/(\d+)"', html).group(1))


# -- 추천 카드의 × (관심 없음) ----------------------------------------------------------


def test_dismiss_and_undo_from_recommendation_card(client, library):
    client.post("/reviews/1", data={"score": "10"}, headers=HX)
    page = client.get("/").text
    assert "card__dismiss" in page
    assert client.post("/reviews/2/dismiss").status_code == 204
    assert review(library, 2).status == "dismissed"
    assert client.post("/reviews/2/undismiss").status_code == 204
    assert review(library, 2) is None  # 다른 칸이 없으면 평가 자체가 사라진다


# -- 변경 기록 → 소식 ------------------------------------------------------------------


def test_upsert_records_changes_only_for_existing_comics(library):
    with db.session(library) as conn:
        assert db.upsert_comic(conn, comic_data(50, "새 작품")) == []  # 처음 수집은 소식이 아니다
        changed = db.upsert_comic(
            conn, comic_data(2, "검황 회귀전", free_episodes=60, total_episodes=310, status="완결")
        )
        assert set(changed) == {"free_episodes", "total_episodes", "status"}
        comic = db.get_comic(conn, 2)
    assert comic.free_gain == 20  # 40 → 60


def test_news_page_shows_free_gain_finished_and_watching(client, library):
    with db.session(library) as conn:
        db.upsert_comic(conn, comic_data(2, "검황 회귀전", free_episodes=60, total_episodes=300))
        db.upsert_comic(conn, comic_data(5, "계약 결혼의 공작님", status="완결"))
        db.upsert_comic(conn, comic_data(6, "악녀의 정원", free_episodes=20))
        db.update_review(conn, ME, 6, status="dismissed")  # 관심 없는 작품 소식은 안 보인다
        db.update_review(conn, ME, 4, status="watching", episodes_read=100)
    page = client.get("/news").text
    assert "무료 40→60화 (+20)" in page
    assert "완결됐어요" in page
    assert "악녀의 정원" not in page.split("바뀐 작품")[1].split("기간 한정")[0]
    assert "100/150화" in page and "50화 남음" in page and "그중 무료 <b>50화</b>" in page
    assert "무료 +20화" in client.get("/comics").text  # 카드 배지


def test_new_promo_is_recorded_once(library):
    with db.session(library) as conn:
        db.replace_promos(conn, {1: "10화 무료 5일 남음"})  # 첫 수집: 기록 안 함
        db.replace_promos(conn, {1: "10화 무료 4일 남음", 2: "20화 무료 9일 남음"})
        db.replace_promos(conn, {1: "10화 무료 3일 남음", 2: "20화 무료 8일 남음"})
        promos = [ch["product_no"] for ch, _ in db.recent_changes(conn, fields=("promo",))]
    assert promos == [2]


def test_plus_one_episode_button(client, library):
    with db.session(library) as conn:
        db.update_review(conn, ME, 4, status="watching", episodes_read=3)
    client.post("/reviews/4", data={"episodes_read": "4"}, headers=HX)
    assert review(library, 4).episodes_read == 4
    assert review(library, 4).status == "watching"


# -- 취향 · 필터 ----------------------------------------------------------------------


def test_taste_page(client, library):
    client.post("/reviews/1", data={"score": "10"}, headers=HX)
    client.post("/reviews/2", data={"score": "8"}, headers=HX)
    client.post("/reviews/5", data={"score": "2"}, headers=HX)
    page = client.get("/users/singun11/taste").text
    assert "별점 분포" in page and "추천기가 보는 내 취향" in page
    assert "무협" in page and "#회귀" in page


def test_comics_filters(client, library):
    def titles(**params):
        import re

        html = client.get("/comics", params=params).text
        return set(re.findall(r'<h3 class="card__title"><a [^>]+>([^<]+)</a>', html))

    assert titles(free="100") == {"화산 귀환 무사"}  # 전부 무료 150화
    assert titles(free="30") == {"화산 귀환 무사", "검황 회귀전"}
    assert titles(free="all") == {"화산 귀환 무사"}
    client.post("/reviews/4", data={"score": "9"}, headers=HX)
    assert "화산 귀환 무사" not in titles(unrated="1")
    with db.session(library) as conn:
        db.upsert_comic(conn, comic_data(1, "천마 회귀록", status="완결"))
    assert titles(status="완결") == {"천마 회귀록"}


# -- 운영 ----------------------------------------------------------------------------


def test_crawl_page_shows_response_times(client):
    client.get("/comics")
    page = client.get("/crawl").text
    assert "응답 시간" in page and "<code>/comics</code>" in page


def test_backup_is_verified(library, tmp_path):
    check = backup.backup_and_verify(library, tmp_path)
    assert check["ok"] and check["integrity"] == "ok"
    assert check["counts"]["comics"] == 7
    with db.session(library) as conn:
        assert backup.last_check(conn)["file"] == check["file"]
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database")
    assert not backup.verify_backup(broken)["ok"]


# -- 수집 우선순위 · 스냅숏 ------------------------------------------------------------------


def test_pending_prefers_often_linked(library):
    with db.session(library) as conn:
        db.enqueue(conn, [900, 901], source=1)
        db.enqueue(conn, [901], source=2)
        db.enqueue(conn, [901], source=3)
        assert db.enqueue(conn, [900, 902], source=4) == 1  # 새로 들어간 건 902 하나
        order = db.next_pending(conn, 10)
    assert order.index(901) < order.index(900) < order.index(902)


def test_snapshot_survives_another_pod(library):
    """무중단 배포 중 다른 파드(=다른 Engine)가 다음 페이지를 받아도 같은 순위를 이어 간다."""
    with db.session(library) as conn:
        for no in range(100, 140):
            db.upsert_comic(conn, comic_data(no, f"무림 회귀 {no}", genre="무협", tags=["회귀"],
                                             synopsis=f"회귀 무림 {no}"))  # fmt: skip
        db.update_review(conn, ME, 1, score=10)
        first, snap = Engine().recommend_page(conn, ME, ScoringOptions(), 0, 10)
        db.update_review(conn, ME, first[0].comic.product_no, score=0)
        second, same = Engine().recommend_page(conn, ME, ScoringOptions(), 10, 10, snapshot=snap)
    assert same == snap
    assert not {r.comic.product_no for r in first} & {r.comic.product_no for r in second}


def test_diversity_spreads_authors(library):
    with db.session(library) as conn:
        for no in range(200, 212):  # 한 작가의 비슷한 작품 12개
            db.upsert_comic(conn, comic_data(no, f"천마 회귀록 외전 {no}", genre="무협",
                                             tags=["회귀", "먼치킨"], authors=["김무협"],
                                             synopsis="천마가 과거로 회귀해 무림을 평정한다"))  # fmt: skip
        db.update_review(conn, ME, 1, score=10)
        plain = Engine().recommend(conn, ME, ScoringOptions(diversity=0, sink_stingy=False))
        mixed = Engine().recommend(conn, ME, ScoringOptions(diversity=0.5, sink_stingy=False))

    def first_other_author(recs):
        return next(i for i, r in enumerate(recs) if r.comic.authors != ["김무협"])

    assert first_other_author(mixed) < first_other_author(plain)
