"""BFS 의 출발점(시드) 페이지들.

robots.txt 가 허용하는 페이지 중 작품 링크가 많이 모여 있는 곳:
  - 코믹스 홈
  - TOP100 (시간/일간/주간/월간 × 전체·장르별 × 5페이지)
  - 매일 10시 무료관 (기간 한정 무료 이벤트 정보도 같이 얻는다)

여기서 얻은 작품 → 상세 페이지 → 그 안의 연관 작품 링크 → ... 순으로 넓혀 간다.
"""

from __future__ import annotations

RANKING_TYPES = ["DAILY", "WEEKLY", "MONTHLY"]
# 전체 + 네이버 시리즈 코믹스 장르 코드 (BL, 무협, 소년, 드라마, 순정)
CATEGORY_CODES = ["ALL", "107", "88", "90", "93", "99"]
TOP100_PAGES = 5
FREE_LIST_PAGES = 10


def seed_paths() -> list[str]:
    paths = ["/comic/home.series"]
    for ranking in RANKING_TYPES:
        for category in CATEGORY_CODES:
            for page in range(1, TOP100_PAGES + 1):
                paths.append(
                    f"/comic/top100List.series?rankingTypeCode={ranking}"
                    f"&categoryCode={category}&page={page}"
                )
    paths.extend(free_list_paths())
    return paths


def free_list_paths() -> list[str]:
    return [f"/comic/specialFreeList.series?page={p}" for p in range(1, FREE_LIST_PAGES + 1)]
