"""桌面模式：内嵌窗口 + 链式启动 + 关窗即退。

启动链：
    配置 → 数据库迁移 → 后端（后台线程）→ 前端挂载 → 内嵌窗口打开
关闭链：
    窗口关闭 → 前端关窗钩子 / 窗口 closed 事件 → 后端优雅退出 → 进程结束

无小黑窗（PyInstaller console=False）时，日志全部写到 `data/logs/app.log`，
崩溃会弹窗告知日志路径。缓存数据（数据库、索引、音频缓存）**保留**，下次启动沿用。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "app.log"


# ---------------------------------------------------------------- 日志
def setup_logging(data_dir: Path) -> Path | None:
    """把日志写到 data/logs/app.log（滚动）。返回日志文件路径。"""
    try:
        log_dir = Path(data_dir) / LOG_DIR_NAME
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / LOG_FILE_NAME
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        # HTTP 客户端的每条请求都打 INFO → 日志瞬间被刷爆，
        # 真正的错误（4xx/5xx）反而被冲掉。降到 WARNING。
        for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        # 幂等：同一个日志文件不重复挂 handler
        for existing in list(root.handlers):
            if isinstance(existing, logging.handlers.RotatingFileHandler):
                if Path(getattr(existing, "baseFilename", "")).resolve() == (
                    log_path.resolve()
                ):
                    return log_path
                existing.close()
                root.removeHandler(existing)
        root.addHandler(handler)
        # uvicorn 自己的日志也进文件
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.INFO)
        # 轮转更频繁一点：2MB × 3 → 5MB × 2（错误日志不容易被冲没）
        handler.maxBytes = 5 * 1024 * 1024
        handler.backupCount = 2
        return log_path
    except Exception:  # noqa: BLE001
        return None


def install_crash_handler(log_path: Path | None) -> None:
    """未捕获异常 → 写日志 + 弹窗告知路径（无小黑窗时用户才有线索）。"""

    def _handler(exc_type, exc, tb) -> None:
        import traceback

        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            if log_path:
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write("\n=== 未捕获异常 ===\n" + text)
        except Exception:  # noqa: BLE001
            pass
        try:
            # 用 Windows 原生 MessageBox（比 tkinter 轻，打包时不用带 tkinter）。
            # ctypes.windll 只在 Windows 上存在：非 Windows（CI 的 Linux runner、
            # 未来的跨平台移植）没有它，所以用 getattr 取，取不到就直接跳过弹窗。
            import ctypes

            windll = getattr(ctypes, "windll", None)
            if windll is not None:
                windll.user32.MessageBoxW(
                    None,
                    f"程序遇到未处理的错误，已写入日志：\n\n{log_path}\n\n请把该文件内容反馈给我。",
                    "SecondBrain 出错了",
                    0x10,  # MB_ICONERROR
                )
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = _handler


# ---------------------------------------------------------------- 后端
def start_backend(app: Any, host: str, port: int) -> Any:
    """在后台线程里跑 uvicorn，主线程留给窗口事件循环。"""
    import uvicorn

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        # 不让 uvicorn 重置 logging 配置（否则 root 上的文件 handler 会被禁用）
        log_config=None,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="sb-backend", daemon=True)
    thread.start()
    # 等端口真正就绪。uvicorn 绑端口失败时线程会立刻退出（[Errno 10048]），
    # 此时别再干等 40 秒 —— 用户只会在很久之后看到一个误导性的崩溃弹窗。
    deadline = time.time() + 30
    while time.time() < deadline:
        if getattr(server, "started", False):
            return server
        if not thread.is_alive():
            break
        time.sleep(0.2)
    raise RuntimeError(
        f"后端未能就绪：{host}:{port} 可能被其他程序占用，或启动失败（详见 data/logs/app.log）"
    )


def stop_backend(server: Any) -> None:
    try:
        server.should_exit = True
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 桌面
def run_desktop(app: Any, host: str, port: int, data_dir: Path, open_browser: bool = False) -> int:
    """启动内嵌窗口；窗口关闭即退出程序。"""
    # 日志已在 run.py 里配置过（所有模式通用），这里只取路径
    log_path = Path(data_dir) / LOG_DIR_NAME / LOG_FILE_NAME
    if not log_path.exists():
        log_path = setup_logging(data_dir) or log_path
    install_crash_handler(log_path)
    logger = logging.getLogger("sb.desktop")
    logger.info("SecondBrain 桌面模式启动")

    server = start_backend(app, host, port)
    # 窗口永远用 127.0.0.1：host=0.0.0.0 是**监听地址**（LAN 模式下让别的设备能连），
    # 不是可导航的地址。之前直接拼 http://0.0.0.0:7688 给 WebView2，
    # 开启 LAN 后重启就可能白屏/黑屏 —— 又会被误判成打包坏了。
    url = f"http://127.0.0.1:{port}"
    logger.info("后端就绪：%s（监听 %s）", url, host)

    def _shutdown(*_args) -> None:
        logger.info("收到退出信号，关闭后端…")
        stop_backend(server)
        os._exit(0)

    try:
        import webview
    except Exception as exc:  # noqa: BLE001
        logger.error("pywebview 不可用，回退到系统浏览器：%s", exc)
        if open_browser:
            webbrowser.open(url)
        print(f"[SecondBrain] 未装内嵌窗口组件，已用浏览器打开 {url}")
        try:
            while not getattr(server, "should_exit", False):
                time.sleep(0.5)
        except KeyboardInterrupt:
            _shutdown()
        return 0

    window = webview.create_window(
        "SecondBrain",
        url,
        width=1440,
        height=920,
        min_size=(1024, 700),
        confirm_close=False,
    )

    def _on_closed() -> None:
        logger.info("窗口已关闭，退出程序")
        _shutdown()

    if window is not None:
        try:
            window.events.closed += _on_closed
        except Exception:  # noqa: BLE001
            pass

    try:
        webview.start()
    except Exception as exc:  # noqa: BLE001
        logger.error("窗口创建失败：%s", exc)
        if open_browser:
            webbrowser.open(url)
        print(f"[SecondBrain] 内嵌窗口启动失败，已用浏览器打开 {url}")
        try:
            while not getattr(server, "should_exit", False):
                time.sleep(0.5)
        except KeyboardInterrupt:
            _shutdown()
        return 1

    _shutdown()
    return 0
