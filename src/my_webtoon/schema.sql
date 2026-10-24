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

-- 사용자. 수집 데이터(comics 등)는 모두가 공유하고, 평가·추천만 사용자별로 나뉜다.
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT,                           -- PBKDF2 (web/auth.py). NULL 이면 로그인 불가
    is_admin        INTEGER NOT NULL DEFAULT 0,     -- 사용자 추가·수집 실행·백업
    is_demo         INTEGER NOT NULL DEFAULT 0,     -- 시연용: 비밀번호 변경·가져오기 막힘, 매일 밤 평가 초기화 (demo.py)
    session_epoch   INTEGER NOT NULL DEFAULT 0,     -- 비밀번호를 바꾸면 +1 → 다른 기기 세션 무효화
    created_at      TEXT    NOT NULL
);

-- 평가. (사용자, 작품)당 하나. 모든 칸이 선택 사항이라 "봤음"만, "8점"만, 둘 다, 세부 점수까지 자유롭게 남긴다.
CREATE TABLE IF NOT EXISTS reviews (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_no      INTEGER NOT NULL REFERENCES comics(product_no),
    status          TEXT CHECK (status IN ('seen', 'watching', 'dropped', 'wish', 'dismissed')),
                                                    -- 봤음 / 보는 중 / 하차 / 보고 싶음 / 관심 없음
    score           INTEGER CHECK (score BETWEEN 0 AND 10),   -- 종합 점수 0~10
    aspects         TEXT    NOT NULL DEFAULT '{}',  -- JSON: {"story": 9, "art": 7} — 항목은 db.ASPECTS
    episodes_read   INTEGER CHECK (episodes_read >= 0),       -- 몇 화까지 봤는지
    memo            TEXT    NOT NULL DEFAULT '',    -- 본인에게만 보인다
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (user_id, product_no)
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

-- 추천 순위 스냅숏 (무한 스크롤). 첫 페이지에서 전체 순위를 저장해 두고 다음 페이지는 여기서 자른다.
-- 메모리가 아니라 DB 에 두는 이유: 무중단 배포 중엔 옛 파드와 새 파드가 같이 요청을 받아서,
-- 스크롤하다 다른 파드로 넘어가도 같은 순위를 이어 가야 한다. 30분 지나면 지운다.
CREATE TABLE IF NOT EXISTS rec_snapshots (
    token           TEXT    PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    created_at      TEXT    NOT NULL,
    items           TEXT    NOT NULL                -- JSON: [[product_no, 유사도, 보정, 최종, 유료위주, 판본수], ...]
);

-- 작품 정보가 바뀐 기록 (주간 갱신 때 비교). '무료가 늘었어요'·'완결됐어요' 소식에 쓴다.
CREATE TABLE IF NOT EXISTS comic_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_no      INTEGER NOT NULL,
    field           TEXT    NOT NULL,               -- free_episodes / total_episodes / status / is_all_free / promo
    old_value       TEXT,
    new_value       TEXT,
    changed_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comic_changes_at ON comic_changes(changed_at);
CREATE INDEX IF NOT EXISTS idx_comic_changes_no ON comic_changes(product_no);

-- 빠른 평가에서 '안 봤음'을 누른 작품. 평가(취향)는 아니라서 reviews 와 따로 두고, 다시 묻지 않는 데만 쓴다.
CREATE TABLE IF NOT EXISTS quick_skips (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_no      INTEGER NOT NULL,
    skipped_at      TEXT    NOT NULL,
    PRIMARY KEY (user_id, product_no)
);
