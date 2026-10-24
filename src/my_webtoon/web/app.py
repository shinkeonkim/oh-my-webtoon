"""FastAPI 앱. 화면은 서버에서 HTML 을 렌더링하고(Jinja2), 별점 클릭 같은 작은 상호작용만
HTMX 로 부분 교체한다 — 프론트엔드 빌드 도구 없이 파이썬 코드만 읽으면 흐름이 다 보인다.

라우트 한눈에 보기:
    GET  /                   추천 목록 (+ 점수 조정 폼)
    GET  /rate?q=            검색해서 평가하기 (온보딩)
    POST /import             네이버 시리즈 URL/번호로 작품 바로 가져오기
    GET  /comic/{no}         작품 상세 + 비슷한 작품
    POST /ratings/{no}       평가 저장 (HTMX 가 별점 위젯만 갈아 끼운다)
    POST /ratings/{no}/delete
    GET  /me                 내 평가 기록
    GET  /crawl, POST /crawl/run   크롤러 상태 / 즉시 실행
    GET  /login, POST /login, POST /logout
    GET  /healthz
"""

from __future__ import annotations

import hmac
import logging
import re
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import db
from ..config import Settings, load_settings
from ..crawler import runner
from ..recommender import Engine, ScoringOptions
from ..recommender import scoring as scoring_mod

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
PUBLIC_PATHS = ("/login", "/static", "/healthz")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    engine = Engine()
    crawler = runner.BackgroundCrawler(settings.database_path, settings)
    crawler.on_finish = engine.invalidate

    with db.session(settings.database_path) as conn:
        db.init_schema(conn)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if settings.crawl_enabled:
            crawler.start()
        yield
        crawler.stop()

    app = FastAPI(title="my-webtoon", lifespan=lifespan)
    app.state.settings, app.state.engine, app.state.crawler = settings, engine, crawler
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(free_label=free_label, episodes_label=episodes_label)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    # -- 인증: APP_PASSWORD 가 설정돼 있을 때만 켠다 --------------------------------

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if settings.app_password and not request.url.path.startswith(PUBLIC_PATHS):
            if not request.session.get("authed"):
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
        ctx.setdefault("auth_enabled", bool(settings.app_password))
        return templates.TemplateResponse(request, name, ctx)

    # -- 추천 -----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, conn=Depends(get_conn), page: int = 0):
        opts = options_from_query(request)
        per_page = 30
        recs = engine.recommend(conn, opts, limit=per_page, offset=page * per_page)
        ratings = db.all_ratings(conn)
        return render(
            request,
            "index.html",
            recs=recs,
            ratings=ratings,
            opts=opts,
            page=page,
            has_next=len(recs) == per_page,
            stats=db.queue_stats(conn),
            popular=[] if recs else engine.popular(conn, opts),
            query_string=_query_without_page(request),
        )

    # -- 평가하기 (검색) ----------------------------------------------------------

    @app.get("/rate", response_class=HTMLResponse)
    def rate_page(request: Request, q: str = "", conn=Depends(get_conn), error: str = ""):
        results = db.search_comics(conn, q) if q.strip() else []
        opts = options_from_query(request)
        return render(
            request,
            "rate.html",
            q=q,
            results=results,
            ratings=db.all_ratings(conn),
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

    # -- 상세 -----------------------------------------------------------------

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
            rating=db.get_rating(conn, product_no),
            similar=engine.similar_to(conn, product_no, opts),
            ratings=db.all_ratings(conn),
            boost=scoring_mod.boost(comic, opts),
            stingy=scoring_mod.is_stingy(comic, opts),
        )

    # -- 평가 저장 (HTMX) ---------------------------------------------------------

    @app.post("/ratings/{product_no}", response_class=HTMLResponse)
    def save_rating(
        request: Request,
        product_no: int,
        kind: str = Form(...),
        score: int | None = Form(None),
        memo: str | None = Form(None),
        conn=Depends(get_conn),
    ):
        comic = db.get_comic(conn, product_no)
        if comic is None:
            raise HTTPException(404)
        try:
            db.set_rating(conn, product_no, kind, score, memo)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _rating_response(request, conn, comic)

    @app.post("/ratings/{product_no}/delete", response_class=HTMLResponse)
    def remove_rating(request: Request, product_no: int, conn=Depends(get_conn)):
        comic = db.get_comic(conn, product_no)
        if comic is None:
            raise HTTPException(404)
        db.delete_rating(conn, product_no)
        return _rating_response(request, conn, comic)

    def _rating_response(request: Request, conn, comic):
        if request.headers.get("HX-Request"):
            return render(
                request, "_rate.html", comic=comic, rating=db.get_rating(conn, comic.product_no)
            )
        return RedirectResponse(request.headers.get("referer") or "/", status_code=303)

    # -- 내 기록 ----------------------------------------------------------------

    @app.get("/me", response_class=HTMLResponse)
    def my_ratings(request: Request, conn=Depends(get_conn)):
        items = db.ratings_with_comics(conn)
        return render(request, "me.html", items=items)

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
    def login(request: Request, password: str = Form(...)):
        expected = settings.app_password or ""
        if hmac.compare_digest(password.encode(), expected.encode()):
            request.session["authed"] = True
            return RedirectResponse("/", status_code=303)
        return RedirectResponse("/login?error=1", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------


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


def _query_without_page(request: Request) -> str:
    return "&".join(f"{k}={v}" for k, v in request.query_params.multi_items() if k != "page")


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
