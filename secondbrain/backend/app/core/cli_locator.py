"""CodeBuddy CLI 定位与调用封装。

**背景**：设计文档 v0.5.0 附录 A 假设了 `wbuddy` / `codebuddy` 两个命令。
实机核查结论（Windows + WorkBuddy 桌面版）：

- `wbuddy` **不存在**——它是设计文档作者的臆测命名。语义上对应的真实命令是
  `codebuddy daemon start` / `codebuddy daemon status`（daemon supervisor）；
- `codebuddy` **存在，但不在 PATH**：WorkBuddy 桌面版自带了同一套引擎
  （npm 包名 `@genie/agent-cli`），入口在
  `<WorkBuddy 安装目录>/resources/app.asar.unpacked/cli/bin/codebuddy`
  （一个 Node 脚本，要求 Node ≥ 18.20.8）；
- CLI 的三个关键 flag 与设计文档一致，已实机核对：
  `-p/--print`、`--output-format json`、`-y/--dangerously-skip-permissions`。

**两个必须处理的坑**：

1. **端口冲突**：CLI 会尝试在 `CODEBUDDY_SERVICE_PROXY_URL` 指定的端口
   （WorkBuddy 桌面端占着 5452）上再绑一个服务代理 →
   `EADDRINUSE`。解决：调用前剥离该环境变量，让它自动分配端口。
2. **代理环境变量污染**：机器上开着 HTTP_PROXY/HTTPS_PROXY 时，CLI 内部的
   `setProxy` 会抛 `Right-hand side of 'instanceof' is not callable`，
   并刷一屏堆栈。解决：调用前剥离代理变量（CLI 有自己的代理配置机制）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

# 调用 CLI 时要剥离的环境变量
# （前两个是 WorkBuddy 桌面端占用的服务代理端口，会导致 EADDRINUSE；
#   后四个是系统代理，会让 CLI 内部 setProxy 崩溃并刷堆栈）
STRIP_ENV = (
    "CODEBUDDY_SERVICE_PROXY_URL",
    "CODEBUDDY_HOST",
    "WORKBUDDY_PROXY_SOURCE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)

# WorkBuddy 桌面版的 CLI 相对路径
_CLI_RELATIVE = Path("resources/app.asar.unpacked/cli/bin/codebuddy")

# Windows 上常见的 WorkBuddy 安装位置（用于自动探测）
_WB_INSTALL_HINTS = (
    Path("D:/AllApp666/HRAppStoreDownload/WorkBuddy"),
    Path("C:/Program Files/WorkBuddy"),
    Path("C:/Program Files (x86)/WorkBuddy"),
    Path.home() / "AppData/Local/Programs/WorkBuddy",
)


@dataclass
class CLIInfo:
    """CLI 定位结果。"""

    path: Path | None = None
    node: str | None = None
    source: str = ""            # builtin / workbuddy / path
    is_node_script: bool = False

    @property
    def available(self) -> bool:
        return self.path is not None and (self.node is not None or not self.is_node_script)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else "",
            "node": self.node or "",
            "source": self.source,
            "is_node_script": self.is_node_script,
            "available": self.available,
        }


def _is_node_script(path: Path) -> bool:
    """判断是不是需要 node 解释的脚本（无扩展名但首行有 shebang node）。"""
    try:
        with path.open("rb") as fh:
            head = fh.read(64)
    except OSError:
        return False
    if path.suffix.lower() in (".exe", ".cmd", ".bat"):
        return False
    return head.startswith(b"#!") and b"node" in head.split(b"\n", 1)[0]


def _find_node() -> str | None:
    """找 node 可执行文件（CLI 是 node 脚本）。"""
    found = shutil.which("node")
    if found:
        return found
    # WorkBuddy 自带的 node
    candidates = [
        Path.home() / ".workbuddy/binaries/node",
    ]
    for base in candidates:
        if not base.exists():
            continue
        for ver in sorted(base.glob("versions/*"), reverse=True):
            for name in ("node.exe", "node"):
                candidate = ver / name
                if candidate.exists():
                    return str(candidate)
    return None


def _workbuddy_cli_candidates() -> list[Path]:
    """WorkBuddy 安装目录下的 CLI 候选路径。"""
    out: list[Path] = []

    # 1) 从当前进程推断（WorkBuddy 桌面端自身）
    #    python.exe 在 <安装目录>/resources/vendor/python/，
    #    CLI 在 <安装目录>/resources/app.asar.unpacked/cli/bin/ —— 往上找几层即可
    exe = Path(os.sys.executable) if os.sys.executable else None
    if exe:
        for parent in list(exe.parents)[:6]:
            candidate = parent / _CLI_RELATIVE
            if candidate.exists():
                out.append(candidate)
                break

    # 2) 常见安装位置
    for base in _WB_INSTALL_HINTS:
        candidate = base / _CLI_RELATIVE
        if candidate.exists() and candidate not in out:
            out.append(candidate)

    # 3) 整个 D:/AllApp666 这类自定义盘（兜底浅层扫描）
    for drive_root in (Path("D:/AllApp666"), Path("E:/AllApp666")):
        if not drive_root.exists():
            continue
        for store in drive_root.glob("*/WorkBuddy"):
            candidate = store / _CLI_RELATIVE
            if candidate.exists() and candidate not in out:
                out.append(candidate)

    return out


def locate_cli(explicit: str | None = None) -> CLIInfo:
    """定位 CodeBuddy CLI。

    顺序：显式配置 → 内置 bin/ → WorkBuddy 安装目录 → PATH。
    """
    cfg_dir = Path(__file__).resolve().parents[2]  # backend/
    builtin_dir = cfg_dir.parent / "bin"

    # 1) 显式配置
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            node = _find_node() if _is_node_script(p) else None
            return CLIInfo(
                path=p, node=node, source="config", is_node_script=_is_node_script(p)
            )

    # 2) 内置 bin/
    for name in ("codebuddy.exe", "codebuddy", "codebuddy.cmd"):
        candidate = builtin_dir / name
        if candidate.exists():
            return CLIInfo(
                path=candidate,
                node=None,
                source="builtin",
                is_node_script=_is_node_script(candidate),
            )

    # 3) WorkBuddy 安装目录
    for candidate in _workbuddy_cli_candidates():
        node = _find_node() if _is_node_script(candidate) else None
        return CLIInfo(
            path=candidate,
            node=node,
            source="workbuddy",
            is_node_script=_is_node_script(candidate),
        )

    # 4) PATH
    found = shutil.which("codebuddy")
    if found:
        p = Path(found)
        return CLIInfo(
            path=p,
            node=_find_node() if _is_node_script(p) else None,
            source="path",
            is_node_script=_is_node_script(p),
        )

    return CLIInfo()


def pick_free_port(start: int = 5460, attempts: int = 40) -> int:
    """挑一个空闲端口（给 CLI 的内部服务代理用）。

    CLI 的 HTTP server 默认端口 5452 是硬编码的，而 5452 正好被 WorkBuddy
    桌面端占着 → EADDRINUSE。主命令接受 `--port`，这里提前占一个空闲端口。
    """
    import socket

    for offset in range(attempts):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def write_login_script(info: CLIInfo, port: int | None = None) -> Path:
    """生成交互式登录用的 .cmd 脚本，返回脚本路径。

    为什么用脚本而不是 `cmd /c start`：
    `subprocess.Popen(["cmd","/c","start","cmd","/k", '"C:\\a b\\node.exe" "..."'])` 会被
    Python 的 list2cmdline 再加一层引号，cmd 最终把引号当成命令名的一部分 →
    `'"C:\\...\\node.exe"' 不是内部或外部命令`。写成 .cmd 再让 cmd 执行可完全绕开这层解析。

    脚本同时剥离代理环境变量（否则 CLI 内部 setProxy 会崩）并指定空闲端口
    （避开 5452 端口冲突）。
    """
    import tempfile

    if info.path is None:
        raise FileNotFoundError("未找到 codebuddy CLI")

    cli_path = str(info.path)
    cli_dir = str(Path(cli_path).parent)
    port = port or pick_free_port()

    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "title SecondBrain - CodeBuddy Login",
    ]
    # 剥离会引发端口冲突/代理崩溃的环境变量
    for key in STRIP_ENV:
        lines.append(f'set "{key}="')
    lines.append(f'cd /d "{cli_dir}"')
    if info.is_node_script and info.node:
        lines.append(f'"{info.node}" "{cli_path}" --port {port}')
    else:
        lines.append(f'"{cli_path}" --port {port}')
    lines += [
        "echo.",
        "echo [SecondBrain] Login finished? Press any key to close this window.",
        "pause >nul",
    ]

    text = "\r\n".join(lines) + "\r\n"
    try:
        # 纯 ASCII：cmd 在 chcp 生效前读首行，带 BOM 会把首行读成乱码
        payload = text.encode("ascii")
    except UnicodeEncodeError:
        # 路径含非 ASCII 时用无 BOM 的 UTF-8（chcp 65001 之后生效）
        payload = text.encode("utf-8")

    script = Path(tempfile.gettempdir()) / "secondbrain_cli_login.cmd"
    script.write_bytes(payload)
    return script


def build_env() -> dict[str, str]:
    """构造子进程环境：剥离会引发端口冲突/代理崩溃的变量。"""
    env = dict(os.environ)
    for key in STRIP_ENV:
        env.pop(key, None)
    return env


def run_cli(
    args: Sequence[str],
    *,
    timeout: int = 180,
    cwd: str | Path | None = None,
    info: CLIInfo | None = None,
) -> tuple[int, str, str]:
    """执行 codebuddy CLI，返回 (退出码, stdout, stderr)。"""
    info = info or locate_cli()
    if info.path is None:
        return 127, "", "未找到 codebuddy CLI"

    cmd: list[str] = []
    if info.is_node_script:
        if not info.node:
            return 127, "", "codebuddy 是 node 脚本，但系统里找不到 node"
        cmd.append(info.node)
    cmd.append(str(info.path))
    cmd.extend(args)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env=build_env(),
            cwd=str(cwd) if cwd else None,
            shell=False,
        )
    except FileNotFoundError:
        return 127, "", f"命令不存在：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"命令超时（{timeout}s）"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)

    return proc.returncode, proc.stdout or "", proc.stderr or ""
