import pytest

from my_webtoon import db
from my_webtoon.web.app import parse_product_no

from .conftest import make_client

HX = {"HX-Request": "true"}


ME = 1  # 관리자 singun11


def review(library, no, user_id=ME):
    with db.session(library) as conn:
        return db.get_review(conn, user_id, no)


def login(c, username="singun11", password="initial-pw"):
    return c.post("/login", data={"username": username, "password": password})


def test_onboarding_when_no_reviews(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "추천을 시작하려면" in resp.text


def test_quick_score_then_recommendations_with_rank(client, library):
    resp = client.post("/reviews/1", data={"score": "10", "toggle": "score"}, headers=HX)
    assert resp.status_code == 200
    assert '<span class="stars__value">5</span>' in resp.text  # 10점 = 별 5개
    assert resp.text.count("stars__half--l is-on") == 5 and "<html" not in resp.text  # 위젯 조각만

    page = client.get("/").text
    assert "검황 회귀전" in page
    assert '<span class="card__rank">1</span>' in page


def test_score_and_status_can_be_set_together(client, library):
    client.post("/reviews/2", data={"score": "7", "toggle": "score"}, headers=HX)
    client.post("/reviews/2", data={"status": "seen", "toggle": "status"}, headers=HX)
    r = review(library, 2)
    assert (r.score, r.status) == (7, "seen")


def test_pressing_same_button_again_clears_only_that_field(client, library):
    client.post("/reviews/2", data={"score": "7", "toggle": "score"}, headers=HX)
    client.post("/reviews/2", data={"status": "seen", "toggle": "status"}, headers=HX)
    client.post("/reviews/2", data={"status": "seen", "toggle": "status"}, headers=HX)
    r = review(library, 2)
    assert (r.score, r.status) == (7, None)


def test_full_form_saves_aspects_and_blanks(client, library):
    form = {
        "full": "1", "score": "", "status": "watching", "episodes_read": "42",
        "aspect_story": "9", "aspect_art": "6", "aspect_character": "", "aspect_pacing": "",
        "aspect_immersion": "", "memo": "작화가 아쉽다",
    }  # fmt: skip
    resp = client.post("/reviews/3", data=form, headers=HX)
    assert "세부 평균 3.8" in resp.text and "42화까지 봄" in resp.text
    assert (
        'is-on" type="button" aria-pressed="true" hx-post="/reviews/3" hx-vals=\'{"status": "watching"'
        in resp.text
    )
    r = review(library, 3)
    assert r.score is None and r.aspects == {"story": 9, "art": 6} and r.episodes_read == 42
    assert "<form" in client.get("/reviews/3/edit").text


def test_delete_review(client, library):
    client.post("/reviews/2", data={"status": "seen"}, headers=HX)
    client.post("/reviews/2/delete", headers=HX)
    assert review(library, 2) is None


def test_invalid_review_is_rejected(client):
    assert client.post("/reviews/1", data={"score": "11"}).status_code == 400
    assert client.post("/reviews/1", data={"score": "abc"}).status_code == 400
    assert client.post("/reviews/999", data={"score": "5"}).status_code == 404


def test_search_and_comics_list(client):
    assert "천마 회귀록" in client.get("/rate", params={"q": "회귀"}).text
    page = client.get("/comics", params={"genre": "순정", "sort": "rating"}).text
    assert "악녀의 정원" in page and "천마 회귀록" not in page


def test_tuning_query_changes_scores(client):
    client.post("/reviews/1", data={"score": "10"})
    default = client.get("/").text
    tuned = client.get(
        "/", params={"tuned": "1", "free_weight": "0", "episode_weight": "0", "rating_weight": "0"}
    ).text
    assert "보정 1.00" in tuned and "보정 1.00" not in default


def test_public_pages_are_read_only(library):
    with make_client(library, password="initial-pw") as c:
        with db.session(library) as conn:
            db.update_review(conn, ME, 5, score=9, status="seen", memo="비밀 메모")
        for path in ("/comics", "/comic/5", "/users", "/users/singun11", "/healthz"):
            assert c.get(path, follow_redirects=False).status_code == 200, path
        page = c.get("/users/singun11").text
        assert "계약 결혼의 공작님" in page and '<span class="stars__value">4.5</span>' in page
        assert "비밀 메모" not in page  # 메모는 본인만
        assert 'hx-post="/reviews/' not in page  # 평가 버튼 없음
        assert c.get("/users/nobody").status_code == 404
        for path in ("/", "/rate", "/crawl", "/settings", "/me", "/reviews/5/edit"):
            assert c.get(path, follow_redirects=False).status_code == 303, path
        assert c.post("/reviews/5", data={"score": "1"}, follow_redirects=False).status_code == 303
        assert c.post("/reviews/5", data={"score": "1"}, headers=HX).status_code == 401


def test_login_and_my_page(library):
    with make_client(library, password="initial-pw") as c:
        assert "error=1" in str(login(c, password="wrong").url)
        login(c)
        assert c.get("/", follow_redirects=False).status_code == 200
        me = c.get("/me", follow_redirects=False)
        assert me.headers["location"] == "/users/singun11"


def test_change_password_logs_out_other_sessions(library):
    with make_client(library, password="initial-pw") as a, make_client(library, "initial-pw") as b:
        for c in (a, b):
            login(c)
            assert c.get("/", follow_redirects=False).status_code == 200

        bad = a.post(
            "/settings/password", data={"current": "x", "new": "n" * 8, "confirm": "n" * 8}
        )
        assert "error=" in str(bad.url)
        short = a.post(
            "/settings/password", data={"current": "initial-pw", "new": "a", "confirm": "a"}
        )
        assert "error=" in str(short.url)
        a.post(
            "/settings/password",
            data={"current": "initial-pw", "new": "new-pw-123", "confirm": "new-pw-123"},
        )

        assert a.get("/", follow_redirects=False).status_code == 200  # 바꾼 세션은 유지
        assert b.get("/", follow_redirects=False).status_code == 303  # 다른 세션은 끊김
        login(b)
        assert b.get("/", follow_redirects=False).status_code == 303  # 옛 비밀번호 안 됨
        login(b, password="new-pw-123")
        assert b.get("/", follow_redirects=False).status_code == 200


def test_admin_adds_user_and_reviews_stay_separate(library):
    with make_client(library, "initial-pw") as admin, make_client(library, "initial-pw") as alice:
        login(admin)
        admin.post("/settings/users", data={"username": "alice", "password": "alice-pw-1"})
        assert "alice" in admin.get("/users").text

        login(alice, "alice", "alice-pw-1")
        alice.post("/reviews/5", data={"score": "10"}, headers=HX)
        admin.post("/reviews/5", data={"status": "dismissed"}, headers=HX)

        with db.session(library) as conn:
            alice_id = db.get_user_by_name(conn, "alice").id
        assert (
            review(library, 5, alice_id).score == 10 and review(library, 5, alice_id).status is None
        )
        assert review(library, 5).status == "dismissed" and review(library, 5).score is None

        # alice 는 관리자가 아니다
        r = alice.post("/settings/users", data={"username": "bob", "password": "bob-pw-123"})
        assert r.status_code == 403
        assert alice.post("/crawl/run").status_code == 403
        # 남의 페이지에서는 읽기 전용, 내 페이지에서는 편집 가능
        assert 'hx-post="/reviews/' not in alice.get("/users/singun11").text
        assert 'hx-post="/reviews/' in alice.get("/users/alice").text


def test_score_filter_on_user_page(client):
    client.post("/reviews/1", data={"score": "10"})
    client.post("/reviews/2", data={"score": "8", "status": "seen"})
    client.post("/reviews/3", data={"status": "wish"})
    ten = client.get("/users/singun11", params={"score": "10"}).text
    assert "천마 회귀록" in ten and "검황 회귀전" not in ten
    none = client.get("/users/singun11", params={"score": "none"}).text
    assert "무림 최강 막내" in none and "천마 회귀록" not in none
    both = client.get("/users/singun11", params={"score": "8", "status": "seen"}).text
    assert "검황 회귀전" in both and "천마 회귀록" not in both


def test_infinite_scroll_returns_fragment(client, library):
    from .conftest import comic_data

    with db.session(library) as conn:
        for no in range(100, 140):
            db.upsert_comic(conn, comic_data(no, f"무림 회귀 {no}", genre="무협", tags=["회귀"]))
    client.post("/reviews/1", data={"score": "10"})
    first = client.get("/").text
    assert 'hx-trigger="revealed"' in first and "page=1" in first
    more = client.get("/", params={"page": "1"}, headers=HX).text
    assert "<html" not in more and '<span class="card__rank">31</span>' in more
    comics_more = client.get("/comics", params={"page": "1"}, headers=HX).text
    assert "<html" not in comics_more and "card" in comics_more


@pytest.mark.parametrize(
    ("ua", "expected"),
    [
        ("Mozilla/5.0 (Linux; Android 15) Chrome/130 Mobile", "https://m.series.naver.com/"),
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)", "https://m.series.naver.com/"),
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "https://series.naver.com/"),
    ],
)
def test_series_link_depends_on_device(client, ua, expected):
    page = client.get("/comic/1", headers={"User-Agent": ua}).text
    assert f'href="{expected}' in page
    if "Mobile" in ua or "iPhone" in ua:
        assert 'target="_blank"' not in page.split("네이버 시리즈에서 보기")[0].rsplit("<a", 1)[-1]


