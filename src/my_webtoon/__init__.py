"""my-webtoon — 네이버 시리즈 코믹스 개인화 추천기.

패키지 구성 (읽는 순서 추천):
    config.py        환경변수 → Settings
    db.py            SQLite 스키마와 쿼리 (ORM 없이 SQL 그대로)
    crawler/         네이버 시리즈 수집 (http → parser → runner)
    recommender/     추천 로직 (features → profile → scoring → engine)
    web/             FastAPI + Jinja2 + HTMX 화면
    cli.py           `my-webtoon serve | crawl | import` 명령
"""
