"""桌面模式关窗即退验证。

真实跑一遍：主线程起内嵌窗口 → 3 秒后从子线程 destroy 窗口 →
验证 closed 事件触发（写标记文件）、进程正常退出。
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "tests"))

HOME = BACKEND_DIR / "scripts" / "_desk_home"
MARKER = HOME / "closed.marker"


def _main() -> int:
    shutil.rmtree(HOME, ignore_errors=True)
    (HOME / "vault" / "01_Inbox").mkdir(parents=True, exist_ok=True)

    # 用隔离夹具渲染配置（保证 data_dir 落在临时目录）
    from harness import IsolatedServer

    srv = IsolatedServer()
    srv.home = HOME
    (HOME / "config.toml").write_text(
        srv._render_config(HOME / "vault"), encoding="utf-8"
    )

    os.environ["SECOND_BRAIN_HOME"] = str(HOME)
    os.environ["SECOND_BRAIN_CONFIG"] = str(HOME / "config.toml")

    from app.core.config import get_config

    cfg = get_config()
    print("data_dir:", cfg.data_dir)

    from app.desktop import setup_logging

    log_path = setup_logging(cfg.data_dir)
    print("日志:", log_path)

    # 探测 closed 事件：包装 run_desktop 的 closed 回调
    import app.desktop as desktop_mod
    from app.main import app

    orig_run = desktop_mod.run_desktop

    def _patched_run(*args, **kwargs):
        # 在窗口事件里加标记
        import webview

        real_create = webview.create_window

        def _create(*a, **kw):
            win = real_create(*a, **kw)
            try:
                win.events.closed += lambda: MARKER.write_text("closed", encoding="utf-8")
            except Exception:
                pass
            return win

        webview.create_window = _create
        try:
            return orig_run(*args, **kwargs)
        finally:
            webview.create_window = real_create

    desktop_mod.run_desktop = _patched_run

    def _closer() -> None:
        for _ in range(80):
            time.sleep(0.5)
            try:
                import webview

                if webview.windows:
                    break
            except Exception:
                pass
        time.sleep(2)
        try:
            import webview

            webview.windows[0].destroy()
        except Exception as exc:  # noqa: BLE001
            print("destroy 失败:", exc)

    threading.Thread(target=_closer, daemon=True).start()

    try:
        desktop_mod.run_desktop(app, "127.0.0.1", 7766, cfg.data_dir, open_browser=False)
    except SystemExit as exc:
        print("SystemExit:", exc.code)
    except Exception as exc:  # noqa: BLE001
        print("异常:", type(exc).__name__, str(exc)[:200])
        return 3

    # run_desktop 正常返回 = webview.start() 因窗口关闭而返回
    closed = MARKER.exists()
    print("closed 事件已触发:", closed)
    if log_path and log_path.exists():
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
        print("--- 日志尾部 ---")
        for line in tail:
            print("   ", line)
    return 0 if closed else 4


if __name__ == "__main__":
    code = _main()
    print(f"退出码: {code}")
    sys.exit(code)
