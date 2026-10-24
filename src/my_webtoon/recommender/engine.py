"""4단계: features → profile → scoring 을 엮어 실제 추천 목록을 만든다.

    recommend()   내 취향 벡터와 비슷한 작품 (메인 화면)
    similar_to()  특정 작품과 비슷한 작품 (상세 화면)
    popular()     평가가 하나도 없을 때(콜드 스타트) 보여줄 목록

작품 벡터는 만드는 데 1초 남짓 걸리므로 메모리에 캐시하고,
DB 의 작품 데이터가 바뀌었을 때(크롤링 후)만 다시 만든다.
"""

from __future__ import annotations

import math
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np

from .. import db
from ..db import Comic, Review
from . import scoring
from .features import ItemFeatures, build_features
from .profile import build_profile, liked_rows
from .scoring import ScoringOptions
from .works import work_key

SNAPSHOT_TTL_SECONDS = 30 * 60
SNAPSHOT_MAX = 64


@dataclass
class Recommendation:
    comic: Comic
    similarity: float  # 취향(또는 기준 작품)과의 코사인 유사도
    boost: float  # 무료/회차/평점 보정 배수
    final: float  # similarity × boost
    stingy: bool  # 무료 3화 이하 '유료 위주' 작품인지
    because: Comic | None = None  # 가장 닮은 '내가 좋아한' 작품
    shared: list[str] = field(default_factory=list)  # 겹치는 장르/태그/작가
    variants: int = 0  # 같은 작품의 다른 판본 수 (works.py) — 이 판본만 대표로 보여 준다


class Engine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._signature: tuple | None = None
        self._features: ItemFeatures | None = None
        self._comics: dict[int, Comic] = {}
        self._work: dict[int, str] = {}  # product_no → 작품 키 (판본 묶기)
        # 무한 스크롤용 순위 스냅숏: 첫 페이지에서 전체 순위를 한 번 계산해 두고, 다음 페이지는 거기서 자른다.
        # 스크롤하는 중에 평가를 남기거나 크롤러가 작품을 더해도 순위가 밀려 같은 작품이 두 번 나오지 않는다.
        self._snapshots: OrderedDict[str, tuple[int, float, list[Recommendation]]] = OrderedDict()

    def invalidate(self) -> None:
        with self._lock:
            self._signature = None

    def warm_up(self, conn: sqlite3.Connection) -> None:
        """작품 벡터를 미리 만들어 둔다 (첫 요청이 느리지 않게). 파드가 뜰 때 /readyz 전에 부른다."""
        self._load(conn)

    def _load(self, conn: sqlite3.Connection) -> ItemFeatures | None:
        signature = db.comics_signature(conn)
        with self._lock:
            if signature != self._signature:
                comics = db.all_comics(conn)
                self._comics = {c.product_no: c for c in comics}
                self._work = {c.product_no: work_key(c.title) for c in comics}
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
        page, _ = self.recommend_page(conn, user_id, opts, offset, limit)
        return page

    def recommend_page(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        opts: ScoringOptions,
        offset: int,
        limit: int,
        snapshot: str | None = None,
    ) -> tuple[list[Recommendation], str]:
        """(이 페이지 추천, 스냅숏 토큰). 다음 페이지를 부를 때 토큰을 넘기면 같은 순위에서 이어서 자른다.
        토큰이 없거나 만료됐으면(30분, 또는 다른 파드) 새로 계산한다."""
        ranked = self._get_snapshot(snapshot, user_id)
        if ranked is None:
            ranked = self._rank_for_user(conn, user_id, opts)
            snapshot = self._put_snapshot(user_id, ranked)
        page = ranked[offset : offset + limit]
        features = self._load(conn)
        if features is not None:
            self._explain(features, db.all_reviews(conn, user_id), page)
        return page, snapshot

    def _rank_for_user(
        self, conn: sqlite3.Connection, user_id: int, opts: ScoringOptions
    ) -> list[Recommendation]:
        features = self._load(conn)
        if features is None:
            return []
        reviews = db.all_reviews(conn, user_id)
        profile = build_profile(features, reviews)
        if profile is None:
            return []
        similarities = features.matrix @ profile  # 모든 작품과 취향의 코사인 유사도 (한 번에)
        # 어느 판본이든 평가했으면 그 작품의 다른 판본도 빼기
        reviewed_works = {self._work[no] for no in reviews if no in self._work}
        return self._rank(
            features, similarities, opts, exclude=set(reviews), exclude_works=reviewed_works
        )

    def _put_snapshot(self, user_id: int, ranked: list[Recommendation]) -> str:
        token = secrets.token_urlsafe(8)
        with self._lock:
            self._snapshots[token] = (user_id, time.monotonic(), ranked)
            while len(self._snapshots) > SNAPSHOT_MAX:
                self._snapshots.popitem(last=False)
        return token

    def _get_snapshot(self, token: str | None, user_id: int) -> list[Recommendation] | None:
        if not token:
            return None
        with self._lock:
            entry = self._snapshots.get(token)
        if entry is None:
            return None
        owner, created, ranked = entry
        if owner != user_id or time.monotonic() - created > SNAPSHOT_TTL_SECONDS:
            return None
        return ranked

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
        # 같은 작품의 다른 판본('나루토 [단행본]')이 '비슷한 작품' 1위로 나오지 않게
        recs = self._rank(
            features, similarities, opts, exclude=exclude, exclude_works={self._work[product_no]}
        )[:limit]
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
        reviewed_works = {self._work.get(no) for no in db.all_reviews(conn, user_id)}
        candidates = [
            c
            for c in self._comics.values()
            if self._work[c.product_no] not in reviewed_works
            and (opts.include_adult or not scoring.is_adult(c))
        ]
        candidates.sort(key=lambda c: -math.log1p(c.popularity or 0) * scoring.boost(c, opts))
        seen_works: set[str] = set()
        result = []
        for c in candidates:  # 판본 묶기: 작품마다 가장 위의 판본 하나
            if self._work[c.product_no] not in seen_works:
                seen_works.add(self._work[c.product_no])
                result.append(c)
        return result[:limit]

    # -- 내부 ----------------------------------------------------------------

    def _rank(
        self,
        features: ItemFeatures,
        similarities: np.ndarray,
        opts: ScoringOptions,
        exclude: set[int],
        exclude_works: set[str] = frozenset(),
    ) -> list[Recommendation]:
        recs = []
        for row, sim in enumerate(similarities):
            if sim <= 0:
                continue
            comic = self._comics[features.product_nos[row]]
            if comic.product_no in exclude or self._work[comic.product_no] in exclude_works:
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
        # 판본 묶기: 같은 작품은 가장 앞선(점수 높은) 판본 하나만 남기고, 나머지 수를 적어 둔다
        first: dict[str, Recommendation] = {}
        unique = []
        for rec in recs:
            key = self._work[rec.comic.product_no]
            if key in first:
                first[key].variants += 1
            else:
                first[key] = rec
                unique.append(rec)
        return unique

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
