from my_webtoon import db
from my_webtoon.recommender import Engine, ScoringOptions


def titles(recs):
    return [r.comic.title for r in recs]


def test_no_ratings_means_no_recommendations(library):
    with db.session(library) as conn:
        assert Engine().recommend(conn, ScoringOptions()) == []


def test_liking_murim_recommends_murim_first(library):
    with db.session(library) as conn:
        db.set_rating(conn, 1, "rated", 5)  # 천마 회귀록 ★5
        recs = Engine().recommend(conn, ScoringOptions(sink_stingy=False))

    top3 = {r.comic.genre for r in recs[:3]}
    assert top3 == {"무협"}
    assert "천마 회귀록" not in titles(recs)  # 이미 평가한 건 빠진다
    assert "성인 무협" not in titles(recs)  # 성인 작품은 기본 제외


def test_recommendation_explains_itself(library):
    with db.session(library) as conn:
        db.set_rating(conn, 1, "rated", 5)
        recs = Engine().recommend(conn, ScoringOptions())
    top = recs[0]
    assert top.because is not None and top.because.title == "천마 회귀록"
    assert top.shared  # 겹치는 장르/태그/작가가 있다
    assert top.final == top.similarity * top.boost


def test_stingy_title_goes_to_bottom(library):
    """'무림 최강 막내'는 같은 작가라 매우 비슷하지만 무료 3화뿐 → 맨 아래."""
    with db.session(library) as conn:
        db.set_rating(conn, 1, "rated", 5)
        sunk = Engine().recommend(conn, ScoringOptions(sink_stingy=True))
        not_sunk = Engine().recommend(conn, ScoringOptions(sink_stingy=False))
    assert titles(sunk)[-1] == "무림 최강 막내"
    assert titles(not_sunk).index("무림 최강 막내") < len(not_sunk) - 1


def test_dislike_pushes_similar_down(library):
    engine = Engine()
    with db.session(library) as conn:
        db.set_rating(conn, 5, "rated", 5)  # 로맨스 좋아함
        db.set_rating(conn, 1, "rated", 1)  # 무협 싫어함
        recs = engine.recommend(conn, ScoringOptions(sink_stingy=False))
    assert recs[0].comic.title == "악녀의 정원"


def test_dismissed_titles_are_not_recommended(library):
    with db.session(library) as conn:
        db.set_rating(conn, 1, "rated", 5)
        db.set_rating(conn, 2, "dismissed")
        recs = Engine().recommend(conn, ScoringOptions())
    assert "검황 회귀전" not in titles(recs)


def test_similar_to(library):
    with db.session(library) as conn:
        recs = Engine().similar_to(conn, 5, ScoringOptions())
    assert recs[0].comic.title == "악녀의 정원"


def test_cache_rebuilds_after_new_comic(library):
    from .conftest import comic_data

    engine = Engine()
    with db.session(library) as conn:
        db.set_rating(conn, 1, "rated", 5)
        before = titles(engine.recommend(conn, ScoringOptions()))
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
        after = titles(engine.recommend(conn, ScoringOptions()))
    assert "천마 회귀 2부" not in before and "천마 회귀 2부" in after
