"""시연용 계정: 막힌 동작, 기준 평가로 매일 초기화."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from my_webtoon import db, demo
from my_webtoon.config import load_settings
from my_webtoon.web import auth
from my_webtoon.web.app import create_app

ME = 1


def setup_demo(library):
    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=10, memo="내 메모")
        db.update_review(conn, ME, 5, status="seen")
        user = db.create_user(conn, "test", auth.hash_password("test1234"))
        n = demo.make_demo(conn, user, copy_from=db.get_user(conn, ME))
        return user, n


def test_make_demo_copies_reviews_without_memo(library):
    user, n = setup_demo(library)
    with db.session(library) as conn:
        reviews = db.all_reviews(conn, user.id)
        assert db.get_user(conn, user.id).is_demo
    assert n == 2 and reviews[1].score == 10 and reviews[1].memo == ""


def test_reset_restores_baseline_only_when_due(library):
    user, _ = setup_demo(library)
    with db.session(library) as conn:
        db.update_review(conn, user.id, 1, score=0)  # 방문자가 바꿈
        db.update_review(conn, user.id, 2, status="dropped")
        assert demo.reset_due(conn) == []  # 방금 초기화했으니 아직
        db.set_meta(
            conn, f"demo_reset_at:{user.id}", (datetime.now(UTC) - timedelta(days=2)).isoformat()
        )
        assert demo.reset_due(conn) == ["test"]
        reviews = db.all_reviews(conn, user.id)
        assert reviews[1].score == 10 and 2 not in reviews
        assert db.get_review(conn, ME, 1).memo == "내 메모"  # 원본은 그대로


def test_demo_user_is_restricted_in_web(library):
    setup_demo(library)
    settings = replace(
        load_settings(), database_path=library, app_password="admin-pw", crawl_enabled=False
    )
    with TestClient(create_app(settings)) as c:
        c.post("/login", data={"username": "test", "password": "test1234"})
        page = c.get("/").text
        assert "시연용 계정이에요" in page
        assert (
            c.post("/reviews/3", data={"score": "8"}, headers={"HX-Request": "true"}).status_code
            == 200
        )

        r = c.post(
            "/settings/password",
            data={"current": "test1234", "new": "hijacked1", "confirm": "hijacked1"},
        )
        assert "error=" in str(r.url)
        r = c.post("/import", data={"url": "123"})
        assert "error=" in str(r.url)
        r = c.post("/settings/import", files={"file": ("r.json", b"{}", "application/json")})
        assert "error=" in str(r.url)
        assert "비밀번호 바꾸기" not in c.get("/settings").text

    with db.session(library) as conn:
        assert auth.authenticate(conn, "test", "test1234") is not None  # 비밀번호 그대로


def test_unmake_demo(library):
    user, _ = setup_demo(library)
    with db.session(library) as conn:
        demo.unmake_demo(conn, user)
        assert not db.get_user(conn, user.id).is_demo
        assert demo.reset_due(conn) == []


def test_cli_demo_command(library, monkeypatch, capsys):
    from my_webtoon import cli

    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=10)
        db.create_user(conn, "test", None)
    monkeypatch.setenv("DATABASE_PATH", str(library))
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    cli.main(["user", "demo", "test", "--copy-from", "singun11"])
    cli.main(["user", "list"])
    out = capsys.readouterr().out
    assert "기준 평가 1개" in out and "데모" in out
