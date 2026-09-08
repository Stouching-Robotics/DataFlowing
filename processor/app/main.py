"""FastAPI application entry point — 纯本地文件存储,不依赖 PostgreSQL。"""

import asyncio
import mimetypes
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from app.config import settings
from app.routes import ingestion, video, export, pages, session, annotations, dashboard, devices, auth
from app.api import workflows, worker, projects, exceptions
from app.api import users
from app.middleware import AuthMiddleware
from app.paths import STATIC_DIR


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Metadata remains file-backed for compatibility, while EGOData stores
    # managed accounts and future relational metadata.
    try:
        from app.database import init_db
        await init_db()
    except Exception as exc:
        # Keep the legacy file-backed application available during a temporary
        # database/tunnel outage; auth.py will use its JSON fallback.
        print(f"[Startup] Database initialization skipped: {exc}")
    from app import localstore
    localstore.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        if localstore.migrate_workflow_definitions():
            print("[Startup] Migrated workflow node types to the canonical RGB/RGB-D schema")
    except Exception as exc:
        # The API/worker also accept legacy aliases, so a migration failure is
        # non-fatal and can be retried on the next startup.
        print(f"[Startup] Workflow type migration skipped: {exc}")
    # Do the one potentially slow storage scan before accepting requests.  This
    # keeps a cold network-mounted data directory from becoming the first page
    # load seen by a user after a service restart.  A transient storage issue
    # must not prevent the compatibility service from starting.
    try:
        await asyncio.to_thread(localstore.scan_sessions)
    except Exception as exc:
        print(f"[Startup] Session cache warm-up skipped: {exc}")
    threading.Thread(
        target=localstore.warm_metadata_caches,
        name="metadata-cache-warmup",
        daemon=True,
    ).start()
    from app import upload_queue
    upload_queue.start()
    print(f"[Startup] processor Service on port {settings.PORT}")
    print(f"[Startup] Storage: {settings.STORAGE_DIR} ({settings.STORAGE_BACKEND})")
    try:
        yield
    finally:
        upload_queue.stop()
        print("[Shutdown] Done")


app = FastAPI(
    title="processor Service",
    version="0.1.0",
    lifespan=lifespan,
)

# GZip compression — JSON responses shrink ~10×
app.add_middleware(GZipMiddleware, minimum_size=500)

# Auth middleware — protect routes (after gzip, before everything else)
app.add_middleware(AuthMiddleware)

# 媒体/压缩文件下载不走 gzip:zip/mp4/parquet/hdf5 本身已是压缩格式,
# 再压一遍只是烧 CPU(zlib level 9 压缩 180MB ≈ 10s),而且流式压缩会
# 删掉 Content-Length —— 浏览器下载进度显示"未知大小",大文件看起来
# 像卡死。这些路径直接透传原始字节(去掉 accept-encoding 头即可)。
_NO_COMPRESS_PATH_HINTS = (
    "download", "stream", "depth-codes", "depth-preview", "skeleton", "hdf5",
)


@app.middleware("http")
async def no_compress_media(request, call_next):
    if any(hint in request.url.path for hint in _NO_COMPRESS_PATH_HINTS):
        request.scope["headers"] = [
            (key, value) for key, value in request.scope["headers"]
            if key.lower() != b"accept-encoding"
        ]
    return await call_next(request)

# Static JS/CSS is versioned (workflow assets use content hashes; the legacy
# page scripts use a ``?v=...`` query).  Tell the browser to retain these
# resources across full page navigation instead of revalidating every common
# asset on each switch between Projects, Review and Workflow Studio.
@app.middleware("http")
async def static_cache_headers(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/"):
        if "/assets/" in path and "/workflow-studio/" in path:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif request.url.query:
            response.headers["Cache-Control"] = "public, max-age=86400"
        else:
            response.headers["Cache-Control"] = "public, max-age=3600"
    return response

# Static files (CSS/JS)
static_dir = STATIC_DIR
static_dir.mkdir(parents=True, exist_ok=True)
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Routes
app.include_router(ingestion.router)
app.include_router(video.router)
app.include_router(export.router)
app.include_router(pages.router)
app.include_router(session.router)
app.include_router(annotations.router)
from app import ai_annotation  # AI 辅助标注(信号切段 + VLM 提案)
app.include_router(ai_annotation.router)
app.include_router(dashboard.router)
# tasks.router 已并入 projects.router(项目概念,任务已移除),不再注册
app.include_router(devices.router)
app.include_router(auth.router)
app.include_router(workflows.router)
app.include_router(worker.router)
app.include_router(projects.router)
app.include_router(exceptions.router)
app.include_router(users.router)
