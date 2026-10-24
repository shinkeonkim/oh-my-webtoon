from pathlib import Path

import pytest

from my_webtoon.crawler import parser

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("text", "total", "expected"),
    [
        ("3화 무료", 349, (3, False)),
        ("가격정보 전화 무료", 653, (653, True)),
        ("전권 무료", 12, (12, True)),
        ("1권 무료", 95, (1, False)),
        ("33화 무료   50일 남음", 272, (33, False)),
        ("820화 무료", 825, (820, False)),
        ("825화 무료", 825, (825, True)),  # 무료 수가 전체 이상이면 사실상 전화 무료
        ("1,200화 무료", 1300, (1200, False)),
        (None, 100, (0, False)),
        ("대여 200원", 100, (0, False)),
    ],
)
def test_parse_free_text(text, total, expected):
    info = parser.parse_free_text(text, total)
    assert (info.free_episodes, info.is_all_free) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5만", 50_000),
        ("1억 1,866만", 118_660_000),
        ("619.9만", 6_199_000),
        ("3,000만", 30_000_000),
        ("812", 812),
        (None, None),
        ("", None),
    ],
)
def test_parse_korean_count(text, expected):
    assert parser.parse_korean_count(text) == expected


def test_parse_meta_tags_drops_comic_and_synopsis():
    desc = "349 화 연재중, #COMIC, #무협, #먼치킨, #액션/판타지, 줄거리: #이건태그아님"
    assert parser.parse_meta_tags(desc) == ["무협", "먼치킨", "액션/판타지"]


def test_parse_detail():
    html = (FIXTURES / "detail_sample.html").read_text(encoding="utf-8")
    data = parser.parse_detail(11724795, html)

    assert data["title"] == "알고 보니 최강 신선"
    assert data["authors"] == ["목하치수", "열문만화", "도악천ci"]
    assert data["publisher"] == "KOCM"
    assert data["genre"] == "무협"
    assert data["status"] == "연재중"
    assert data["age_rating"] == "전체 이용가"
    assert data["tags"] == ["무협", "먼치킨", "액션/판타지", "환생"]
    assert data["total_episodes"] == 349
    assert data["unit"] == "화"
    assert data["free_episodes"] == 3
    assert data["is_all_free"] is False
    assert data["naver_rating"] == 9.9
    assert data["popularity"] == 118_660_000
    assert data["thumbnail_url"].endswith("sample.jpg?type=m260")
    # 숨겨진 '전체 줄거리'를 써야 하고, 더보기/접기 같은 UI 글자는 빠져야 한다
    assert "요괴왕" in data["synopsis"] and "환생했다" in data["synopsis"]
    assert "접기" not in data["synopsis"] and "\xa0" not in data["synopsis"]


def test_parse_detail_rejects_unknown_page():
    with pytest.raises(ValueError):
        parser.parse_detail(1, "<html><body>점검 중</body></html>")


def test_extract_product_links_excludes_self():
    html = (FIXTURES / "detail_sample.html").read_text(encoding="utf-8")
    assert parser.extract_product_links(html, exclude=11724795) == {5350583, 2140867}


def test_extract_promos_only_time_limited():
    html = (FIXTURES / "free_list_sample.html").read_text(encoding="utf-8")
    assert parser.extract_promos(html) == {2177348: "33화 무료 50일 남음"}


def test_is_login_page():
    assert parser.is_login_page("https://nid.naver.com/nidlogin.login?x", "")
    assert not parser.is_login_page("https://series.naver.com/comic/detail.series", "<html>")
