from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from my_webtoon import db
from my_webtoon.config import load_settings
from my_webtoon.web.app import create_app, parse_product_no


@pytest.fixture
def client(library, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    settings = replace(
        load_settings(), database_path=library, app_password=None, crawl_enabled=False
    )
    with TestClient(create_app(settings)) as c:
        yield c


def test_onboarding_when_no_ratings(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "먼저 평가해 주세요" in resp.text


def test_rate_via_htmx_then_get_recommendations(client, library):
    resp = client.post(
        "/ratings/1", data={"kind": "rated", "score": "5"}, headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200
    assert "★5 저장됨" in resp.text  # 위젯 조각만 돌아온다
    assert "<html" not in resp.text

    resp = client.get("/")
    assert "검황 회귀전" in resp.text
    assert "천마 회귀록" in resp.text  # '...와 비슷해요' 설명에 등장

    with db.session(library) as conn:
        assert db.get_rating(conn, 1).score == 5


def test_quick_mark_seen_and_undo(client, library):
    client.post("/ratings/2", data={"kind": "seen"}, headers={"HX-Request": "true"})
    with db.session(library) as conn:
        assert db.get_rating(conn, 2).kind == "seen"
    client.post("/ratings/2/delete", headers={"HX-Request": "true"})
    with db.session(library) as conn:
        assert db.get_rating(conn, 2) is None


def test_invalid_rating_is_rejected(client):
    assert client.post("/ratings/1", data={"kind": "rated"}).status_code == 400


def test_search(client):
    resp = client.get("/rate", params={"q": "회귀"})
    assert "천마 회귀록" in resp.text and "계약 결혼" not in resp.text


def test_detail_and_me_and_crawl_pages(client):
    client.post("/ratings/5", data={"kind": "rated", "score": "4"})
    assert "악녀의 정원" in client.get("/comic/5").text
    assert "계약 결혼의 공작님" in client.get("/me").text
    assert client.get("/crawl").status_code == 200
    assert client.get("/comic/999").status_code == 404


def test_tuning_query_changes_scores(client):
    client.post("/ratings/1", data={"kind": "rated", "score": "5"})
    default = client.get("/").text
    tuned = client.get(
        "/", params={"tuned": "1", "free_weight": "0", "episode_weight": "0", "rating_weight": "0"}
    ).text
    assert "보정 1.00" in tuned and "보정 1.00" not in default


def test_login_required_when_password_set(library):
    settings = replace(
        load_settings(), database_path=library, app_password="pw", crawl_enabled=False
    )
    with TestClient(create_app(settings)) as c:
        assert c.get("/", follow_redirects=False).headers["location"] == "/login"
        assert c.get("/healthz").status_code == 200
        c.post("/login", data={"password": "wrong"})
        assert c.get("/", follow_redirects=False).status_code == 303
        c.post("/login", data={"password": "pw"})
        assert c.get("/", follow_redirects=False).status_code == 200


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
