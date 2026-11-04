"""FastAPI 应用入口。

对齐 v0.5.0 §3.1：
- 桌面常驻服务（本机，不上外网），端口 7688，被占自动顺延并在 UI 顶部提示实际端口；
- LAN 开关默认关闭；开启时强制 token 校验（hmac.compare_digest 比对）；
- 静态页壳可无 token 加载，API 必须 token；
- 开机自启（任务计划程序，托盘图标 P10）。
"""

from __future__ import annotations

import hmac
import os
import socket
import sys
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import APP_VERSION, get_config
from app.core.db import get_db
from app.core.tokenizer import jieba_error, search_mode

FRONTEND_DIST = (
    get_config().data_dir.parent / "frontend" / "dist"
)


def _find_frontend_dist() -> Any:
    """定位前端构建产物（开发态找不到则返回 None，走 API-only 模式）。"""
    candidates = [
        FRONTEND_DIST,
        get_config().data_dir.parent / "frontend" / "dist",
        # backend/app/main.py → 上两级是项目根 secondbrain → frontend/dist
        os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist"),
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "dist"),
    ]
    # 打包态：前端在 _MEIPASS/frontend/dist
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.insert(0, os.path.join(meipass, "frontend", "dist"))
    for path in candidates:
        p = os.path.abspath(str(path))
        if os.path.isdir(p) and os.path.exists(os.path.join(p, "index.html")):
            return p
    return None


class _CachedStaticFiles(StaticFiles):
    """给带 hash 的静态资源加长缓存（文件名变了就等于内容变了）。"""

    def file_response(self, *args: Any, **kwargs: Any) -> FileResponse:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


