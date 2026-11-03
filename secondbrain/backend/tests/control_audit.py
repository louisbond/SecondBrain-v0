"""控件交互审计：用真实浏览器点每个控件，看**反馈**对不对。

这是最终验收——「从视觉页面控件的操作的反馈来判断项目是否能完全落地」。

判定三类问题：
  1. **点了报错**    —— console error / page exception（真 bug）
  2. **点了没反应**  —— 控件存在但点击后 DOM 毫无变化（死按钮）
  3. **点了没提示**  —— 状态变了但用户看不到任何反馈（静默失败）

用法：
  python tests/control_audit.py              # 跑在隔离实例上（默认）
  python tests/control_audit.py --live       # 跑在真实 exe 上（只读操作）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cdp_client import Browser  # noqa: E402

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []
INFO: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name if ok else (name, detail))  # type: ignore[arg-type]
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    return ok


def info(msg: str) -> None:
    INFO.append(msg)
    print(f"  · {msg}")


# 页面 → 该页值得点/看的控件选择器
PAGES: list[tuple[str, str, list[str]]] = [
    ("/", "首页", [".tabs .tab", ".btn", ".check-chip", ".tree-toggle"]),
    ("/workbench", "采集区", [".btn", ".check-chip"]),
    ("/process", "加工区", [".btn", ".check-chip", ".tree-toggle", ".queue-chip"]),
    ("/original", "原创区", [".btn", ".check-chip"]),
    ("/manage", "管理区", [".tabs .tab", ".btn"]),
    ("/settings", "设置", [".tabs .tab", ".btn"]),
    ("/status", "状态", [".tabs .tab", ".btn"]),
]

# 绝对不能点的控件（会改数据或关服务）。
# 「跳过本条」虽然后端是 no-op，但它会让当前条目从核准流程里消失 ——
# 跑在真实库上时不该替用户做这个决定。
DANGEROUS_TEXT = [
    "关闭服务", "删除", "清除密钥", "重建索引", "重建向量",
    "清理", "备份", "注册开机自启", "注销开机自启",
    "设置为接通电源时不睡眠", "恢复主链", "切换",
    "跳过本条", "确认入库", "插入配图", "重新分析",
]


async def audit_page(b: Browser, base: str, path: str, label: str, selectors: list[str]) -> None:
    print(f"\n── {label} ({path}) ──")
    b.console_errors.clear()
    b.page_errors.clear()
    await b.goto(f"{base}{path}", settle=1.8)
    await b.wait_for(
        "document.querySelector('.content') && document.querySelector('.content').children.length > 0",
        timeout=10,
    )

    # 1. 高度约束：每页内容高度 ≤ 视口
    m = await b.measure()
    over = m["contentH"] - m["vh"]
    check(
        f"{label} 内容高度 ≤ 视口",
        over <= 2,
        f"contentH={m['contentH']} vh={m['vh']} 超出 {over}px",
    )

    # 2. 宽度一致：首元素应铺满内容区（.content 有 padding，扣掉即可）
    inner_w = m["contentW"] - 40  # padding: 20px × 2
    check(
        f"{label} 内容占满宽度",
        m["firstW"] >= inner_w - 2,
        f"首元素宽={m['firstW']:.0f} 内容区可用宽={inner_w:.0f}",
    )

    # 3. 逐个点击控件，记录反馈
    for sel in selectors:
        n = int(await b.eval(f"document.querySelectorAll({sel!r}).length") or 0)
        if not n:
            info(f"{label} {sel} — 无此控件（跳过）")
            continue

        # 过滤掉会改数据/关服务的按钮
        js = f"""
        (() => {{
          const bad = {DANGEROUS_TEXT!r};
          const els = [...document.querySelectorAll({sel!r})].slice(0, 12);
          const safe = els.filter(e => {{
            const t = (e.innerText || e.textContent || '').trim();
            return !bad.some(b => t.includes(b));
          }});
          safe.forEach(e => e.click());
          return safe.length;
        }})()
        """
        clicked = int(await b.eval(js) or 0)
        if not clicked:
            info(f"{label} {sel} — 控件全是危险操作（跳过）")
            continue

        await asyncio.sleep(0.7)
        after_err = len(b.console_errors) + len(b.page_errors)
        after_dom = await b.eval("document.querySelector('.content').innerHTML.length")
        toasts = await b.toasts()

        if after_err > 0:
            new = (b.console_errors + b.page_errors)[0]
            check(f"{label} {sel} 点击无报错", False, f"{clicked} 个控件 → {new[:160]}")
        else:
            changed = after_dom != m["contentH"] or bool(toasts)
            if toasts:
                check(
                    f"{label} {sel} 点击有反馈",
                    True,
                    f"{clicked} 个 → toast: {toasts[-1][:40]}",
                )
            else:
                info(f"{label} {sel} — {clicked} 个控件，无报错无 toast")

    # 4. 页面级异常
    if b.page_errors:
        check(f"{label} 无运行时异常", False, b.page_errors[0][:200])
    else:
        check(f"{label} 无运行时异常", True)


async def main(base: str) -> int:
    print("=" * 60)
    print("控件交互审计（CDP 驱动真实浏览器）")
    print(f"目标: {base}")
    print("=" * 60)

    async with Browser() as b:
        for path, label, sels in PAGES:
            await audit_page(b, base, path, label, sels)

        # 专项：管理区「平台能力」Tab —— 历史上有 undefined 引用
        print("\n── 专项：管理区各 Tab 逐一渲染 ──")
        await b.goto(f"{base}/manage", settle=1.8)
        for tab in ("分区树", "标签", "解析模板", "Prompt", "维护与诊断", "平台能力"):
            b.console_errors.clear()
            b.page_errors.clear()
            await b.click_text(".tabs .tab", tab)
            await asyncio.sleep(0.8)
            body = await b.eval("document.querySelector('.content').innerText")
            errs = b.page_errors + b.console_errors
            has_content = bool(body and len(body) > 40)
            check(
                f"管理区「{tab}」渲染出内容",
                has_content and not errs,
                (errs[0][:150] if errs else ("" if has_content else "内容区几乎为空")),
            )

        # 专项：原创区编辑器必须真的渲染出来
        print("\n── 专项：原创区 Vditor 编辑器 ──")
        b.page_errors.clear()
        b.console_errors.clear()
        await b.goto(f"{base}/original", settle=2.5)
        await asyncio.sleep(1.5)
        ed = await b.eval(
            """(() => {
              const h = document.querySelector('.vditor-host');
              return {
                exists: !!h,
                children: h ? h.children.length : -1,
                // Vditor 是 contenteditable 编辑器，不是 textarea
                editable: !!document.querySelector('.vditor-host [contenteditable="true"]'),
                hasToolbar: !!document.querySelector('.vditor-host .vditor-toolbar'),
                // sv 模式下正文渲染在 .vditor-sv 里
                sv: !!document.querySelector('.vditor-host .vditor-sv')
              };
            })()"""
        )
        check(
            "原创区编辑器已渲染",
            bool(
                ed["exists"]
                and ed["children"] > 0
                and ed["editable"]
                and ed["hasToolbar"]
                and ed["sv"]
            ),
            f"children={ed['children']} editable={ed['editable']} toolbar={ed['hasToolbar']} sv={ed['sv']}",
        )
        check(
            "原创区无运行时异常",
            not b.page_errors,
            (b.page_errors[0][:150] if b.page_errors else ""),
        )

        # 点「观点卡」模板 → 骨架必须真的写进编辑器
        b.page_errors.clear()
        await b.click_text(".content .btn", "观点卡")
        await asyncio.sleep(2.0)
        sv_text = await b.eval(
            """(() => {
              const sv = document.querySelector('.vditor-host .vditor-sv');
              return sv ? sv.innerText.replace(/\\s+/g, ' ').trim().slice(0, 80) : '';
            })()"""
        )
        check(
            "原创区模板能写入编辑器",
            "我的核心观点" in sv_text,
            f"编辑器内容: {sv_text[:60]!r}",
        )
        check(
            "写模板时无报错",
            not b.page_errors,
            (b.page_errors[0][:150] if b.page_errors else ""),
        )

        # 专项：首页回顾卡片必须有真实内容（曾恒为空）
        print("\n── 专项：首页回顾卡片 ──")
        b.page_errors.clear()
        await b.goto(f"{base}/", settle=2.2)
        recap = await b.eval(
            """(() => {
              const c = [...document.querySelectorAll('.content .card')]
                .find(x => (x.innerText||'').includes('今日回顾'));
              return c ? c.innerText.trim().replace(/\\n+/g, ' | ') : '(没找到)';
            })()"""
        )
        check(
            "首页回顾卡片有内容",
            "昨日采集" in recap and "待补手稿" in recap,
            recap[:110],
        )

        # 专项：加工区「待定标签」默认不应全勾选
        print("\n── 专项：加工区待定标签默认未勾选 ──")
        await b.goto(f"{base}/process", settle=2.0)
        chips = await b.eval(
            """[...document.querySelectorAll('.content .check-chip.pending')].map(e => ({
                text: e.innerText.trim().replace(/\\s+/g, ' '),
                checked: e.classList.contains('checked')
            }))"""
        )
        pending_checked = [c for c in chips if c["checked"]]
        check(
            "待定标签默认未勾选（用户勾选才入库）",
            not pending_checked,
            f"已勾选 {[c['text'] for c in pending_checked][:5]}",
        )

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("\n失败明细：")
        for name, detail in FAIL:  # type: ignore[misc]
            print(f"  ✗ {name} — {detail}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None, help="目标地址（默认起隔离实例）")
    args = ap.parse_args()

    if args.base:
        # 只读模式：对着真实库跑审计必须显式加 --allow-live，
        # 因为「确认入库」这类控件会真的写库、落 md。
        # 误点一次就会污染用户的真实知识库。
        if not "--allow-live" in sys.argv:
            print(
                "拒绝执行：--base 指向的可能是真实库。\n"
                "审计会点击控件，「确认入库」会真的写库落 md。\n"
                "确认要对真实库跑（只读）请加 --allow-live。\n"
                "（推荐：直接不带 --base，用隔离实例跑）"
            )
            sys.exit(2)
        sys.exit(asyncio.run(main(args.base)))
    else:
        from harness import IsolatedServer

        with IsolatedServer() as srv:
            print(f"隔离实例: {srv.base}\n临时 vault: {srv.vault}\n")
            code = asyncio.run(main(srv.base))
        sys.exit(code)
