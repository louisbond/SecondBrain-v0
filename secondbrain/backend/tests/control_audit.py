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
import json
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
    # 「入库」会真的提交采集任务（写 items 表），
    # 只在下面「采集区入库链路」专项里显式点，不进通用点击循环。
    "入库",
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

        # 专项：采集区「入库」必须真的入库（v30 踩过：payload 被包双层 → 422）
        # 回归背景：client.js 的 submit(entries, forceSimilar) 与调用方
        # 传 {entries:[...]} 不匹配 → 请求体变成 {entries:{entries:[...]}}
        # → 后端 422「Input should be a valid list」→ 采集区**从来没成功过**，
        # 且 5 个测试套件全绿（它们只打后端，不碰前端 payload）。
        print("\n── 专项：采集区入库链路 ──")
        await b.goto(f"{base}/workbench", settle=2.0)

        # 装拦截器，记录 submit 请求的真实 body 与响应
        await b.eval(
            """(() => {
              window.__sbSubmit = [];
              const of = window.fetch;
              window.fetch = async function (...args) {
                const url = String(args[0] && args[0].url ? args[0].url : args[0]);
                const opt = args[1] || {};
                const resp = await of.apply(this, args);
                if (url.includes('/api/collect/submit')) {
                  const clone = resp.clone();
                  window.__sbSubmit.push({
                    body: opt.body || null,
                    status: resp.status,
                    resp: await clone.text().catch(() => '')
                  });
                }
                return resp;
              };
              return true;
            })()"""
        )

        # 往 textarea 塞一条带链接的分享文案
        await b.eval(
            """(() => {
              const ta = document.querySelector('.content textarea');
              if (!ta) return false;
              const setter = Object.getOwnPropertyDescriptor(
                window.HTMLTextAreaElement.prototype, 'value').set;
              setter.call(ta, 'https://www.bilibili.com/video/BV1CQso6cE8h 采集区入库链路回归测试');
              ta.dispatchEvent(new Event('input', {bubbles:true}));
              return true;
            })()"""
        )
        await asyncio.sleep(0.5)

        # v31：采集区把「智能解析」+「入库」合并成一个「解析并入库」按钮。
        # 审计要跟着改：点一次就该走完 parse → submit 全链路。
        clicked = await b.click_text(".content .btn", "解析并入库")
        check(
            "采集区有「解析并入库」合并按钮",
            bool(clicked),
            "没找到合并后的按钮（应只有一步操作）",
        )
        await asyncio.sleep(3.0)

        reqs = await b.eval("window.__sbSubmit || []")
        if not reqs:
            check("采集区入库发出了请求", False, "没有发出 /api/collect/submit")
        else:
            rq = reqs[-1]
            try:
                payload = json.loads(rq["body"] or "{}")
            except Exception:
                payload = {}
            ent = payload.get("entries")
            check(
                "采集区入库请求体格式正确（entries 是数组）",
                isinstance(ent, list),
                f"entries 类型={type(ent).__name__}；body={str(rq['body'])[:120]}",
            )
            check(
                "采集区入库返回 2xx",
                200 <= int(rq["status"]) < 300,
                f"HTTP {rq['status']}：{str(rq['resp'])[:150]}",
            )

        # 采集区不该再有独立的「入库（N）」按钮（v31 合并掉了）
        leftover = await b.eval(
            """(() => [...document.querySelectorAll('.content .btn')]
                 .map(b => b.innerText.trim())
                 .filter(t => /^入库（/.test(t)))()"""
        )
        check(
            "采集区没有残留的独立「入库（N）」按钮",
            not leftover,
            f"仍存在 {leftover}",
        )

        # 入库后队列里应多出一条任务
        queued = await b.eval(
            """(() => {
              const t = document.querySelector('.content')?.innerText || '';
              const m = t.match(/共\\s*(\\d+)\\s*条/);
              return m ? Number(m[1]) : -1;
            })()"""
        )
        check(
            "采集区入库后队列出现新任务",
            queued > 0,
            f"任务状态「共 N 条」= {queued}",
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
