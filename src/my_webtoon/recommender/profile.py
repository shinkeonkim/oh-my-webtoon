"""2단계: 내 평가 → "취향 벡터" (사용자 프로필).

내가 평가한 작품들의 벡터를 가중합한다 (Rocchio 방식).

    profile = Σ  weight(평가) × 작품벡터

가중치는 "종합 점수"가 있으면 점수로, 없으면 상태로 정한다.

  종합 점수 (0~10) → (점수 - 4) × 0.4
      10점 +2.4   8점 +1.6   6점 +0.8   5점 +0.4   4점 0   2점 -0.8   0점 -1.6
      종합 점수를 안 줬어도 세부 점수(스토리·작화…)가 있으면 그 평균을 종합 점수로 쓴다.

  점수가 전혀 없을 때 상태로:
      봤음 +0.5   보는 중 +0.5   보고 싶음 +0.8   하차 -0.8   관심 없음 -1.0

4점을 0으로 둔 이유: 10점 척도에서 5점은 "그럭저럭 볼 만했다"는 약한 긍정이라서.
'보고 싶음'이 '봤음'보다 큰 이유: 아직 안 본 작품을 일부러 찍어 둔 건 취향을 꽤 직접 드러내니까.
점수와 상태를 둘 다 남겼다면 점수가 이긴다 (예: 하차했지만 6점 → +0.8).

그 위에 세 가지 조정 (ProfileOptions, 기본값은 `my-webtoon eval` 로 비교해 골랐다 — docs/03):
  like_power     긍정 가중치를 거듭제곱한다. 1.5 면 10점(2.4→3.7)이 7점(1.2→1.3)보다 훨씬 세다.
                 "인생작"과 "볼 만했다"가 같은 간격으로 취향을 끌지 않게.
  negative_scale 부정 가중치에 곱한다. 0.5 면 하차·낮은 점수가 반만 끌어내린다 — 하차는 "이 작품이
                 별로"일 뿐 그 장르 전체가 싫다는 뜻은 아닐 때가 많다.
  half_life_days 오래된 평가일수록 약하게: 이 일수마다 반으로 (단 recency_floor 아래로는 안 내려감).
                 취향은 바뀌니까. 0 이면 끈다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from ..db import Review
from .features import ItemFeatures

STATUS_WEIGHTS = {
    "seen": 0.5,
    "watching": 0.5,
    "wish": 0.8,
    "dropped": -0.8,
    "dismissed": -1.0,
}
SCORE_PIVOT = 4  # 이 점수가 가중치 0
SCORE_SLOPE = 0.4


@dataclass(frozen=True)
class ProfileOptions:
    like_power: float = 1.5
    negative_scale: float = 0.5
    half_life_days: float = 365
    recency_floor: float = 0.5


def base_weight(review: Review) -> float:
    """점수(또는 상태)만으로 정한 가중치 — 위 표 그대로."""
    overall = review.overall
    if overall is not None:
        return (overall - SCORE_PIVOT) * SCORE_SLOPE
    return STATUS_WEIGHTS.get(review.status or "", 0.0)


def review_weight(
    review: Review, opts: ProfileOptions | None = None, now: datetime | None = None
) -> float:
    opts = opts or ProfileOptions()
    w = base_weight(review)
    w = w**opts.like_power if w > 0 else w * opts.negative_scale
    if opts.half_life_days and review.updated_at:
        age_days = ((now or datetime.now(UTC)) - datetime.fromisoformat(review.updated_at)).days
        w *= max(opts.recency_floor, 0.5 ** (max(0, age_days) / opts.half_life_days))
    return w


def build_profile(
    features: ItemFeatures, reviews: dict[int, Review], opts: ProfileOptions | None = None
) -> np.ndarray | None:
    """취향 벡터 (길이 1로 정규화). 긍정 신호가 하나도 없으면 None — 아직 추천할 근거가 없다."""
    rows, weights = [], []
    now = datetime.now(UTC)
    for product_no, review in reviews.items():
        if product_no in features.index:
            rows.append(features.index[product_no])
            weights.append(review_weight(review, opts, now))

    if not any(w > 0 for w in weights):
        return None

    # (특성수, 평가수) @ (평가수,) → (특성수,): 가중합을 행렬곱 한 번으로.
    profile = np.asarray(features.matrix[rows].T @ np.array(weights)).ravel()
    norm = np.linalg.norm(profile)
    return profile / norm if norm > 0 else None


def liked_rows(features: ItemFeatures, reviews: dict[int, Review]) -> list[tuple[int, float]]:
    """긍정적으로 평가한 작품들 (행 번호, 가중치). '닮은 작품' 설명에 쓴다."""
    return [
        (features.index[no], w)
        for no, r in reviews.items()
        if no in features.index and (w := base_weight(r)) > 0
    ]


def taste_similarities(
    features: ItemFeatures, reviews: dict[int, Review], opts: ProfileOptions | None = None
) -> np.ndarray | None:
    """모든 작품과 취향 벡터의 코사인 유사도. 단 **내가 평가한 작품은 그 평가를 뺀 취향**과 비교한다.

    그냥 계산하면 10점 준 작품은 자기 자신이 취향에 들어가 있어서 유사도가 당연히 높다(답을 보고 푸는 셈).
    '본 것 포함' 추천이나 작품 상세의 추천도에서는 "이 평가가 없었어도 추천됐을까"가 궁금하니 자기 몫을 뺀다.

    다시 계산하지 않고 식으로 뺀다 (작품 벡터 x 는 길이 1):
        P   = Σ w·x               (정규화 전 취향)
        P'  = P - w_i·x_i          (i 를 뺀 취향)
        x_i·P' = x_i·P - w_i
        |P'|²  = |P|² - 2·w_i·(x_i·P) + w_i²
    """
    rows, weights = [], []
    now = datetime.now(UTC)
    for product_no, review in reviews.items():
        if product_no in features.index:
            rows.append(features.index[product_no])
            weights.append(review_weight(review, opts, now))
    if not any(w > 0 for w in weights):
        return None

    raw = np.asarray(features.matrix[rows].T @ np.array(weights)).ravel()
    norm = np.linalg.norm(raw)
    if norm == 0:
        return None
    dots = np.asarray(features.matrix @ raw).ravel()  # 모든 작품 x·P
    sims = dots / norm
    for row, w in zip(rows, weights, strict=True):
        rest = norm**2 - 2 * w * dots[row] + w**2
        sims[row] = (dots[row] - w) / np.sqrt(rest) if rest > 1e-9 else 0.0
    return sims
