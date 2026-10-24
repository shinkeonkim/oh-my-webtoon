"""4단계: features → profile → scoring 을 엮어 실제 추천 목록을 만든다.

    recommend()   내 취향 벡터와 비슷한 작품 (메인 화면)
    similar_to()  특정 작품과 비슷한 작품 (상세 화면)
    popular()     평가가 하나도 없을 때(콜드 스타트) 보여줄 목록
    quick_queue() 빠른 평가 모드에서 물어볼 순서 ('봤을 것 같은' 작품부터)

작품 벡터는 만드는 데 1초 남짓 걸리므로 메모리에 캐시하고,
DB 의 작품 데이터가 바뀌었을 때(크롤링 후)만 다시 만든다.
"""

from __future__ import annotations

import json
import math
import secrets
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np

from .. import db
from ..db import Comic, Review
from . import scoring
from .features import DEFAULT_META_WEIGHT, DEFAULT_TEXT_WEIGHT, ItemFeatures, build_features
from .profile import ProfileOptions, build_profile, liked_rows, taste_similarities
from .scoring import ScoringOptions
from .works import work_keys

SNAPSHOT_TTL = timedelta(minutes=30)
DIVERSITY_WINDOW = 150  # 다양화(MMR)는 앞쪽 이만큼만 다시 줄 세운다 — 뒤쪽은 어차피 잘 안 본다


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
    def __init__(
        self,
        text_weight: float = DEFAULT_TEXT_WEIGHT,
        meta_weight: float = DEFAULT_META_WEIGHT,
        profile_opts: ProfileOptions | None = None,
    ) -> None:
        self.text_weight, self.meta_weight = text_weight, meta_weight
        self.profile_opts = profile_opts or ProfileOptions()
        self._lock = threading.Lock()
        self._signature: tuple | None = None
        self._features: ItemFeatures | None = None
        self._comics: dict[int, Comic] = {}
        self._work: dict[int, str] = {}  # product_no → 작품 키 (판본 묶기)

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
                self._work = work_keys(comics)
                self._features = (
                    build_features(comics, self.text_weight, self.meta_weight) if comics else None
                )
                self._signature = signature
            return self._features

    def work_of(self, product_no: int) -> str | None:
        return self._work.get(product_no)

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
        토큰이 없거나 만료됐으면(30분) 새로 계산한다. 스냅숏은 DB 에 있어서 어느 파드가 받아도 같다."""
        features = self._load(conn)
        ranked = self._get_snapshot(conn, snapshot, user_id)
        if ranked is None:
            ranked = self.rank_for_reviews(db.all_reviews(conn, user_id), opts)
            snapshot = self._put_snapshot(conn, user_id, ranked)
        page = ranked[offset : offset + limit]
        if features is not None:
            self._explain(features, db.all_reviews(conn, user_id), page)
        return page, snapshot

    def rank_for_reviews(
        self, reviews: dict[int, Review], opts: ScoringOptions
    ) -> list[Recommendation]:
        """평가 묶음 → 전체 추천 순위. DB 를 안 쓰는 순수 계산이라 평가 실험(evaluate.py)에서도 그대로 쓴다.
        (_load 로 작품 벡터가 준비돼 있어야 한다)"""
        features = self._features
        if features is None:
            return []
        # 모든 작품과 취향의 코사인 유사도 (평가한 작품은 그 평가를 뺀 취향과 — profile.taste_similarities)
        similarities = taste_similarities(features, reviews, self.profile_opts)
        if similarities is None:
            return []
        dismissed = {no for no, r in reviews.items() if r.status == "dismissed"}
        if opts.reviewed == "include":  # 본 것 포함: '관심 없음'만 뺀다
            return self._rank(features, similarities, opts, exclude=dismissed)
        if opts.reviewed == "only":  # 본 것만: 평가한 작품 중에서 순위
            return self._rank(
                features, similarities, opts, exclude=dismissed, only=set(reviews) - dismissed
            )
        # 기본 (안 본 것만): 평가한 작품과, 어느 판본이든 평가했으면 그 작품의 다른 판본도 빼기
        reviewed_works = {self._work[no] for no in reviews if no in self._work}
        return self._rank(
            features, similarities, opts, exclude=set(reviews), exclude_works=reviewed_works
        )

    def match_score(
        self, conn: sqlite3.Connection, user_id: int, product_no: int, opts: ScoringOptions
    ) -> dict | None:
        """작품 상세의 '추천도' (0~100). 이 작품의 최종 점수가 전체 작품 중 상위 몇 %인지를 뒤집은 값.

            추천도 = 최종 점수가 이 작품보다 낮은 작품 비율 × 100   (100 = 1등, 50 = 딱 중간)

        유사도 자체(0.1~0.5 정도)는 크기가 직관적이지 않아서 백분위로 바꿨다. 평가한 작품이면 그 평가를 빼고
        계산한다 — "내 다른 평가만으로도 추천됐을까". 취향이 아직 없으면 None.
        """
        features = self._load(conn)
        if features is None or product_no not in features.index:
            return None
        reviews = db.all_reviews(conn, user_id)
        sims = taste_similarities(features, reviews, self.profile_opts)
        if sims is None:
            return None
        comics = [self._comics[no] for no in features.product_nos]
        keep = np.array([opts.include_adult or not scoring.is_adult(c) for c in comics])
        boosts = np.array([scoring.boost(c, opts) for c in comics])
        finals = sims * boosts
        row = features.index[product_no]
        pool_finals, pool_sims = finals[keep], sims[keep]
        n = max(1, len(pool_finals) - 1)
        return {
            # 내림: 100 은 진짜 1등만 (7위가 반올림으로 100 이 되지 않게)
            "percent": math.floor(100 * float((pool_finals < finals[row]).sum()) / n),
            "taste_percent": math.floor(100 * float((pool_sims < sims[row]).sum()) / n),
            "rank": int((pool_finals > finals[row]).sum()) + 1,
            "total": len(pool_finals),
            "similarity": float(sims[row]),
            "boost": float(boosts[row]),
            "stingy": scoring.is_stingy(self._comics[product_no], opts),
            "left_out": product_no in reviews,
        }

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
        return self._one_per_work(candidates)[:limit]

    def quick_queue(self, conn: sqlite3.Connection, user_id: int, limit: int = 20) -> list[Comic]:
        """빠른 평가에서 물어볼 작품 순서. 목표는 '봤을 법한' 작품을 먼저 보여서 평가를 빨리 모으는 것.

            우선순위 = log(1 + 다운로드 수) × (1 + 2 × 취향 유사도)

        다운로드가 많을수록 봤을 확률이 높고, 취향과 비슷할수록 봤거나 볼 작품일 확률이 높다.
        이미 평가했거나 '안 봤음'으로 넘긴 작품(판본 포함)은 빼고, 성인 작품도 뺀다.
        """
        features = self._load(conn)
        if features is None:
            return []
        reviews = db.all_reviews(conn, user_id)
        skipped = db.quick_skips(conn, user_id)
        done_works = {self._work[no] for no in (*reviews, *skipped) if no in self._work}
        profile = build_profile(features, reviews, self.profile_opts)
        sims = (
            np.asarray(features.matrix @ profile).ravel()
            if profile is not None
            else np.zeros(len(features.product_nos))
        )
        scored = []
        for row, no in enumerate(features.product_nos):
            comic = self._comics[no]
            if self._work[no] in done_works or scoring.is_adult(comic):
                continue
            scored.append(
                (math.log1p(comic.popularity or 0) * (1 + 2 * max(0.0, sims[row])), comic)
            )
        scored.sort(key=lambda sc: -sc[0])
        return self._one_per_work([c for _, c in scored])[:limit]

    # -- 스냅숏 (무한 스크롤) -----------------------------------------------------

    def _put_snapshot(
        self, conn: sqlite3.Connection, user_id: int, ranked: list[Recommendation]
    ) -> str:
        token = secrets.token_urlsafe(8)
        items = [
            [r.comic.product_no, r.similarity, r.boost, r.final, r.stingy, r.variants]
            for r in ranked
        ]
        now = datetime.now(UTC)
        conn.execute(
            "DELETE FROM rec_snapshots WHERE created_at < ?",
            ((now - SNAPSHOT_TTL).isoformat(timespec="seconds"),),
        )
        conn.execute(
            "INSERT INTO rec_snapshots (token, user_id, created_at, items) VALUES (?, ?, ?, ?)",
            (token, user_id, now.isoformat(timespec="seconds"), json.dumps(items)),
        )
        conn.commit()
        return token

    def _get_snapshot(
        self, conn: sqlite3.Connection, token: str | None, user_id: int
    ) -> list[Recommendation] | None:
        if not token:
            return None
        row = conn.execute(
            "SELECT user_id, created_at, items FROM rec_snapshots WHERE token = ?", (token,)
        ).fetchone()
        if row is None or row["user_id"] != user_id:
            return None
        if datetime.now(UTC) - datetime.fromisoformat(row["created_at"]) > SNAPSHOT_TTL:
            return None
        return [
            Recommendation(
                comic=self._comics[no],
                similarity=sim,
                boost=b,
                final=final,
                stingy=stingy,
                variants=variants,
            )  # fmt: skip
            for no, sim, b, final, stingy, variants in json.loads(row["items"])
            if no in self._comics  # 그사이 지워진 작품(제외 장르 등)은 건너뛴다
        ]

    # -- 내부 ----------------------------------------------------------------

    def _one_per_work(self, comics: list[Comic]) -> list[Comic]:
        """판본 묶기: 작품마다 가장 앞의 판본 하나."""
        seen: set[str] = set()
        result = []
        for c in comics:
            if self._work[c.product_no] not in seen:
                seen.add(self._work[c.product_no])
                result.append(c)
        return result

    def _rank(
        self,
        features: ItemFeatures,
        similarities: np.ndarray,
        opts: ScoringOptions,
        exclude: set[int],
        exclude_works: set[str] = frozenset(),
        only: set[int] | None = None,
    ) -> list[Recommendation]:
        recs = []
        for row, sim in enumerate(similarities):
            if sim <= 0 and only is None:  # '본 것만'에선 취향과 반대인 작품도 순위에 둔다
                continue
            comic = self._comics[features.product_nos[row]]
            if only is not None and comic.product_no not in only:
                continue
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
        if opts.diversity > 0 and only is None:  # '본 것만'은 내 평가 목록이라 섞을 필요가 없다
            unique = self._diversify(features, unique, opts)
        return unique

    def _diversify(
        self, features: ItemFeatures, recs: list[Recommendation], opts: ScoringOptions
    ) -> list[Recommendation]:
        """MMR (Maximal Marginal Relevance) 다시 줄 세우기.

        그냥 점수순이면 좋아한 작품 하나와 닮은 작품(같은 작가·같은 시리즈)이 위를 다 채운다.
        그래서 한 장씩 고를 때마다 '이미 고른 것들과 얼마나 겹치는지'를 벌점으로 뺀다:

            다음 = argmax  (1-d) × 점수/최고점수  -  d × max(이미 고른 작품과의 유사도)

        d = opts.diversity (0 이면 점수순 그대로). '유료 위주' 묶음은 섞지 않고 원래대로 맨 뒤에 둔다.
        """
        head = [r for r in recs[:DIVERSITY_WINDOW] if not (r.stingy and opts.sink_stingy)]
        if len(head) < 3:
            return recs
        head_ids = {id(r) for r in head}
        rows = [features.index[r.comic.product_no] for r in head]
        sim = np.asarray((features.matrix[rows] @ features.matrix[rows].T).todense())
        relevance = np.array([r.final for r in head])
        relevance = relevance / relevance.max()
        d = opts.diversity
        chosen: list[int] = []
        max_overlap = np.zeros(len(head))
        remaining = np.ones(len(head), dtype=bool)
        for _ in range(len(head)):
            gain = np.where(remaining, (1 - d) * relevance - d * max_overlap, -np.inf)
            pick = int(np.argmax(gain))
            chosen.append(pick)
            remaining[pick] = False
            max_overlap = np.maximum(max_overlap, sim[pick])
        return [head[i] for i in chosen] + [r for r in recs if id(r) not in head_ids]

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
            if rec.comic.product_no not in features.index:
                continue
            # '본 것 포함'에선 후보가 내가 좋아한 작품 자신일 수 있다 → 같은 작품(판본 포함)은 '닮은 작품'에서 뺀다
            own = self._work[rec.comic.product_no]
            others = [i for i, no in enumerate(liked_nos) if self._work[no] != own]
            if not others:
                continue
            sims = np.asarray(
                (liked_matrix[others] @ features.row(rec.comic.product_no).T).todense()
            ).ravel()
            best = int(np.argmax(sims))
            if sims[best] > 0:
                rec.because = self._comics[liked_nos[others[best]]]
            rec.shared = self._shared_tokens(
                features, rec.comic.product_no, [liked_nos[i] for i in others]
            )

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
