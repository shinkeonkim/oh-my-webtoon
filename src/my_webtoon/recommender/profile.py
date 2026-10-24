"""2단계: 내 평가 → "취향 벡터" (사용자 프로필).

내가 평가한 작품들의 벡터를 가중합한다 (Rocchio 방식).

    profile = Σ  weight(평가) × 작품벡터

  ★5 → +2.5   ★4 → +1.5   ★3 → +0.5   ★2 → -0.5   ★1 → -1.5
  봤음(별점 없음) → +0.5      관심 없음 → -1.0

★3 을 0 이 아니라 +0.5 로 둔 이유: 끝까지 봤다는 것 자체가 약한 긍정 신호라서.
싫어한 작품은 음수라서, 그 작품과 닮은 작품은 점수가 깎인다.

이후 추천 점수 = cos(profile, 후보 작품 벡터).
"""

from __future__ import annotations

import numpy as np

from ..db import Rating
from .features import ItemFeatures

SCORE_WEIGHTS = {1: -1.5, 2: -0.5, 3: 0.5, 4: 1.5, 5: 2.5}
SEEN_WEIGHT = 0.5
DISMISSED_WEIGHT = -1.0


def rating_weight(rating: Rating) -> float:
    if rating.kind == "rated" and rating.score:
        return SCORE_WEIGHTS[rating.score]
    if rating.kind == "seen":
        return SEEN_WEIGHT
    if rating.kind == "dismissed":
        return DISMISSED_WEIGHT
    return 0.0


def build_profile(features: ItemFeatures, ratings: dict[int, Rating]) -> np.ndarray | None:
    """취향 벡터 (길이 1로 정규화). 긍정 신호가 하나도 없으면 None — 아직 추천할 근거가 없다."""
    rows, weights = [], []
    for product_no, rating in ratings.items():
        if product_no in features.index:
            rows.append(features.index[product_no])
            weights.append(rating_weight(rating))

    if not any(w > 0 for w in weights):
        return None

    # (특성수, 평가수) @ (평가수,) → (특성수,): 가중합을 행렬곱 한 번으로.
    profile = np.asarray(features.matrix[rows].T @ np.array(weights)).ravel()
    norm = np.linalg.norm(profile)
    return profile / norm if norm > 0 else None


def liked_rows(features: ItemFeatures, ratings: dict[int, Rating]) -> list[tuple[int, float]]:
    """긍정적으로 평가한 작품들 (행 번호, 가중치). '이 작품과 비슷해요' 설명에 쓴다."""
    return [
        (features.index[no], rating_weight(r))
        for no, r in ratings.items()
        if no in features.index and rating_weight(r) > 0
    ]
