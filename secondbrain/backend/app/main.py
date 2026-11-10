"""FastAPI 应用入口。

对齐 v0.5.0 §3.1：
- 桌面常驻服务（本机，不上外网），端口 7688，被占自动顺延并在 UI 顶部提示实际端口；
- LAN 开关默认关闭；开启时强制 token 校验（hmac.compare_digest 比对）；
- 静态页壳可无 token 加载，API 必须 token；
- 开机自启（任务计划程序，托盘图标 P10）。
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path
import socket
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import APP_VERSION, get_config
from app.core.db import get_db
from app.core.tls import enable_system_trust_store
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

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


def _is_api(path: str) -> bool:
    return path.startswith("/api")


def _is_loopback(host: str) -> bool:
    """请求是否来自本机。LAN token 是防「别人连进来」的，不该把本机窗口也锁在外面。"""
    return host in ("127.0.0.1", "::1", "localhost")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启停：初始化 DB、启动任务队列、注册 worker。"""
    # 必须在任何 HTTP 客户端之前：让 TLS 校验走系统证书存储，
    # 否则交叉签名证书链（如火山方舟）会被 certifi 判为"找不到本地签发者"。
    enable_system_trust_store()

    cfg = get_config()
    cfg.vault.ensure()
    db = get_db()

    # 主链就是本地模型时，启动即拉起（后台线程，不拖慢启动）
    if cfg.ai.provider == "ollama" and cfg.ai.auto_start_ollama:
        from app.services.ollama_service import ensure_running_async

        ensure_running_async(cfg.ai.ollama.base_url)

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

    # 安全/体验（2026-11-07）：索引为空时自动建一次。
    # md 是真相源，重建幂等；否则首次运行或换了 vault 之后全文搜索全空，
    # 用户只会看到"搜不到任何东西"，以为搜索功能坏了。
    try:
        from app.services.index_service import get_index_service

        index_svc = get_index_service()
        if not index_svc.db.scalar("SELECT COUNT(*) FROM notes"):
            _scan = index_svc.scan_vault()
            print(f"[SecondBrain] 空索引，已自动扫描 vault：{_scan.indexed} 篇")
    except Exception as exc:  # noqa: BLE001
        print(f"[SecondBrain] 自动扫描失败（不影响启动）：{exc}")

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


# /docs、/redoc、/openapi.json 默认关闭：这三个是给开发者用的，
# 对普通用户只是信息暴露面（局域网内可枚举全部接口路径）。
# 需要调试时：set SECOND_BRAIN_DEV_DOCS=1 再启动。
_DEV_DOCS = os.environ.get("SECOND_BRAIN_DEV_DOCS") == "1"

app = FastAPI(
    title="SecondBrain",
    version=APP_VERSION,
    description="自用优先的个人知识库：采集 → 提炼 → 创作 → 运维",
    lifespan=lifespan,
    docs_url="/docs" if _DEV_DOCS else None,
    redoc_url="/redoc" if _DEV_DOCS else None,
    openapi_url="/openapi.json" if _DEV_DOCS else None,
)

# 安全整改（2026-11-07）：原先 allow_origins=["*"] 允许**任意网页**跨域读取
# /api/*（笔记原文、音频、/api/settings 里的分享链接），且浏览器发起的请求源就是
# 127.0.0.1，lan_token_guard 的 loopback 豁免等于不设防。
# 本应用前后端同源同端口，不存在合法的跨源调用 —— 连读都不该给。
# 这里直接拒绝带异源 Origin 的请求（CORS 只阻止"读"，这层连"发"都拒）。


@app.middleware("http")
async def cross_origin_guard(request: Request, call_next):
    """拒绝一切跨源请求：浏览器跨域 fetch/表单必带 Origin，同源请求不带。"""
    origin = (request.headers.get("origin") or "").strip().lower()
    if origin:
        host = (request.headers.get("host") or "").strip().lower()
        if not host or origin not in (f"http://{host}", f"https://{host}"):
            return JSONResponse(
                status_code=403,
                content={"detail": "cross-origin request blocked"},
            )
    return await call_next(request)


@app.middleware("http")
async def host_guard(request: Request, call_next):
    """只接受本机/局域网 IP 的 Host，阻断 DNS rebinding（Host: evil.com）。"""
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    cfg = get_config()
    allowed = {"", "127.0.0.1", "localhost", "::1"}
    if cfg.web.allow_lan:
        from app.core.network import lan_ip

        ip = lan_ip()
        if ip:
            allowed.add(ip.lower())
    if host not in allowed:
        return JSONResponse(status_code=400, content={"detail": "invalid host header"})
    return await call_next(request)


