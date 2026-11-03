"""子进程执行工具（统一不弹黑窗）。

**为什么需要这个模块**（v32）：
用户反馈「还是跳黑窗，几秒后自动关了」「运行期间还会有一个小于 1s 的窗口
闪退」。根因是散落各处的 `subprocess.run` **没加无窗口标志**：

- `cli_locator.run_cli()` —— 每次 AI provider 健康检查都调
  `codebuddy --version`，于是**每隔几秒闪一个 cmd 窗口**；
- `asr_service._run_ytdlp()` —— 抽音频时闪；
- `social_video` 的 yt-dlp 探测 —— 抓取时闪。

Windows 上 `subprocess` 默认给子进程一个新控制台（因为父进程是
windowed 应用时，控制台子系统程序会自己申请一个）。必须显式加
`CREATE_NO_WINDOW` 才能彻底不弹。

**用法**：所有子进程都用这里的 `run()` / `popen()`，不要直接调
`subprocess`。例外是「需要用户看见窗口」的登录流程 —— 那走
`cli_locator.open_login_window()`，它自己带 `CREATE_NEW_CONSOLE`。
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def _no_window_flags() -> int:
    """返回"不弹窗口"的 creationflags（非 Windows 返回 0）。"""
    if sys.platform != "win32":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """执行命令并等待（**不弹窗口**）。

    参数与 `subprocess.run` 相同，只是自动补 creationflags。
    若调用方显式传了 creationflags，以调用方为准。
    """
    kwargs.setdefault("creationflags", _no_window_flags())
    # stdin=DEVNULL：避免子进程等输入（服务是后台跑的，没有终端）
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(cmd, **kwargs)


def popen(cmd: list[str], **kwargs: Any) -> subprocess.Popen:
    """启动子进程不等待（**不弹窗口**）。"""
    kwargs.setdefault("creationflags", _no_window_flags())
    return subprocess.Popen(cmd, **kwargs)
