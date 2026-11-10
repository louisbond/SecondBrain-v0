"""工作区新功能的端到端验证（真实 AI 调用，隔离实例）。

验证：
1. 解析模板 CRUD（增 / 改 / 删）
2. AI 自动匹配模板 + 生成解析结果，且**不少于 1000 字**
3. 不足字数会被拒绝并重新生成（用假 provider 快速验证）
4. 配图插入指定位置
5. 实时状态接口
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "tests")

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append((name, detail))
        print(f"  ✗ {name}  {detail}")


def call(base: str, method: str, path: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")[:300]
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:300]


LONG_TEXT = (
    "本节课讨论深度工作与注意力管理的关系。研究者发现，人的注意力在被打断后平均需要二十三分钟才能恢复到原来的专注水平，"
    "这意味着频繁切换任务的代价远高于大多数人的直觉。\n\n"
    "第一，注意力残留。被打断后，即使回到原任务，一部分注意力仍然停留在上一件事上，"
    "导致产出质量下降。一个常见的应对方式是把未完成的事项显式写下来，把外部记忆交给纸笔。\n\n"
    "第二，深度工作的前提是完整的时间块。把同样的两小时切成四段，每段三十分钟，"
    "几乎无法进入深度状态；而连续两个小时的效果会明显不同。这对日程安排的含义是："
    "重要的认知任务应当被放在一天中精力最好的时段。\n\n"
    "第三，环境设计比意志力更可靠。把手机放在另一个房间，比反复提醒自己不要看手机有效得多。"
    "类似地，把需要专注的工作安排在没有通知的环境里，成功率会显著提升。\n\n"
    "第四，刻意练习与深度工作相关但不相同。刻意练习强调在略高于当前能力的区间训练并获得反馈；"
    "深度工作强调在没有干扰的状态下专注。两者结合才能产生长期的能力增长。\n\n"
    "最后是复盘。每次深度工作之后花五分钟记录做了什么、卡在哪里，比事后凭记忆总结更可靠。"
    "复盘让下一次能够直接改进，而不是重复同样的错误。"
) * 3


def main(base: str) -> int:
    print(f"工作区端到端验证 @ {base}\n" + "=" * 60)

    # ---- 1. 模板 CRUD ----
    print("\n── 1. 解析模板 CRUD ──")
    st, r = call(base, "POST", "/api/workbench/templates",
                  {"name": "自检模板", "body": "请按如下结构输出：\n1. 核心论点\n2. 展开说明\n3. 可落地做法",
                   "description": "端到端自检用"})
    check("新增模板", st == 200 and isinstance(r, dict) and r.get("template"), f"{st} {r}")
    tpl_id = (r or {}).get("template", {}).get("id") if isinstance(r, dict) else None

    st, r = call(base, "PUT", f"/api/workbench/templates/{tpl_id}",
                  {"body": "请按如下结构输出：\n1. 核心论点\n2. 展开说明\n3. 可落地做法（更新版）"})
    check("编辑模板", st == 200 and "更新版" in (r or {}).get("template", {}).get("body", ""), f"{st} {r}")

    st, r = call(base, "GET", "/api/workbench/templates")
    names = [t["name"] for t in (r or {}).get("templates", [])]
    check("模板列表含默认模板与自检模板", "要点摘要" in names and "自检模板" in names, str(names))

    # ---- 2. 造长内容条目 ----
    print("\n── 2. 准备内容 ──")
    st, r = call(base, "POST", "/api/collect/quick-note", {"text": LONG_TEXT})
    item_id = (r or {}).get("id") if isinstance(r, dict) else None
    check("创建条目", st == 200 and item_id, f"{st} {r}")

    # ---- 3. AI 自动匹配模板 + 生成（≥1000 字）----
    print("\n── 3. AI 自动匹配模板并生成解析结果 ──")
    t0 = time.time()
    st, r = call(base, "POST", "/api/workbench/parse",
                  {"item_id": item_id, "template_id": None, "min_chars": 1000})
    dt = time.time() - t0
    check("解析接口返回成功", st == 200 and isinstance(r, dict) and r.get("ok"), f"{st} {str(r)[:200]}")
    if isinstance(r, dict) and r.get("ok"):
        check("字数 ≥ 1000", int(r.get("chars", 0)) >= 1000, f"实际 {r.get('chars')} 字")
        check("自动匹配了模板", r.get("matched_by") == "ai", str(r.get("matched_by")))
        check("返回模板名", bool((r.get("template") or {}).get("name")), str(r.get("template")))
        check("内容不是 Python repr", "{" not in (r.get("content") or "")[:200], "疑似 repr")
        print(f"    （耗时 {dt:.0f}s，{r.get('chars')} 字，模板「{(r.get('template') or {}).get('name')}」）")
        print("    开头:", (r.get("content") or "")[:90].replace("\n", " "))

    # ---- 4. 不足字数会被拒绝（用真实 AI 跑一次很慢，这里只验参数校验；
    #         完整的"不足→重新生成"逻辑由 TestWorkbench 用假 provider 覆盖）----
    print("\n── 4. 不足字数的处理 ──")
    st, r = call(base, "POST", "/api/workbench/parse",
                  {"item_id": item_id, "template_id": tpl_id, "min_chars": 0})
    check("非法阈值被拒", st == 422, f"{st} {str(r)[:120]}")

    # ---- 5. 配图插入 ----
    print("\n── 5. 配图插入指定位置 ──")
    st, r = call(base, "POST", f"/api/workbench/items/{item_id}/screenshots",
                  {"position": 1, "image_ref": "assets/selfcheck.png", "caption": "自检配图"})
    check("插入配图成功", st == 200 and isinstance(r, dict) and r.get("ok"), f"{st} {str(r)[:160]}")
    check("配图登记到 frontmatter", isinstance(r, dict) and len(r.get("screenshots") or []) == 1,
          str(r.get("screenshots") if isinstance(r, dict) else r))
    st, r = call(base, "GET", f"/api/items/{item_id}/content")
    body = (r or {}).get("body", "")
    check("正文里出现图片引用", "assets/selfcheck.png" in body, body[:120])

    st, r = call(base, "DELETE", f"/api/workbench/items/{item_id}/screenshots/0")
    check("移除配图成功", st == 200 and isinstance(r, dict) and r.get("ok"), f"{st} {str(r)[:120]}")
    st, r = call(base, "GET", f"/api/items/{item_id}/content")
    check("正文里图片已移除", "assets/selfcheck.png" not in (r or {}).get("body", ""), "")

    # ---- 6. 实时状态 ----
    print("\n── 6. 实时状态 ──")
    st, r = call(base, "GET", "/api/workbench/status?limit=20")
    ok = st == 200 and isinstance(r, dict) and "items" in r and "stats" in r
    check("状态接口可用", ok, f"{st}")
    if ok:
        has_stage = all("stage" in i and "progress" in i for i in r["items"])
        check("每条都有阶段与进度", has_stage, "")
        print("    stats:", r["stats"])

    # ---- 7. 删除模板 ----
    st, r = call(base, "DELETE", f"/api/workbench/templates/{tpl_id}")
    check("删除模板", st == 200, f"{st} {str(r)[:120]}")

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
    from harness import IsolatedServer

    with IsolatedServer() as srv:
        os_base = srv.base
        print(f"[隔离实例] {os_base}  vault={srv.vault}")
        sys.exit(main(os_base))