@app.middleware("http")
async def lan_token_guard(request: Request, call_next):
    """LAN 模式下**跨设备**的 API 请求必须带 token（静态页壳可无 token 加载）。

    v0.7.5：本机回环请求豁免。否则开启 LAN 后，桌面窗口打开的
    `http://127.0.0.1:7688` 没带 token，所有 /api 全 401 —— 手机还没连上，
    本机先白屏了。token 的用途是防别人连进来，不是给自己上锁。
    """
    cfg = get_config()
    # 安全整改（2026-11-07）：原条件里的 `cfg.web.allow_lan` 已去掉。
    # 鉴权的依据应该是「请求从哪来」，不是「开关开没开」—— 绑定地址一变，
    # 原来的写法会让整个 /api 裸奔。
    if (
        cfg.web.token
        and _is_api(request.url.path)
        and not _is_loopback(request.client.host if request.client else "")
    ):
        provided = (
            request.headers.get("x-sb-token")
            or request.query_params.get("token")
            or request.cookies.get("sb_token")
            or ""
        )
        if not hmac.compare_digest(provided, cfg.web.token):
            return JSONResponse(
                status_code=401,
                content={"detail": "token 无效或缺失（LAN 模式已开启）"},
            )
    return await call_next(request)


_access_logger = logging.getLogger("sb.access")


@app.middleware("http")
async def access_log(request: Request, call_next):
    """把请求写进日志（方法 / 路径 / 状态 / 耗时）。

    uvicorn 自带的 access log 在桌面模式（console=False）下会整个丢掉 ——
    它把 handler 挂在 stderr 上，而窗口程序没有控制台。结果出问题时
    日志里只有「启动」和「退出」两行，用户说"卡"也只能靠猜。
    这里自己记，慢请求标 WARNING，静态资源只在超时时记。
    """
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        _access_logger.exception(
            "%s %s 未捕获异常", request.method, request.url.path
        )
        raise
    cost_ms = (time.perf_counter() - started) * 1000
    is_api = request.url.path.startswith("/api/")
    if is_api:
        # API 一律禁缓存。WebView2 会对没有缓存头的 GET 做启发式缓存，
        # 于是出现"界面显示旧数据、后端日志里完全没有这次请求"的现象 ——
        # 排查时会误判成后端卡死。
        response.headers["Cache-Control"] = "no-store"
    if is_api or cost_ms > 1000:
        level = logging.WARNING if cost_ms > 1000 else logging.INFO
        _access_logger.log(
            level,
            "%s %s -> %s (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            cost_ms,
        )
    return response


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
    from app.core.network import lan_ip
    from app.services.diagnostics import collect_critical_checks, summarize

    cfg = get_config()
    db = get_db()
    ip = lan_ip()
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
        "lan_ip": ip,
        "lan_url": f"http://{ip}:{cfg.web.port}" if ip else "",
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
        """SPA fallback：非 API 路径一律返回 index.html。

        **路径必须锁在 dist 内**：原来的
        `os.path.join(dist, full_path)` 在 Windows 上遇到绝对路径
        （如 `D:/.../config/secrets.json`）会**直接丢弃 dist**，
        `..` 也没有校验 —— 任何未鉴权的静态请求都能读到任意文件
        （包括全部密钥）。现在一律 resolve() 后校验在 dist 内。
        """
        if _is_api(full_path):
            raise HTTPException(status_code=404, detail="Not Found")
        try:
            dist_root = Path(dist).resolve()
            rel = Path(full_path)
            # 绝对路径直接拒绝（Windows 下 "D:/x" 会被 is_absolute 识别）
            is_safe = not rel.is_absolute() and ".." not in rel.parts
            if is_safe:
                candidate = (dist_root / rel).resolve()
                is_safe = candidate.is_relative_to(dist_root)
            if is_safe and candidate.is_file():
                return FileResponse(candidate)
        except (OSError, ValueError):
            pass
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
    # LAN 模式必须有令牌：原来 token 为空时鉴权中间件整个被短路，
    # 等于全部 API 裸奔。缺就自动生成一个（设置页的分享链接会带上它）。
    try:
        from app.core.config import ensure_web_token

        if get_config().web.allow_lan and not get_config().web.token:
            ensure_web_token()
            store.reload()
            logging.getLogger("sb").info(
                "LAN 模式已开启但未配置 web_token，已自动生成并保存到 secrets.json"
            )
    except Exception:
        pass
    import uvicorn

    cfg = get_config()
    host = "0.0.0.0" if cfg.web.allow_lan else cfg.web.host
    port = find_free_port("127.0.0.1", cfg.web.port)
    if port != cfg.web.port:
        print(f"[SecondBrain] 端口 {cfg.web.port} 被占用，已顺延到 {port}")
    # log_config=None：沿用 setup_logging() 装好的 root 文件 handler。
    # 否则 uvicorn 会把 access 日志挂到自己的 stderr StreamHandler 上，
    # 而桌面模式（console=False）根本没有控制台 —— 整段访问日志直接消失，
    # 出问题时只能看到启动和退出两行，排查全靠猜。
    uvicorn.run(app, host=host, port=port, log_level="info", log_config=None)


if __name__ == "__main__":
    run()