def test_naver_search_link_on_rate(client):
    page = client.get("/rate", params={"q": "없는작품"}).text
    assert "series.naver.com/search/search.series" in page


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://series.naver.com/comic/detail.series?productNo=2140867", 2140867),
        ("https://m.series.naver.com/comic/detail.series?productNo=12&x=1", 12),
        (" 667537 ", 667537),
        ("신의 탑", None),
    ],
)
def test_parse_product_no(text, expected):
    assert parse_product_no(text) == expected


def test_half_star_scores_and_styleguide(client):
    resp = client.post("/reviews/1", data={"score": "7", "toggle": "score"}, headers=HX)
    assert '<span class="stars__value">3.5</span>' in resp.text  # 7점 = 별 3.5개
    assert resp.text.count(' is-on"') >= 7  # 반 개 7칸이 켜짐
    resp = client.post("/reviews/1", data={"score": "0", "toggle": "score"}, headers=HX)
    assert "stars__zero is-on" in resp.text  # 0점(별 0개)도 따로 줄 수 있다
    page = client.get("/ui").text
    assert "UI 컴포넌트" in page and "stars__track" in page


def test_static_assets_are_versioned(client):
    page = client.get("/comics").text
    assert "/static/style.css?v=" in page and "/static/htmx.min.js?v=" in page


