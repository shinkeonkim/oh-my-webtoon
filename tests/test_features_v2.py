"""빠른 평가 · 관심 없음 · 소식(변경 기록) · 이어 보기 · 취향 · 필터 · 운영 · 수집 우선순위 · 스냅숏."""

from datetime import UTC, datetime, timedelta

from my_webtoon import backup, db
from my_webtoon.recommender import Engine, ScoringOptions

from .conftest import comic_data
from .test_web import HX, ME, review

# -- 빠른 평가 -----------------------------------------------------------------


def test_quick_rating_saves_all_fields_on_explicit_save(client, library):
    page = client.get("/quick").text
    assert "빠른 평가" in page and 'id="quick"' in page

    first = _quick_product(page)
    data = {
        "action": "save",
        "score": "8",
        "status": "watching",
        "episodes_read": "12",
        "done": "0",
    }
    resp = client.post(f"/quick/{first}", data=data, headers=HX)
    assert resp.status_code == 200 and "<html" not in resp.text  # 다음 장 조각만
    r = review(library, first)
    assert (r.score, r.status, r.episodes_read) == (8, "watching", 12)  # 한 번에 여러 값
    assert _quick_product(resp.text) != first
    assert "이번에 1장" in resp.text and "되돌리기" in resp.text


def test_quick_score_only_means_seen_and_empty_save_stays(client, library):
    first = _quick_product(client.get("/quick").text)
    resp = client.post(
        f"/quick/{first}",
        data={"action": "save", "score": "", "status": "", "done": "0"},
        headers=HX,
    )
    assert (
        "하나는 골라 주세요" in resp.text and _quick_product(resp.text) == first
    )  # 넘어가지 않는다
    assert review(library, first) is None
    client.post(f"/quick/{first}", data={"action": "save", "score": "6", "status": ""}, headers=HX)
    assert review(library, first).status == "seen"


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
    client.post("/reviews/4", data={"status": "seen"}, headers=HX)
    assert "화산 귀환 무사" not in titles(unrated="1")  # 옛 주소: 상태 없는 것만 (mine=none)
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


# -- 본 것 포함/미포함 · 추천도 ------------------------------------------------------------


def test_reviewed_filter_include_and_only(library):
    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=10)
        db.update_review(conn, ME, 2, score=9)
        db.update_review(conn, ME, 6, status="dismissed")
        engine = Engine()

        def nos(mode):
            return {
                r.comic.product_no
                for r in engine.recommend(
                    conn, ME, ScoringOptions(reviewed=mode, sink_stingy=False), limit=50
                )
            }

        assert not nos("exclude") & {1, 2}
        assert {1, 2} <= nos("include") and 6 not in nos("include")  # 관심 없음은 늘 뺀다
        assert nos("only") == {1, 2}


def test_reviewed_item_similarity_leaves_itself_out(library):
    """10점 준 작품이 '자기 자신'과 닮아서 1등이 되면 안 된다 — 그 평가를 빼고 계산한다."""
    from my_webtoon.recommender.profile import taste_similarities

    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=10)
        db.update_review(conn, ME, 5, score=10)
        engine = Engine()
        features = engine._load(conn)
        sims = taste_similarities(features, db.all_reviews(conn, ME), engine.profile_opts)
    # 1 을 빼면 남은 취향은 로맨스(5) 하나뿐 → 무협인 1 은 로맨스인 6 보다 덜 비슷해야 한다.
    # 자기 자신을 포함했다면 1 의 유사도가 가장 높게 나왔을 것이다.
    assert sims[features.index[1]] < sims[features.index[6]]


def test_match_score_on_detail(client, library):
    assert 'class="match__value"' not in client.get("/comic/2").text  # 평가가 없으면 안 보인다
    client.post("/reviews/1", data={"score": "10"}, headers=HX)
    page = client.get("/comic/2").text
    assert 'class="match__value"' in page
    murim = _match(page)
    romance = _match(client.get("/comic/6").text)
    assert murim > romance  # 무협을 좋아하면 무협 작품 추천도가 더 높다
    assert "이 작품 평가는 빼고 계산" in client.get("/comic/1").text


