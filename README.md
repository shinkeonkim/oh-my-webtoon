# oh-my-webtoon

네이버 시리즈 코믹스 데이터로 만드는 **나만의 웹툰 추천기**. 내가 본 작품을 평가하면
제목·줄거리·장르·태그·작가가 비슷한 작품을 추천하고, **무료로 볼 수 있는 회차가 많고
총 회차가 긴 작품을 위로, 무료 3화 이하 '유료 위주' 작품은 맨 아래로** 보낸다.

- 공개 주소: https://my-webtoon.shinkeonkim.com (비밀번호 로그인)
- 내부 확인: https://my-webtoon.homelab.shinkeonkim.com (tailnet)

## 어떻게 동작하나 (한 장 요약)

```
 네이버 시리즈 ──(1) crawler/──▶ SQLite(comics) ──(2) recommender/──▶ 추천 목록 ──▶ web/ (FastAPI+HTMX)
   TOP100·무료관 시드                 작품 메타데이터          TF-IDF 벡터 × 내 취향            ▲
   → 상세 페이지 BFS                                       × 무료/회차 보정                  │
                                                                                       별점·봤음·관심없음
                                       SQLite(reviews) ◀──────────────────────────────────────┘
```

1. **수집** — robots.txt 가 목록 페이지를 막고 있어서, TOP100/무료관 페이지에서 작품 번호를 모은 뒤
   각 상세 페이지 안의 연관 작품 링크를 따라 넓혀 간다(BFS). 요청 간격 1.5초. → [docs/02_크롤링.md](docs/02_크롤링.md)
2. **추천** — 작품을 TF-IDF 벡터로 만들고, 내 평가의 가중합으로 취향 벡터를 만든 뒤 코사인 유사도에
   무료/회차/평점 보정을 곱한다. → [docs/03_추천_알고리즘.md](docs/03_추천_알고리즘.md)
3. **화면** — 추천 카드마다 별점(반 개 단위, 0~10점) / 봤음·보는 중·하차·보고 싶음·관심 없음 / "자세히"(스토리·작화·캐릭터·전개·몰입도,
   본 화수, 메모) 가 있어서 이미 본 작품이 추천되면 그 자리에서 평가한다. 모든 칸이 선택 사항이고 서로 독립이다.
   작품 목록과 사용자별 평가 기록은 로그인 없이 공개, 추천·평가·설정은 로그인 필요.
4. **사용자** — 수집한 작품 데이터는 모두가 공유하고, 평가와 추천만 사용자별이다. 첫 관리자는 `singun11`,
   관리자가 `/settings` 에서 사용자를 추가한다(공개 가입 없음).

## 코드 읽는 순서

| 순서 | 파일 | 내용 |
|---|---|---|
| 1 | `src/my_webtoon/schema.sql` + `db.py` | 공유: comics, crawl_queue, free_promos, crawl_runs / 사용자별: users, reviews. 평가 항목은 `db.STATUSES`/`db.ASPECTS` |
| 2 | `src/my_webtoon/crawler/parser.py` | HTML → dict. 순수 함수라 테스트로 동작을 확인하기 쉽다 |
| 3 | `src/my_webtoon/crawler/runner.py` | 시드 → 큐 → 상세 → 링크 확장 루프 |
| 4 | `src/my_webtoon/recommender/features.py` → `profile.py` → `scoring.py` → `engine.py` | 추천 4단계 |
| 5 | `src/my_webtoon/web/app.py` + `templates/` | 라우트와 화면 |
| 6 | `tests/` | 각 단계가 무엇을 보장하는지 (`test_engine.py` 가 추천 동작 명세 역할) |

전체 구조와 설계 결정은 [docs/01_아키텍처.md](docs/01_아키텍처.md), 배포는 [docs/04_배포.md](docs/04_배포.md), 화면 컴포넌트는 [docs/05_디자인_시스템.md](docs/05_디자인_시스템.md) (살아 있는 목록: `/ui`).

## 로컬에서 돌리기

```bash
uv sync
uv run my-webtoon crawl --limit 300     # 시드 수집(약 2~3분) + 상세 300개(약 8분)
uv run my-webtoon serve --reload        # http://localhost:8000
uv run pytest                           # 테스트
```

`APP_PASSWORD` 를 비워 두면 로그인 없이 쓴다. 다른 설정은 [.env.example](.env.example) 참고.

## 데이터에 대해

개인 학습용이다. 수집한 데이터는 SQLite 파일(PVC)에만 있고 저장소에는 커밋하지 않는다.
robots.txt 를 지키고(목록 페이지 미접근), 요청 사이에 1.5초 이상 쉬며, 19금 등 로그인이 필요한 페이지는 건너뛴다.
