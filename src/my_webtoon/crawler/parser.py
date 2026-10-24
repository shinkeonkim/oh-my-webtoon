"""네이버 시리즈 HTML → 파이썬 dict.

네트워크와 완전히 분리된 "순수 함수"만 둔다. 그래서 tests/fixtures 의 HTML 로
바로 테스트할 수 있고, 네이버가 마크업을 바꾸면 여기만 고치면 된다.

상세 페이지(https://series.naver.com/comic/detail.series?productNo=...)에서 쓰는 부분:

    <div class="end_head"><h2>제목</h2> ... <div class="score_area">...<em>9.9</em></div>
        <a class="btn_download"><span>5만</span></a>          ← 다운로드 수
    <li class="info_lst"><ul>
        <li class="ing"><span>연재중</span></li>              ← 연재 상태 (완결이면 class 없음)
        <li><span><a href="...genreCode=88">무협</a></span></li>
        <li><span>글</span><a>작가</a></li> / <li><span>그림</span>... / <li><span>출판사</span>...
        <li>전체 이용가</li>
    <div class="_synopsis">짧은 줄거리 .. 더보기</div>
    <div class="_synopsis" style="display: none">전체 줄거리</div>
    <div class="area_text_information">가격정보 <strong>3화</strong> 무료</div>
    <h5 class="end_total_episode">총 <strong>349</strong>화</h5>
    <meta name="description" content="349 화 연재중, #COMIC, #무협, #먼치킨, ... 줄거리: ...">
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

PRODUCT_NO_RE = re.compile(r"productNo=(\d+)")


# ---------------------------------------------------------------------------
# 작은 텍스트 파서들 — 각자 테스트가 있다 (tests/test_parser.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FreeInfo:
    free_episodes: int  # 무료로 볼 수 있는 회차(또는 권) 수
    is_all_free: bool  # "전화 무료" / "전권 무료"


def parse_free_text(text: str | None, total_episodes: int | None) -> FreeInfo:
    """'3화 무료' / '전화 무료' / '1권 무료' / '33화 무료 50일 남음' → FreeInfo.

    표시가 아예 없으면 무료 회차 0 으로 본다.
    """
    if not text:
        return FreeInfo(0, False)
    compact = re.sub(r"\s+", "", text)
    if re.search(r"전(?:화|권)무료", compact):
        return FreeInfo(total_episodes or 0, True)
    m = re.search(r"(\d[\d,]*)(?:화|권)무료", compact)
    if not m:
        return FreeInfo(0, False)
    n = int(m.group(1).replace(",", ""))
    all_free = total_episodes is not None and total_episodes > 0 and n >= total_episodes
    return FreeInfo(n, all_free)


def parse_korean_count(text: str | None) -> int | None:
    """'5만' → 50000, '1억 1,866만' → 118660000, '619.9만' → 6199000, '3,000만' → 30000000."""
    if not text:
        return None
    s = text.replace(",", "").replace(" ", "")
    total = 0.0
    matched = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)(억|만|천)?", s):
        matched = True
        total += float(num) * {"억": 1e8, "만": 1e4, "천": 1e3}.get(unit, 1)
    return int(total) if matched else None


def parse_meta_tags(description: str | None) -> list[str]:
    """메타 설명의 해시태그 목록. '#COMIC' 처럼 모든 작품에 붙는 건 뺀다.

    '349 화 연재중, #COMIC, #무협, #먼치킨, #액션/판타지, 줄거리: ...' → ['무협', '먼치킨', '액션/판타지']
    """
    if not description:
        return []
    head = description.split("줄거리:", 1)[0]
    tags = [t.strip() for t in re.findall(r"#([^,#]+)", head)]
    return [t for t in dict.fromkeys(tags) if t and t.upper() != "COMIC"]


def _clean(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"(더보기|접기)\s*$", "", text.strip())
    text = re.sub(r"\s*\.\.\s*$", "", text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*", "\n", text)).strip()


def thumbnail_from_og(url: str | None) -> str | None:
    """og:image 는 600x314 로 잘린 이미지라, 세로 표지(m260)로 바꿔 쓴다."""
    if not url:
        return None
    return re.sub(r"\?type=.*$", "", url) + "?type=m260"


# ---------------------------------------------------------------------------
# 페이지 단위 파서
# ---------------------------------------------------------------------------


class LoginRequired(Exception):
    """19금 등 로그인이 필요한 작품. 크롤러는 이 작품을 건너뛴다."""


def is_login_page(final_url: str, html: str) -> bool:
    return "nid.naver.com" in final_url or "nidlogin" in html[:5000]


def parse_detail(product_no: int, html: str) -> dict:
    """상세 페이지 HTML → db.upsert_comic() 에 바로 넣을 수 있는 dict."""
    soup = BeautifulSoup(html, "html.parser")
    head = soup.select_one("div.end_head")
    if head is None or head.h2 is None:
        raise ValueError(f"상세 페이지 구조를 찾지 못했다 (productNo={product_no})")

    title = head.h2.get_text(strip=True)
    score_el = head.select_one(".score_area em")
    download_el = head.select_one(".btn_download span")

    status = genre = publisher = age_rating = None
    authors: list[str] = []
    for li in soup.select("li.info_lst > ul > li"):
        label_el = li.find("span", recursive=False)
        label = label_el.get_text(strip=True) if label_el else ""
        link = li.find("a")
        if label in ("연재중", "완결", "휴재"):
            status = label
        elif link and "genreCode=" in (link.get("href") or ""):
            genre = link.get_text(strip=True)
        elif label in ("글", "그림", "글/그림", "원작"):
            for a in li.find_all("a"):
                name = a.get_text(strip=True)
                if name and name not in authors:
                    authors.append(name)
        elif label == "출판사" and link:
            publisher = link.get_text(strip=True)
        elif "이용" in li.get_text():
            age_rating = li.get_text(strip=True)

    synopses = soup.select("div.end_dsc div._synopsis")
    synopsis = _clean(synopses[-1].get_text("\n")) if synopses else ""

    total_el = soup.select_one("h5.end_total_episode")
    total_episodes = unit = None
    if total_el:
        m = re.search(r"총\s*([\d,]+)\s*(화|권)", total_el.get_text(" ", strip=True))
        if m:
            total_episodes = int(m.group(1).replace(",", ""))
            unit = m.group(2)

    free_el = soup.select_one("div.area_text_information")
    free_text = free_el.get_text(" ", strip=True).replace("가격정보", "") if free_el else None
    free = parse_free_text(free_text, total_episodes)

    meta = soup.find("meta", attrs={"name": "description"})
    og_image = soup.find("meta", attrs={"property": "og:image"})

    return {
        "product_no": product_no,
        "title": title,
        "authors": authors,
        "publisher": publisher,
        "genre": genre,
        "tags": parse_meta_tags(meta.get("content") if meta else None),
        "synopsis": synopsis,
        "status": status,
        "age_rating": age_rating,
        "unit": unit or "화",
        "total_episodes": total_episodes,
        "free_episodes": free.free_episodes,
        "is_all_free": free.is_all_free,
        "naver_rating": float(score_el.get_text(strip=True)) if score_el else None,
        "popularity": parse_korean_count(download_el.get_text(strip=True) if download_el else None),
        "thumbnail_url": thumbnail_from_og(og_image.get("content") if og_image else None),
    }


def extract_product_links(html: str, exclude: int | None = None) -> set[int]:
    """페이지 안의 모든 작품 링크(productNo). 상세 페이지의 '작가의 다른 만화',
    '이 장르의 인기 만화', 'TOP10' 같은 영역이 BFS 의 다음 후보가 된다."""
    found = {int(n) for n in PRODUCT_NO_RE.findall(html)}
    if exclude is not None:
        found.discard(exclude)
    return found


def extract_promos(html: str) -> dict[int, str]:
    """무료관(specialFreeList) 목록에서 '33화 무료 50일 남음' 같은 기간 한정 이벤트를 뽑는다."""
    soup = BeautifulSoup(html, "html.parser")
    promos: dict[int, str] = {}
    for li in soup.select("li"):
        a = li.find("a", href=PRODUCT_NO_RE)
        info = li.select_one("p.free_info")
        if not a or not info:
            continue
        label = re.sub(r"\s+", " ", info.get_text(" ", strip=True))
        if "남음" in label:
            promos[int(PRODUCT_NO_RE.search(a["href"]).group(1))] = label
    return promos
