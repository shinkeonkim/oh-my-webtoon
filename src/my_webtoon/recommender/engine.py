"""4단계: features → profile → scoring 을 엮어 실제 추천 목록을 만든다.

    recommend()   내 취향 벡터와 비슷한 작품 (메인 화면)
    similar_to()  특정 작품과 비슷한 작품 (상세 화면)
    popular()     평가가 하나도 없을 때(콜드 스타트) 보여줄 목록
    quick_queue() 빠른 평가 모드에서 물어볼 순서 ('봤을 것 같은' 작품부터)

작품 벡터는 만드는 데 1초 남짓 걸리므로 메모리에 캐시하고,
DB 의 작품 데이터가 바뀌었을 때(크롤링 후)만 다시 만든다.

다시 만드는 건 요청을 세우지 않고 뒤에서 한다 — 그동안은 이전 벡터로 답한다.
크롤러는 작품을 하나 저장할 때마다 crawled_at 을 바꾸므로, 요청마다 다시 만들면
수집 중(3시간마다 10분쯤) 모든 화면이 1초 이상(운영에선 4초까지) 걸렸다.
그래서 수집 중에는 REFRESH_INTERVAL 에 한 번만 다시 만들고, 수집이 끝나면(invalidate) 바로 만든다.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
import sqlite3
import threading
import time
from collections import Counter
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
EXPLORE_EVERY = 4  # 빠른 평가에서 이 장마다 한 번은 덜 평가한 장르의 작품 (quick_next)
EXPLORE_GENRES = 5
ONBOARDING_MIN_GENRE = 5  # 온보딩에서 돌아가며 물어볼 장르: 남은 작품이 이만큼은 있는 장르만  # 그 '덜 평가한 장르'를 몇 개까지 돌아가며 고를지
REFRESH_INTERVAL = 600  # 초. invalidate 없이 작품 데이터만 바뀌었을 때 다시 만드는 최소 간격

log = logging.getLogger(__name__)


@dataclass
class _Scores:
    """작품별 보정값을 옵션 하나에 대해 한 번에 계산해 둔 배열 (행 = features 행 순서)."""

    boost: np.ndarray
    stingy: np.ndarray  # bool
    adult: np.ndarray  # bool
    hidden: np.ndarray  # bool — 화면에 안 내는 단행본 판본 (Comic.is_volume). 취향 계산엔 쓰고 후보·비교 대상에선 뺀다


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


def _percentile(pool: np.ndarray, value: float) -> int:
    """pool 중 value 보다 낮은 비율 × 100, 내림 — 100 은 진짜 1등만 (7위가 반올림으로 100 이 되지 않게)."""
    return math.floor(100 * float((pool < value).sum()) / max(1, len(pool) - 1))


class Engine:
    def __init__(
        self,
        text_weight: float = DEFAULT_TEXT_WEIGHT,
        meta_weight: float = DEFAULT_META_WEIGHT,
        profile_opts: ProfileOptions | None = None,
        refresh_interval: float | None = None,
    ) -> None:
        """refresh_interval: None 이면 작품 데이터가 바뀐 걸 본 요청이 바로(기다려서) 다시 만든다 — 테스트·CLI.
        숫자면 뒤에서 다시 만들고 그동안 이전 벡터로 답한다 — 크롤러가 같은 프로세스에서 도는 웹 서버."""
        self.text_weight, self.meta_weight = text_weight, meta_weight
        self.profile_opts = profile_opts or ProfileOptions()
        self.refresh_interval = refresh_interval
        self._lock = threading.Lock()
        self._signature: tuple | None = None
        self._features: ItemFeatures | None = None
        self._comics: dict[int, Comic] = {}
        self._work: dict[int, str] = {}  # product_no → 작품 키 (판본 묶기)
        self._scores: dict[ScoringOptions, _Scores] = {}
        self._built_at = 0.0  # time.monotonic()
        self._stale = False  # invalidate() 됨 — 간격과 상관없이 다음 요청에서 다시 만든다
        self._refreshing = False

    def invalidate(self) -> None:
        with self._lock:
            self._stale = True
            if self.refresh_interval is None:
                self._signature = None

    def warm_up(self, conn: sqlite3.Connection) -> None:
        """작품 벡터를 미리 만들어 둔다 (첫 요청이 느리지 않게). 파드가 뜰 때 /readyz 전에 부른다."""
        self._load(conn)

    def _load(self, conn: sqlite3.Connection) -> ItemFeatures | None:
        signature = db.comics_signature(conn)
        with self._lock:
            if signature == self._signature:
                return self._features
            # 처음 한 번은 기다려서 만든다 — 돌려줄 이전 벡터가 없다
            if self._signature is None or self.refresh_interval is None:
                self._build(db.all_comics(conn), signature)
                return self._features
            due = self._stale or time.monotonic() - self._built_at >= self.refresh_interval
            if not due or self._refreshing:
                return self._features
            self._refreshing, self._stale = True, False
        # 작품 읽기는 요청의 연결로(빠르다), 벡터 만들기는 뒤에서. 그동안 이 요청과 다른 요청은 이전 벡터를 쓴다.
        comics = db.all_comics(conn)
        threading.Thread(
            target=self._refresh, args=(comics, signature), name="features", daemon=True
        ).start()
        return self._features

    def _refresh(self, comics: list[Comic], signature: tuple) -> None:
        try:
            built = self._compute(comics)  # lock 밖에서 — 만드는 동안에도 요청은 이전 벡터로 답한다
            with self._lock:
                self._swap(built, signature)
        except Exception:
            log.exception("작품 벡터 다시 만들기 실패 — 이전 벡터를 계속 쓴다")
        finally:
            self._refreshing = False

    def _build(self, comics: list[Comic], signature: tuple) -> None:
        self._swap(self._compute(comics), signature)

    def _compute(self, comics: list[Comic]) -> tuple:
        features = build_features(comics, self.text_weight, self.meta_weight) if comics else None
        return features, {c.product_no: c for c in comics}, work_keys(comics)

    def _swap(self, built: tuple, signature: tuple) -> None:
        """(lock 안에서) 작품 벡터와 그에 딸린 캐시를 한 번에 바꾼다."""
        self._features, self._comics, self._work = built
        self._scores = {}
        self._signature = signature
        self._built_at = time.monotonic()

    def _scores_for(self, features: ItemFeatures, opts: ScoringOptions) -> _Scores:
        """작품별 보정·유료위주·성인 여부 — 옵션마다 한 번만 계산한다 (요청마다 작품 수천 개를 돌던 부분)."""
        cache = self._scores
        scores = cache.get(opts) if features is self._features else None
        if scores is None:
            comics = [self._comics[no] for no in features.product_nos]
            scores = _Scores(
                boost=np.array([scoring.boost(c, opts) for c in comics]),
                stingy=np.array([scoring.is_stingy(c, opts) for c in comics], dtype=bool),
                adult=np.array([scoring.is_adult(c) for c in comics], dtype=bool),
                hidden=np.array([c.is_volume for c in comics], dtype=bool),
            )
            if features is self._features:
                cache[opts] = scores
        return scores

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

        유사도 자체(0.1~0.5 정도)는 크기가 직관적이지 않아서 백분위로 바꿨다. 취향이 아직 없으면 None.

        평가한 작품이면 **그 평가까지 넣은 취향**으로 계산한다 — 10점을 주면 올라가고 2점을 주면 내려가야
        평점과 어긋나 보이지 않는다. 예전에는 그 평가를 빼고 계산해서("평가가 없었어도 추천됐을까")
        평점을 바꿔도 이 숫자가 그대로였다. 그 값은 predicted 로 따로 준다.
        """
        features = self._load(conn)
        if features is None or product_no not in features.index:
            return None
        reviews = db.all_reviews(conn, user_id)
        sims = taste_similarities(features, reviews, self.profile_opts, leave_one_out=False)
        if sims is None:
            return None
        scores = self._scores_for(features, opts)
        keep = ~scores.hidden if opts.include_adult else ~(scores.adult | scores.hidden)
        boosts = scores.boost
        finals = sims * boosts
        row = features.index[product_no]
        pool_finals, pool_sims = finals[keep], sims[keep]
        predicted = None
        if product_no in reviews:  # 이 평가를 빼고 계산했다면 — "평가 없이도 추천됐을까"
            loo = taste_similarities(features, reviews, self.profile_opts) * boosts
            predicted = _percentile(loo[keep], loo[row])
        return {
            "percent": _percentile(pool_finals, finals[row]),
            "taste_percent": _percentile(pool_sims, sims[row]),
            "rank": int((pool_finals > finals[row]).sum()) + 1,
            "total": len(pool_finals),
            "similarity": float(sims[row]),
            "boost": float(boosts[row]),
            "stingy": scoring.is_stingy(self._comics[product_no], opts),
            "rated": product_no in reviews,
            "predicted": predicted,
        }

    def match_percents(
        self, conn: sqlite3.Connection, user_id: int, opts: ScoringOptions
    ) -> dict[int, int]:
        """목록 카드용: 모든 작품의 추천도(match_score 의 percent)를 한 번에. 취향이 아직 없으면 빈 dict.

        작품마다 match_score 를 부르면 수천 번 비교해야 하니, 비교 대상 점수를 한 번 정렬해 두고
        searchsorted 로 '이보다 낮은 작품 수'를 센다 — 값은 match_score 와 같다.
        """
        features = self._load(conn)
        if features is None:
            return {}
        reviews = db.all_reviews(conn, user_id)
        sims = taste_similarities(features, reviews, self.profile_opts, leave_one_out=False)
        if sims is None:
            return {}
        scores = self._scores_for(features, opts)
        finals = sims * scores.boost
        pool = np.sort(
            finals[~scores.hidden if opts.include_adult else ~(scores.adult | scores.hidden)]
        )
        n = max(1, len(pool) - 1)
        below = np.searchsorted(pool, finals, side="left")
        return {
            no: math.floor(100 * float(count) / n)
            for no, count in zip(features.product_nos, below.tolist(), strict=True)
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
        offset: int = 0,
    ) -> list[Comic]:
        """콜드 스타트: 많이 본 작품 중 무료 많은 순. 여기서 '봤던 것'을 골라 평가하면 된다."""
        self._load(conn)
        reviewed_works = {self._work.get(no) for no in db.all_reviews(conn, user_id)}
        candidates = [
            c
            for c in self._comics.values()
            if self._work[c.product_no] not in reviewed_works
            and not c.is_volume
            and (opts.include_adult or not scoring.is_adult(c))
        ]
        candidates.sort(key=lambda c: -math.log1p(c.popularity or 0) * scoring.boost(c, opts))
        return self._one_per_work(candidates)[offset : offset + limit]

    def quick_queue(self, conn: sqlite3.Connection, user_id: int, limit: int = 20) -> list[Comic]:
        """빠른 평가에서 물어볼 작품 순서. 목표는 '봤을 법한' 작품을 먼저 보여서 평가를 빨리 모으는 것.

            우선순위 = log(1 + 다운로드 수) × (1 + 2 × 취향 유사도)

        다운로드가 많을수록 봤을 확률이 높고, 취향과 비슷할수록 봤거나 볼 작품일 확률이 높다.
        이미 평가했거나 '보류'로 넘긴 작품(판본 포함)은 빼고, 성인 작품도 뺀다.
        """
        return self._quick_candidates(conn, user_id)[:limit]

    def quick_next(
        self, conn: sqlite3.Connection, user_id: int, done: int
    ) -> tuple[Comic | None, bool]:
        """빠른 평가의 다음 한 장 (작품, 탐색 카드인지). done = 이번에 넘긴 장 수.

        quick_queue 순서만 따르면 평가할수록 취향 쪽으로만 좁아진다 — 무협을 몇 개 평가하면 계속 무협.
        그래서 EXPLORE_EVERY 장마다 한 번은 '내가 가장 덜 평가한 장르' 중 하나에서 가장 많이 본 작품을 낸다.
        장르는 덜 평가한 순으로 EXPLORE_GENRES 개를 돌아가며 고른다 (매번 같은 장르만 나오지 않게).
        """
        candidates = self._quick_candidates(conn, user_id)
        if not candidates:
            return None, False
        if (done + 1) % EXPLORE_EVERY == 0:
            pick = self._explore_pick(candidates, db.all_reviews(conn, user_id), done)
            if pick is not None:
                return pick, True
        return candidates[0], False

    def onboarding_next(self, conn: sqlite3.Connection, user_id: int) -> Comic | None:
        """처음 들어온 사용자(평가가 적을 때)의 빠른 평가 다음 장 — 대략적인 취향을 빨리 잡는 게 목표.

        아직 취향이 없으니 유사도로 고를 수 없다. 대신 **장르를 돌아가며 그 장르에서 가장 많이 본 작품**을 낸다:
        지금까지 답한(평가·보류) 수가 가장 적은 장르 → 그 장르의 다운로드 1위. 많이 본 작품이라 '봤음'일 확률이
        높고, 장르를 고르게 훑어서 첫 10개만으로도 좋아하는 장르·싫어하는 장르가 갈린다.
        작품이 몇 개 없는 장르(ONBOARDING_MIN_GENRE 미만)는 대표작이라 하기 어려워 뺀다.
        """
        candidates = self._quick_candidates(conn, user_id)
        if not candidates:
            return None
        answered = Counter(
            self._comics[no].genre
            for no in (*db.all_reviews(conn, user_id), *db.quick_skips(conn, user_id))
            if no in self._comics
        )
        size = Counter(c.genre for c in candidates if c.genre)
        weight = Counter()
        for c in candidates:
            if c.genre:
                weight[c.genre] += c.popularity or 0
        genres = [g for g, n in size.items() if n >= ONBOARDING_MIN_GENRE] or list(size)
        if not genres:
            return max(candidates, key=lambda c: c.popularity or 0)
        genre = min(
            genres, key=lambda g: (answered[g], -weight[g])
        )  # 덜 물어본 장르, 같으면 큰 장르부터
        return max((c for c in candidates if c.genre == genre), key=lambda c: c.popularity or 0)

    def _quick_candidates(self, conn: sqlite3.Connection, user_id: int) -> list[Comic]:
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
            if self._work[no] in done_works or comic.is_volume or scoring.is_adult(comic):
                continue
            scored.append(
                (math.log1p(comic.popularity or 0) * (1 + 2 * max(0.0, sims[row])), comic)
            )
        scored.sort(key=lambda sc: -sc[0])
        return self._one_per_work([c for _, c in scored])

    def _explore_pick(
        self, candidates: list[Comic], reviews: dict[int, Review], done: int
    ) -> Comic | None:
        mine = Counter(self._comics[no].genre for no in reviews if no in self._comics)
        available = Counter(c.genre for c in candidates if c.genre)
        # 남은 작품이 몇 개는 있는 장르 중, 내 평가가 적은 순 (같으면 작품이 많은 장르부터)
        genres = sorted(
            (g for g, n in available.items() if n >= 3), key=lambda g: (mine[g], -available[g])
        )[:EXPLORE_GENRES]
        if not genres:
            return None
        genre = genres[(done // EXPLORE_EVERY) % len(genres)]
        return max((c for c in candidates if c.genre == genre), key=lambda c: c.popularity or 0)

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
        # 거르기·점수·정렬은 배열로 한 번에 — 작품 수천 개를 파이썬으로 돌면 요청마다 수십~수백 ms 였다
        scores = self._scores_for(features, opts)
        finals = similarities * scores.boost
        keep = ~scores.hidden
        if only is None:  # '본 것만'에선 취향과 반대인 작품도 순위에 둔다
            keep &= similarities > 0
        if not opts.include_adult:
            keep &= ~scores.adult
        sunk = scores.stingy & opts.sink_stingy
        # scoring.sort_key 와 같은 순서: (유료위주 여부, -점수). lexsort 는 안정 정렬이라 동점은 행 순서 그대로
        order = np.lexsort((-finals, sunk))
        recs = []
        for row in order[keep[order]]:
            no = features.product_nos[row]
            if only is not None and no not in only:
                continue
            if no in exclude or self._work[no] in exclude_works:
                continue
            recs.append(
                Recommendation(
                    comic=self._comics[no],
                    similarity=float(similarities[row]),
                    boost=float(scores.boost[row]),
                    final=float(finals[row]),
                    stingy=bool(scores.stingy[row]),
                )
            )
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
        liked_nos = [features.product_nos[row] for row in liked_idx]
        liked_works = np.array([self._work[no] for no in liked_nos], dtype=object)
        page = [rec for rec in recs if rec.comic.product_no in features.index]
        if not page:
            return
        # 좋아한 작품 × 이 페이지 작품 유사도를 한 번에 (카드마다 희소 행렬을 잘라 곱하던 걸 묶었다)
        page_rows = [features.index[rec.comic.product_no] for rec in page]
        sims_all = (features.matrix[liked_idx] @ features.matrix[page_rows].T).toarray()
        meta = features.meta_matrix
        liked_meta = meta[liked_idx] if meta.shape[1] else None
        theirs_all = np.asarray(liked_meta.sum(axis=0)).ravel() if liked_meta is not None else None
        for j, rec in enumerate(page):
            # '본 것 포함'에선 후보가 내가 좋아한 작품 자신일 수 있다 → 같은 작품(판본 포함)은 '닮은 작품'에서 뺀다
            others = liked_works != self._work[rec.comic.product_no]
            if not others.any():
                continue
            sims = np.where(others, sims_all[:, j], -np.inf)
            best = int(np.argmax(sims))
            if sims[best] > 0:
                rec.because = self._comics[liked_nos[best]]
            if liked_meta is not None:
                theirs = (
                    theirs_all
                    if others.all()
                    else np.asarray(liked_meta[others].sum(axis=0)).ravel()
                )
                rec.shared = self._top_shared(features, page_rows[j], theirs)

    def _shared_tokens(
        self, features: ItemFeatures, product_no: int, others: list[int], top: int = 4
    ) -> list[str]:
        meta = features.meta_matrix
        if meta.shape[1] == 0:
            return []
        theirs = np.asarray(meta[[features.index[no] for no in others]].sum(axis=0)).ravel()
        return self._top_shared(features, features.index[product_no], theirs, top)

    def _top_shared(
        self, features: ItemFeatures, row: int, theirs: np.ndarray, top: int = 4
    ) -> list[str]:
        """row 작품의 메타 토큰 중 theirs(다른 작품들의 메타 합)와 겹치는 것, 무거운 순으로."""
        mine = features.meta_matrix[row]
        scored = [(mine[0, col] * theirs[col], col) for col in mine.indices if theirs[col] > 0]
        scored.sort(reverse=True)
        return [str(features.meta_vocab[col]) for _, col in scored[:top]]
