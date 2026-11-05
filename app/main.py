"""FastAPI アプリ本体。

    uvicorn app.main:app --reload
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.config import PROJECT_ROOT, get_settings
from app.db import connection
from app.jobs.scheduler import scheduler
from app.logging_setup import configure_logging
from app.security import redact

STATIC_DIR = PROJECT_ROOT / "static"

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    connection.init_db()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(
    title="Flight Price Predictor",
    description="出発日までの価格推移を予測し、買い時をガイドする",
    lifespan=lifespan,
    # 匿名の公開エンドポイントなので、本番では内部構造を広げない。
    # DOCS_ENABLED=false で /api/docs ごと消える。
    docs_url="/api/docs" if get_settings().docs_enabled else None,
    openapi_url="/api/openapi.json" if get_settings().docs_enabled else None,
    redoc_url=None,
)

app.include_router(api_router)


@app.middleware("http")
async def privacy_headers(request: Request, call_next) -> Response:
    """トークンが外に漏れる経路を塞ぐ。

    - `Referrer-Policy: no-referrer` — これが無いと、ページから読み込む
      外部リソースへの Referer ヘッダに `/w/{token}` がそのまま載る。
      Chart.js を CDN ではなく static/vendor に置いているのも同じ理由。
    - `X-Robots-Tag: noindex` — 共有 URL を検索エンジンに載せない。
    """
    response = await call_next(request)
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith(("/w/", "/l/", "/api/watches/",
                                    "/api/collections")):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        # 共有 URL がプロキシや CDN にキャッシュされないように。
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """想定外の例外。**パスをそのままログに出さない。**"""
    logger.exception("unhandled error at %s", redact(request.url.path))
    return JSONResponse(
        status_code=500,
        content={"detail": "サーバ側でエラーが発生しました"},
    )


@app.get("/w/{token}", include_in_schema=False)
def watch_page(token: str) -> FileResponse:
    """モニター画面。

    トークンの実在確認はここではしない。存在しないトークンでも同じ HTML を
    返し、ページ内の API 呼び出しで 404 を出す。ここで 404 を出し分けると、
    HTML が返るかどうかだけでトークンの実在を総当たりで判定できてしまう。
    """
    return FileResponse(
        STATIC_DIR / "watch.html",
        headers={"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"},
    )


@app.get("/l/{token}", include_in_schema=False)
def collection_page(token: str) -> FileResponse:
    """一覧の画面。**モニターと同じくトークンの実在を返り値で漏らさない。**

    存在しないトークンでも同じ HTML を返す。中身の取得は API 側で 404 に
    なるが、HTML の有無で総当たり判定はできない。
    """
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"},
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    """Service Worker を**ルート直下から**配る。

    **これは置き場所の趣味ではない。** Service Worker が制御できるのは
    自分が配信されたパス以下だけで、/static/sw.js だと scope が /static/ に
    なり、モニター画面（/w/{token}）を制御できない。通知の購読も成立しない。

    キャッシュさせない。worker を差し替えたのに古いものが動き続けると、
    直したはずの不具合が消えない。
    """
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store",
                 "Service-Worker-Allowed": "/"},
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
