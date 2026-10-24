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


def test_work_key_groups_editions():
    from my_webtoon.recommender.works import work_key

    assert work_key("나루토 [단행본]") == work_key("나루토")
    assert work_key("붉은매2 [단행본]") == work_key("붉은 매2")
    assert work_key("천하제일인[독점]") == work_key("천하제일인")
    assert work_key("나 혼자만 레벨업") != work_key("나 혼자 만렙 뉴비")


def test_same_title_different_authors_are_different_works():
    from my_webtoon.recommender.works import work_keys

    from .conftest import comic_data

    def comic(no, title, authors):
        return db.Comic(**{**comic_data(no, title, authors=authors), "crawled_at": ""})

    keys = work_keys([
        comic(1, "킹덤", ["Yasuhisa HARA"]),
        comic(2, "킹덤 [단행본]", ["HARA Yasuhisa"]),  # 성·이름 순서, 대소문자만 다름 → 같은 작품
        comic(3, "킹덤", ["국내작가"]),  # 제목만 같은 다른 작품
    ])  # fmt: skip
    assert keys[1] == keys[2] != keys[3]


def test_editions_are_grouped_and_reviewed_work_is_excluded(library):
    from .conftest import comic_data

    with db.session(library) as conn:
        db.upsert_comic(conn, comic_data(60, "검황 회귀전 [단행본]", genre="무협", tags=["회귀"], authors=["이검객"],
                                         synopsis="검황이 회귀하여 무림에 복수한다", unit="권"))  # fmt: skip
        db.upsert_comic(conn, comic_data(61, "천마 회귀록 [단행본]", genre="무협", tags=["회귀"], authors=["김무협"],
                                         synopsis="천마가 회귀해 무림을 평정한다", unit="권"))  # fmt: skip
        db.update_review(conn, ME, 1, score=10)  # '천마 회귀록' 평가
        recs = Engine().recommend(conn, ME, ScoringOptions(sink_stingy=False))
    names = titles(recs)
    assert "천마 회귀록 [단행본]" not in names  # 평가한 작품의 다른 판본은 빠진다
    assert sum(1 for t in names if t.startswith("검황 회귀전")) == 1  # 판본은 하나만
    assert next(r for r in recs if r.comic.title.startswith("검황 회귀전")).variants == 1


def test_snapshot_keeps_pages_stable_when_ratings_change(library):
    from .conftest import comic_data

    engine = Engine()
    with db.session(library) as conn:
        for no in range(100, 140):
            db.upsert_comic(conn, comic_data(no, f"무림 회귀 {no}", genre="무협", tags=["회귀"],
                                             synopsis=f"회귀 무림 {no}"))  # fmt: skip
        db.update_review(conn, ME, 1, score=10)
        first, snap = engine.recommend_page(conn, ME, ScoringOptions(), offset=0, limit=10)
        db.update_review(
            conn, ME, first[0].comic.product_no, score=0
        )  # 스크롤 중에 평가 → 취향이 바뀜
        second, same = engine.recommend_page(conn, ME, ScoringOptions(), 10, 10, snapshot=snap)
        assert same == snap
        assert not {r.comic.product_no for r in first} & {r.comic.product_no for r in second}
        _, other = engine.recommend_page(conn, 999, ScoringOptions(), 10, 10, snapshot=snap)
        assert other != snap  # 남의 스냅숏은 못 쓴다
