"""4단계: features → profile → scoring 을 엮어 실제 추천 목록을 만든다.

    recommend()   내 취향 벡터와 비슷한 작품 (메인 화면)
    similar_to()  특정 작품과 비슷한 작품 (상세 화면)
    popular()     평가가 하나도 없을 때(콜드 스타트) 보여줄 목록

작품 벡터는 만드는 데 1초 남짓 걸리므로 메모리에 캐시하고,
DB 의 작품 데이터가 바뀌었을 때(크롤링 후)만 다시 만든다.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from dataclasses import dataclass, field

import numpy as np

from .. import db
from ..db import Comic, Review
from . import scoring
from .features import ItemFeatures, build_features
from .profile import build_profile, liked_rows
from .scoring import ScoringOptions


@dataclass
class Recommendation:
    comic: Comic
    similarity: float  # 취향(또는 기준 작품)과의 코사인 유사도
    boost: float  # 무료/회차/평점 보정 배수
    final: float  # similarity × boost
    stingy: bool  # 무료 3화 이하 '유료 위주' 작품인지
    because: Comic | None = None  # 가장 닮은 '내가 좋아한' 작품
    shared: list[str] = field(default_factory=list)  # 겹치는 장르/태그/작가


class Engine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._signature: tuple | None = None
        self._features: ItemFeatures | None = None
        self._comics: dict[int, Comic] = {}

    def invalidate(self) -> None:
        with self._lock:
            self._signature = None

    def _load(self, conn: sqlite3.Connection) -> ItemFeatures | None:
        signature = db.comics_signature(conn)
        with self._lock:
            if signature != self._signature:
                comics = db.all_comics(conn)
                self._comics = {c.product_no: c for c in comics}
                self._features = build_features(comics) if comics else None
                self._signature = signature
            return self._features

    # -- 공개 API ------------------------------------------------------------

    def recommend(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        opts: ScoringOptions,
        limit: int = 30,
        offset: int = 0,
    ) -> list[Recommendation]:
        """user_id 의 평가로 만든 취향에 맞는 작품. 작품 벡터는 모든 사용자가 공유한다."""
        features = self._load(conn)
        if features is None:
            return []
        reviews = db.all_reviews(conn, user_id)
        profile = build_profile(features, reviews)
        if profile is None:
            return []

        similarities = features.matrix @ profile  # 모든 작품과 취향의 코사인 유사도 (한 번에)
        recs = self._rank(features, similarities, opts, exclude=set(reviews))
        page = recs[offset : offset + limit]
        self._explain(features, reviews, page)
        return page

    def similar_to(
        self,
        conn: sqlite3.Connection,
        product_no: int,
        opts: ScoringOptions,
        user_id: int | None = None,
        limit: int = 12,
    ) -> list[Recommendation]:
        features = self._load(conn)
        if features is None or product_no not in features.index:
            return []
        target = features.row(product_no)
        similarities = np.asarray((features.matrix @ target.T).todense()).ravel()
        reviews = db.all_reviews(conn, user_id)
        exclude = {product_no} | {no for no, r in reviews.items() if r.status == "dismissed"}
        recs = self._rank(features, similarities, opts, exclude=exclude)[:limit]
        for rec in recs:
            rec.shared = self._shared_tokens(features, rec.comic.product_no, [product_no])
        return recs

    def popular(
        self,
        conn: sqlite3.Connection,
        opts: ScoringOptions,
        user_id: int | None = None,
        limit: int = 30,
    ) -> list[Comic]:
        """콜드 스타트: 많이 본 작품 중 무료 많은 순. 여기서 '봤던 것'을 골라 평가하면 된다."""
        self._load(conn)
        reviewed = set(db.all_reviews(conn, user_id))
        candidates = [
            c
            for c in self._comics.values()
            if c.product_no not in reviewed and (opts.include_adult or not scoring.is_adult(c))
        ]
        candidates.sort(key=lambda c: -math.log1p(c.popularity or 0) * scoring.boost(c, opts))
        return candidates[:limit]

    # -- 내부 ----------------------------------------------------------------

    def _rank(
        self,
        features: ItemFeatures,
        similarities: np.ndarray,
        opts: ScoringOptions,
        exclude: set[int],
    ) -> list[Recommendation]:
        recs = []
        for row, sim in enumerate(similarities):
            if sim <= 0:
                continue
            comic = self._comics[features.product_nos[row]]
            if comic.product_no in exclude:
                continue
            if not opts.include_adult and scoring.is_adult(comic):
                continue
            b = scoring.boost(comic, opts)
            recs.append(
                Recommendation(
                    comic=comic,
                    similarity=float(sim),
                    boost=b,
                    final=float(sim) * b,
                    stingy=scoring.is_stingy(comic, opts),
                )
            )
        recs.sort(key=lambda r: scoring.sort_key(r.final, r.stingy, opts))
        return recs

    def _explain(
        self, features: ItemFeatures, reviews: dict[int, Review], recs: list[Recommendation]
    ) -> None:
        """'왜 추천됐는지' 채우기: 가장 닮은 좋아한 작품 + 겹치는 태그."""
        liked = liked_rows(features, reviews)
        if not liked:
            return
        liked_idx = [row for row, _ in liked]
        liked_matrix = features.matrix[liked_idx]
        liked_nos = [features.product_nos[row] for row in liked_idx]
        for rec in recs:
            sims = np.asarray(
                (liked_matrix @ features.row(rec.comic.product_no).T).todense()
            ).ravel()
            best = int(np.argmax(sims))
            if sims[best] > 0:
                rec.because = self._comics[liked_nos[best]]
            rec.shared = self._shared_tokens(features, rec.comic.product_no, liked_nos)

    def _shared_tokens(
        self, features: ItemFeatures, product_no: int, others: list[int], top: int = 4
    ) -> list[str]:
        meta = features.meta_matrix
        if meta.shape[1] == 0:
            return []
        mine = meta[features.index[product_no]]
        theirs = meta[[features.index[no] for no in others]].sum(axis=0)
        theirs = np.asarray(theirs).ravel()
        scored = [(mine[0, col] * theirs[col], col) for col in mine.indices if theirs[col] > 0]
        scored.sort(reverse=True)
        return [str(features.meta_vocab[col]) for _, col in scored[:top]]
