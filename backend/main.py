"""
TransformerProcure Intelligence - application entrypoint.

    python backend/main.py            # serve on http://127.0.0.1:8000
    python backend/main.py --seed     # (re)build the demo dataset first
    python backend/main.py --no-serve # bootstrap only
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

# allow `python backend/main.py` as well as `python -m backend.main`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Request                             # noqa: E402
from fastapi.middleware.cors import CORSMiddleware               # noqa: E402
from fastapi.responses import (FileResponse, HTMLResponse,        # noqa: E402
                               JSONResponse, Response)
from fastapi.staticfiles import StaticFiles                      # noqa: E402

from backend.config import settings                              # noqa: E402
from backend.database import init_db                             # noqa: E402
from backend.utils.logging_config import get_logger              # noqa: E402

log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("%s %s starting - data mode %s", settings.APP_NAME,
             settings.APP_VERSION, settings.DATA_MODE)
    init_db()
    if settings.is_demo:
        from backend.services.seed_data import run_seed
        info = run_seed()
        if info["prices"]:
            log.info("seeded %s demo price rows", info["prices"])
    _bootstrap_forecasts_if_empty()
    from backend.scheduler import start_scheduler, stop_scheduler
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()
        log.info("shutdown complete")


def _bootstrap_forecasts_if_empty() -> None:
    """
    Train the models once, in a background thread, when nothing is stored yet.

    Back-testing the whole zoo takes minutes. Doing it inline would block
    startup; not doing it at all leaves every forecast panel empty on a fresh
    install until the first weekly retrain fires.
    """
    if not settings.BOOTSTRAP_FORECASTS:
        return
    import threading

    from sqlalchemy import func, select

    from backend.database import SessionLocal
    from backend.models import Forecast

    with SessionLocal() as db:
        if db.execute(select(func.count()).select_from(Forecast)).scalar_one():
            return

    def _run():
        try:
            from backend.services.forecast_service import refresh_forecasts
            log.info("no stored forecasts - training models in the background "
                     "(this takes a few minutes; the rest of the app is usable now)")
            out = refresh_forecasts()
            log.info("forecast bootstrap finished: %d ok, %d failed",
                     len(out["succeeded"]), len(out["failed"]))
        except Exception:                                        # noqa: BLE001
            log.exception("forecast bootstrap failed - retry with POST /api/forecast/refresh")

    threading.Thread(target=_run, name="forecast-bootstrap", daemon=True).start()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=("Commodity price tracking, forecasting and transformer BOM cost "
                 "exposure for power and solar transformer procurement."),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def timing_and_errors(request: Request, call_next):
    t0 = time.time()
    try:
        response = await call_next(request)
    except Exception as exc:                                     # noqa: BLE001
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error",
                     "detail": str(exc) if settings.DEBUG else
                     "An internal error occurred. Check the server log.",
                     "path": request.url.path})
    response.headers["X-Process-Time-ms"] = f"{(time.time() - t0) * 1000:.1f}"
    response.headers["X-Data-Mode"] = settings.DATA_MODE
    return response


# ---- routers ---------------------------------------------------------------
from backend.api import (routes_bom, routes_data, routes_forecast,      # noqa: E402
                         routes_market, routes_meta, routes_procurement,
                         routes_quotes)

for r in (routes_meta.router, routes_market.router, routes_forecast.router,
          routes_bom.router, routes_procurement.router, routes_quotes.router,
          routes_data.router):
    app.include_router(r)


# ---- frontend --------------------------------------------------------------
class RevalidatingStatic(StaticFiles):
    """
    Static files that must be revalidated on every request.

    Browsers cache /js/app.js aggressively. After an update, an already-open tab
    keeps executing the old script against the new API and silently misbehaves -
    the kind of fault that looks like "the feature just doesn't work" and is
    very hard to diagnose from a user's description. `no-cache` still allows a
    304 via ETag, so the cost is a conditional request, not a re-download.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


def _no_store(response):
    response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


def _versioned_html(path: Path) -> Response:
    """
    Serve an HTML page with a cache-busting stamp on its own js/css URLs.

    `no-cache` headers only govern requests the browser actually makes; a tab
    that already holds app.js in its memory cache will happily keep running the
    old script against a new API. Stamping the URL changes the cache key, so an
    upgrade is guaranteed to load. The stamp combines the app version with the
    newest asset mtime, so it also picks up edits during development without a
    version bump.
    """
    try:
        newest = max((f.stat().st_mtime for d in ("js", "css")
                      for f in (FRONTEND / d).glob("*") if f.is_file()), default=0)
    except OSError:
        newest = 0
    stamp = f"{settings.APP_VERSION}-{int(newest)}"
    html = path.read_text(encoding="utf-8")
    html = re.sub(r'(src|href)="(/(?:js|css)/[^"?]+)"',
                  rf'\1="\2?v={stamp}"', html)
    return _no_store(HTMLResponse(html))


FRONTEND = Path(settings.FRONTEND_DIR)
if FRONTEND.exists():
    app.mount("/css", RevalidatingStatic(directory=FRONTEND / "css"), name="css")
    app.mount("/js", RevalidatingStatic(directory=FRONTEND / "js"), name="js")
    if (FRONTEND / "assets").exists():
        # Vendored third-party assets change only when explicitly replaced.
        app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return _versioned_html(FRONTEND / "index.html")

    @app.get("/standalone.html", include_in_schema=False)
    def standalone():
        return _no_store(FileResponse(FRONTEND / "standalone.html"))

    @app.get("/procurement_kpi.html", include_in_schema=False)
    def kpi_page():
        return _versioned_html(FRONTEND / "procurement_kpi.html")


def main() -> None:
    ap = argparse.ArgumentParser(description=settings.APP_NAME)
    ap.add_argument("--seed", action="store_true", help="rebuild the demo dataset")
    ap.add_argument("--force-seed", action="store_true", help="wipe and rebuild prices")
    ap.add_argument("--forecast", action="store_true", help="run a forecast retrain now")
    ap.add_argument("--no-serve", action="store_true", help="bootstrap and exit")
    ap.add_argument("--host", default=settings.HOST)
    ap.add_argument("--port", type=int, default=settings.PORT)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    init_db()
    if args.seed or args.force_seed:
        from backend.services.seed_data import run_seed
        print("seeding...", flush=True)
        info = run_seed(force=args.force_seed)
        print(f"  price rows: {info['prices']}, transformers: {info['transformers']}")
    if args.forecast:
        from backend.services.forecast_service import refresh_forecasts
        print("training forecast models (this takes a few minutes)...", flush=True)
        out = refresh_forecasts()
        print(f"  run {out['run_id']}: {len(out['succeeded'])} ok, {len(out['failed'])} failed")
    if args.no_serve:
        return

    import uvicorn
    print(f"\n  {settings.APP_NAME} {settings.APP_VERSION}")
    print(f"  Dashboard : http://{args.host}:{args.port}/")
    print(f"  API docs  : http://{args.host}:{args.port}/docs")
    print(f"  Data mode : {settings.DATA_MODE}\n")
    uvicorn.run("backend.main:app" if args.reload else app,
                host=args.host, port=args.port, reload=args.reload,
                log_level=settings.LOG_LEVEL.lower())


if __name__ == "__main__":
    main()
