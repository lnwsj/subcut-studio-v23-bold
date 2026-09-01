"""Standalone FastAPI application for SJ88 SubCut Studio."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import auth, hub_account, hub_jobs, member_admin, subcut_api, subtitle_editor, subtitle_trim, trim_preview
from .config import (
    CORS_ORIGINS,
    DB_ENGINE,
    DB_PATH,
    ENABLE_EMBEDDED_WORKER,
    STATIC_DIR,
    WORKER_MAX_CONCURRENCY,
    WORKER_POLL_INTERVAL,
)
from .services.job_store import JobStore
from .services.subcut_worker import SubCutWorker


logger = logging.getLogger("sj88.subcut")
store = JobStore(DB_PATH)
workers: list[SubCutWorker] = []
if ENABLE_EMBEDDED_WORKER:
    base_id = os.getenv("APP_WORKER_ID", "").strip() or f"subcut-web:{os.getpid()}"
    for index in range(max(1, WORKER_MAX_CONCURRENCY)):
        workers.append(
            SubCutWorker(
                store,
                poll_interval=WORKER_POLL_INTERVAL,
                worker_id=f"{base_id}:{index + 1}",
            )
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if workers:
        try:
            recovered = store.recover_expired_leases()
            if recovered:
                logger.warning("Recovered %d expired job lease(s)", len(recovered))
        except Exception:
            logger.exception("Lease recovery failed")
        for worker in workers:
            if not worker.is_alive():
                worker.start()
        logger.info("Started %d SubCut worker(s)", len(workers))
    yield
    for worker in workers:
        worker.stop()
    for worker in workers:
        if worker.is_alive():
            worker.join(timeout=10.0)
    if workers:
        logger.info("SubCut workers stopped")


app = FastAPI(
    title="SJ88 SubCut Studio",
    version="2.3.0",
    description="Bold job hub with resumable uploads, synced devices, notifications, library, and background processing.",
    lifespan=lifespan,
)


@app.middleware("http")
async def frontend_no_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in {"/", "/index.html", "/static/app.js", "/static/app.css", "/static/bold-core.js", "/static/bold-views.js", "/static/upload-dock.js", "/static/preview.js", "/static/bold.css", "/static/subtitle-editor.js", "/static/subtitle-editor.css"}:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.middleware("http")
async def request_id_echo(request: Request, call_next):
    request_id = str(request.headers.get("X-Request-ID") or "").strip()
    response = await call_next(request)
    if request_id:
        response.headers["X-Request-ID"] = request_id
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=CORS_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth.router)
app.include_router(member_admin.router)
app.include_router(subtitle_trim.router)
app.include_router(subcut_api.router)
app.include_router(hub_jobs.router)
app.include_router(hub_account.router)
app.include_router(subtitle_editor.router)
app.include_router(trim_preview.router)


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "sj88-subcut-studio",
        "version": "2.3.0",
        "database": DB_ENGINE,
        "embedded_workers": len(workers),
        "modes": ["autosu_only", "silence_trim_only"],
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise RuntimeError(f"Frontend index is missing: {index_path}")
    return FileResponse(str(index_path), media_type="text/html")


@app.get("/index.html", include_in_schema=False)
def index_html() -> FileResponse:
    return index()


@app.exception_handler(404)
async def not_found_handler(request: Request, _exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path), media_type="text/html")
    return JSONResponse({"detail": "Not found"}, status_code=404)