def _is_api(path: str) -> bool:
    return path.startswith("/api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启停：初始化 DB、启动任务队列、注册 worker。"""
    cfg = get_config()
    cfg.vault.ensure()
    db = get_db()

    from app.workers.pipeline import register_workers
    from app.workers.queue import get_queue
    from app.workers.scheduler import get_scheduler

    queue = get_queue()
    register_workers(queue)
    queue.start()

    # 续跑未完成的 AI 任务（重启后自动把"原文就绪"的素材送进待核准）
    try:
        from app.workers.pipeline import resume_pending_ai

        resumed = resume_pending_ai(queue)
        if resumed:
            print(f"[SecondBrain] 续跑 {resumed} 条未完成的 AI 任务")
    except Exception as exc:  # noqa: BLE001
        print(f"[SecondBrain] 续跑失败（不影响启动）：{exc}")

    scheduler = get_scheduler()
    scheduler.start()

    app.state.db = db
    app.state.queue = queue
    app.state.scheduler = scheduler

    # AI 降级 toast 钩子（§6.1：自动降级时通知用户）
    try:
        # 注意：`get_config` 已在**模块顶部**导入（第 25 行）。
        # 这里绝对不能再写 `from app.core.config import get_config` ——
        # 函数内任何位置的 import 都会让这个名字变成**整个函数的局部变量**，
        # 于是本函数开头 `cfg = get_config()` 那一行会抛
        # UnboundLocalError: cannot access local variable 'get_config'
        # （v34 踩过：exe 启动直接失败，服务起不来）。
        from app.providers.ai_providers import get_router
        from app.services.notifier import notify_fallback

        # 也不能用 router.current_name —— `_trigger_fallback()` 会先把
        # `_current` 改成 ollama 再回调，那时读到的是备链名，
        # 提示会变成"ollama 失败，已降级到 ollama"。用配置里的主链名。
        get_router().set_fallback_listener(
            lambda reason: notify_fallback(get_config().ai.provider, reason)
        )
    except Exception:
        pass

    try:
        yield
    finally:
        scheduler.stop()
        queue.stop()


app = FastAPI(
    title="SecondBrain",
    version=APP_VERSION,
    description="自用优先的个人知识库：采集 → 提炼 → 创作 → 运维",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def lan_token_guard(request: Request, call_next):
    """LAN 模式下 API 必须带 token（静态页壳可无 token 加载）。"""
    cfg = get_config()
    if cfg.web.allow_lan and cfg.web.token and _is_api(request.url.path):
        provided = (
            request.headers.get("x-sb-token")
            or request.query_params.get("token")
            or ""
        )
        if not hmac.compare_digest(provided, cfg.web.token):
            return JSONResponse(
                status_code=401,
                content={"detail": "token 无效或缺失（LAN 模式已开启）"},
            )
    return await call_next(request)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """健康检查（设置页 / 启动自检 / 前端轮询用）。

    v39：`ok` 改成**真实反映状态**，不再恒为 true。
    以前数据库坏了、vault 目录被删了，它照样说"健康"。

    这里只跑**关键项**（轻量）：前端每 20 秒轮询一次，
    碰 ASR/磁盘/Provider 会拖慢每次轮询 —— 全套检查在
    `/api/workbench/diagnose`（用户手动触发）。
    两边共用 `app/services/diagnostics.py`，不会各说各话。
    """
    from app.services.diagnostics import collect_critical_checks, summarize

    cfg = get_config()
    db = get_db()
    mode = search_mode()
    checks = collect_critical_checks()
    state = summarize(checks)
    payload: dict[str, Any] = {
        "ok": state["ok"],
        "checks": checks,
        "app": "SecondBrain",
        "version": APP_VERSION,
        "port": cfg.web.port,
        "host": cfg.web.host,
        "allow_lan": cfg.web.allow_lan,
        "vault": str(cfg.vault.path),
        "search_mode": mode,
        "search_mode_warning": (
            f"jieba 不可用，已降级为 bigram 分词：{jieba_error()}"
            if mode == "bigram"
            else None
        ),
        "db_items": db.scalar("SELECT COUNT(*) FROM items") or 0,
        "db_notes": db.scalar("SELECT COUNT(*) FROM notes") or 0,
    }
    return payload


def find_free_port(host: str, start_port: int, attempts: int = 20) -> int:
    """端口占用自动顺延（§3.1：7688 → 7689 → …）。"""
    for offset in range(attempts):
        port = start_port + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    return start_port


def mount_frontend(app_instance: FastAPI) -> None:
    """挂载前端静态资源（SPA fallback 到 index.html）。

    幂等：重复调用不会重复注册路由。
    """
    if getattr(app_instance.state, "_sb_frontend_mounted", False):
        return

    dist = _find_frontend_dist()
    if dist is None:

        @app_instance.get("/")
        async def root_no_frontend() -> dict[str, Any]:
            return {
                "message": "SecondBrain 后端已启动，前端尚未构建",
                "hint": "cd frontend && npm install && npm run build",
                "api_docs": "/docs",
            }

        app_instance.state._sb_frontend_mounted = True
        return

    assets_dir = os.path.join(dist, "assets")
    if os.path.isdir(assets_dir):
        app_instance.mount(
            "/assets",
            _CachedStaticFiles(directory=assets_dir),
            name="assets",
        )

    @app_instance.get("/")
    async def root() -> FileResponse:
        # index.html 必须 no-store：资源文件名带 hash，浏览器若缓存了旧 index.html，
        # 会去请求已被 vite 清理掉的旧 JS → 404 → 整个前端起不来（表现为"按钮全废"）。
        return FileResponse(
            os.path.join(dist, "index.html"),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    @app_instance.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:
        """SPA fallback：非 API 路径一律返回 index.html。"""
        if _is_api(full_path):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = os.path.join(dist, full_path)
        if os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(
            os.path.join(dist, "index.html"),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    app_instance.state._sb_frontend_mounted = True


# 路由注册顺序很重要：
#   1. /api/* 具体路由
#   2. 前端 SPA catch-all（放最后，否则会吞掉 API 路由）
from app.api import register_routes  # noqa: E402

register_routes(app)
mount_frontend(app)


def run() -> None:
    """开发/生产启动入口。"""
    import uvicorn

    cfg = get_config()
    host = "0.0.0.0" if cfg.web.allow_lan else cfg.web.host
    port = find_free_port("127.0.0.1", cfg.web.port)
    if port != cfg.web.port:
        print(f"[SecondBrain] 端口 {cfg.web.port} 被占用，已顺延到 {port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
