from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_webtoon import db
from my_webtoon.config import load_settings
from my_webtoon.web.app import create_app


def comic_data(product_no: int, title: str, **overrides) -> dict:
    """테스트용 작품 dict (db.upsert_comic 에 바로 넣을 수 있는 모양)."""
    data = {
        "product_no": product_no,
        "title": title,
        "authors": ["작가"],
        "publisher": "출판사",
        "genre": "소년",
        "tags": [],
        "synopsis": "",
        "status": "연재중",
        "age_rating": "전체 이용가",
        "unit": "화",
        "total_episodes": 100,
        "free_episodes": 10,
        "is_all_free": False,
        "naver_rating": 9.5,
        "popularity": 10_000,
        "thumbnail_url": None,
    }
    data.update(overrides)
    return data


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    with db.session(path) as conn:
        db.init_schema(conn)
    return path


@pytest.fixture
def library(db_path: Path) -> Path:
    """무협 4편 + 로맨스 3편이 들어 있는 작은 DB."""
    rows = [
        comic_data(
            1,
            "천마 회귀록",
            genre="무협",
            tags=["회귀", "먼치킨"],
            authors=["김무협"],
            synopsis="천마가 과거로 회귀해 무림을 평정한다. 강호의 고수들과 대결.",
        ),
        comic_data(
            2,
            "검황 회귀전",
            genre="무협",
            tags=["회귀", "복수"],
            authors=["이검객"],
            synopsis="검황이 회귀하여 무림에 복수한다. 강호 고수와의 결투.",
            free_episodes=40,
            total_episodes=300,
        ),
        comic_data(
            3,
            "무림 최강 막내",
            genre="무협",
            tags=["먼치킨"],
            authors=["김무협"],
            synopsis="무림 세가의 막내가 최강의 고수가 된다.",
            free_episodes=3,
            total_episodes=200,
        ),
        comic_data(
            4,
            "화산 귀환 무사",
            genre="무협",
            tags=["회귀"],
            authors=["박화산"],
            synopsis="화산파 무사가 회귀해 문파를 다시 세운다. 강호 무림.",
            is_all_free=True,
            free_episodes=150,
            total_episodes=150,
        ),
        comic_data(
            5,
            "계약 결혼의 공작님",
            genre="순정",
            tags=["로맨스판타지", "계약결혼"],
            authors=["최로판"],
            synopsis="공작과 계약 결혼한 영애의 달콤한 로맨스.",
        ),
        comic_data(
            6,
            "악녀의 정원",
            genre="순정",
            tags=["로맨스판타지", "빙의"],
            authors=["정빙의"],
            synopsis="소설 속 악녀에 빙의한 영애가 공작과 사랑에 빠진다.",
        ),
        comic_data(
            7,
            "성인 무협",
            genre="무협",
            tags=["회귀"],
            authors=["김무협"],
            synopsis="천마가 회귀한다 무림 강호",
            age_rating="청소년 이용불가",
        ),
    ]
    with db.session(db_path) as conn:
        for r in rows:
            db.upsert_comic(conn, r)
    return db_path


def make_client(library, password=None):
    settings = replace(
        load_settings(), database_path=library, app_password=password, crawl_enabled=False
    )
    return TestClient(create_app(settings))


@pytest.fixture
def client(library):
    with make_client(library) as c:
        yield c
