"""职责：FastAPI 应用装配（唯一入口，必须单 worker）：lifespan 建 Runtime 并跑启动自愈、挂路由、挂 /var 静态产物与前端 dist。
输入：Settings（默认从环境变量读取）。
输出：`app`（`uvicorn dramaforge.main:app --workers 1`）。

副作用：lifespan 内建连接池、跑 recovery、收尾时关闭连接；不做 import 期 IO。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

from .api import assets as assets_api
from .api import constraints as constraints_api
from .api import events as events_api
from .api import package as package_api
from .api import review as review_api
from .api import tasks as tasks_api
from .api.runtime import Runtime
from .core.config import Settings, get_settings
from .obs.logs import setup_console

DESCRIPTION = "DramaForge — AI 短剧受控生产系统（题材+预算 → 剧本/分镜/提示词/真实资产/约束报告/成本报告）"


def _wrap(data, code: int = 0, message: str = "ok") -> dict:
    """统一响应外壳 {code, message, data}（§12）。无副作用。"""
    return {"code": code, "message": message, "data": data}


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造应用（测试可注入 Settings）。副作用：lifespan 内建连接与后台协程。"""
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """启动：建 Runtime + 自愈；关闭：取消协程 + 释放连接池。副作用：见注释。"""
        runtime = Runtime(cfg)
        app.state.runtime = runtime
        await runtime.start()
        logger.info("app ready: dry_run={} workers=1 runtime_dir={}", cfg.dry_run, cfg.runtime_path)
        try:
            yield
        finally:
            await runtime.stop()
            logger.info("app stopped")

    app = FastAPI(title="DramaForge", description=DESCRIPTION, version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Last-Event-ID"],
    )

    @app.middleware("http")
    async def envelope(request: Request, call_next):
        """把 /api 的 JSON 响应包成 {code,message,data}；SSE / 文件 / 非 JSON 原样透传。副作用：无。"""
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            return response
        ctype = response.headers.get("content-type", "")
        if "application/json" not in ctype or response.headers.get("content-disposition"):
            return response
        chunks = [chunk async for chunk in response.body_iterator]
        raw = b"".join(chunks)
        try:
            data = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return Response(content=raw, status_code=response.status_code, media_type=ctype)
        if isinstance(data, dict) and set(data) >= {"code", "message", "data"}:
            return JSONResponse(content=data, status_code=response.status_code)
        return JSONResponse(content=_wrap(data), status_code=response.status_code)

    @app.exception_handler(RequestValidationError)
    async def on_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        """请求体不合法 → 400（不外泄堆栈）。无副作用。"""
        return JSONResponse(status_code=400, content=_wrap(None, 400, f"invalid request: {exc.errors()[:3]}"))

    @app.exception_handler(HTTPException)
    async def on_http(request: Request, exc: HTTPException) -> JSONResponse:
        """HTTP 异常 → 统一外壳。无副作用。"""
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
        return JSONResponse(status_code=exc.status_code, content=_wrap(None, exc.status_code, detail))

    @app.exception_handler(ValueError)
    async def on_value(request: Request, exc: ValueError) -> JSONResponse:
        """参数错误 → 400。无副作用。"""
        return JSONResponse(status_code=400, content=_wrap(None, 400, str(exc)[:300]))

    @app.exception_handler(LookupError)
    async def on_lookup(request: Request, exc: LookupError) -> JSONResponse:
        """找不到对象 → 404。无副作用。"""
        return JSONResponse(status_code=404, content=_wrap(None, 404, str(exc)[:300]))

    @app.exception_handler(RuntimeError)
    async def on_runtime(request: Request, exc: RuntimeError) -> JSONResponse:
        """运行期冲突 → 409。无副作用。"""
        return JSONResponse(status_code=409, content=_wrap(None, 409, str(exc)[:300]))

    @app.exception_handler(Exception)
    async def on_unknown(request: Request, exc: Exception) -> JSONResponse:
        """未知异常 → 500，只记日志不外泄堆栈。副作用：写日志。"""
        logger.exception("unhandled error on {}", request.url.path)
        return JSONResponse(status_code=500, content=_wrap(None, 500, "internal error"))

    for router in (
        tasks_api.router,
        events_api.router,
        constraints_api.router,
        assets_api.router,
        review_api.router,
        package_api.router,
    ):
        app.include_router(router)

    @app.get("/healthz", tags=["meta"])
    async def healthz(request: Request) -> JSONResponse:
        """探活：进程 + 数据库。副作用：一次 SQL。"""
        runtime: Runtime | None = getattr(request.app.state, "runtime", None)
        db_ok = bool(runtime and await runtime.store.ping())
        return JSONResponse({"ok": True, "db": db_ok, "dry_run": cfg.dry_run}, status_code=200 if db_ok else 503)

    @app.get("/api/drama/meta", tags=["meta"])
    async def meta() -> dict:
        """前端首屏需要的配置事实（不含任何密钥）。无副作用。"""
        return {
            "app": "DramaForge",
            "version": "1.0.0",
            "dry_run": cfg.dry_run,
            "episode_duration_range": list(cfg.episode_duration_range),
            "shot_ladder": list(cfg.shot_ladder),
            "image_model": cfg.image_model,
            "video_models": {"fast": cfg.video_model("fast"), "hero": cfg.video_model("hero")},
            "tier_price": cfg.tier_price,
            "max_video_cost_cny": cfg.max_video_cost_cny,
        }

    var_dir = cfg.runtime_path
    var_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/var", StaticFiles(directory=str(var_dir)), name="var")

    dist = cfg.web_dist_path
    if dist.is_dir():
        app.mount("/app", StaticFiles(directory=str(dist), html=True), name="web")
        assets_dir = dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")
    else:
        logger.info("web/dist not found ({}), 跳过前端挂载；用 `cd web && npm run build` 生成", dist)

    @app.get("/favicon.ico", include_in_schema=False, response_model=None)
    async def favicon():
        """浏览器图标：返回 SVG 图标避免 404 告警。无副作用。"""
        svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <rect width="32" height="32" rx="8" fill="#2563eb"/>
  <path d="M10 8h5a7 7 0 0 1 0 14h-5V8zm4 3.5v7h1a3.5 3.5 0 0 0 0-7h-1z" fill="#ffffff"/>
  <circle cx="21" cy="20" r="2.5" fill="#38bdf8"/>
</svg>"""
        return Response(content=svg, media_type="image/svg+xml")

    @app.get("/", include_in_schema=False, response_model=None)
    async def index():
        """根路径：有前端 dist 时优先返回 index.html，否则给 JSON 说明。副作用：读磁盘。"""
        target = dist / "index.html"
        if target.is_file():
            return FileResponse(target)
        raise HTTPException(status_code=404, detail="web/dist 未构建；API 见 /docs，产物见 /var")

    return app


app = create_app()
