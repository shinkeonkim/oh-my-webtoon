from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from my_webtoon import db
from my_webtoon.config import load_settings
from my_webtoon.web.app import create_app, parse_product_no

HX = {"HX-Request": "true"}


def make_client(library, password=None):
    settings = replace(
        load_settings(), database_path=library, app_password=password, crawl_enabled=False
    )
    return TestClient(create_app(settings))


@pytest.fixture
def client(library):
    with make_client(library) as c:
        yield c


def review(library, no):
    with db.session(library) as conn:
        return db.get_review(conn, no)


def test_onboarding_when_no_reviews(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "먼저 평가해 주세요" in resp.text


def test_quick_score_then_recommendations_with_rank(client, library):
    resp = client.post("/reviews/1", data={"score": "10", "toggle": "score"}, headers=HX)
    assert resp.status_code == 200
    assert "<b>10점</b>" in resp.text and "<html" not in resp.text  # 위젯 조각만

    page = client.get("/").text
    assert "검황 회귀전" in page
    assert '<span class="rank">1</span>' in page


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
    assert "7.5점" in resp.text and "보는 중" in resp.text and "42화까지" in resp.text
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
            db.update_review(conn, 5, score=9, status="seen", memo="비밀 메모")
        for path in ("/comics", "/comic/5", "/me", "/healthz"):
            assert c.get(path, follow_redirects=False).status_code == 200, path
        me = c.get("/me").text
        assert "계약 결혼의 공작님" in me and "9점" in me
        assert "비밀 메모" not in me  # 메모는 로그인했을 때만
        assert 'hx-post="/reviews/' not in me  # 평가 버튼 없음
        for path in ("/", "/rate", "/crawl", "/settings", "/reviews/5/edit"):
            assert c.get(path, follow_redirects=False).status_code == 303, path
        assert c.post("/reviews/5", data={"score": "1"}, follow_redirects=False).status_code == 303
        assert c.post("/reviews/5", data={"score": "1"}, headers=HX).status_code == 401


def test_change_password_logs_out_other_sessions(library):
    with make_client(library, password="initial-pw") as a, make_client(library, "initial-pw") as b:
        for c in (a, b):
            c.post("/login", data={"password": "initial-pw"})
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
        b.post("/login", data={"password": "initial-pw"})
        assert b.get("/", follow_redirects=False).status_code == 303  # 옛 비밀번호 안 됨
        b.post("/login", data={"password": "new-pw-123"})
        assert b.get("/", follow_redirects=False).status_code == 200

    with db.session(library) as conn:
        assert "new-pw-123" not in (db.get_meta(conn, "password_hash") or "")


def test_export_and_import_via_settings(client, library):
    client.post("/reviews/1", data={"score": "9"})
    exported = client.get("/settings/export.json")
    assert exported.headers["content-disposition"].startswith("attachment")
    client.post("/reviews/1/delete")
    resp = client.post(
        "/settings/import", files={"file": ("r.json", exported.content, "application/json")}
    )
    assert "1개를 가져왔어요" in resp.text
    assert review(library, 1).score == 9


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