def test_match_percents_equal_detail_match_score(library):
    """목록 카드용 일괄 계산은 상세 화면의 작품별 계산과 값이 같아야 한다 (성인 포함/제외 둘 다)."""
    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=10)
        db.update_review(conn, ME, 6, score=2)
        engine = Engine()
        for opts in (ScoringOptions(), ScoringOptions(include_adult=True)):
            percents = engine.match_percents(conn, ME, opts)
            assert percents and set(percents) == set(engine._comics)
            for no, percent in percents.items():
                assert percent == engine.match_score(conn, ME, no, opts)["percent"], no


def test_match_tone_and_card_badge(client, library):
    from my_webtoon.web.app import match_tone

    assert [match_tone(p) for p in (100, 90, 89, 70, 69, 40, 39, 0)] == [
        "top", "top", "high", "high", "mid", "mid", "low", "low",
    ]  # fmt: skip
    assert "badge--match-" not in client.get("/comics").text  # 취향이 없으면 배지도 없다
    client.post("/reviews/1", data={"score": "10"}, headers=HX)
    detail = client.get("/comic/2").text
    tone = match_tone(_match(detail))
    assert f'class="match match--{tone}"' in detail
    listing = client.get("/comics").text
    assert f'badge--match-{tone}" title="추천도 {_match(detail)} / 100' in listing


def _match(html: str) -> int:
    import re

    return int(re.search(r'class="match__value">(\d+)<', html).group(1))


def test_index_has_reviewed_tabs(client, library):
    client.post("/reviews/1", data={"score": "10"}, headers=HX)
    page = client.get("/?reviewed=include").text
    assert "본 것 포함" in page and "천마 회귀록" in page
    assert "/reviews/1/dismiss" not in page  # 평가한 작품 카드엔 × 가 없다 (상태를 덮어쓰지 않게)


# -- 시리즈에서 가져오기 -------------------------------------------------------------------


def test_series_import_preview_and_save(client, library):
    page = client.get("/import/series").text
    assert "javascript:" in page and "/import/series?nos=" in page  # 북마클릿

    client.post("/reviews/2", data={"status": "dropped"}, headers=HX)
    client.post("/reviews/4", data={"score": "9"}, headers=HX)
    preview = client.get("/import/series", params={"nos": "1,2,4,999999"}).text
    assert "받은 작품 4개" in preview and "수집된 만화 3개" in preview
    assert "이미 하차" in preview and "아직 수집 안 된 1개" in preview
    assert review(library, 1) is None  # 미리보기는 저장하지 않는다

    resp = client.post(
        "/import/series",
        data={"no": ["1", "2", "4"], "status": "seen", "queue_unknown": "1", "unknown": ["999999"]},
    )
    assert resp.status_code == 200 and "2개를" in resp.text
    assert review(library, 1).status == "seen"
    assert review(library, 2).status == "dropped"  # 이미 상태가 있으면 덮어쓰지 않는다
    assert (review(library, 4).score, review(library, 4).status) == (
        9,
        "seen",
    )  # 점수만 있으면 상태를 더한다
    with db.session(library) as conn:
        assert 999999 in db.next_pending(conn, 100)


def test_rated_card_does_not_resemble_itself(library):
    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=10)
        db.update_review(conn, ME, 2, score=9)
        recs = Engine().recommend(conn, ME, ScoringOptions(reviewed="include", sink_stingy=False))
    for rec in recs:
        assert rec.because is None or rec.because.product_no != rec.comic.product_no


# -- 평가하기: 상태 필터 · 무한 스크롤 · 가져오기 기본값 ---------------------------------


def test_rate_status_filter_combines_with_search(client, library):
    client.post("/reviews/1", data={"status": "watching"}, headers=HX)  # 천마 회귀록
    client.post("/reviews/2", data={"status": "seen"}, headers=HX)  # 검황 회귀전
    client.post("/reviews/4", data={"score": "8"}, headers=HX)  # 별점만 → 상태 없음

    def titles(**params):
        page = client.get("/rate", params=params).text
        return {t for t in ("천마 회귀록", "검황 회귀전", "화산 귀환 무사") if t in page}

    assert titles(mine="watching") == {"천마 회귀록"}
    assert titles(q="회귀", mine="seen") == {"검황 회귀전"}
    assert titles(q="회귀", mine="none") == set()  # 1·2는 상태가 있다
    assert titles(status="watching") == {"천마 회귀록"}  # 옛 주소 /rate?status=<내 상태>
    assert "천마 회귀록" not in client.get("/rate", params={"mine": "none"}).text
    assert "화산 귀환 무사" in client.get("/rate", params={"mine": "none"}).text
    assert titles(mine="none", genre="무협") == {
        "화산 귀환 무사"
    }  # 장르 같은 작품 필터와도 같이 걸린다

    page = client.get("/rate", params={"q": "회귀"}).text
    assert 'href="/rate?q=%ED%9A%8C%EA%B7%80&amp;mine=watching"' in page  # 탭이 검색어를 유지
    assert 'name="mine" value' not in page  # 상태 탭을 안 골랐으면 숨은 칸도 없다
    with db.session(library) as conn:
        counts = db.review_status_counts(conn, ME, "회귀")
    assert counts["watching"] == 1 and counts["seen"] == 1


