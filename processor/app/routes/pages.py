"""Web page routes."""

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from pathlib import Path
import os

from app.database import engine
from app.config import settings
from app.paths import STATIC_DIR, TEMPLATES_DIR
from app.auth import require_roles

router = APIRouter(tags=["pages"])

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
_static_dir = STATIC_DIR

# 静态资源缓存键的唯一来源:模板里写 ``?v={{ asset_version }}``。
# 以前每个模板各写各的日期串,发版时靠人记得逐个改 —— 漏一个,那个页面
# 就会继续发浏览器缓存里的旧脚本。
#
# ★ 取的是**前端号**(web/VERSION),不是后端号。改前端只 bump 那一个文件即可,
#   提交信息里的「前端 X」就是它 —— 缓存键和版本记录是同一个数,不会对不上。
#   (原来取后端号,于是每次改前端都要同时 bump 后端号和前端号两个地方。)
from app.web_version import frontend_version as _frontend_version

templates.env.globals["asset_version"] = _frontend_version()

# 服务端取词 —— 模板里写 ``{{ t('nav_home') }}``。语言由 app/i18n.py 的
# LanguageMiddleware 从 cookie 读进 ContextVar,这里是同一个上下文,读得到。
# 这样首屏渲染出来的就是正确语言,不再"先英文后由 JS 纠正"(那会闪一下)。
from app.i18n import get_current_lang as _get_lang
from app.i18n import t as _t

templates.env.globals["t"] = _t
# 模板要写 <html lang="…">,所以语言码也放进全局。
#
# ★ 注册的是**函数**,模板里必须写成 ``{{ current_lang() }}`` 带括号。
#   Jinja 的全局是"每次渲染取一次这个对象" —— 值是固定的,而语言是**每请求**
#   才定的(存在 ContextVar 里),所以只能注册一个可调用的东西,渲染时调它。
#   少写括号不会报错,只会把函数的 repr 渲染进 HTML:
#       <html lang="&lt;function get_current_lang at 0x7f…&gt;">
templates.env.globals["current_lang"] = _get_lang


@router.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    """Home — Overview Dashboard."""
    return templates.TemplateResponse("overview.html", {"request": request})


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page — public."""
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/review", response_class=HTMLResponse)
async def review_page(request: Request):
    """Video review page."""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "page_mode": "review",
    })


@router.get("/annotation-studio", response_class=HTMLResponse)
async def annotation_studio_page(request: Request):
    """Standalone annotation workspace; it reuses the synchronized media UI."""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "page_mode": "annotation",
    })


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    """Task group browser."""
    return templates.TemplateResponse("tasks.html", {"request": request})


@router.get("/trash", response_class=HTMLResponse)
async def trash_page(request: Request):
    return templates.TemplateResponse("trash.html", {"request": request})


@router.get("/train", response_class=HTMLResponse)
async def train_page(request: Request):
    """Model training — read-only view of checkpoints produced outside the platform.

    Data comes from ``GET /api/v1/training/runs`` (see ``app/routes/training.py``).
    Nothing on this page starts or stops training yet.
    """
    return templates.TemplateResponse("train.html", {"request": request})


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, _: dict = Depends(require_roles("admin"))):
    """Account management is visible only to administrators."""
    return templates.TemplateResponse("users.html", {"request": request})


@router.get("/workflow-studio", response_class=HTMLResponse)
async def workflow_studio_page(request: Request):
    """Workflow Studio — React SPA embedded in base layout with sidebar."""
    dev_mode = os.environ.get("WORKFLOW_STUDIO_DEV", "").lower() in ("1", "true", "yes")

    if dev_mode:
        # Dev: load the source entry from Vite so React Fast Refresh/HMR is
        # active.  Keep the URL configurable because ``localhost`` is only
        # correct when the browser and Vite server run on the same machine.
        dev_server_url = os.environ.get("WORKFLOW_STUDIO_DEV_URL", "http://localhost:5173").rstrip("/")
        return templates.TemplateResponse("workflow_studio.html", {
            "request": request,
            "dev_mode": True,
            "dev_server_url": dev_server_url,
            "css_file": "",
            "js_file": "",
        })

    # Production: read manifest.json for hashed filenames
    manifest_path = _static_dir / "workflow-studio" / ".vite" / "manifest.json"
    css_file = ""
    js_file = ""

    if manifest_path.exists():
        import json
        try:
            manifest = json.loads(manifest_path.read_text())
            # Find the main HTML entry (Vite outputs it as the only multi-page entry)
            for key, val in manifest.items():
                if val.get("isEntry"):
                    js_file = val.get("file", "")
                    css_list = val.get("css", [])
                    if css_list:
                        css_file = css_list[0]
                    break
        except Exception:
            pass

    if not js_file:
        # Fallback: manifest.json 缺失(构建失败/中断)时,从 assets 目录
        # 扫描产物并仍渲染在 base.html 布局里 —— 侧边栏永不消失。
        # 不要直接返回 vite 独立 index.html:那个页面没有左侧导航。
        assets_dir = _static_dir / "workflow-studio" / "assets"
        try:
            if assets_dir.is_dir():
                js_candidates = sorted(assets_dir.glob("index-*.js"))
                css_candidates = sorted(assets_dir.glob("index-*.css"))
                if js_candidates:
                    js_file = js_candidates[-1].name
                    if css_candidates:
                        css_file = css_candidates[-1].name
        except OSError:
            pass

    if not js_file:
        return HTMLResponse(
            content="<h1>Workflow Studio not built. Run: cd web/workflow-studio && npm run build</h1>",
            status_code=503,
        )

    return templates.TemplateResponse("workflow_studio.html", {
        "request": request,
        "dev_mode": False,
        "css_file": css_file,
        "js_file": js_file,
    })


@router.get("/health")
async def health():
    """Fast, non-blocking liveness/readiness probe.

    Both PostgreSQL and the authoritative storage can be remote.  Never run a
    blocking filesystem probe on Uvicorn's event loop, and never allow a
    broken DB/mount to hold the health request indefinitely.
    """
    db_ok = False
    try:
        async def _check_db():
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        # Health is a short probe, independent from the longer command
        # timeout used by normal application queries.
        await asyncio.wait_for(_check_db(), timeout=2.0)
        db_ok = True
    except Exception:
        pass

    from app.storage import storage_ok as check_storage
    try:
        # A stale SSHFS mount may block a pathlib operation in the kernel.  A
        # worker thread keeps that from freezing every HTTP request; the
        # timeout makes the probe degrade instead of hanging the endpoint.
        storage_ok = await asyncio.wait_for(
            asyncio.to_thread(check_storage), timeout=2.0,
        )
    except Exception:
        storage_ok = False
    from app.version import __version__

    return {
        "status": "ok" if db_ok and storage_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "storage": "ok" if storage_ok else "error",
        "version": __version__,
    }
