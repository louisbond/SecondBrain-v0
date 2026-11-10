#!/usr/bin/env python3
"""一键检查（v39）——成熟软件的 `make check`。

以前要跑全套验证得记住 7 个命令、7 种参数组合、还要先起服务。
现在一条命令跑完，失败项一眼看到：

    python secondbrain/scripts/check.py            # 全套
    python secondbrain/scripts/check.py --quick    # 只跑不需要服务的（快）
    python secondbrain/scripts/check.py --list     # 列出所有检查项

退出码：0 = 全过，1 = 有失败（CI 可直接用）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"

BASE_URL = "http://127.0.0.1:7688"

# Windows 上 npm / npx 是 .cmd（不是 .exe），直接 subprocess 调 "npm" 会
# 报 FileNotFoundError。按平台挑可执行名。
NPM = "npm.cmd" if sys.platform == "win32" else "npm"
NPX = "npx.cmd" if sys.platform == "win32" else "npx"


@dataclass
class Check:
    name: str
    cmd: list[str]
    cwd: Path
    needs_server: bool = False
    timeout: int = 600


CHECKS: list[Check] = [
    # ---- 代码质量（v39：工具链进仓库，别只在个人电脑上生效）----
    Check("后端 lint", [sys.executable, "-m", "ruff", "check", "."], BACKEND, timeout=180),
    Check("前端 lint", [NPX, "eslint", "src"], FRONTEND, timeout=300),
    Check("格式检查", [NPX, "prettier", "--check", "src"], FRONTEND, timeout=300),
    # v42：类型检查。后端 mypy（配置在 backend/pyproject.toml），
    #       前端 JS 用 tsc --checkJs（配置在 frontend/jsconfig.json + env.d.ts）
    Check("后端类型检查", [sys.executable, "-m", "mypy", "app"], BACKEND, timeout=300),
    Check("前端类型检查", [NPX, "tsc", "-p", "jsconfig.json", "--noEmit"], FRONTEND, timeout=300),
    # v41：防止组件又长回 900 行巨石（棘轮：新文件 ≤500，历史文件不许再长）
    Check("前端结构", [sys.executable, "-m", "pytest", "tests/frontend_structure_test.py", "-q"], BACKEND, timeout=120),
    # ---- 功能 ----
    Check("后端单测", [sys.executable, "-m", "pytest", "tests/test_secondbrain.py", "tests/test_media_ocr.py", "tests/test_collect_adapters.py", "tests/test_item_lifecycle.py", "-q"], BACKEND, timeout=300),
    Check("前后端契约", [sys.executable, "-m", "pytest", "tests/frontend_contract_test.py", "-q"], BACKEND, timeout=300),
    Check("前端构建", [NPM, "run", "build"], FRONTEND, timeout=600),
    Check("E2E 冒烟", [sys.executable, "tests/e2e_smoke.py"], BACKEND, needs_server=True, timeout=600),
    Check("边界用例", [sys.executable, "tests/edge_case_test.py"], BACKEND, needs_server=True, timeout=600),
    Check("渲染检查", [sys.executable, "tests/frontend_render_test.py"], BACKEND, needs_server=True, timeout=600),
    Check("用户走查", [sys.executable, "scripts/user_journey_test.py"], BACKEND, needs_server=True, timeout=600),
    Check("控件审计", [sys.executable, "tests/control_audit.py"], BACKEND, needs_server=True, timeout=900),
]

# --quick 时跳过这些（要么慢，要么需要服务在跑）
QUICK_ONLY = {
    "后端 lint", "前端 lint", "格式检查",
    "后端单测", "前后端契约", "前端构建", "前端结构",
    "后端类型检查", "前端类型检查",
}


def server_alive() -> bool:
    import urllib.error
    import urllib.request

    # **必须绕过系统代理**：Windows 上 urllib 会读注册表里的 ProxyServer
    # （Clash 常见的 127.0.0.1:26561），而 ProxyOverride 往往不含回环地址，
    # 于是 http://127.0.0.1:7688/api/health 被代理拦成 502。
    # 表现是「服务明明在跑，门禁却说没起来」，非常误导。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{BASE_URL}/api/health", timeout=3) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def run(check: Check) -> tuple[bool, str, float]:
    started = time.time()
    try:
        proc = subprocess.run(
            check.cmd,
            cwd=str(check.cwd),
            capture_output=True,
            text=True,
            timeout=check.timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, f"超时（>{check.timeout}s）", time.time() - started
    except FileNotFoundError as exc:
        hint = ""
        if "npm" in str(exc) or "npx" in str(exc):
            hint = "（npm/npx 不在 PATH 里？装了 Node 吗？）"
        return False, f"命令不存在：{exc}{hint}", time.time() - started
    except Exception as exc:  # noqa: BLE001
        # ruff 没装是最常见的情况：给一句能直接抄的命令
        if "ruff" in check.name and "No module named" in str(exc):
            return False, "没装 ruff（pip install ruff）", time.time() - started
        if "mypy" in check.name and "No module named" in str(exc):
            return False, "没装 mypy（pip install mypy）", time.time() - started
        if "mypy" in check.name and "No module named" in str(exc):
            return False, "没装 mypy（pip install mypy）", time.time() - started
        return False, str(exc)[:200], time.time() - started
    cost = time.time() - started
    if proc.returncode == 0:
        return True, "通过", cost
    tail = (proc.stdout or "").strip().splitlines()[-6:]
    err = (proc.stderr or "").strip().splitlines()[-6:]
    detail = "\n".join(tail + err)[:800] or f"退出码 {proc.returncode}"
    return False, detail, cost


def main() -> int:
    ap = argparse.ArgumentParser(description="SecondBrain 一键检查")
    ap.add_argument("--quick", action="store_true", help="只跑不需要服务的检查")
    ap.add_argument(
        "--ci",
        action="store_true",
        help="CI 模式：同 --quick，只跑平台无关项（浏览器 E2E 需 Windows Edge + 本机服务）",
    )
    ap.add_argument(
        "--e2e",
        action="store_true",
        help="只跑需要本机服务的浏览器检查（5 项），给 Windows self-hosted runner 用",
    )
    ap.add_argument("--list", action="store_true", help="列出检查项")
    args = ap.parse_args()

    if args.list:
        for c in CHECKS:
            flag = "（需要服务）" if c.needs_server else ""
            print(f"  {c.name:12} {flag}")
        return 0

    quick = args.quick or args.ci
    if args.e2e:
        # 与 CI 的 9 项互补：Linux CI 跑平台无关项，Windows runner 只补浏览器项，
        # 不重复跑 lint / 类型 / 单测。
        selected = [c for c in CHECKS if c.needs_server]
        print("· E2E 模式（只跑需要本机服务的检查）")
    elif quick:
        selected = [c for c in CHECKS if c.name in QUICK_ONLY]
        print("· 快速模式（跳过需要服务的检查）")
    else:
        selected = list(CHECKS)

    if any(c.needs_server for c in selected):
        if not server_alive():
            print(f"✗ 后端没在 {BASE_URL} 跑起来")
            print("  先启动服务再跑需要联网的检查（或用 --quick 只跑本地检查）")
            print("  开发态启动：cd secondbrain/backend && python run.py")
            return 2
        print(f"✓ 后端在线 {BASE_URL}")
    print("=" * 60)

    results: list[tuple[str, bool, str, float]] = []
    for c in selected:
        print(f"▶ {c.name} …", flush=True)
        ok, detail, cost = run(c)
        results.append((c.name, ok, detail, cost))
        mark = "✓" if ok else "✗"
        print(f"  {mark} {c.name} — {detail}（{cost:.1f}s）")

    print("=" * 60)
    failed = [r for r in results if not r[1]]
    print(f"共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("\n失败项：")
        for name, _ok, detail, _cost in failed:
            print(f"  ✗ {name}")
            for line in detail.splitlines():
                print(f"      {line}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
