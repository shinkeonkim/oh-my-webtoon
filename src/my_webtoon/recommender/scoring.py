"""3단계: 취향 유사도에 "볼 수 있는 양" 보정을 곱한다.

네이버 시리즈 작품은 대부분 앞의 몇 화만 무료고 나머지는 쿠키(유료)다.
취향에 딱 맞아도 3화 보고 결제해야 하면 아쉬우니, 이렇게 점수를 정한다:

    최종점수 = 유사도 × (1 + free_weight × 무료점수
                           + episode_weight × 회차점수
                           + rating_weight × 평점점수)

  무료점수  = log(1 + 무료화수) / log(1 + free_saturation)   (0~1, '전화 무료'면 1)
  회차점수  = log(1 + 총화수)   / log(1 + episode_saturation) (0~1)
  평점점수  = (네이버평점 - 8.5) / 1.5                         (0~1, 네이버 평점은 대부분 9점대라 아래를 잘라낸다)

로그를 쓰는 이유: 3화 → 30화 무료는 큰 차이지만 300화 → 330화는 거의 같다(수확 체감).

그리고 "무료가 3화 이하"인 작품(= 사실상 유료작)은 sink_stingy 가 켜져 있으면
점수와 상관없이 목록 맨 아래로 보낸다. 유사도가 아무리 높아도 무료 많은 작품보다 뒤에 온다.

단행본('권' 단위)은 한 권이 웹툰 여러 화 분량이라 VOLUME_TO_EPISODES 배로 환산한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..db import Comic

VOLUME_TO_EPISODES = 8


@dataclass(frozen=True)
class ScoringOptions:
    free_weight: float = 1.0
    episode_weight: float = 0.5
    rating_weight: float = 0.2
    stingy_threshold: int = 3  # 무료 회차가 이 이하이면 '유료 위주'
    sink_stingy: bool = True  # '유료 위주' 작품을 맨 아래로
    free_saturation: int = 50  # 무료 50화면 무료점수 만점
    episode_saturation: int = 300  # 총 300화면 회차점수 만점
    include_adult: bool = False


def _episodes(comic: Comic, n: int | None) -> float:
    n = n or 0
    return n * VOLUME_TO_EPISODES if comic.unit == "권" else n


def free_score(comic: Comic, opts: ScoringOptions) -> float:
    if comic.is_all_free:
        return 1.0
    free = _episodes(comic, comic.free_episodes)
    return min(1.0, math.log1p(free) / math.log1p(opts.free_saturation))


def episode_score(comic: Comic, opts: ScoringOptions) -> float:
    total = _episodes(comic, comic.total_episodes)
    return min(1.0, math.log1p(total) / math.log1p(opts.episode_saturation))


def rating_score(comic: Comic) -> float:
    if comic.naver_rating is None:
        return 0.0
    return min(1.0, max(0.0, (comic.naver_rating - 8.5) / 1.5))


def boost(comic: Comic, opts: ScoringOptions) -> float:
    return (
        1.0
        + opts.free_weight * free_score(comic, opts)
        + opts.episode_weight * episode_score(comic, opts)
        + opts.rating_weight * rating_score(comic)
    )


def is_stingy(comic: Comic, opts: ScoringOptions) -> bool:
    """사실상 유료작인가? (무료 회차가 threshold 이하, 단 '전화 무료'는 제외)

    단행본은 권 단위 그대로 비교하지 않고 화 단위로 환산한 값으로 본다 — 1권 무료면 8화쯤.
    """
    if comic.is_all_free:
        return False
    return _episodes(comic, comic.free_episodes) <= opts.stingy_threshold


def is_adult(comic: Comic) -> bool:
    return bool(comic.age_rating) and ("19" in comic.age_rating or "불가" in comic.age_rating)


def sort_key(final_score: float, stingy: bool, opts: ScoringOptions) -> tuple:
    """정렬 키 — 오름차순 정렬 기준. (유료위주 여부, -점수)"""
    return (stingy and opts.sink_stingy, -final_score)