def test_readyz_after_warm_up(client):
    import time

    for _ in range(50):
        if client.get("/readyz").status_code == 200:
            break
        time.sleep(0.05)
    assert client.get("/readyz").json() == {"ready": True}


def test_next_crawl_waits_for_interval():
    from datetime import UTC, datetime, timedelta

    from my_webtoon.crawler.runner import next_run_delay

    now = datetime(2026, 10, 25, 12, tzinfo=UTC)
    assert next_run_delay(None, 3600, now) == 0  # 처음이면 바로
    assert next_run_delay((now - timedelta(minutes=20)).isoformat(), 3600, now) == 2400
    assert next_run_delay((now - timedelta(hours=2)).isoformat(), 3600, now) == 0


def test_mobile_tabbar_and_favicon(client):
    page = client.get("/").text
    assert 'class="tabbar"' in page and 'rel="icon" href="/static/favicon.svg' in page
    for path in (
        "/static/favicon.svg",
        "/static/apple-touch-icon.png",
        "/static/manifest.webmanifest",
    ):
        assert client.get(path).status_code == 200, path
    tabbar = page.split('class="tabbar"', 1)[1].split("</nav>", 1)[0]
    assert all(label in tabbar for label in ("추천", "평가", "소식", "내 정보"))
    # 하위 메뉴는 화면 위 탭으로 (모바일에서도 갈 수 있게)
    assert 'href="/crawl"' in client.get("/news").text.split('class="subnav"', 1)[1]
    rate = client.get("/rate").text
    assert all(
        h in rate.split('class="subnav"', 1)[1][:2000] for h in ('href="/quick"', 'href="/users/')
    )
    assert 'class="subnav"' not in client.get("/comic/1").text  # 하위 화면이 아니면 숨긴다


