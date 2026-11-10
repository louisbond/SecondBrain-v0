"""yt-dlp 统一调用层（v0.7.2）。

两个坑，都是实测出来的：

1. **系统代理会打断国内站点**。yt-dlp 的 urllib 后端在 Windows 上直接读
   注册表 `Internet Settings\\ProxyServer`，于是用户开着 Clash（127.0.0.1:26561）
   时，请求 v.kuaishou.com 也会被塞进代理；代理对域名做 TLS 拦截后报
   `[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate`。
   同一台机器加 `--proxy ""` 直连立刻恢复正常。
   → 策略：**先直连**，只有遇到网络类错误才回退系统代理
     （保留"必须挂代理才能出网"的场景）。

2. **原始报错对用户没有信息量**。用户看到的是
   `ERROR: [generic] nZZ1KENF: Unable to download webpage: [SSL: ...] (_ssl.c:1032)`。
   这里翻成人话：证书被代理拦了 / 分享链接失效 / 需要登录。

所有 yt-dlp 子进程都必须走 `run()`，它内部用 `app.core.proc` 保证不弹黑窗。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.core import proc as proc_mod
from app.core.config import get_config

# 出现这些字样说明"没连上"，值得换一种网络路径再试一次。
# 注意不要包含 `Unsupported URL` —— 那是网络已通、内容不支持，重试没意义。
_NETWORK_HINTS = (
    "certificate_verify_failed",
    "unable to get local issuer",
    "ssl:",
    "unable to download webpage",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "connection aborted",
    "temporary failure",
    "eof occurred",
    "getaddrinfo failed",
    "network is unreachable",
)

_MAX_ERROR = 300


@dataclass
class Result:
    """一次 yt-dlp 调用的结果（错误已翻译成中文）。"""

    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    used_system_proxy: bool = False


def find_bin() -> str:
    """定位 yt-dlp：内嵌 bin/ → 解释器同级 Scripts/ → PATH。

    第二步很重要：pip 装的 console script 落在 `<python-dir>/Scripts/`，
    该目录未必在 PATH 里，找不到会让整条管线直接失败。
    """
    cfg = get_config()
    for name in ("yt-dlp.exe", "yt-dlp"):
        candidate = cfg.bin_dir / name
        if candidate.exists():
            return str(candidate)

    install_dir = Path(sys.executable).resolve().parent
    for base in (install_dir, install_dir / "Scripts", install_dir / "bin"):
        for name in ("yt-dlp.exe", "yt-dlp"):
            candidate = base / name
            if candidate.exists():
                return str(candidate)

    return shutil.which("yt-dlp") or "yt-dlp"


def is_network_error(text: str) -> bool:
    low = (text or "").lower()
    return any(hint in low for hint in _NETWORK_HINTS)


def _error_line(stderr: str) -> str:
    """取 yt-dlp 的 ERROR 行，比整段 traceback 可读。"""
    detail = (stderr or "").strip()
    for line in detail.splitlines():
        if line.startswith("ERROR:"):
            return line
    # 没有 ERROR 前缀时取最后一行非空内容
    lines = [ln for ln in detail.splitlines() if ln.strip()]
    return lines[-1] if lines else "yt-dlp 执行失败"


def humanize(stderr: str, exit_code: int = 1) -> str:
    """把 yt-dlp 英文报错翻成能指导用户动作的中文。"""
    line = _error_line(stderr)
    low = line.lower()

    if "certificate_verify_failed" in low or "unable to get local issuer" in low:
        return (
            "HTTPS 证书校验失败：系统代理或 VPN 拦截了该站点。"
            "请把 kuaishou.com / douyin.com 加入代理软件的直连规则，"
            "或临时关闭系统代理后重试。"
        )
    if "unsupported url" in low:
        return (
            "分享链接已失效（平台直接跳回了首页），不是网络问题。"
            "请在 App 里重新复制分享链接后再采集。"
        )
    if "sign in" in low or "login required" in low or "cookies" in low:
        return "该内容需要登录才能访问，请改用采集区「导入视频/音频」上传本地文件。"
    if "video unavailable" in low or "404" in low or "not found" in low:
        return "内容不存在或已被删除（404）。"
    if "timed out" in low or "timeout" in low:
        return "连接超时：网络不通或被代理拦住了，稍后重试。"

    # 兜底：保留原始信息，但去掉 `ERROR: ` 前缀并截断
    cleaned = line[len("ERROR:") :].strip() if line.startswith("ERROR:") else line
    if len(cleaned) > _MAX_ERROR:
        cleaned = cleaned[: _MAX_ERROR - 1] + "…"
    return cleaned or f"yt-dlp 失败（退出码 {exit_code}）"


def _build_cmd(bin_path: str, args: list[str], url: str, direct: bool) -> list[str]:
    cmd = [bin_path]
    if direct:
        # 空字符串 = 不使用任何代理（yt-dlp 语义）
        cmd.extend(["--proxy", ""])
    # 直连时收紧连接超时，避免代理残留导致长时间挂住
    cmd.extend(["--socket-timeout", "15"])
    cmd.extend(args)
    cmd.append(url)
    return cmd


def run(
    args: list[str],
    url: str,
    *,
    timeout: float = 120,
    prefer_direct: bool = True,
) -> Result:
    """执行 yt-dlp，直连优先、网络类错误回退系统代理。

    `args` 不含可执行文件本身与 URL。
    """
    bin_path = find_bin()
    plans = [True, False] if prefer_direct else [False]

    last: Result | None = None
    for direct in plans:
        cmd = _build_cmd(bin_path, args, url, direct)
        try:
            proc = proc_mod.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
        except FileNotFoundError:
            return Result(ok=False, error="未找到 yt-dlp（请放到 bin/ 目录或安装到 PATH）")
        except subprocess.TimeoutExpired:
            last = Result(
                ok=False,
                error=f"yt-dlp 超时（{int(timeout)}s）：网络太慢或被代理拦住了。",
                used_system_proxy=not direct,
            )
            continue
        except Exception as exc:  # noqa: BLE001
            return Result(ok=False, error=f"yt-dlp 执行异常：{exc}")

        if proc.returncode == 0:
            return Result(
                ok=True,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                used_system_proxy=not direct,
            )

        stderr = proc.stderr or proc.stdout or ""
        last = Result(
            ok=False,
            stdout=proc.stdout or "",
            stderr=stderr,
            error=humanize(stderr, proc.returncode),
            used_system_proxy=not direct,
        )
        # 只有"没连上"才值得换代理再来一次；内容不支持/链接失效就别浪费时间
        if not is_network_error(stderr):
            break
        if direct:
            continue

    return last or Result(ok=False, error="yt-dlp 执行失败")
