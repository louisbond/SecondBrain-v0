"""打包与开发启动入口。

开发态：
    python run.py                     # 启动服务（自动挂载前端 dist）
    python run.py --port 7690         # 指定端口
    python run.py --no-frontend       # 只启动 API（开发时用）

打包后（PyInstaller）：
    SecondBrain.exe                  # 单目录，内含前端与 bin
    SecondBrain.exe --port 7690
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

# 保证 app 包可导入
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _frontend_dist() -> Path | None:
    """定位前端构建产物（打包后位于 _MEIPASS/frontend/dist）。"""
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys._MEIPASS) / "frontend" / "dist")
    root = BACKEND_DIR.parent
    candidates.append(root / "frontend" / "dist")
    for path in candidates:
        if path.exists() and (path / "index.html").exists():
            return path
    return None


def _fix_windows_console_encoding() -> None:
    """让 exe 的中文输出在控制台正常显示。

    Windows 控制台默认代码页是 936(GBK)，而 Python 的 stdout 在打包后可能用 UTF-8，
    结果中文全是乱码。这里把控制台输出代码页切到 65001(UTF-8)，
    并把 stdout/stderr 也设成 UTF-8（errors=replace 防止个别字符直接崩）。
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass



_log = logging.getLogger("sb.app")


def _p(*args: Any) -> None:
    """安全打印 + 写日志。

    console=False（无小黑窗）时 sys.stdout 是 None，直接 print 会崩；
    而且没有终端时日志文件是唯一线索，所以两条路都走。
    """
    try:
        print(*args)
    except Exception:  # noqa: BLE001
        pass
    try:
        msg = " ".join(str(a) for a in args)
        _log.info(msg)
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    _fix_windows_console_encoding()

    parser = argparse.ArgumentParser(description="SecondBrain v0.5.0")
    parser.add_argument("--host", default=None, help="监听地址")
    parser.add_argument("--port", type=int, default=None, help="监听端口")
    parser.add_argument("--no-frontend", action="store_true", help="只启动 API")
    parser.add_argument("--vault", default=None, help="vault 根目录")
    parser.add_argument("--reload", action="store_true", help="开发热重载")
    parser.add_argument(
        "--open-browser", action="store_true", help="启动后自动打开浏览器（打包版默认开启）"
    )
    parser.add_argument("--no-browser", action="store_true", help="启动后不打开浏览器")
    args = parser.parse_args()

    if args.vault:
        os.environ["SECOND_BRAIN_VAULT"] = args.vault

    from app.core.config import get_config, store

    cfg = get_config()

    # 日志与崩溃处理：所有模式都配（无小黑窗时这是唯一的线索）
    try:
        from app.desktop import install_crash_handler, setup_logging

        _log_path = setup_logging(cfg.data_dir)
        install_crash_handler(_log_path)
        if _log_path:
            _p(f"[SecondBrain] 日志：{_log_path}")
        # 立刻落一条，保证文件非空（哪怕后面启动失败也有线索）
        _log.info("SecondBrain 启动中… 版本 0.5.0")
    except Exception as _exc:  # noqa: BLE001
        _p(f"[SecondBrain] 日志初始化失败：{_exc}")
    if args.port:
        cfg.web.port = args.port
    if args.host:
        cfg.web.host = args.host

    import uvicorn

    from app.main import app, find_free_port, mount_frontend

    if not args.no_frontend:
        dist = _frontend_dist()
        if dist is None:
            _p("[SecondBrain] 未找到前端构建产物，仅启动 API")
            _p("               构建前端：cd frontend && npm install && npm run build")
        else:
            mount_frontend(app)
            _p(f"[SecondBrain] 前端已挂载：{dist}")

    host = "0.0.0.0" if cfg.web.allow_lan else cfg.web.host
    port = find_free_port("127.0.0.1", cfg.web.port)
    if port != cfg.web.port:
        _p(f"[SecondBrain] 端口 {cfg.web.port} 被占用，已顺延到 {port}")

    _p(f"[SecondBrain] v0.5.0 → http://{host}:{port}")
    _p(f"[SecondBrain] vault: {cfg.vault.path}")

    # 打包后双击即用：内嵌窗口（桌面模式），关窗即退
    frozen = bool(getattr(sys, "frozen", False))
    if frozen and not args.no_browser and not args.reload:
        from app.desktop import run_desktop

        raise SystemExit(
            run_desktop(
                app,
                host,
                port,
                cfg.data_dir,
                open_browser=args.open_browser,
            )
        )

    if args.open_browser or (frozen and not args.no_browser):
        _open_browser_later(f"http://{host}:{port}")

    uvicorn.run(
        "app.main:app" if args.reload else app,
        host=host,
        port=port,
        reload=args.reload,
        log_level="info",
        # 不让 uvicorn 重置 logging 配置（否则会禁用 root handler，日志写不进文件）
        log_config=None,
    )


def _open_browser_later(url: str, delay: float = 1.5) -> None:
    """延迟打开浏览器（等服务起来再开，否则会看到连接失败页）。"""

    def _worker() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_worker, daemon=True).start()


if __name__ == "__main__":
    main()
