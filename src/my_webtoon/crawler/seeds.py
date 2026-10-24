"""BFS 의 출발점(시드) 페이지들.

robots.txt 가 허용하는 페이지 중 작품 링크가 많이 모여 있는 곳:
  - 코믹스 홈
  - TOP100 (시간/일간/주간/월간 × 전체·장르별 × 5페이지)
  - 매일 10시 무료관 (기간 한정 무료 이벤트 정보도 같이 얻는다)

여기서 얻은 작품 → 상세 페이지 → 그 안의 연관 작품 링크 → ... 순으로 넓혀 간다.
"""

from __future__ import annotations

RANKING_TYPES = ["DAILY", "WEEKLY", "MONTHLY"]
# 네이버 시리즈 코믹스 장르 코드 (TOP100 의 categoryCode)
GENRE_CODES = {"BL": "107", "무협": "88", "소년": "90", "드라마": "93", "순정": "99"}
TOP100_PAGES = 5
FREE_LIST_PAGES = 10


def seed_paths(excluded_genres: frozenset[str] = frozenset()) -> list[str]:
    """제외 장르의 장르별 TOP100 은 아예 열지 않는다 (전체 TOP100 에 섞여 나오는 건 상세에서 거른다)."""
    categories = ["ALL"] + [code for g, code in GENRE_CODES.items() if g not in excluded_genres]
    paths = ["/comic/home.series"]
    for ranking in RANKING_TYPES:
        for category in categories:
            for page in range(1, TOP100_PAGES + 1):
                paths.append(
                    f"/comic/top100List.series?rankingTypeCode={ranking}"
                    f"&categoryCode={category}&page={page}"
                )
    paths.extend(free_list_paths())
    return paths


def free_list_paths() -> list[str]:
    return [f"/comic/specialFreeList.series?page={p}" for p in range(1, FREE_LIST_PAGES + 1)]
