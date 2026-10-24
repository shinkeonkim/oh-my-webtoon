"""FastAPI 앱. 화면은 서버에서 HTML 을 렌더링하고(Jinja2), 점수 클릭 같은 작은 상호작용만
HTMX 로 부분 교체한다 — 프론트엔드 빌드 도구 없이 파이썬 코드만 읽으면 흐름이 다 보인다.

라우트 한눈에 보기 (🔓 = 로그인 없이 볼 수 있음)
    GET  /                       추천 목록 (+ 점수 공식 조정 폼)
    GET  /rate?q=                검색해서 평가하기 (온보딩)
    POST /import                 네이버 시리즈 URL/번호로 작품 바로 가져오기
    GET  /comics                 🔓 수집된 작품 목록 (검색·장르·정렬)
    GET  /comic/{no}             🔓 작품 상세 + 비슷한 작품
    GET  /me                     🔓 평가 기록 (메모는 로그인했을 때만)
    POST /reviews/{no}           평가 저장 — 바꾼 칸만 반영 (HTMX 가 위젯만 갈아 끼운다)
    GET  /reviews/{no}/edit      세부 평가 폼 (스토리·작화·…, 본 화수, 메모)
    GET  /reviews/{no}/widget    폼 닫기 → 간단 위젯
    POST /reviews/{no}/delete    평가 전체 삭제
    GET  /settings               비밀번호 변경, 평가 내보내기/가져오기, 백업
    GET  /crawl, POST /crawl/run 크롤러 상태 / 즉시 실행
    GET  /login, POST /login, POST /logout, GET /healthz   🔓
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

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
PUBLIC_GET = ("/comics", "/comic/", "/me")  # 읽기 전용 공개 화면
PER_PAGE = 30


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    engine = Engine()
    crawler = runner.BackgroundCrawler(settings.database_path, settings)
    crawler.on_finish = engine.invalidate
    stop_backup = threading.Event()

    with db.session(settings.database_path) as conn:
        db.init_schema(conn)

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
        episodes_label=episodes_label,
        STATUSES=db.STATUSES,
        ASPECTS=db.ASPECTS,
        SCORES=list(db.SCORE_RANGE),
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    # -- 인증 -----------------------------------------------------------------
    # 비밀번호(APP_PASSWORD 또는 /settings 에서 바꾼 해시)가 있을 때만 켠다.
    # 세션의 epoch 가 DB 의 epoch 와 다르면(=다른 곳에서 비밀번호를 바꿨으면) 로그아웃된 것으로 본다.

    def is_authed(request: Request, conn) -> bool:
        if not auth.auth_enabled(conn, settings.app_password):
            return True
        return bool(request.session.get("authed")) and request.session.get(
            "epoch", "0"
        ) == auth.session_epoch(conn)

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        public = path.startswith(ALWAYS_PUBLIC) or (
            request.method == "GET" and path.startswith(PUBLIC_GET)
        )
        with db.session(settings.database_path) as conn:
            request.state.authed = is_authed(request, conn)
            request.state.auth_enabled = auth.auth_enabled(conn, settings.app_password)
        if not public and not request.state.authed:
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

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.setdefault("authed", request.state.authed)
        ctx.setdefault("auth_enabled", request.state.auth_enabled)
        return templates.TemplateResponse(request, name, ctx)

    # -- 추천 -----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, conn=Depends(get_conn), page: int = 0):
        opts = options_from_query(request)
        recs = engine.recommend(conn, opts, limit=PER_PAGE, offset=page * PER_PAGE)
        reviews = db.all_reviews(conn)
        return render(
            request,
            "index.html",
            recs=recs,
            reviews=reviews,
            opts=opts,
            page=page,
            rank_offset=page * PER_PAGE,
            has_next=len(recs) == PER_PAGE,
            stats=db.queue_stats(conn),
            popular=[] if recs else engine.popular(conn, opts),
            query_string=_query_without(request, "page"),
        )

    # -- 평가하기 (검색 + 가져오기) -------------------------------------------------

    @app.get("/rate", response_class=HTMLResponse)
    def rate_page(request: Request, q: str = "", conn=Depends(get_conn), error: str = ""):
        results = db.search_comics(conn, q) if q.strip() else []
        opts = options_from_query(request)
        return render(
            request,
            "rate.html",
            q=q,
            results=results,
            reviews=db.all_reviews(conn),
            popular=[] if q.strip() else engine.popular(conn, opts, limit=24),
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
                    settings.database_path, product_no, user_agent=settings.user_agent
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

    # -- 공개 화면: 수집 목록 / 상세 / 평가 기록 --------------------------------------

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
        return render(
            request,
            "comics.html",
            comics=comics,
            total=total,
            q=q,
            genre=genre,
            sort=sort,
            page=page,
            has_next=(page + 1) * PER_PAGE < total,
            genres=db.genres(conn),
            sorts=db.COMIC_SORTS,
            reviews=db.all_reviews(conn),
            query_string=_query_without(request, "page"),
        )

    @app.get("/comic/{product_no}", response_class=HTMLResponse)
    def comic_detail(request: Request, product_no: int, conn=Depends(get_conn)):
        comic = db.get_comic(conn, product_no)
        if comic is None:
            raise HTTPException(404, "아직 수집하지 않은 작품이에요")
        opts = options_from_query(request)
        return render(
            request,
            "comic.html",
            comic=comic,
            review=db.get_review(conn, product_no),
            similar=engine.similar_to(conn, product_no, opts),
            reviews=db.all_reviews(conn),
            boost=scoring_mod.boost(comic, opts),
            stingy=scoring_mod.is_stingy(comic, opts),
        )

    @app.get("/me", response_class=HTMLResponse)
    def my_reviews(request: Request, conn=Depends(get_conn), status: str = ""):
        items = db.reviews_with_comics(conn)
        total = len(items)
        counts = {key: sum(1 for r, _ in items if r.status == key) for key in db.STATUSES}
        if status == "none":
            items = [(r, c) for r, c in items if r.status is None]
        elif status:
            items = [(r, c) for r, c in items if r.status == status]
        # 점수 높은 순, 점수 없는 건 뒤로 (최근 수정순 유지)
        items.sort(key=lambda rc: -(rc[0].overall if rc[0].overall is not None else -1))
        return render(request, "me.html", items=items, status=status, counts=counts, total=total)

    # -- 평가 저장 (HTMX) ---------------------------------------------------------

    def review_widget(request: Request, conn, comic, template: str = "_review.html"):
        return render(request, template, comic=comic, review=db.get_review(conn, comic.product_no))

    def comic_or_404(conn, product_no: int):
        comic = db.get_comic(conn, product_no)
        if comic is None:
            raise HTTPException(404)
        return comic

    @app.post("/reviews/{product_no}", response_class=HTMLResponse)
    async def save_review(request: Request, product_no: int, conn=Depends(get_conn)):
        comic = comic_or_404(conn, product_no)
        form = await request.form()
        try:
            changes = parse_review_form(form, db.get_review(conn, product_no))
            db.update_review(conn, product_no, **changes)
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
        db.delete_review(conn, product_no)
        if not request.headers.get("HX-Request"):
            return RedirectResponse(request.headers.get("referer") or "/", status_code=303)
        return review_widget(request, conn, comic)

    # -- 설정: 비밀번호 / 내보내기·가져오기 / 백업 -------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, conn=Depends(get_conn), msg: str = "", error: str = ""):
        return render(
            request,
            "settings.html",
            msg=msg,
            error=error,
            password_in_db=db.get_meta(conn, "password_hash") is not None,
            backup_dir=settings.backup_dir,
            last_backup_at=db.get_meta(conn, "last_backup_at"),
            review_count=len(db.all_reviews(conn)),
        )

    @app.post("/settings/password")
    def settings_password(
        request: Request,
        current: str = Form(""),
        new: str = Form(...),
        confirm: str = Form(...),
        conn=Depends(get_conn),
    ):
        if auth.auth_enabled(conn, settings.app_password) and not auth.check_password(
            conn, settings.app_password, current
        ):
            return RedirectResponse("/settings?error=지금 비밀번호가 달라요", status_code=303)
        if new != confirm:
            return RedirectResponse("/settings?error=새 비밀번호 두 칸이 달라요", status_code=303)
        try:
            epoch = auth.change_password(conn, new)
        except ValueError as exc:
            return RedirectResponse(f"/settings?error={exc}", status_code=303)
        request.session.update(authed=True, epoch=epoch)  # 지금 세션만 살리고 나머지는 끊긴다
        return RedirectResponse("/settings?msg=비밀번호를 바꿨어요", status_code=303)

    @app.get("/settings/export.json")
    def settings_export(conn=Depends(get_conn)):
        data = backup.export_reviews(conn)
        filename = f"my-webtoon-reviews-{db.now_iso()[:10]}.json"
        return JSONResponse(
            data, headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    @app.post("/settings/import")
    async def settings_import(file: UploadFile, conn=Depends(get_conn)):
        try:
            payload = json.loads(await file.read())
            imported, missing = backup.import_reviews(conn, payload)
        except (ValueError, KeyError) as exc:
            return RedirectResponse(f"/settings?error=가져오기 실패: {exc}", status_code=303)
        msg = f"평가 {imported}개를 가져왔어요"
        if missing:
            msg += f" · 아직 수집 안 된 {len(missing)}개는 수집 대기열에 넣었어요 (수집 후 다시 가져오기)"
        return RedirectResponse(f"/settings?msg={msg}", status_code=303)

    @app.post("/settings/backup")
    def settings_backup():
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
            stats=db.queue_stats(conn),
            runs=db.recent_runs(conn),
            running=crawler.running,
            settings=settings,
            seeds_at=db.get_meta(conn, "seeds_refreshed_at"),
        )

    @app.post("/crawl/run")
    def crawl_run():
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
    def login(request: Request, password: str = Form(...), conn=Depends(get_conn)):
        if auth.check_password(conn, settings.app_password, password):
            request.session.update(authed=True, epoch=auth.session_epoch(conn))
            return RedirectResponse("/", status_code=303)
        return RedirectResponse("/login?error=1", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/comics", status_code=303)

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


def free_label(comic) -> str:
    if comic.is_all_free:
        return f"전{comic.unit} 무료"
    if comic.free_episodes:
        return f"{comic.free_episodes}{comic.unit} 무료"
    return "무료 없음"


def episodes_label(comic) -> str:
    return f"총 {comic.total_episodes}{comic.unit}" if comic.total_episodes else ""
