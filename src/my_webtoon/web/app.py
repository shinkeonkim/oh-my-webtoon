"""FastAPI 앱. 화면은 서버에서 HTML 을 렌더링하고(Jinja2), 점수 클릭 같은 작은 상호작용만
HTMX 로 부분 교체한다 — 프론트엔드 빌드 도구 없이 파이썬 코드만 읽으면 흐름이 다 보인다.

데이터 경계: 수집한 작품(comics)은 모두가 공유하고, 평가(reviews)와 추천은 로그인한 사용자 것만 쓴다.

라우트 한눈에 보기 (🔓 = 로그인 없이 볼 수 있음, 👑 = 관리자)
    GET  /                       내 추천 목록 (무한 스크롤: HX-Request 면 다음 페이지 조각만)
    GET  /quick, POST /quick/{no}  빠른 평가 — 한 장씩 보여 주고 누르면 다음 장 (되돌리기 /quick/{no}/undo)
    GET  /news                   소식: 이어 보기, 무료가 늘었어요, 완결, 기간 한정 무료
    GET  /rate?q=                검색해서 평가하기 (온보딩)
    GET  /import/series          네이버 시리즈 '내 서재'에서 본 작품 가져오기 (북마클릿 → ?nos= 미리보기 → POST 저장)
    POST /import                 네이버 시리즈 URL/번호로 작품 바로 가져오기
    GET  /comics                 🔓 수집된 작품 목록 (검색·장르·정렬, 무한 스크롤)
    GET  /comic/{no}             🔓 작품 상세 + 비슷한 작품
    GET  /users                  🔓 사용자 목록
    GET  /users/{name}           🔓 그 사용자의 평가 기록 (상태·점수 필터, 메모는 본인만)
    GET  /users/{name}/taste     🔓 취향 요약 (점수 분포, 장르·작가·태그, 취향 키워드)
    GET  /me                     → /users/{내 아이디}
    POST /reviews/{no}           내 평가 저장 — 바꾼 칸만 반영 (HTMX 가 위젯만 갈아 끼운다)
    GET  /reviews/{no}/edit      세부 평가 폼 (스토리·작화·…, 본 화수, 메모)
    GET  /reviews/{no}/widget    폼 닫기 → 간단 위젯
    POST /reviews/{no}/delete    내 평가 전체 삭제
    POST /reviews/{no}/dismiss   추천 카드의 × — '관심 없음' (undismiss 로 되돌리기)
    GET  /settings               비밀번호 변경, 평가 내보내기/가져오기, 👑 사용자 추가·백업
    GET  /crawl, POST /crawl/run 수집·운영 상태 (응답 시간 포함) / 👑 즉시 실행
    GET  /login, POST /login, POST /logout, GET /healthz   🔓
    POST /theme                  🔓 화면 테마 (자동/라이트/다크) — 쿠키, 사용자 메뉴에서
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import backup, db, demo, taste
from ..config import Settings, load_settings
from ..crawler import runner
from ..recommender import Engine, ScoringOptions
from ..recommender import scoring as scoring_mod
from ..recommender.engine import REFRESH_INTERVAL
from . import auth, prom
from .metrics import RequestLog
from .prom import Metrics

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
ALWAYS_PUBLIC = ("/login", "/demo", "/static", "/healthz", "/readyz", "/theme")
# 화면 테마. DB 가 아니라 쿠키에 둔다 — 로그인 전 화면에도 쓰고, 기기마다(폰은 다크·PC 는 라이트) 다를 수 있으니까.
# system 이면 OS 설정(prefers-color-scheme)을 따른다. 서버가 <html data-theme> 으로 그려서 깜빡임이 없다.
THEMES = ("system", "light", "dark")
THEME_COOKIE = "theme"
PUBLIC_GET = ("/comics", "/comic/", "/users", "/ui")  # 읽기 전용 공개 화면
NEWS_DAYS = 30  # 소식 화면이 보여 주는 기간
REVIEWED_FILTERS = {
    "exclude": "안 본 것만",
    "include": "본 것 포함",
    "only": "본 것만",
}  # 추천의 ?reviewed=
SERIES_IMPORT_MAX = 2000  # 북마클릿 한 번에 받는 작품 번호 수
PER_PAGE = 30
# /rate?status=...  내가 남긴 상태로 거르기. 'none' 은 상태 없는 작품(안 본 것 + 별점만 남긴 것).
RATE_STATUS_FILTERS = {
    "": "전체",
    "none": "상태 없음",
    **{k: v for k, v in db.STATUSES.items()},
}
SCORE_FILTERS = {  # /users/{name}?score=...  점수는 0~10(=별 반 개 단위). 세부 점수만 있으면 평균을 반올림.
    "10": ("★5", lambda s: s is not None and round(s) == 10),
    "9": ("★4.5", lambda s: s is not None and round(s) == 9),
    "8": ("★4", lambda s: s is not None and round(s) == 8),
    "7": ("★3.5", lambda s: s is not None and round(s) == 7),
    "6": ("★3", lambda s: s is not None and round(s) == 6),
    "low": ("★2.5 이하", lambda s: s is not None and round(s) <= 5),
    "none": ("별점 없음", lambda s: s is None),
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    # 크롤러가 같은 프로세스에서 돌면 수집 중 작품 데이터가 계속 바뀐다 — 벡터는 뒤에서 다시 만든다 (engine.py)
    engine = Engine(refresh_interval=REFRESH_INTERVAL if settings.crawl_enabled else None)
    crawler = runner.BackgroundCrawler(settings.database_path, settings)
    crawler.on_finish = engine.invalidate
    stop_backup = threading.Event()

    with db.session(settings.database_path) as conn:
        initial_hash = auth.hash_password(settings.app_password) if settings.app_password else None
        db.init_schema(conn, settings.admin_username, initial_hash)
        removed, restored = db.apply_genre_exclusion(conn, settings.excluded_genres)
        if removed or restored:
            log.info(
                "제외 장르 %s: 작품 %d개 삭제, %d개 재수집 예약",
                sorted(settings.excluded_genres),
                removed,
                restored,
            )

    def maintenance_loop() -> None:
        """한 시간마다: 하루 지난 데모 계정 평가 되돌리기 + (BACKUP_DIR 이 있으면) 하루 한 번 백업."""
        while not stop_backup.is_set():
            try:
                with db.session(settings.database_path) as conn:
                    demo.reset_due(conn)
                    backup_due = settings.backup_dir and backup.backup_due(conn)
                if backup_due:
                    backup.backup_and_verify(settings.database_path, settings.backup_dir)
            except Exception:
                log.exception("정기 작업 실패")
            stop_backup.wait(timeout=3600)

    ready = threading.Event()

    def warm_up() -> None:
        try:
            with db.session(settings.database_path) as conn:
                engine.warm_up(conn)
        except Exception:
            log.exception("워밍업 실패 — 그래도 요청은 받는다 (첫 추천이 조금 느릴 뿐)")
        ready.set()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        threading.Thread(target=warm_up, name="warm-up", daemon=True).start()
        if settings.crawl_enabled:
            crawler.start()
        threading.Thread(target=maintenance_loop, name="maintenance", daemon=True).start()
        if settings.metrics_port:
            prometheus.serve(settings.metrics_port)
        yield
        prometheus.stop()
        crawler.stop()
        stop_backup.set()

    app = FastAPI(title="my-webtoon", lifespan=lifespan)
    app.state.settings, app.state.engine, app.state.crawler = settings, engine, crawler
    request_log = app.state.request_log = RequestLog()
    app.state.ready = ready
    prometheus = app.state.prometheus = Metrics(settings.database_path, app.state)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        free_label=free_label,
        free_tone=free_tone,
        episodes_label=episodes_label,
        series_link=series_link,
        is_mobile=is_mobile,
        format_stars=format_stars,
        reason_label=reason_label,
        news_label=news_label,
        reading_progress=reading_progress,
        static_version=static_version(HERE / "static"),
        STATUSES=db.STATUSES,
        ASPECTS=db.ASPECTS,
        SCORES=list(db.SCORE_RANGE),
    )
    templates.env.filters.update(
        kst=kst,
        star_option=lambda v: (v, f"★{format_stars(v)}"),  # 셀렉트용 (값, 라벨)
        pair=lambda v: (v, v),
        sort_option=lambda item: (item[0], item[1][0]),
        subnav_item=lambda item: (item[0], item[1], None, item[2]),  # base.html 하위 메뉴 → ui.tabs
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    # -- 인증 -----------------------------------------------------------------
    # request.state.user = 로그인한 사용자 (없으면 None). 공개 화면이 아니면 로그인 화면으로 보낸다.

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        public = path.startswith(ALWAYS_PUBLIC) or (
            request.method == "GET" and path.startswith(PUBLIC_GET)
        )
        with db.session(settings.database_path) as conn:
            request.state.user = auth.current_user(conn, request.session)
            request.state.open_mode = auth.open_mode(conn)
        if not public and request.state.user is None:
            if request.headers.get("HX-Request"):
                return Response(status_code=401, headers={"HX-Redirect": "/login"})
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    # SessionMiddleware 는 require_login 보다 바깥에 있어야 request.session 을 쓸 수 있다
    # (나중에 add 한 미들웨어가 바깥을 감싼다).
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        same_site="lax",
        https_only=False,
        max_age=60 * 60 * 24 * 30,
    )

    @app.middleware("http")
    async def time_requests(request: Request, call_next):
        """맨 바깥에서 응답 시간을 잰다 (metrics.py → /crawl 의 '응답 시간')."""
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", None)
        elapsed = time.perf_counter() - started
        request_log.add(path or request.url.path, elapsed * 1000, response.status_code)
        # Prometheus 라벨은 라우트 패턴만 — 없는 주소(404)를 그대로 넣으면 라벨이 끝없이 늘어난다
        # /static 은 마운트라 route 가 안 잡혀서, 거르는 건 실제 주소로 한다
        if not request.url.path.startswith(prom.SKIP):
            prometheus.observe(path or "(unmatched)", request.method, response.status_code, elapsed)
        return response

    def get_conn() -> Iterator:
        with db.session(settings.database_path) as conn:
            yield conn

    def me(request: Request) -> db.User:
        user = request.state.user
        if user is None:
            raise HTTPException(401)
        return user

    def require_admin(request: Request) -> db.User:
        user = me(request)
        if not user.is_admin:
            raise HTTPException(403, "관리자만 할 수 있어요")
        return user

    def forbid_demo(request: Request, back_to: str, message: str) -> RedirectResponse | None:
        """데모 계정이면 막는다. 막았으면 돌려보낼 응답을, 아니면 None."""
        user = request.state.user
        if user is not None and user.is_demo:
            return RedirectResponse(f"{back_to}?error={message}", status_code=303)
        return None

    def my_id(request: Request) -> int | None:
        return request.state.user.id if request.state.user else None

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        user = request.state.user
        ctx.setdefault("user", user)
        ctx.setdefault("authed", user is not None)
        ctx.setdefault("can_logout", not request.state.open_mode)
        ctx.setdefault("theme", theme_of(request))
        return templates.TemplateResponse(request, name, ctx)

    # -- 추천 -----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, conn=Depends(get_conn), page: int = 0, snap: str = ""):
        user = me(request)
        opts = options_from_query(request)
        # snap: 첫 페이지에서 만든 순위 스냅숏. 다음 페이지는 같은 순위에서 이어서 잘라 중복·누락이 없다.
        recs, snap = engine.recommend_page(
            conn, user.id, opts, offset=page * PER_PAGE, limit=PER_PAGE, snapshot=snap or None
        )
        ctx = dict(
            recs=recs,
            reviews=db.all_reviews(conn, user.id),
            opts=opts,
            page=page,
            rank_offset=page * PER_PAGE,
            has_next=len(recs) == PER_PAGE,
            query_string=_query_without(request, "page", "snap"),
            snap=snap,
            reviewed_filters=REVIEWED_FILTERS,
            reviewed_tabs=[
                (
                    "/?"
                    + urlencode(
                        {
                            **_query_dict(request, "page", "snap", "reviewed"),
                            **({} if k == "exclude" else {"reviewed": k}),
                        }
                    ),
                    label,
                    None,
                    opts.reviewed == k,
                )
                for k, label in REVIEWED_FILTERS.items()
            ],
        )
        if request.headers.get("HX-Request") and page > 0:
            return render(request, "_rec_page.html", **ctx)  # 무한 스크롤: 다음 30개 + 다음 센티넬
        return render(
            request,
            "index.html",
            stats=db.queue_stats(conn),
            popular=[] if recs else engine.popular(conn, opts, user.id),
            news_count=len(news_items(conn, user.id, days=7)["changes"]),
            **ctx,
        )

    # -- 빠른 평가 -------------------------------------------------------------
    # 한 번에 한 작품만. 누르면 저장하고 바로 다음 작품 조각을 돌려준다 (HTMX 가 #quick 을 통째로 바꾼다).
    # 목표는 평가 개수를 빨리 늘리는 것 — 평가가 많을수록 추천이 정확해진다 (docs/03).

    # 카드 하나가 폼이다. 별점·상태·본 화수를 고르는 동안은 아무것도 저장하지 않고, 버튼을 눌러야 저장하고 넘어간다.
    #   save       고른 값 저장 (별점만 골랐으면 상태는 '봤음')
    #   skip       안 봤음 — 평가 아님, db.QUICK_SKIP_DAYS 동안만 다시 묻지 않는다 (quick_skips)
    #   dismissed  관심 없음

    def quick_card(request: Request, conn, done: int = 0, last=None, comic=None, error=""):
        user = me(request)
        if comic is None:
            queue = engine.quick_queue(conn, user.id, limit=1)
            comic = queue[0] if queue else None
        return render(
            request,
            "_quick.html" if request.headers.get("HX-Request") else "quick.html",
            comic=comic,
            done=done,
            last=last,
            error=error,
            review_count=len(db.all_reviews(conn, user.id)),
            skip_days=db.QUICK_SKIP_DAYS,
        )

    @app.get("/quick", response_class=HTMLResponse)
    def quick_page(request: Request, conn=Depends(get_conn)):
        return quick_card(request, conn)

    @app.post("/quick/{product_no}", response_class=HTMLResponse)
    async def quick_answer(request: Request, product_no: int, conn=Depends(get_conn)):
        user = me(request)
        comic = comic_or_404(conn, product_no)
        form = await request.form()
        action, done = form.get("action", ""), int(form.get("done") or 0)
        if action == "save":
            score, status = _opt_int(form.get("score")), form.get("status") or None
            episodes = _opt_int(form.get("episodes_read"))
            if score is None and status is None:
                return quick_card(
                    request, conn, done, comic=comic, error="별점이나 상태를 하나는 골라 주세요"
                )
            try:
                db.update_review(
                    conn, user.id, product_no,
                    score=score, status=status or "seen", episodes_read=episodes,
                )  # fmt: skip
            except ValueError as exc:
                return quick_card(request, conn, done, comic=comic, error=str(exc))
        elif action == "dismissed":
            db.update_review(conn, user.id, product_no, status="dismissed")
        elif action == "skip":
            db.set_quick_skip(conn, user.id, product_no, True)
        else:
            raise HTTPException(400, f"모르는 동작: {action}")
        return quick_card(request, conn, done=done + 1, last=comic)

    @app.post("/quick/{product_no}/undo", response_class=HTMLResponse)
    async def quick_undo(request: Request, product_no: int, conn=Depends(get_conn)):
        """방금 누른 것 되돌리기. 빠른 평가에는 평가 안 한 작품만 나오니, 지우면 원래 상태다."""
        user = me(request)
        comic_or_404(conn, product_no)
        form = await request.form()
        db.delete_review(conn, user.id, product_no)
        db.set_quick_skip(conn, user.id, product_no, False)
        return quick_card(
            request,
            conn,
            done=max(0, int(form.get("done") or 1) - 1),
            comic=db.get_comic(conn, product_no),
        )

    # -- 소식 --------------------------------------------------------------------

    def news_items(conn, user_id: int, days: int = NEWS_DAYS) -> dict:
        """소식 화면 재료. 내가 '관심 없음'·낮은 점수를 준 작품 소식은 뺀다."""
        reviews = db.all_reviews(conn, user_id)
        hidden = {
            no for no, r in reviews.items()
            if r.status in ("dismissed", "dropped") or (r.overall is not None and r.overall < 5)
        }  # fmt: skip
        changes = [
            (ch, c)
            for ch, c in db.recent_changes(conn, days)
            if c.product_no not in hidden and is_good_news(ch)
        ]
        return {"changes": changes, "reviews": reviews}

    @app.get("/news", response_class=HTMLResponse)
    def news_page(request: Request, conn=Depends(get_conn)):
        user = me(request)
        items = news_items(conn, user.id)
        reviews = items["reviews"]
        mine = {no for no, r in reviews.items() if r.status in ("watching", "wish", "seen")}
        # 같은 작품에 소식이 여러 개면 (무료↑ + 총 화수↑) 한 카드로 묶는다
        grouped: dict[int, tuple] = {}
        for ch, comic in items["changes"]:
            entry = grouped.setdefault(comic.product_no, (comic, []))
            entry[1].append(ch)
        news = sorted(grouped.values(), key=lambda e: e[1][0]["changed_at"], reverse=True)
        news.sort(
            key=lambda e: e[0].product_no not in mine
        )  # 내가 보는·찜한 작품 소식을 위로 (안정 정렬)
        watching = [
            (r, c) for r, c in db.reviews_with_comics(conn, user.id) if r.status == "watching"
        ]
        return render(
            request,
            "news.html",
            news=news,
            mine=mine,
            watching=watching,
            promos=[c for c in db.promo_comics(conn) if c.product_no not in reviews][:24],
            reviews=reviews,
            days=NEWS_DAYS,
        )

    # -- 평가하기 (검색 + 가져오기) -------------------------------------------------

    @app.post("/import")
    def import_comic(request: Request, url: str = Form(...), conn=Depends(get_conn)):
        if blocked := forbid_demo(request, "/rate", "데모 계정에서는 링크로 가져올 수 없어요"):
            return blocked
        product_no = parse_product_no(url)
        if product_no is None:
            return RedirectResponse("/rate?error=productNo 를 찾지 못했어요", status_code=303)
        if db.get_comic(conn, product_no) is None:
            try:
                runner.import_one(
                    settings.database_path,
                    product_no,
                    user_agent=settings.user_agent,
                    excluded_genres=settings.excluded_genres,
                )
            except runner.parser.ExcludedGenre as exc:
                return RedirectResponse(
                    f"/rate?error={exc} 장르는 수집하지 않아요", status_code=303
                )
            except runner.parser.LoginRequired:
                return RedirectResponse(
                    "/rate?error=로그인이 필요한 작품이에요(19금)", status_code=303
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("가져오기 실패 %s: %s", product_no, exc)
                return RedirectResponse(f"/rate?error=가져오기 실패: {exc}", status_code=303)
            engine.invalidate()
        return RedirectResponse(f"/comic/{product_no}", status_code=303)

    # -- 네이버 시리즈에서 본 작품 가져오기 -----------------------------------------------
    # 서버는 네이버에 로그인할 수 없다(비밀번호를 받으면 안 되고, '내 서재'는 robots.txt 가 막는다).
    # 그래서 내 브라우저에서 북마클릿이 '지금 보고 있는 시리즈 페이지'의 작품 번호만 모아 여기로 넘긴다.
    # 넘어온 번호는 미리보기만 하고, 확인 버튼을 눌러야 저장한다.

    @app.get("/import/series", response_class=HTMLResponse)
    def series_import_page(request: Request, nos: str = "", conn=Depends(get_conn)):
        user = me(request)
        numbers = list(dict.fromkeys(int(n) for n in re.findall(r"\d+", nos)))[:SERIES_IMPORT_MAX]
        known = db.comics_by_ids(conn, numbers)
        reviews = db.all_reviews(conn, user.id)
        return render(
            request,
            "import_series.html",
            received=len(numbers),
            items=[(known[n], reviews.get(n)) for n in numbers if n in known],
            unknown=[n for n in numbers if n not in known],
            bookmarklet=bookmarklet(str(request.base_url).rstrip("/")),
        )

    @app.post("/import/series")
    async def series_import_save(request: Request, conn=Depends(get_conn)):
        user = me(request)
        form = await request.form()
        # 기본은 '보는 중' — 서재에 있는 건 대개 아직 보고 있는 작품이고, 그래야 소식의 '이어 보기'에 바로 잡힌다
        status = form.get("status") if form.get("status") in db.STATUSES else "watching"
        saved = 0
        for value in form.getlist("no"):
            no = int(value)
            current = db.get_review(conn, user.id, no)
            if db.get_comic(conn, no) is None or (current and current.status):
                continue  # 이미 상태를 남긴 작품은 덮어쓰지 않는다 (점수만 있으면 상태만 더한다)
            db.update_review(conn, user.id, no, status=status)
            saved += 1
        queued = 0
        if form.get("queue_unknown"):
            queued = db.enqueue(conn, [int(n) for n in form.getlist("unknown")], None)
        msg = f"{saved}개를 '{db.STATUSES[status]}'으로 저장했어요"
        if queued:
            msg += f" · 수집 안 된 {queued}개는 수집 대기열에 넣었어요"
        return RedirectResponse(f"/users/{user.username}?msg={msg}", status_code=303)

    # -- 공개 화면: 수집 목록 / 상세 / 사용자별 평가 기록 ---------------------------------

    # /rate (로그인) 와 /comics (공개, 읽기 전용) 는 같은 화면이다: 작품을 찾아 보고 바로 평가한다.
    # 조건이 전부 같이 걸린다 — 검색어 + 장르 + 무료 + 연재 상태 + 내가 남긴 상태(mine) + 정렬.
    # 예전 주소도 받는다: /comics?unrated=1 → mine=none, /rate?status=watching → mine=watching.

    def browse(request: Request, conn, base_path: str):
        p = request.query_params
        uid = my_id(request)
        q, genre, free, status = (
            p.get("q", ""),
            p.get("genre", ""),
            p.get("free", ""),
            p.get("status", ""),
        )
        mine = p.get("mine", "")
        if status and status in RATE_STATUS_FILTERS and status not in db.STATUS_FILTERS:
            status, mine = "", status  # 옛 /rate?status=<내 상태>
        if p.get("unrated"):
            mine = "none"
        if uid is None or mine not in RATE_STATUS_FILTERS:
            mine = ""
        sort = p.get("sort") or ("search" if q.strip() else "popular")
        page = int(p.get("page") or 0)
        filters = dict(genre=genre, free=free, status=status)
        comics, total = db.list_comics(
            conn,
            query=q,
            sort=sort,
            review_of=uid,
            review_status=mine,
            limit=PER_PAGE,
            offset=page * PER_PAGE,
            **filters,
        )
        ctx = dict(
            comics=comics,
            page=page,
            has_next=(page + 1) * PER_PAGE < total,
            reviews=db.all_reviews(conn, uid),
            query_string=_query_without(request, "page"),
            base_path=base_path,
        )
        if request.headers.get("HX-Request") and page > 0:
            return render(request, "_comics_page.html", **ctx)
        mine_tabs = []
        if uid is not None:
            counts = db.review_status_counts(conn, uid, q, **filters)
            keep = {k: v for k, v in _query_dict(request, "page", "mine", "unrated").items()}
            mine_tabs = [
                (
                    base_path + "?" + urlencode({**keep, **({"mine": k} if k else {})}),
                    label,
                    counts.get(k) if k else None,
                    mine == k,
                )
                for k, label in RATE_STATUS_FILTERS.items()
            ]
        return render(
            request,
            "browse.html",
            total=total,
            q=q,
            genre=genre,
            sort=p.get("sort", ""),
            free=free,
            status=status,
            mine=mine,
            mine_tabs=mine_tabs,
            genres=db.genres(conn),
            sorts=db.COMIC_SORTS,
            free_filters=db.FREE_FILTERS,
            status_filters=db.STATUS_FILTERS,
            # 서버가 네이버 검색을 대신할 수는 없다(robots.txt 가 /search 를 막음).
            # 대신 내 브라우저에서 검색 결과를 열어 링크를 복사해 오게 한다.
            naver_search_url=(
                "https://series.naver.com/search/search.series?t=all&fs=comic&q=" + quote(q)
                if q.strip()
                else None
            ),
            error=p.get("error", ""),
            **ctx,
        )

    @app.get("/rate", response_class=HTMLResponse)
    def rate_page(request: Request, conn=Depends(get_conn)):
        return browse(request, conn, "/rate")

    @app.get("/comics", response_class=HTMLResponse)
    def comics_page(request: Request, conn=Depends(get_conn)):
        if request.state.user is not None and not request.headers.get("HX-Request"):
            # 로그인했으면 평가할 수 있는 /rate 로 (같은 화면, 조건 그대로)
            return RedirectResponse(
                "/rate" + (f"?{request.url.query}" if request.url.query else ""), 303
            )
        return browse(request, conn, "/comics")

    @app.get("/comic/{product_no}", response_class=HTMLResponse)
    def comic_detail(request: Request, product_no: int, conn=Depends(get_conn)):
        comic = db.get_comic(conn, product_no)
        if comic is None:
            raise HTTPException(404, "아직 수집하지 않은 작품이에요")
        opts = options_from_query(request)
        uid = my_id(request)
        return render(
            request,
            "comic.html",
            comic=comic,
            review=db.get_review(conn, uid, product_no) if uid else None,
            similar=engine.similar_to(conn, product_no, opts, uid),
            reviews=db.all_reviews(conn, uid),
            boost=scoring_mod.boost(comic, opts),
            stingy=scoring_mod.is_stingy(comic, opts),
            match=engine.match_score(conn, uid, product_no, opts) if uid else None,
        )

    @app.get("/me")
    def my_page(request: Request):
        return RedirectResponse(f"/users/{me(request).username}", status_code=303)

    @app.get("/users", response_class=HTMLResponse)
    def users_page(request: Request, conn=Depends(get_conn)):
        return render(request, "users.html", users=db.list_users(conn))

    @app.get("/users/{username}", response_class=HTMLResponse)
    def user_reviews(
        request: Request, username: str, conn=Depends(get_conn), status: str = "", score: str = ""
    ):
        owner = db.get_user_by_name(conn, username)
        if owner is None:
            raise HTTPException(404, "없는 사용자예요")
        items = db.reviews_with_comics(conn, owner.id)
        total = len(items)
        status_counts = {k: sum(1 for r, _ in items if r.status == k) for k in db.STATUSES}
        score_counts = {
            k: sum(1 for r, _ in items if keep(r.overall)) for k, (_, keep) in SCORE_FILTERS.items()
        }
        if status == "none":
            items = [(r, c) for r, c in items if r.status is None]
        elif status:
            items = [(r, c) for r, c in items if r.status == status]
        if score in SCORE_FILTERS:
            keep = SCORE_FILTERS[score][1]
            items = [(r, c) for r, c in items if keep(r.overall)]
        # 점수 높은 순, 점수 없는 건 뒤로 (최근 수정순 유지)
        items.sort(key=lambda rc: -(rc[0].overall if rc[0].overall is not None else -1))
        is_owner = request.state.user is not None and request.state.user.id == owner.id
        return render(
            request,
            "user.html",
            owner=owner,
            is_owner=is_owner,
            items=items,
            total=total,
            status=status,
            score=score,
            status_counts=status_counts,
            score_counts=score_counts,
            score_filters={k: label for k, (label, _) in SCORE_FILTERS.items()},
        )

    @app.get("/users/{username}/taste", response_class=HTMLResponse)
    def user_taste(request: Request, username: str, conn=Depends(get_conn)):
        owner = db.get_user_by_name(conn, username)
        if owner is None:
            raise HTTPException(404, "없는 사용자예요")
        summary = taste.summarize(db.reviews_with_comics(conn, owner.id), engine, conn)
        return render(request, "taste.html", owner=owner, taste=summary)

    # -- 평가 저장 (HTMX) — 항상 로그인한 본인의 평가만 ------------------------------------

    def review_widget(request: Request, conn, comic, template: str = "_review.html"):
        review = db.get_review(conn, me(request).id, comic.product_no)
        return render(request, template, comic=comic, review=review, editable=True, show_memo=True)

    def comic_or_404(conn, product_no: int):
        comic = db.get_comic(conn, product_no)
        if comic is None:
            raise HTTPException(404)
        return comic

    @app.post("/reviews/{product_no}", response_class=HTMLResponse)
    async def save_review(request: Request, product_no: int, conn=Depends(get_conn)):
        user = me(request)
        comic = comic_or_404(conn, product_no)
        form = await request.form()
        try:
            changes = parse_review_form(form, db.get_review(conn, user.id, product_no))
            db.update_review(conn, user.id, product_no, **changes)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not request.headers.get("HX-Request"):
            return RedirectResponse(request.headers.get("referer") or "/", status_code=303)
        return review_widget(request, conn, comic)

    @app.get("/reviews/{product_no}/edit", response_class=HTMLResponse)
    def edit_review(request: Request, product_no: int, conn=Depends(get_conn)):
        return review_widget(request, conn, comic_or_404(conn, product_no), "_review_form.html")

    @app.get("/reviews/{product_no}/widget", response_class=HTMLResponse)
    def review_widget_only(request: Request, product_no: int, conn=Depends(get_conn)):
        return review_widget(request, conn, comic_or_404(conn, product_no))

    @app.post("/reviews/{product_no}/delete", response_class=HTMLResponse)
    def remove_review(request: Request, product_no: int, conn=Depends(get_conn)):
        comic = comic_or_404(conn, product_no)
        db.delete_review(conn, me(request).id, product_no)
        if not request.headers.get("HX-Request"):
            return RedirectResponse(request.headers.get("referer") or "/", status_code=303)
        return review_widget(request, conn, comic)

    @app.post("/reviews/{product_no}/dismiss")
    def dismiss(request: Request, product_no: int, conn=Depends(get_conn)):
        """추천 카드의 × 버튼. 카드는 화면에서 접히고(클라이언트), 순위는 다음 새로고침부터 빠진다."""
        comic_or_404(conn, product_no)
        db.update_review(conn, me(request).id, product_no, status="dismissed")
        return Response(status_code=204)

    @app.post("/reviews/{product_no}/undismiss")
    def undismiss(request: Request, product_no: int, conn=Depends(get_conn)):
        user = me(request)
        review = db.get_review(conn, user.id, product_no)
        if review and review.status == "dismissed":
            db.update_review(conn, user.id, product_no, status=None)
        return Response(status_code=204)

    # -- 설정 -------------------------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, conn=Depends(get_conn), msg: str = "", error: str = ""):
        user = me(request)
        return render(
            request,
            "settings.html",
            msg=msg,
            error=error,
            has_password=user.password_hash is not None,
            backup_dir=settings.backup_dir,
            last_backup_at=db.get_meta(conn, "last_backup_at"),
            backup_check=backup.last_check(conn),
            review_count=len(db.all_reviews(conn, user.id)),
            users=db.list_users(conn) if user.is_admin else [],
        )

    @app.post("/settings/password")
    def settings_password(
        request: Request,
        current: str = Form(""),
        new: str = Form(...),
        confirm: str = Form(...),
        conn=Depends(get_conn),
    ):
        user = me(request)
        if blocked := forbid_demo(request, "/settings", "데모 계정은 비밀번호를 바꿀 수 없어요"):
            return blocked
        if user.password_hash and not auth.verify_hash(current, user.password_hash):
            return RedirectResponse("/settings?error=지금 비밀번호가 달라요", status_code=303)
        if new != confirm:
            return RedirectResponse("/settings?error=새 비밀번호 두 칸이 달라요", status_code=303)
        try:
            epoch = auth.change_password(conn, user, new)
        except ValueError as exc:
            return RedirectResponse(f"/settings?error={exc}", status_code=303)
        request.session.update(user_id=user.id, epoch=epoch)  # 지금 세션만 살리고 나머지는 끊긴다
        return RedirectResponse("/settings?msg=비밀번호를 바꿨어요", status_code=303)

    @app.post("/settings/users")
    def settings_add_user(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        is_admin: str = Form(""),
        conn=Depends(get_conn),
    ):
        require_admin(request)
        try:
            auth.validate_new_password(password)
            created = db.create_user(conn, username, auth.hash_password(password), is_admin == "1")
        except ValueError as exc:
            return RedirectResponse(f"/settings?error={exc}", status_code=303)
        return RedirectResponse(
            f"/settings?msg={created.username} 을(를) 추가했어요", status_code=303
        )

    @app.get("/settings/export.json")
    def settings_export(request: Request, conn=Depends(get_conn)):
        user = me(request)
        data = backup.export_reviews(conn, user)
        filename = f"my-webtoon-{user.username}-{db.now_iso()[:10]}.json"
        return JSONResponse(
            data, headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    @app.post("/settings/import")
    async def settings_import(request: Request, file: UploadFile, conn=Depends(get_conn)):
        if blocked := forbid_demo(request, "/settings", "데모 계정에서는 평가를 가져올 수 없어요"):
            return blocked
        try:
            payload = json.loads(await file.read())
            imported, missing = backup.import_reviews(conn, me(request), payload)
        except (ValueError, KeyError) as exc:
            return RedirectResponse(f"/settings?error=가져오기 실패: {exc}", status_code=303)
        msg = f"평가 {imported}개를 가져왔어요"
        if missing:
            msg += f" · 아직 수집 안 된 {len(missing)}개는 수집 대기열에 넣었어요 (수집 후 다시 가져오기)"
        return RedirectResponse(f"/settings?msg={msg}", status_code=303)

    @app.post("/settings/backup")
    def settings_backup(request: Request):
        require_admin(request)
        if not settings.backup_dir:
            return RedirectResponse(
                "/settings?error=BACKUP_DIR 이 설정돼 있지 않아요", status_code=303
            )
        check = backup.backup_and_verify(settings.database_path, settings.backup_dir)
        if not check["ok"]:
            return RedirectResponse(
                f"/settings?error=백업은 만들었지만 검사에 실패했어요: {check['file']}",
                status_code=303,
            )
        return RedirectResponse(
            f"/settings?msg=백업하고 검사까지 마쳤어요: {check['file']}", status_code=303
        )

    # -- 크롤러 ----------------------------------------------------------------

    @app.get("/crawl", response_class=HTMLResponse)
    def crawl_status(request: Request, conn=Depends(get_conn)):
        return render(
            request,
            "crawl.html",
            stats=db.queue_stats(conn, settings.recrawl_after_days),
            runs=db.recent_runs(conn),
            running=crawler.running,
            settings=settings,
            seeds_at=db.get_meta(conn, "seeds_refreshed_at"),
            timings=request_log.summary(),
            timing_count=len(request_log),
            timing_since=datetime.fromtimestamp(request_log.started, UTC).isoformat(),
        )

    @app.post("/crawl/run")
    def crawl_run(request: Request):
        require_admin(request)
        if settings.crawl_enabled:
            crawler.trigger()
        else:
            threading.Thread(target=crawler.run_now, daemon=True).start()
        return RedirectResponse("/crawl", status_code=303)

    # -- 로그인 ----------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, error: str = "", conn=Depends(get_conn)):
        demo_user = db.get_user_by_name(conn, settings.demo_username)
        return render(
            request, "login.html", error=error, has_demo=bool(demo_user and demo_user.is_demo)
        )

    @app.post("/login")
    def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        conn=Depends(get_conn),
    ):
        user = auth.authenticate(conn, username, password)
        if user is None:
            return RedirectResponse("/login?error=1", status_code=303)
        request.session.update(user_id=user.id, epoch=user.session_epoch)
        return RedirectResponse("/", status_code=303)

    @app.get("/demo")
    def demo_login(request: Request, conn=Depends(get_conn)):
        """공유용 링크: 데모 계정으로 바로 로그인한다. 데모 계정은 망가뜨려도 매일 밤 되돌아간다(demo.py).

        데모로 지정된 계정(is_demo)일 때만 동작한다 — DEMO_USERNAME 을 실수로 진짜 계정으로 바꿔도
        그 계정이 열리지 않게. 다른 계정으로 로그인해 있었어도 데모 계정으로 바꾼다(링크가 늘 같은 화면을 열도록).
        """
        user = db.get_user_by_name(conn, settings.demo_username)
        if user is None or not user.is_demo:
            raise HTTPException(404, "데모 계정이 없어요")
        request.session.clear()
        request.session.update(user_id=user.id, epoch=user.session_epoch)
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/comics", status_code=303)

    @app.post("/theme")
    async def set_theme(request: Request):
        """HTMX 면 쿠키만 바꾼다(화면은 base.html 의 스크립트가 바로 바꾼다). JS 가 없으면 보던 화면으로 돌아간다."""
        form = await request.form()
        theme = form.get("theme", "")
        if theme not in THEMES:
            raise HTTPException(400, "모르는 테마예요")
        back = str(form.get("next") or "/")
        if not back.startswith("/") or back.startswith("//"):  # 다른 사이트로 보내지 않게
            back = "/"
        response = (
            Response(status_code=204)
            if request.headers.get("HX-Request")
            else RedirectResponse(back, status_code=303)
        )
        response.set_cookie(THEME_COOKIE, theme, max_age=60 * 60 * 24 * 365 * 2, samesite="lax")
        return response

    @app.get("/ui", response_class=HTMLResponse)
    def styleguide(request: Request, conn=Depends(get_conn)):
        """공통 컴포넌트 모음 (스타일 가이드). 새 화면을 만들 때 여기 있는 것만 쓴다."""
        comics, _ = db.list_comics(conn, limit=1)
        return render(request, "ui.html", sample=comics[0] if comics else None)

    @app.get("/healthz")
    def healthz():
        """살아 있는지 (livenessProbe). 프로세스가 응답하면 OK."""
        return {"ok": True}

    @app.get("/readyz")
    def readyz():
        """트래픽을 받아도 되는지 (readinessProbe). 추천 벡터 워밍업이 끝나야 200.
        무중단 배포에서 새 파드는 이게 200 이 된 뒤에야 Service 에 붙고, 그다음에 옛 파드가 내려간다."""
        if not ready.is_set():
            return JSONResponse({"ready": False}, status_code=503)
        return {"ready": True}

    return app


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------


def _opt_int(value) -> int | None:
    value = (value or "").strip() if isinstance(value, str) else value
    return int(value) if value not in (None, "") else None


def parse_review_form(form, current: db.Review | None) -> dict:
    """폼 → update_review() 에 넘길 변경분.

    빠른 위젯: 누른 버튼 하나의 값만 온다 (score=8 또는 status=seen).
               toggle=score 처럼 오면, 이미 같은 값일 때 다시 누른 것 → 그 칸을 비운다.
    세부 폼 (full=1): 모든 칸이 온다. 빈 칸은 '비움'.
    """
    changes: dict = {}
    if form.get("full"):
        changes["status"] = form.get("status") or None
        changes["score"] = _opt_int(form.get("score"))
        changes["aspects"] = {key: _opt_int(form.get(f"aspect_{key}")) for key in db.ASPECTS}
        changes["episodes_read"] = _opt_int(form.get("episodes_read"))
        changes["memo"] = form.get("memo", "")
        return changes

    if "score" in form:
        changes["score"] = _opt_int(form["score"])
    if "status" in form:
        changes["status"] = form["status"] or None
    if "episodes_read" in form:  # 소식 화면 '이어 보기'의 +1화 버튼
        changes["episodes_read"] = _opt_int(form["episodes_read"])
    toggle = form.get("toggle")
    if toggle in changes and current is not None and getattr(current, toggle) == changes[toggle]:
        changes[toggle] = None
    return changes


def options_from_query(request: Request) -> ScoringOptions:
    """URL 쿼리(?free_weight=1.5&sink=0 ...)로 점수 공식을 바꿔 볼 수 있게 한다."""
    q = request.query_params
    d = ScoringOptions()

    def num(name: str, default: float) -> float:
        try:
            return float(q[name]) if q.get(name) not in (None, "") else default
        except ValueError:
            return default

    tuned = "tuned" in q  # 폼을 한 번이라도 제출했으면 체크박스가 빠진 걸 'off'로 본다
    return ScoringOptions(
        free_weight=num("free_weight", d.free_weight),
        episode_weight=num("episode_weight", d.episode_weight),
        rating_weight=num("rating_weight", d.rating_weight),
        stingy_threshold=int(num("stingy_threshold", d.stingy_threshold)),
        sink_stingy=(q.get("sink") == "1") if tuned else d.sink_stingy,
        include_adult=(q.get("adult") == "1") if tuned else d.include_adult,
        diversity=min(1.0, max(0.0, num("diversity", d.diversity))),
        reviewed=q.get("reviewed") if q.get("reviewed") in REVIEWED_FILTERS else d.reviewed,
    )


def _query_without(request: Request, *keys: str) -> str:
    return urlencode([(k, v) for k, v in request.query_params.multi_items() if k not in keys])


def _query_dict(request: Request, *drop: str) -> dict:
    return {k: v for k, v in request.query_params.items() if k not in drop}


def bookmarklet(base_url: str) -> str:
    """네이버 시리즈 페이지에서 누르면, 그 페이지의 작품 번호(productNo)를 모아 이 앱의 가져오기 화면을 연다.

    내 브라우저에서 내 로그인으로 보고 있는 페이지를 읽는 것이라 서버는 네이버에 아무 요청도 하지 않는다.
    소설·이북 번호가 섞여도 괜찮다 — 수집된 만화만 미리보기에 나온다.
    """
    js = (
        "(()=>{const s=new Set();"
        "for(const m of document.documentElement.innerHTML.matchAll(/productNo=(\\d+)/g))s.add(m[1]);"
        "if(!s.size){alert('이 페이지에서 작품을 못 찾았어요. 시리즈 내 서재·최근 본 작품 페이지에서 눌러 주세요');return}"
        f"window.open('{base_url}/import/series?nos='+[...s].slice(0,{SERIES_IMPORT_MAX}).join(','))}})()"
    )
    return "javascript:" + js


def parse_product_no(text: str) -> int | None:
    """'https://series.naver.com/comic/detail.series?productNo=123' 또는 '123' → 123."""
    m = re.search(r"productNo=(\d+)", text) or re.fullmatch(r"\s*(\d+)\s*", text)
    return int(m.group(1)) if m else None


MOBILE_UA = re.compile(r"Android|iPhone|iPad|iPod|Mobile")


def theme_of(request: Request) -> str:
    theme = request.cookies.get(THEME_COOKIE, "system")
    return theme if theme in THEMES else "system"


def is_mobile(request: Request) -> bool:
    return bool(MOBILE_UA.search(request.headers.get("user-agent", "")))


def series_link(request: Request, comic) -> str:
    """'네이버 시리즈에서 보기' 링크.

    모바일이면 모바일 웹 상세(m.series.naver.com)로 보낸다. 그 페이지가 열리면서 네이버 쪽 스크립트가
    시리즈 앱을 바로 띄운다 (앱이 없으면 웹으로 남는다) — 실제 휴대폰에서 확인한 동작이라 따로 앱 링크를
    만들 필요가 없다. 데스크톱이면 PC 웹.
    """
    path = f"comic/detail.series?productNo={comic.product_no}"
    if is_mobile(request):
        return f"https://m.series.naver.com/{path}"
    return f"https://series.naver.com/{path}"


KST = timezone(timedelta(hours=9))  # 한국은 서머타임이 없어 고정 +9 로 충분하다


def kst(iso: str | None, fmt: str = "%m-%d %H:%M") -> str:
    """DB 의 UTC ISO 시각 → 한국 시각 문자열. 템플릿에서 {{ r.started_at | kst }}."""
    if not iso:
        return ""
    when = datetime.fromisoformat(iso)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(KST).strftime(fmt)


def format_stars(score) -> str:
    """0~10 점 → 별 개수 문자열. 9 → '4.5', 8 → '4', 7.5(세부 평균) → '3.8'."""
    if score is None:
        return ""
    stars = score / 2
    return f"{stars:.0f}" if stars == int(stars) else f"{stars:.1f}"


def reason_label(token: str) -> str:
    """추천 이유 토큰을 읽기 좋게: '장르:무협' → '무협', '작가:JP' → '작가 JP', '#회귀' 는 그대로."""
    kind, _, value = token.partition(":")
    if not value:
        return token
    return {"장르": value, "작가": f"작가 {value}", "출판사": value}.get(kind, value)


def free_tone(comic) -> str:
    """무료 배지 색: 전부 무료 → solid, 3화 이하(유료 위주) → warn, 그 밖 → accent."""
    if comic.is_all_free:
        return "solid"
    if comic.unit == "화" and (comic.free_episodes or 0) <= 3:
        return "warn"
    return "accent"


def static_version(static_dir: Path) -> str:
    """정적 파일 내용 해시 앞 8자리. 파일이 바뀌면 URL(?v=)이 바뀌어 브라우저 캐시를 피한다."""
    digest = hashlib.sha256()
    for path in sorted(static_dir.iterdir()):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:8]


def is_good_news(change) -> bool:
    """소식으로 보여 줄 만한 변화인가: 무료↑, 전부 무료가 됨, 완결, 새 회차, 기간 한정 무료."""
    field, old, new = change["field"], change["old_value"], change["new_value"]
    if field in ("free_episodes", "total_episodes"):
        return int(new or 0) > int(old or 0)
    if field == "is_all_free":
        return str(new) == "1"
    if field == "status":
        return new == "완결"
    return field == "promo"


def news_label(change, comic) -> str:
    field, old, new = change["field"], change["old_value"], change["new_value"]
    unit = comic.unit
    if field == "free_episodes":
        return f"무료 {old}→{new}{unit} (+{int(new) - int(old or 0)})"
    if field == "total_episodes":
        return f"새 {unit} {int(new) - int(old or 0)}개 (총 {new}{unit})"
    if field == "is_all_free":
        return f"전{unit} 무료가 됐어요"
    if field == "status":
        return "완결됐어요"
    return f"기간 한정: {new}"


def reading_progress(review, comic) -> dict | None:
    """'이어 보기' 계산. 본 화수를 기록했을 때만: 남은 화수, 그중 무료로 볼 수 있는 화수."""
    if review is None or review.episodes_read is None or not comic.total_episodes:
        return None
    read = min(review.episodes_read, comic.total_episodes)
    free_until = comic.total_episodes if comic.is_all_free else (comic.free_episodes or 0)
    return {
        "read": read,
        "left": comic.total_episodes - read,
        "free_left": max(0, free_until - read),
        "percent": round(100 * read / comic.total_episodes),
    }


def free_label(comic) -> str:
    if comic.is_all_free:
        return f"전{comic.unit} 무료"
    if comic.free_episodes:
        return f"{comic.free_episodes}{comic.unit} 무료"
    return "무료 없음"


def episodes_label(comic) -> str:
    return f"총 {comic.total_episodes}{comic.unit}" if comic.total_episodes else ""
