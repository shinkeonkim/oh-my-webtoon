-- my-webtoon 스키마. 앱 시작 시 db.init_schema()가 그대로 실행한다 (IF NOT EXISTS 라 여러 번 실행해도 안전).

-- 수집한 작품 하나 = 한 행. 네이버 시리즈 productNo 를 그대로 기본키로 쓴다.
CREATE TABLE IF NOT EXISTS comics (
    product_no      INTEGER PRIMARY KEY,
    title           TEXT    NOT NULL,
    authors         TEXT    NOT NULL DEFAULT '[]',  -- JSON 배열: ["글 작가", "그림 작가"]
    publisher       TEXT,
    genre           TEXT,                           -- 네이버 장르 (무협, 소년, 드라마, 순정, BL ...)
    tags            TEXT    NOT NULL DEFAULT '[]',  -- JSON 배열: 메타 설명의 해시태그 (#먼치킨 #회귀 ...)
    synopsis        TEXT    NOT NULL DEFAULT '',
    status          TEXT,                           -- 연재중 / 완결
    age_rating      TEXT,                           -- 전체 이용가 / 15세 이용가 ...
    unit            TEXT    NOT NULL DEFAULT '화',   -- 화(웹툰) / 권(단행본)
    total_episodes  INTEGER,
    free_episodes   INTEGER,                        -- "전화 무료"면 total_episodes 와 같다
    is_all_free     INTEGER NOT NULL DEFAULT 0,
    naver_rating    REAL,                           -- 네이버 평점 (10점 만점)
    popularity      INTEGER,                        -- 다운로드 수 근사치 ("5만" -> 50000)
    thumbnail_url   TEXT,
    crawled_at      TEXT    NOT NULL
);

-- 크롤러가 "앞으로 가져올 작품" 목록 (BFS 프런티어).
CREATE TABLE IF NOT EXISTS crawl_queue (
    product_no      INTEGER PRIMARY KEY,
    state           TEXT    NOT NULL DEFAULT 'pending',  -- pending / done / skipped / failed
    discovered_from INTEGER,                              -- 어느 작품 페이지에서 발견했는지 (시드면 NULL)
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    discovered_at   TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crawl_queue_state ON crawl_queue(state);

-- 무료관에 걸린 기간 한정 무료 이벤트 ("33화 무료 50일 남음"). 상세 페이지에는 기간이 안 나와서 따로 둔다.
CREATE TABLE IF NOT EXISTS free_promos (
    product_no      INTEGER PRIMARY KEY,
    label           TEXT    NOT NULL,
    seen_at         TEXT    NOT NULL
);

-- 내 평가. 작품당 하나.
--   rated     : 별점(1~5)을 줬다
--   seen      : 봤는데 별점은 아직
--   dismissed : 관심 없음 (다시 추천하지 마)
CREATE TABLE IF NOT EXISTS ratings (
    product_no      INTEGER PRIMARY KEY REFERENCES comics(product_no),
    kind            TEXT    NOT NULL CHECK (kind IN ('rated', 'seen', 'dismissed')),
    score           INTEGER CHECK (score BETWEEN 1 AND 5),
    memo            TEXT    NOT NULL DEFAULT '',
    updated_at      TEXT    NOT NULL
);

-- 크롤러 실행 기록 (/crawl 화면에서 보여준다).
CREATE TABLE IF NOT EXISTS crawl_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    fetched         INTEGER NOT NULL DEFAULT 0,
    discovered      INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    note            TEXT
);

-- 작은 키-값 저장소 (마지막 시드 수집 시각 등).
CREATE TABLE IF NOT EXISTS meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);
