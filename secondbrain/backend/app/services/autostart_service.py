"""开机自启与端口管理（P10）。

对齐 v0.5.0 §3.1 / P10：
- 服务生命周期：开机自启（任务计划程序，登录时启动），托盘图标可选；
- 端口：默认 7688；被占用自动顺延（7689、7690…）并在 UI 顶部提示实际端口；
- 可用性边界：笔记本休眠=服务下线（附电源策略建议）。

Windows 任务计划程序用 `schtasks` 注册：
    schtasks /Create /TN "SecondBrain" /TR "<python> -m uvicorn app.main:app ..." /SC ONLOGON /RL LIMITED /F
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.core.config import get_config

TASK_NAME = "SecondBrain"
IS_WINDOWS = platform.system() == "Windows"


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """执行命令（统一走 app.core.proc：Windows 下不弹黑框）。"""
    # v32：改用 app.core.proc.run —— 它统一处理 CREATE_NO_WINDOW，
    # 避免每个模块自己拼 creationflags（漏一处就闪黑窗）。
    from app.core import proc as proc_mod

    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "encoding": "utf-8",
        "errors": "replace",
    }
    try:
        proc = proc_mod.run(cmd, **kwargs)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"命令不存在：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", "命令超时"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def _python_exe() -> str:
    return sys.executable


def _app_command() -> str:
    """构造启动命令（打包后用 exe，开发态用 uvicorn）。"""
    cfg = get_config()
    _backend_dir = Path(__file__).resolve().parents[3]
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{_python_exe()}" -m uvicorn app.main:app --host 127.0.0.1 --port {cfg.web.port}'


def enable_autostart() -> dict[str, Any]:
    """注册开机自启（任务计划程序，登录时启动）。"""
    if not IS_WINDOWS:
        return {"ok": False, "message": "仅 Windows 支持（当前平台未支持任务计划程序）"}

    backend_dir = Path(__file__).resolve().parents[3]
    cmd = [
        "schtasks",
        "/Create",
        "/TN",
        TASK_NAME,
        "/TR",
        _app_command(),
        "/SC",
        "ONLOGON",
        "/RL",
        "LIMITED",
        "/F",
    ]
    code, out, err = _run(cmd)
    if code != 0:
        return {"ok": False, "message": (err or out).strip()[:400]}
    return {
        "ok": True,
        "message": f"已注册开机自启任务「{TASK_NAME}」",
        "command": _app_command(),
        "backend_dir": str(backend_dir),
    }


def disable_autostart() -> dict[str, Any]:
    """注销开机自启。"""
    if not IS_WINDOWS:
        return {"ok": False, "message": "仅 Windows 支持"}
    code, out, err = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if code != 0:
        return {"ok": False, "message": (err or out).strip()[:400]}
    return {"ok": True, "message": "已注销开机自启"}


def autostart_status() -> dict[str, Any]:
    """查询自启任务状态。"""
    if not IS_WINDOWS:
        return {"installed": False, "supported": False}
    code, out, _ = _run(["schtasks", "/Query", "/TN", TASK_NAME])
    installed = code == 0
    return {
        "installed": installed,
        "supported": True,
        "detail": out.strip()[:600] if installed else "未注册",
    }


# ---- 电源策略建议 ------------------------------------------------------


def power_suggestion() -> dict[str, Any]:
    """电源策略建议（§3.1：休眠会中断批处理）。"""
    return {
        "supported": IS_WINDOWS,
        "message": (
            "笔记本休眠或关机 = 服务下线，ASR 队列会暂停（唤醒后自动续跑）。"
            "建议：接通电源时把电源策略设为「从不睡眠」，以便过夜批处理。"
        ),
        "howto": [
            "控制面板 → 电源选项 → 更改计划设置 → 高级电源设置 → 睡眠 → 接通电源时设为「从不」",
            "或在设置页批量任务运行时留意休眠提示",
        ],
        "note": "UI 在批量任务运行时提示休眠风险；jobs 状态持久化于 SQLite，唤醒后自动续跑",
    }


def set_power_never_sleep() -> dict[str, Any]:
    """尝试把「接通电源时睡眠」设为从不（需要管理员权限）。"""
    if not IS_WINDOWS:
        return {"ok": False, "message": "仅 Windows 支持"}
    code, out, err = _run(
        [
            "powercfg",
            "-change",
            "standby-timeout-ac",
            "0",
        ]
    )
    if code != 0:
        return {
            "ok": False,
            "message": (err or out).strip()[:300] or "需要管理员权限",
        }
    return {"ok": True, "message": "已设置：接通电源时永不睡眠"}


# ---- 端口 --------------------------------------------------------------


def find_free_port(start_port: int, host: str = "127.0.0.1", attempts: int = 20) -> int:
    """端口占用自动顺延（7688 → 7689 → …）。"""
    import socket

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
