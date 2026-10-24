"""FastAPI 앱. 화면은 서버에서 HTML 을 렌더링하고(Jinja2), 점수 클릭 같은 작은 상호작용만
HTMX 로 부분 교체한다 — 프론트엔드 빌드 도구 없이 파이썬 코드만 읽으면 흐름이 다 보인다.

데이터 경계: 수집한 작품(comics)은 모두가 공유하고, 평가(reviews)와 추천은 로그인한 사용자 것만 쓴다.

라우트 한눈에 보기 (🔓 = 로그인 없이 볼 수 있음, 👑 = 관리자)
    GET  /                       내 추천 목록 (무한 스크롤: HX-Request 면 다음 페이지 조각만)
    GET  /rate?q=                검색해서 평가하기 (온보딩)
    POST /import                 네이버 시리즈 URL/번호로 작품 바로 가져오기
    GET  /comics                 🔓 수집된 작품 목록 (검색·장르·정렬, 무한 스크롤)
    GET  /comic/{no}             🔓 작품 상세 + 비슷한 작품
    GET  /users                  🔓 사용자 목록
    GET  /users/{name}           🔓 그 사용자의 평가 기록 (상태·점수 필터, 메모는 본인만)
    GET  /me                     → /users/{내 아이디}
    POST /reviews/{no}           내 평가 저장 — 바꾼 칸만 반영 (HTMX 가 위젯만 갈아 끼운다)
    GET  /reviews/{no}/edit      세부 평가 폼 (스토리·작화·…, 본 화수, 메모)
    GET  /reviews/{no}/widget    폼 닫기 → 간단 위젯
    POST /reviews/{no}/delete    내 평가 전체 삭제
    GET  /settings               비밀번호 변경, 평가 내보내기/가져오기, 👑 사용자 추가·백업
    GET  /crawl, POST /crawl/run 크롤러 상태 / 👑 즉시 실행
    GET  /login, POST /login, POST /logout, GET /healthz   🔓
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import backup, db
from ..config import Settings, load_settings
from ..crawler import runner
from ..recommender import Engine, ScoringOptions
from ..recommender import scoring as scoring_mod
from . import auth

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
ALWAYS_PUBLIC = ("/login", "/static", "/healthz")
PUBLIC_GET = ("/comics", "/comic/", "/users", "/ui")  # 읽기 전용 공개 화면
PER_PAGE = 30
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
    engine = Engine()
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

    def backup_loop() -> None:
        while not stop_backup.is_set():
            try:
                with db.session(settings.database_path) as conn:
                    due = backup.backup_due(conn)
                if due:
                    backup.run_backup(settings.database_path, settings.backup_dir)
            except Exception:
                log.exception("자동 백업 실패")
            stop_backup.wait(timeout=3600)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if settings.crawl_enabled:
            crawler.start()
        if settings.backup_dir:
            threading.Thread(target=backup_loop, name="backup", daemon=True).start()
        yield
        crawler.stop()
        stop_backup.set()

    app = FastAPI(title="my-webtoon", lifespan=lifespan)
    app.state.settings, app.state.engine, app.state.crawler = settings, engine, crawler
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        free_label=free_label,
        free_tone=free_tone,
        episodes_label=episodes_label,
        series_link=series_link,
        format_stars=format_stars,
        reason_label=reason_label,
        static_version=static_version(HERE / "static"),
        STATUSES=db.STATUSES,
        ASPECTS=db.ASPECTS,
        SCORES=list(db.SCORE_RANGE),
    )
    templates.env.filters.update(
        star_option=lambda v: (v, f"★{format_stars(v)}"),  # 셀렉트용 (값, 라벨)
        pair=lambda v: (v, v),
        sort_option=lambda item: (item[0], item[1][0]),
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

    def my_id(request: Request) -> int | None:
        return request.state.user.id if request.state.user else None

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        user = request.state.user
        ctx.setdefault("user", user)
        ctx.setdefault("authed", user is not None)
        ctx.setdefault("can_logout", not request.state.open_mode)
        return templates.TemplateResponse(request, name, ctx)

    # -- 추천 -----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, conn=Depends(get_conn), page: int = 0):
        user = me(request)
        opts = options_from_query(request)
        recs = engine.recommend(conn, user.id, opts, limit=PER_PAGE, offset=page * PER_PAGE)
        ctx = dict(
            recs=recs,
            reviews=db.all_reviews(conn, user.id),
            opts=opts,
            page=page,
            rank_offset=page * PER_PAGE,
            has_next=len(recs) == PER_PAGE,
            query_string=_query_without(request, "page"),
        )
        if request.headers.get("HX-Request") and page > 0:
            return render(request, "_rec_page.html", **ctx)  # 무한 스크롤: 다음 30개 + 다음 센티넬
        return render(
            request,
            "index.html",
            stats=db.queue_stats(conn),
            popular=[] if recs else engine.popular(conn, opts, user.id),
            **ctx,
        )

    # -- 평가하기 (검색 + 가져오기) -------------------------------------------------

    @app.get("/rate", response_class=HTMLResponse)
    def rate_page(request: Request, q: str = "", conn=Depends(get_conn), error: str = ""):
        user = me(request)
        results = db.search_comics(conn, q) if q.strip() else []
        opts = options_from_query(request)
        return render(
            request,
            "rate.html",
            q=q,
            results=results,
            reviews=db.all_reviews(conn, user.id),
            popular=[] if q.strip() else engine.popular(conn, opts, user.id, limit=24),
            # 서버가 네이버 검색을 대신할 수는 없다(robots.txt 가 /search 를 막음).
            # 대신 내 브라우저에서 검색 결과를 열어 링크를 복사해 오게 한다.
            naver_search_url=(
                "https://series.naver.com/search/search.series?t=all&fs=comic&q=" + quote(q)
                if q.strip()
                else None
            ),
            error=error,
        )

    @app.post("/import")
    def import_comic(url: str = Form(...), conn=Depends(get_conn)):
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

    # -- 공개 화면: 수집 목록 / 상세 / 사용자별 평가 기록 ---------------------------------

    @app.get("/comics", response_class=HTMLResponse)
    def comics_page(
        request: Request,
        conn=Depends(get_conn),
        q: str = "",
        genre: str = "",
        sort: str = "popular",
        page: int = 0,
    ):
        comics, total = db.list_comics(
            conn, query=q, genre=genre, sort=sort, limit=PER_PAGE, offset=page * PER_PAGE
        )
        ctx = dict(
            comics=comics,
            page=page,
            has_next=(page + 1) * PER_PAGE < total,
            reviews=db.all_reviews(conn, my_id(request)),
            query_string=_query_without(request, "page"),
        )
        if request.headers.get("HX-Request") and page > 0:
            return render(request, "_comics_page.html", **ctx)
        return render(
            request,
            "comics.html",
            total=total,
            q=q,
            genre=genre,
            sort=sort,
            genres=db.genres(conn),
            sorts=db.COMIC_SORTS,
            **ctx,
        )

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
        path = backup.run_backup(settings.database_path, settings.backup_dir)
        return RedirectResponse(f"/settings?msg=백업했어요: {path.name}", status_code=303)

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
    def login_page(request: Request, error: str = ""):
        return render(request, "login.html", error=error)

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

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/comics", status_code=303)

    @app.get("/ui", response_class=HTMLResponse)
    def styleguide(request: Request, conn=Depends(get_conn)):
        """공통 컴포넌트 모음 (스타일 가이드). 새 화면을 만들 때 여기 있는 것만 쓴다."""
        comics, _ = db.list_comics(conn, limit=1)
        return render(request, "ui.html", sample=comics[0] if comics else None)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

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
    )


def _query_without(request: Request, key: str) -> str:
    return urlencode([(k, v) for k, v in request.query_params.multi_items() if k != key])


def parse_product_no(text: str) -> int | None:
    """'https://series.naver.com/comic/detail.series?productNo=123' 또는 '123' → 123."""
    m = re.search(r"productNo=(\d+)", text) or re.fullmatch(r"\s*(\d+)\s*", text)
    return int(m.group(1)) if m else None


SERIES_APP_PACKAGE = "com.nhn.android.nbooks"  # 네이버 시리즈 안드로이드 앱


def series_link(request: Request, comic) -> str:
    """'네이버 시리즈에서 보기' 링크. 기기에 따라 다르게:

    - 안드로이드: 시리즈 앱을 지정한 intent:// 링크. 앱이 이 주소를 받으면 앱에서 열리고,
      아니면(또는 앱이 없으면) browser_fallback_url 의 모바일 웹으로 간다.
    - 아이폰 등 다른 모바일: 모바일 웹 상세 (그 페이지의 '앱으로 보기' 버튼이 앱을 연다).
      시리즈 앱이 유니버설 링크를 등록해 두지 않아서 바로 앱으로 보낼 확실한 방법이 없다.
    - 데스크톱: PC 웹.
    """
    ua = request.headers.get("user-agent", "")
    path = f"comic/detail.series?productNo={comic.product_no}"
    mobile_url = f"https://m.series.naver.com/{path}"
    if "Android" in ua:
        return (
            f"intent://m.series.naver.com/{path}#Intent;scheme=https;package={SERIES_APP_PACKAGE};"
            f"S.browser_fallback_url={quote(mobile_url, safe='')};end"
        )
    if re.search(r"iPhone|iPad|Mobile", ua):
        return mobile_url
    return f"https://series.naver.com/{path}"


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


def free_label(comic) -> str:
    if comic.is_all_free:
        return f"전{comic.unit} 무료"
    if comic.free_episodes:
        return f"{comic.free_episodes}{comic.unit} 무료"
    return "무료 없음"


def episodes_label(comic) -> str:
    return f"총 {comic.total_episodes}{comic.unit}" if comic.total_episodes else ""
