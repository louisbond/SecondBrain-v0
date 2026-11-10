"""确保本机 Ollama 在运行：探测不到就顺手把它拉起来。

**为什么需要**：切到 ollama 时最常见的失败不是模型或参数，而是进程根本没启动——
设置页显示的是 `[WinError 10061] 由于目标计算机积极拒绝，无法连接`。
应用既然已经把 ollama 当成"本地备链"，发现连不上就拉起它，
比让用户自己去开始菜单里找要合理（IDE / 浏览器对本地服务也是这么做的）。

**边界（很重要）**：
- 只对**回环地址**尝试：绝不隔着网线去启动别人机器上的服务；
- 60 秒内最多尝试一次：设置页会反复探测，不能每次调用都 fork 一个进程；
- 找不到 `ollama.exe` 就安静返回 False，由上层如实报"未启动"；
- 任何异常都不外抛：备链没装，不能反过来阻断主链。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 60.0
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008

_last_attempt: dict[str, float] = {}
_lock = threading.Lock()


def _candidates() -> list[Path]:
    """按常见安装位置找 ollama 可执行文件（找不到就返回空列表）。"""
    exe_name = "ollama.exe" if sys.platform == "win32" else "ollama"
    roots: list[Path] = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        roots.append(Path(local_appdata) / "Programs" / "Ollama")
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):  # noqa: SIM112
        if base:
            roots.append(Path(base) / "Ollama")

    found: list[Path] = []
    for root in roots:
        candidate = root / exe_name
        if candidate.is_file():
            found.append(candidate)
    on_path = shutil.which("ollama")
    if on_path:
        found.append(Path(on_path))
    return found


def is_loopback(base_url: str) -> bool:
    """只对 127.0.0.1 / localhost / ::1 动手。"""
    from urllib.parse import urlparse

    host = (urlparse(base_url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _alive(base_url: str, timeout: float = 1.2) -> bool:
    """问一句 /models：只要不是连不上/超时就算活着（401 也算活）。"""
    try:
        resp = httpx.get(
            f"{base_url.rstrip('/')}/models",
            timeout=timeout,
            trust_env=False,  # 回环地址绝不能走系统代理
            follow_redirects=True,
        )
    except Exception:  # noqa: BLE001 - 连不上就是"没起"
        return False
    return resp.status_code < 500


def _spawn(exe: Path) -> bool:
    """后台拉起 Ollama。

    优先 `ollama app.exe`（带托盘图标，与用户自己双击效果一致）；
    没有它就退回 `ollama serve`（无窗口的纯服务）。
    """
    app_exe = exe.with_name("ollama app.exe" if sys.platform == "win32" else "ollama app")
    cmd = [str(app_exe)] if app_exe.is_file() else [str(exe), "serve"]

    try:
        if sys.platform == "win32":
            # 无窗口 + 脱离父进程：关掉应用不该把 ollama 一起带走
            subprocess.Popen(  # noqa: S603 - 路径来自固定候选列表
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=_CREATE_NO_WINDOW | _DETACHED_PROCESS,
            )
        else:
            subprocess.Popen(  # noqa: S603
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
    except OSError as exc:
        logger.info("拉起 Ollama 失败（%s）：%s", cmd[0], exc)
        return False
    return True


def ensure_running(base_url: str, *, wait: float = 12.0) -> bool:
    """确保 Ollama 在监听；返回最终是否可用。

    `wait` 是**冷启动**等待上限：拉起的进程要先加载运行时再监听端口，
    通常 1～3 秒，留到 12 秒是为了慢盘。
    """
    if not is_loopback(base_url):
        return False
    if _alive(base_url):
        return True

    key = base_url.rstrip("/")
    now = time.time()
    with _lock:
        if now - _last_attempt.get(key, 0.0) < COOLDOWN_SECONDS:
            return False  # 冷却中：避免设置页反复探测时狂拉进程
        _last_attempt[key] = now

    exes = _candidates()
    if not exes:
        logger.info("未找到 ollama 可执行文件，跳过自动拉起")
        return False
    if not _spawn(exes[0]):
        return False

    deadline = time.time() + wait
    while time.time() < deadline:
        if _alive(base_url):
            logger.info("Ollama 已自动拉起（%s）", exes[0])
            return True
        time.sleep(0.5)
    logger.info("Ollama 拉起后 %.0f 秒内仍未监听，放弃等待", wait)
    return False


def ensure_running_async(base_url: str, *, wait: float = 20.0) -> None:
    """不阻塞调用方地拉起（用于切换 provider 这类交互路径）。"""
    threading.Thread(
        target=ensure_running,
        args=(base_url,),
        kwargs={"wait": wait},
        name="ollama-autostart",
        daemon=True,
    ).start()
