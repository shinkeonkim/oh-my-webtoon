from my_webtoon.db import Comic
from my_webtoon.recommender import scoring
from my_webtoon.recommender.scoring import ScoringOptions

from .conftest import comic_data


def make(**overrides) -> Comic:
    data = comic_data(1, "작품", **overrides)
    return Comic(**data, crawled_at="2026-01-01T00:00:00+00:00")


def test_more_free_episodes_boost_more():
    opts = ScoringOptions()
    few, many, all_free = make(free_episodes=3), make(free_episodes=40), make(is_all_free=True)
    assert scoring.boost(few, opts) < scoring.boost(many, opts) < scoring.boost(all_free, opts)


def test_more_total_episodes_boost_more():
    opts = ScoringOptions()
    assert scoring.boost(make(total_episodes=20), opts) < scoring.boost(
        make(total_episodes=400), opts
    )


def test_log_scale_has_diminishing_returns():
    opts = ScoringOptions()
    gain_low = scoring.free_score(make(free_episodes=30), opts) - scoring.free_score(
        make(free_episodes=3), opts
    )
    gain_high = scoring.free_score(make(free_episodes=50), opts) - scoring.free_score(
        make(free_episodes=40), opts
    )
    assert gain_low > gain_high * 5


def test_stingy_detection():
    opts = ScoringOptions(stingy_threshold=3)
    assert scoring.is_stingy(make(free_episodes=3), opts)
    assert scoring.is_stingy(make(free_episodes=0), opts)
    assert not scoring.is_stingy(make(free_episodes=4), opts)
    assert not scoring.is_stingy(make(is_all_free=True, free_episodes=0), opts)
    # 단행본 1권 무료 ≈ 8화 → 유료 위주가 아니다
    assert not scoring.is_stingy(make(unit="권", free_episodes=1), opts)


def test_stingy_sinks_below_everything_when_enabled():
    on, off = ScoringOptions(sink_stingy=True), ScoringOptions(sink_stingy=False)
    strong_but_stingy = scoring.sort_key(0.9, True, on)
    weak_but_free = scoring.sort_key(0.1, False, on)
    assert weak_but_free < strong_but_stingy
    assert scoring.sort_key(0.9, True, off) < scoring.sort_key(0.1, False, off)


def test_adult_detection():
    assert scoring.is_adult(make(age_rating="청소년 이용불가"))
    assert scoring.is_adult(make(age_rating="19세 이용가"))
    assert not scoring.is_adult(make(age_rating="15세 이용가"))
