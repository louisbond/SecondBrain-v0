"""前端渲染自测（headless Edge 渲染真实页面并检查 DOM）。

验证的不只是"页面能打开"，而是 Vue 应用真的挂载了、内容真的渲染出来了——
这能捕捉到组件运行时错误、API 调用失败、路由未匹配等问题。

用法：python tests/frontend_render_test.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
BASE = os.environ.get("SB_BASE", "http://127.0.0.1:7688")

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []

# 每个页面必须出现的关键文案（来自组件模板）
PAGE_EXPECTATIONS: dict[str, list[str]] = {
    "/": [
        "知识图谱",
        "个人手稿",
    ],
    "/workbench": [
        "采集",
        # v33：「采集」与「解析」左右并列（原「解析工作区」已改名）
        "解析",
        # v31：「智能解析」+「入库」合并成「解析并入库」
        "解析并入库",
        # 状态横条嵌在采集与解析之间（不是一级导航）
        "任务状态",
    ],
    "/status": [
        "全部",
        "进行中",
        "已完成",
        "失败",
    ],
    # 注：「解析模板/目标字数/确认入库」在**选中条目后**才出现（数据依赖内容）
    # 注：加工区**不要**任务状态条（待加工工作区已经列出同样的任务，重复了）
    "/process": [
        "待加工",
        "核准",
    ],
    "/original": [
        "创作",
        "空模板",
        # v35：模板换成兵哥库「我的知识库」的 B 层标准格式
        "知识提炼",
        "知识聚合",
        "保存为创作笔记",
    ],
    "/manage": [
        "分区树",
        "标签",
        "解析模板",
        "Prompt",
        "维护与诊断",
        "平台能力",
    ],
    # 设置是一级页 + Tab（与管理区同款）；默认显示 AI 组，
    # 「存储/系统」组要点 Tab 才渲染（同管理区的 Tab 行为）
    "/settings": [
        "AI Provider",
        "API 密钥",
        "常用预设",
        "存储",
        "系统",
    ],

}


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append((name, detail))
        print(f"  ✗ {name}  {detail}")


def render(path: str, timeout: int = 90) -> str:
    """用 headless Edge 渲染页面并返回 DOM。"""
    out = Path(tempfile.gettempdir()) / f"sb_render_{abs(hash(path))}.html"
    cmd = [
        EDGE,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--virtual-time-budget=6000",
        "--dump-dom",
        BASE + path,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout, shell=False
        )
        text = proc.stdout.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return ""
    finally:
        out.unlink(missing_ok=True)
    return text


def app_body(html: str) -> str:
    """取出 #app 容器的渲染内容。

    Vue 挂载后容器会带上 data-v-app=""，所以不能按 `<div id="app">` 精确匹配。
    """
    m = re.search(r'<div id="app"[^>]*>(.*?)</body>', html, re.S)
    return m.group(1) if m else ""


def seed_task() -> bool:
    """造一条**待核准**素材，让核准页能选中条目（否则只有空态）。

    工作区只需要"有条目"，加工区需要"选中了条目"——所以统一造
    pending_review 状态的条目，两边都能渲染出内容。
    """
    import urllib.request

    # ① 造速记条目
    body = json.dumps(
        {"text": "渲染自测用的速记内容：验证工作区右栏（原文/配图）能正常渲染。"}
    ).encode()
    req = urllib.request.Request(
        f"{BASE}/api/collect/quick-note",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def check_vocab_chips_render(html: str) -> bool:
    """词表 chip 必须渲染出名字，而不是空的复选框。

    回归 bug：vocab 降级成字符串数组后，模板仍用 t.name → 渲染成 `☐ ☐`。
    """
    import re

    # 找 check-chip 元素，看里面有没有非空的文本
    chips = re.findall(r'class="check-chip[^"]*"[^>]*>(.*?)</span>\s*</div>', html, re.S)
    if not chips:
        # 退而求其次：找所有 check-chip 片段
        chips = re.findall(r'class="check-chip[^"]*"[^>]*>(.{0,200}?)</span>', html, re.S)
    for chunk in chips:
        text = re.sub(r"<[^>]+>", "", chunk)
        text = re.sub(r"\s+", " ", text).strip()
        if text and text not in ("☑", "☐"):
            return True
    return False


def main() -> int:
    print(f"前端渲染自测 @ {BASE}\n" + "=" * 60)

    if not Path(EDGE).exists():
        print(f"未找到 Edge：{EDGE}")
        return 2

    for path, keywords in PAGE_EXPECTATIONS.items():
        print(f"\n── {path} ──")
        if path in ("/workbench", "/process", "/process/parse"):
            # 右栏（原文 / 配图 / 确认入库 / 素材选择）需要有条目才出现
            seed_task()
            time.sleep(1.5)
        html = render(path)
        if not html:
            check(f"{path} 渲染", False, "Edge 超时或无输出")
            continue

        body = app_body(html)
        check(
            f"{path} Vue 应用已挂载",
            len(body) > 800,
            f"#app 内容 {len(body)} 字符",
        )

        # 数据依赖的文案：按条目状态才渲染（手动补充/重试 AI 只出现在失败条目上，
        # 核准驾驶舱只在选中条目后出现），不属于页面级不变量。
        missing = [kw for kw in keywords if kw not in html]
        check(
            f"{path} 关键内容完整",
            not missing,
            f"缺少 {missing}",
        )

        # 不应出现 Vue 运行时错误提示
        check(
            f"{path} 无运行时错误",
            "Vue warn" not in html and "Unhandled" not in html,
            "检测到错误提示",
        )

        # 注：词表 chip 的渲染检查放在 scripts/user_journey_test.py——
        # 隔离实例的词表是空的（无笔记 → 无标签），断言在隔离环境不成立。

        # 侧边导航必须完整（所有页面共享）
        for nav in ("首页", "采集区", "加工区", "原创区", "管理区", "设置"):
            if nav not in html:
                check(f"{path} 导航完整", False, f"缺少导航项 {nav}")
                break
        else:
            check(f"{path} 导航完整", True)

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("\n失败明细：")
        for name, detail in FAILED:
            print(f"  · {name} — {detail}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    # 隔离实例：绝不碰用户的真实 vault
    from harness import IsolatedServer

    with IsolatedServer() as _srv:
        os.environ["SB_BASE"] = _srv.base
        globals()["BASE"] = _srv.base
        print(f"[隔离实例] {_srv.base}  vault={_srv.vault}")
        sys.exit(main())
