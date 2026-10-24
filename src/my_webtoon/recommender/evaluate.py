"""추천 품질 측정 — "가중치를 감이 아니라 숫자로 고르기" 위한 잣대.

방법: leave-one-out (하나 빼고 맞히기)
    내가 좋게 평가한 작품 h 를 하나씩 숨긴다 (같은 작품의 다른 판본 평가도 같이 숨긴다 — 안 그러면 답을 보고 푸는 셈).
    나머지 평가로 취향을 만들고 전체 추천 순위를 계산한 뒤, h 가 몇 위에 나오는지 본다.
    좋아한 작품을 상위에 올릴수록 좋은 추천기다.

지표
    HR@10, HR@30   숨긴 작품이 10위/30위 안에 든 비율 (Hit Rate). 첫 화면 30개 안에 드는지가 체감에 가깝다.
    MRR            1/순위 의 평균 (1위면 1, 10위면 0.1). 순위가 조금만 올라도 반영된다.
    중앙 순위       순위의 중앙값. 작을수록 좋다.
    다양성          전체 평가로 만든 실제 추천 상위 30개 안의 서로 다른 작가 수, 평균 쌍 유사도(ILS, 낮을수록 다양).

두 가지 모드
    taste  유사도만으로 순위 (무료·회차 보정, 유료 위주 내리기 끔) — "취향을 얼마나 잘 맞히나"
    final  실제 화면과 같은 점수 — 보정 때문에 순위가 내려가는 건 일부러 그런 것이니 참고용

평가가 수십 개뿐이면 숫자가 크게 흔들린다. 설정 몇 개를 비교해 '확실히 나쁜 것'을 거르는 용도로 쓴다.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, replace

import numpy as np

from .. import db
from .engine import Engine
from .profile import ProfileOptions, base_weight
from .scoring import ScoringOptions

TASTE = ScoringOptions(
    free_weight=0, episode_weight=0, rating_weight=0, sink_stingy=False, include_adult=True
)


@dataclass
class EvalResult:
    label: str
    cases: int
    hr10: float
    hr30: float
    mrr: float
    median_rank: float
    authors_top30: int = 0
    ils_top30: float = 0.0

    def row(self) -> str:
        return (
            f"{self.label:<34} n={self.cases:<3} HR@10 {self.hr10:5.1%}  HR@30 {self.hr30:5.1%}  "
            f"MRR {self.mrr:.3f}  중앙순위 {self.median_rank:6.0f}  "
            f"작가수 {self.authors_top30:2d}  ILS {self.ils_top30:.3f}"
        )


def leave_one_out(
    engine: Engine, conn: sqlite3.Connection, user_id: int, opts: ScoringOptions, label: str
) -> EvalResult:
    features = engine._load(conn)
    reviews = db.all_reviews(conn, user_id)
    positives = [no for no, r in reviews.items() if base_weight(r) > 0 and no in features.index]
    ranks = []
    for held in positives:
        work = engine.work_of(held)
        rest = {no: r for no, r in reviews.items() if engine.work_of(no) != work}
        ranked = engine.rank_for_reviews(rest, opts)
        position = next(
            (i + 1 for i, rec in enumerate(ranked) if engine.work_of(rec.comic.product_no) == work),
            len(ranked) + 1,  # 순위에 아예 없으면 (유사도 0 등) 맨 끝
        )
        ranks.append(position)

    authors, ils = _diversity(engine, features, engine.rank_for_reviews(reviews, opts)[:30])
    n = len(ranks) or 1
    return EvalResult(
        label=label,
        cases=len(ranks),
        hr10=sum(r <= 10 for r in ranks) / n,
        hr30=sum(r <= 30 for r in ranks) / n,
        mrr=sum(1 / r for r in ranks) / n,
        median_rank=statistics.median(ranks) if ranks else 0,
        authors_top30=authors,
        ils_top30=ils,
    )


def _diversity(engine: Engine, features, recs) -> tuple[int, float]:
    if len(recs) < 2:
        return len(recs), 0.0
    authors = {a for r in recs for a in r.comic.authors[:1]}
    rows = [features.index[r.comic.product_no] for r in recs]
    sim = np.asarray((features.matrix[rows] @ features.matrix[rows].T).todense())
    k = len(rows)
    return len(authors), float((sim.sum() - np.trace(sim)) / (k * (k - 1)))


def experiments() -> list[tuple[str, dict, ProfileOptions, ScoringOptions]]:
    """비교할 설정들: (이름, 작품 벡터 가중치, 프로필 옵션, 점수 옵션). 맨 위가 예전 설정, 맨 아래가 지금 기본값."""
    flat = ProfileOptions(like_power=1.0, negative_scale=1.0, half_life_days=0)
    new = ProfileOptions()
    old_w = {"text_weight": 0.5, "meta_weight": 0.5}
    final = ScoringOptions()
    return [
        ("[취향만] 예전 (텍스트.5 평탄)", old_w, flat, replace(TASTE, diversity=0)),
        ("[취향만] 지금 기본값", {}, new, TASTE),
        ("예전 설정", old_w, flat, replace(final, diversity=0)),
        ("텍스트.3 메타.7", {"text_weight": 0.3, "meta_weight": 0.7}, new, final),
        ("텍스트.5 메타.5", old_w, new, final),
        ("평탄 프로필 (고득점 강조·부정 절반 끔)", {}, flat, final),
        ("다양화 끔", {}, new, replace(final, diversity=0)),
        ("다양화 0.5", {}, new, replace(final, diversity=0.5)),
        ("평점 보정 0.5", {}, new, replace(final, rating_weight=0.5)),
        ("무료 보정 0.5", {}, new, replace(final, free_weight=0.5)),
        ("무료 보정 1.5", {}, new, replace(final, free_weight=1.5)),
        ("지금 기본값", {}, new, final),
    ]


def run_all(conn: sqlite3.Connection, user_id: int) -> list[EvalResult]:
    results = []
    engines: dict[tuple, Engine] = {}
    for label, weights, profile_opts, opts in experiments():
        key = tuple(sorted(weights.items()))
        base = engines.get(key)
        engine = Engine(**weights, profile_opts=profile_opts)
        if base is not None:  # 같은 작품 벡터는 다시 만들지 않는다 (몇 초씩 걸림)
            engine._signature, engine._features = base._signature, base._features
            engine._comics, engine._work = base._comics, base._work
        else:
            engines[key] = engine
        results.append(leave_one_out(engine, conn, user_id, opts, label))
    return results
