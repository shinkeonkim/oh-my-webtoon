"""'내 취향' 요약 — 평가 기록을 숫자로 정리한다 (/users/{name}/taste).

추천 계산과는 따로 논다: 여기 숫자는 사람이 읽으라고 만든 것이고, 추천은 recommender/ 가 한다.
단 '취향 키워드'만은 추천기가 실제로 쓰는 취향 벡터의 메타 부분(장르·태그·작가)에서 꺼낸다 —
"추천기가 나를 어떻게 보고 있는지"를 그대로 보여 주려고.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np

from . import db
from .db import Comic, Review
from .recommender.profile import base_weight, build_profile


@dataclass
class Group:
    name: str
    count: int = 0
    scores: list[float] = field(default_factory=list)

    @property
    def avg(self) -> float | None:
        return statistics.fmean(self.scores) if self.scores else None


@dataclass
class Taste:
    total: int
    scored: int
    avg_score: float | None
    histogram: list[int]  # 0~10점 각각 몇 개 (세부 평균은 반올림)
    statuses: dict[str, int]
    genres: list[Group]
    authors: list[Group]  # 2편 이상 평가한 작가
    tags: list[Group]  # 좋게 평가한 작품에 많이 붙은 태그
    aspects: list[Group]  # 세부 항목별 평균
    keywords: list[tuple[str, float]]  # 추천기의 취향 벡터에서 무게가 큰 메타 토큰
    episodes_read: int  # 기록한 '본 화수' 합


def summarize(items: list[tuple[Review, Comic]], engine=None, conn=None) -> Taste:
    scored = [r.overall for r, _ in items if r.overall is not None]
    histogram = [0] * 11
    for s in scored:
        histogram[round(s)] += 1

    genres: dict[str, Group] = {}
    authors: dict[str, Group] = {}
    tags: dict[str, Group] = defaultdict(lambda: Group(""))
    for review, comic in items:
        for bucket, keys in ((genres, [comic.genre or "기타"]), (authors, comic.authors[:2])):
            for key in keys:
                g = bucket.setdefault(key, Group(key))
                g.count += 1
                if review.overall is not None:
                    g.scores.append(review.overall)
        if base_weight(review) > 0:  # 좋게 본 작품의 태그만 센다
            for t in comic.tags:
                if t != comic.genre:
                    g = tags[t]
                    g.name = t
                    g.count += 1
                    if review.overall is not None:
                        g.scores.append(review.overall)

    aspect_groups = []
    for key, label in db.ASPECTS.items():
        values = [r.aspects[key] for r, _ in items if key in r.aspects]
        if values:
            aspect_groups.append(Group(label, len(values), values))

    return Taste(
        total=len(items),
        scored=len(scored),
        avg_score=statistics.fmean(scored) if scored else None,
        histogram=histogram,
        statuses=dict(Counter(r.status for r, _ in items if r.status)),
        genres=sorted(genres.values(), key=lambda g: (-g.count, -(g.avg or 0))),
        authors=sorted(
            (g for g in authors.values() if g.count >= 2),
            key=lambda g: (-(g.avg or 0), -g.count),
        )[:10],
        tags=sorted(tags.values(), key=lambda g: -g.count)[:15],
        aspects=sorted(aspect_groups, key=lambda g: -(g.avg or 0)),
        keywords=_keywords(engine, conn, {r.product_no: r for r, _ in items}),
        episodes_read=sum(r.episodes_read or 0 for r, _ in items),
    )


def _keywords(engine, conn, reviews: dict[int, Review], top: int = 12) -> list[tuple[str, float]]:
    """취향 벡터에서 메타 토큰(장르·#태그·작가) 부분만 꺼내 무게 순으로. 음수(싫어하는 쪽)는 뺀다."""
    if engine is None or conn is None:
        return []
    features = engine._load(conn)
    if features is None or features.meta_matrix.shape[1] == 0:
        return []
    profile = build_profile(features, reviews, engine.profile_opts)
    if profile is None:
        return []
    meta = profile[-features.meta_matrix.shape[1] :]  # 결합 벡터의 뒤쪽이 메타 블록 (features.py)
    order = np.argsort(-meta)[:top]
    peak = meta[order[0]] if len(order) and meta[order[0]] > 0 else 1.0
    return [(str(features.meta_vocab[i]), float(meta[i] / peak)) for i in order if meta[i] > 0]