def test_kst_filter_and_mobile_link():
    from my_webtoon.web.app import kst

    assert kst("2026-10-24T14:14:00+00:00") == "10-24 23:14"
    assert kst("2026-10-24T16:30:00+00:00", "%Y-%m-%d %H:%M") == "2026-10-25 01:30"
    assert kst(None) == ""


def test_infinite_scroll_carries_snapshot(client, library):
    from .conftest import comic_data

    with db.session(library) as conn:
        for no in range(100, 140):
            db.upsert_comic(conn, comic_data(no, f"무림 회귀 {no}", genre="무협", tags=["회귀"]))
    client.post("/reviews/1", data={"score": "10"})
    first = client.get("/").text
    import re

    snap = re.search(r"snap=([\w-]+)&amp;page=1", first).group(1)
    shown = set(re.findall(r'href="/comic/(\d+)"', first))
    client.post("/reviews/100", data={"score": "0"})  # 스크롤 중 평가
    more = client.get("/", params={"snap": snap, "page": "1"}, headers=HX).text
    assert not shown & set(re.findall(r'href="/comic/(\d+)"', more))


def test_theme_defaults_to_system(client):
    page = client.get("/").text
    assert "data-theme=" not in page.split(">", 2)[1]  # <html> 에 속성 없음 → OS 설정을 따른다
    assert 'data-theme-option="system" class="is-on"' in page


def test_theme_choice_is_kept_in_cookie(client):
    res = client.post(
        "/theme", data={"theme": "light", "next": "/news?x=1"}, follow_redirects=False
    )
    assert res.status_code == 303 and res.headers["location"] == "/news?x=1"
    assert client.cookies.get("theme") == "light"
    page = client.get("/").text
    assert '<html lang="ko" data-theme="light">' in page
    assert 'data-theme-option="light" class="is-on"' in page
    # HTMX 는 쿠키만 바꾸고 화면은 스크립트가 바꾼다
    assert client.post("/theme", data={"theme": "dark"}, headers=HX).status_code == 204
    assert '<html lang="ko" data-theme="dark">' in client.get("/comics").text
    client.post("/theme", data={"theme": "system"}, headers=HX)
    assert "data-theme=" not in client.get("/").text.split(">", 2)[1]


def test_theme_rejects_unknown_value_and_foreign_redirect(client):
    assert client.post("/theme", data={"theme": "pink"}).status_code == 400
    res = client.post(
        "/theme", data={"theme": "dark", "next": "//evil.example"}, follow_redirects=False
    )
    assert res.headers["location"] == "/"
    client.cookies.set("theme", "pink")  # 손으로 바꾼 쿠키는 무시
    assert "data-theme=" not in client.get("/").text.split(">", 2)[1]


def test_navbar_search_suggests_and_links_to_detail(client):
    assert 'class="search__input"' in client.get("/").text  # 모든 화면 위 메뉴에 검색창
    html = client.get("/search/suggest", params={"q": "천마"}).text
    assert "<html" not in html and 'href="/comic/1"' in html  # 조각만, 누르면 상세로
    assert "<mark>천마</mark>" in html
    assert 'href="/comics?q=%EC%B2%9C%EB%A7%88"' in html  # 전체 결과 보기
    assert (
        'href="/comic/1"' in client.get("/search/suggest", params={"q": "천마회귀"}).text
    )  # 띄어쓰기 무시
    assert "맞는 작품이 없어요" in client.get("/search/suggest", params={"q": "없는제목"}).text
    assert client.get("/search/suggest", params={"q": " "}).text.strip() == ""


def test_search_and_guide_are_public(library):
    from .conftest import make_client

    with make_client(library, password="pw") as anon:
        assert 'href="/comic/1"' in anon.get("/search/suggest", params={"q": "천마"}).text
        guide = anon.get("/guide")
        assert (
            guide.status_code == 200 and "사용 가이드" in guide.text and 'id="match"' in guide.text
        )
        assert "badge--match-" not in anon.get("/search/suggest", params={"q": "천마"}).text
