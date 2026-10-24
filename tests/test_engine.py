from my_webtoon import db
from my_webtoon.recommender import Engine, ScoringOptions

ME = 1  # conftest 의 DB 를 만들 때 생기는 관리자 singun11


def titles(recs):
    return [r.comic.title for r in recs]


def test_no_ratings_means_no_recommendations(library):
    with db.session(library) as conn:
        assert Engine().recommend(conn, ME, ScoringOptions()) == []


def test_liking_murim_recommends_murim_first(library):
    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=10)  # 천마 회귀록 10점
        recs = Engine().recommend(conn, ME, ScoringOptions(sink_stingy=False))

    top3 = {r.comic.genre for r in recs[:3]}
    assert top3 == {"무협"}
    assert "천마 회귀록" not in titles(recs)  # 이미 평가한 건 빠진다
    assert "성인 무협" not in titles(recs)  # 성인 작품은 기본 제외


def test_recommendation_explains_itself(library):
    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=10)
        recs = Engine().recommend(conn, ME, ScoringOptions())
    top = recs[0]
    assert top.because is not None and top.because.title == "천마 회귀록"
    assert top.shared  # 겹치는 장르/태그/작가가 있다
    assert top.final == top.similarity * top.boost


def test_stingy_title_goes_to_bottom(library):
    """'무림 최강 막내'는 같은 작가라 매우 비슷하지만 무료 3화뿐 → 맨 아래."""
    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=10)
        sunk = Engine().recommend(conn, ME, ScoringOptions(sink_stingy=True))
        not_sunk = Engine().recommend(conn, ME, ScoringOptions(sink_stingy=False))
    assert titles(sunk)[-1] == "무림 최강 막내"
    assert titles(not_sunk).index("무림 최강 막내") < len(not_sunk) - 1


def test_dislike_pushes_similar_down(library):
    engine = Engine()
    with db.session(library) as conn:
        db.update_review(conn, ME, 5, score=10)  # 로맨스 좋아함 (10점)
        db.update_review(conn, ME, 1, score=2)  # 무협 싫어함 (2점)
        recs = engine.recommend(conn, ME, ScoringOptions(sink_stingy=False))
    assert recs[0].comic.title == "악녀의 정원"


def test_dismissed_titles_are_not_recommended(library):
    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=10)
        db.update_review(conn, ME, 2, status="dismissed")
        recs = Engine().recommend(conn, ME, ScoringOptions())
    assert "검황 회귀전" not in titles(recs)


def test_similar_to(library):
    with db.session(library) as conn:
        recs = Engine().similar_to(conn, 5, ScoringOptions(), ME)
    assert recs[0].comic.title == "악녀의 정원"


def test_cache_rebuilds_after_new_comic(library):
    from .conftest import comic_data

    engine = Engine()
    with db.session(library) as conn:
        db.update_review(conn, ME, 1, score=10)
        before = titles(engine.recommend(conn, ME, ScoringOptions()))
        db.upsert_comic(
            conn,
            comic_data(
                8,
                "천마 회귀 2부",
                genre="무협",
                tags=["회귀"],
                authors=["김무협"],
                synopsis="천마가 다시 회귀한다 무림",
                crawled_at="2099-01-01T00:00:00+00:00",
            ),
        )
        after = titles(engine.recommend(conn, ME, ScoringOptions()))
    assert "천마 회귀 2부" not in before and "천마 회귀 2부" in after


def test_status_only_review_is_enough_to_start(library):
    """점수 없이 '봤음'만 눌러도 추천이 시작된다."""
    with db.session(library) as conn:
        db.update_review(conn, ME, 5, status="seen")
        recs = Engine().recommend(conn, ME, ScoringOptions())
    assert recs and recs[0].comic.title == "악녀의 정원"


def test_aspects_only_review_uses_their_average(library):
    """종합 점수 없이 세부 점수만 있어도 평균을 종합 점수로 쓴다."""
    with db.session(library) as conn:
        review = db.update_review(conn, ME, 5, aspects={"story": 10, "art": 8})
        assert review.overall == 9
        recs = Engine().recommend(conn, ME, ScoringOptions())
    assert recs[0].comic.title == "악녀의 정원"


def test_low_score_beats_positive_status(library):
    """'봤음'이어도 1점이면 싫어한 것으로 본다 (점수가 상태보다 우선)."""
    from my_webtoon.recommender.profile import review_weight

    with db.session(library) as conn:
        review = db.update_review(conn, ME, 1, status="seen", score=1)
    assert review_weight(review) < 0


def test_recommendations_are_per_user(library):
    engine = Engine()
    with db.session(library) as conn:
        alice = db.create_user(conn, "alice", None)
        db.update_review(conn, ME, 1, score=10)  # 나는 무협
        db.update_review(conn, alice.id, 5, score=10)  # alice 는 로맨스
        mine = engine.recommend(conn, ME, ScoringOptions(sink_stingy=False))
        hers = engine.recommend(conn, alice.id, ScoringOptions(sink_stingy=False))
    assert mine[0].comic.genre == "무협"
    assert hers[0].comic.title == "악녀의 정원"
    assert "계약 결혼의 공작님" in titles(mine)  # 남이 평가한 건 내 후보에서 안 빠진다


def test_plan_batch_splits_refresh_and_discovery():
    from my_webtoon.crawler.runner import plan_batch

    assert plan_batch([1, 2, 3, 4], [10, 11, 12, 13], 4) == [1, 2, 10, 11]
    assert plan_batch([1, 2, 3, 4], [10], 4) == [1, 2, 10, 3]  # 발견이 모자라면 갱신이 채운다
    assert plan_batch([1], [10, 11, 12, 13], 4) == [1, 10, 11, 12]  # 갱신이 모자라면 발견이 채운다
    assert plan_batch([], [], 4) == []