def test_rate_infinite_scroll(client, monkeypatch):
    import importlib

    monkeypatch.setattr(importlib.import_module("my_webtoon.web.app"), "PER_PAGE", 2)
    first = client.get("/rate", params={"mine": "none"}).text
    assert 'hx-get="/rate?mine=none&amp;page=1"' in first
    more = client.get("/rate", params={"mine": "none", "page": 1}, headers=HX).text
    assert "<html" not in more and "card" in more
    assert 'hx-get="/rate?page=1"' in client.get("/rate").text


def test_series_import_defaults_to_watching(client, library):
    preview = client.get("/import/series", params={"nos": "1"}).text
    assert 'value="watching" id="status-watching" checked' in preview
    client.post("/import/series", data={"no": ["1"]})
    assert review(library, 1).status == "watching"


# -- Prometheus 메트릭 ---------------------------------------------------------


def test_prometheus_metrics(client, library):
    client.get("/comics")
    client.get("/comic/1")
    client.get("/없는-주소")
    client.get("/static/style.css")
    client.post("/reviews/1", data={"status": "watching"}, headers=HX)
    with db.session(library) as conn:
        run = db.start_run(conn)
        db.finish_run(conn, run, fetched=3, discovered=1, failed=0, note="", skipped=2)

    text = client.app.state.prometheus.text().decode()
    assert (
        'mywebtoon_http_requests_total{method="GET",route="/comic/{product_no}",status="200"} 1.0'
        in text
    )
    assert 'route="(unmatched)"' in text and "없는" not in text  # 라벨이 주소마다 늘지 않는다
    assert "/healthz" not in text and "/static" not in text
    assert 'route="(unmatched)",status="200"' not in text  # 정적 파일이 (unmatched) 로 새지 않는다
    assert 'mywebtoon_comics{status="연재중"}' in text
    assert 'mywebtoon_reviews{status="watching"} 1.0' in text
    assert 'mywebtoon_crawl_last_pages{result="skipped"} 2.0' in text
    assert "mywebtoon_crawl_last_finished_timestamp_seconds" in text
    assert 'mywebtoon_comic_changes_24h{field="free_episodes"} 0.0' in text  # 변경 없어도 0 으로
    assert "mywebtoon_db_up 1.0" in text  # process_* 는 /proc 이 있는 리눅스(파드)에서만 나온다
    assert client.get("/metrics").status_code == 404  # 공개 포트에는 없다


def test_metrics_server_listens_on_its_own_port(library):
    import socket
    import urllib.request
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from my_webtoon.config import load_settings
    from my_webtoon.web.app import create_app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings = replace(
        load_settings(), database_path=library, crawl_enabled=False, metrics_port=port
    )
    with TestClient(create_app(settings)):
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read()
        assert b"mywebtoon_comics" in body


def test_quick_skip_expires(client, library):
    first = _quick_product(client.get("/quick").text)
    client.post(f"/quick/{first}", data={"action": "skip", "done": "0"}, headers=HX)
    assert "30일 동안만" in client.get("/quick").text
    with db.session(library) as conn:
        assert first in db.quick_skips(conn, ME)
        old = (datetime.now(UTC) - timedelta(days=db.QUICK_SKIP_DAYS + 1)).isoformat()
        conn.execute("UPDATE quick_skips SET skipped_at = ?", (old,))
        conn.commit()
        assert first not in db.quick_skips(conn, ME)  # 기간이 지나면 다시 물어본다
        db.set_quick_skip(conn, ME, first, True)  # 다시 넘기면 그때부터 다시 센다
        assert first in db.quick_skips(conn, ME)
