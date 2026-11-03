"""真实管线自测（打运行中的服务）。

与 e2e_smoke.py 的区别：这里用**真实 B站链接**跑完整条链路，
验证「粘贴 → 解析 → 入队 → 抓取 → 落盘 → AI → 核准 → 搜索命中」。

用法：python tests/live_pipeline_test.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("SB_BASE", "http://127.0.0.1:7688")

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, _maybe_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, _maybe_json(raw)
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def _maybe_json(raw: str):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append((name, detail))
        print(f"  ✗ {name}  {detail}")


def section(title: str) -> None:
    print(f"\n── {title} ──")


def main() -> int:
    print(f"真实管线自测 @ {BASE}\n" + "=" * 60)

    # ---- 1. 真实 B站链接走完整管线 ------------------------------------
    section("1. 真实 B站链接 → 抓取 → A 层落盘")

    paste = (
        "【Rick Astley - Never Gonna Give You Up】 "
        "https://www.bilibili.com/video/BV1GJ411x7h7 "
        "复制本条信息，打开B站搜索，直接观看视频！"
    )
    status, data = call("POST", "/api/collect/parse", {"text": paste})
    check("解析分享文案", status == 200 and data.get("count") == 1, str(data)[:120])
    if isinstance(data, dict) and data.get("items"):
        item = data["items"][0]
        check("平台识别为 B站", item["platform"] == "bilibili", item["platform"])
        check("标题提取成功", bool(item.get("title")), item.get("title", ""))

    status, data = call(
        "POST",
        "/api/collect/submit",
        {
            "entries": [
                {
                    "url": "https://www.bilibili.com/video/BV1GJ411x7h7",
                    "platform": "bilibili",
                    "title": "",
                }
            ]
        },
    )
    check("提交入队", status == 200, f"HTTP {status}")
    item_id = None
    if isinstance(data, dict):
        accepted = data.get("accepted", [])
        dup = data.get("duplicates", [])
        if accepted:
            item_id = accepted[0]["id"]
            check("新条目已入队", True)
        elif dup:
            # 上次跑过 → 精确去重正确拦下，复用该条目继续验证
            item_id = dup[0].get("id")
            check("重复 URL 被精确去重（复跑幂等）", True, f"条目 #{item_id}")

    # 用唯一 URL 验证"新条目确实能被接受"
    unique_url = f"https://www.bilibili.com/video/BV1Live{int(time.time()) % 1000000:06d}"
    status, data2 = call(
        "POST",
        "/api/collect/submit",
        {"entries": [{"url": unique_url, "platform": "bilibili", "title": "唯一性验证"}]},
    )
    if isinstance(data2, dict):
        check(
            "全新 URL 被接受",
            len(data2.get("accepted", [])) == 1,
            str(data2.get("summary")),
        )

    if item_id:
        # 等后台抓取完成
        deadline = time.time() + 90
        status_final = ""
        while time.time() < deadline:
            _, cur = call("GET", f"/api/items/{item_id}")
            if isinstance(cur, dict):
                status_final = cur.get("status", "")
                if status_final in (
                    "extracted",
                    "extract_failed",
                    "pending_review",
                    "ai_processing",
                    "ai_failed",
                ):
                    break
            time.sleep(2)

        check("抓取流程有终态", bool(status_final), f"status={status_final}")

        _, content = call("GET", f"/api/items/{item_id}/content")
        if isinstance(content, dict):
            body = str(content.get("body") or "")
            check("A 层 md 已落盘", bool(body.strip()), f"body 长度 {len(body)}")
            check(
                "落盘内容含标题",
                "原始内容" in body,
                body[:80].replace("\n", " "),
            )
            check(
                "frontmatter 完整",
                content.get("frontmatter", {}).get("type") == "inbox",
                str(content.get("frontmatter", {}).get("type")),
            )
            check(
                "platform 记录为 bilibili",
                content.get("frontmatter", {}).get("platform") == "bilibili",
                str(content.get("frontmatter", {}).get("platform")),
            )

        _, item = call("GET", f"/api/items/{item_id}")
        if isinstance(item, dict):
            print(
                f"     · 状态={item.get('status')} extractor={item.get('extractor')} "
                f"asr={item.get('asr')} 长度={item.get('raw_length')}"
            )
            if item.get("error_detail"):
                print(f"     · 错误：{item['error_detail'][:150]}")

    # ---- 2. AI 全链路不可用时的降级 ----------------------------------
    section("2. AI 不可用时的降级与错误可见性")
    status, prov = call("GET", "/api/settings/providers")
    if isinstance(prov, dict):
        names = {p["name"]: p for p in prov.get("providers", [])}
        print(
        )
        print(
            f"     · ollama: {names.get('ollama', {}).get('detail', '')[:60]}"
        )
        check("Provider 健康状态可见", len(names) >= 2, str(list(names)))
        check(
            "降级状态被显式标记",
            isinstance(prov.get("fallback_active"), bool),
            str(prov.get("fallback_active")),
        )

    # ---- 3. 手动补充兜底（AI 不可用时也能入库）-------------------------
    section("3. 手动补充兜底")
    status, data = call(
        "POST",
        "/api/collect/submit",
        {
            "entries": [
                {
                    "url": f"https://example.com/live-fallback-{int(time.time())}",
                    "platform": "web",
                    "title": "手动补充测试",
                }
            ]
        },
    )
    manual_id = None
    if isinstance(data, dict) and data.get("accepted"):
        manual_id = data["accepted"][0]["id"]
    if manual_id:
        status, res = call(
            "POST",
            f"/api/items/{manual_id}/manual-fill",
            {
                "text": "这是自测时手动粘贴的正文内容，用来验证失败兜底路径。"
                * 10,
                "title": "手动补充测试",
            },
        )
        check("行内手动补充成功", status == 200, f"HTTP {status}")
        if isinstance(res, dict):
            check(
                "补充后进入 extracted",
                res.get("item", {}).get("status") == "extracted",
                str(res.get("item", {}).get("status")),
            )

    # ---- 4. 核准闭环（B 层落盘 + 搜索命中）-----------------------------
    section("4. 核准闭环 → B 层落盘 → 搜索命中")
    # 取一条待核准条目；AI 不可用时用"人工处理"队列里的条目（手写摘要入库）
    status, pending = call("GET", "/api/review/pending")
    review_id = None
    ai_available = True
    if isinstance(pending, dict):
        items = pending.get("items", [])
        manual = pending.get("manual", [])
        failed = pending.get("ai_failed", [])
        check("待核准队列可读", isinstance(items, list), f"{len(items)} 条")
        check("人工处理队列可读", isinstance(manual, list), f"{len(manual)} 条")
        if items:
            review_id = items[0]["id"]
        elif manual:
            review_id = manual[0]["id"]
            ai_available = False
            print("     · AI 不可用，改走人工核准路径")
        elif failed:
            review_id = failed[0]["id"]
            ai_available = False
            print("     · AI 不可用，改走 AI 失败条目的手工核准")

    if review_id:
        status, page = call("GET", f"/api/review/{review_id}")
        check("核准页数据可取", status == 200, f"HTTP {status}")
        if isinstance(page, dict):
            check(
                "核准页含 AI 摘要位",
                "ai_summary" in page,
                str(list(page.keys()))[:120],
            )
            check(
                "核准页含受控词表",
                "vocab" in page,
                str(list(page.keys()))[:120],
            )

        status, res = call(
            "POST",
            f"/api/review/{review_id}/confirm",
            {
                "title": "自测用第一性原理条目",
                "personal_summary": "我的理解：先回到基本事实，再重新推导。",
                "tags": ["思维模型"],
                "partitions": [],
                "confirmed_related": [],
                "ai_summary": "- 要点一：拆解到最基本的事实与公理\n- 要点二：从基本事实重新推导",
            },
        )
        check("确认入库成功", status == 200, f"HTTP {status} {str(res)[:120]}")
        if isinstance(res, dict) and res.get("path"):
            check("B 层文件已写入", Path(res["path"]).exists(), res["path"])
            check("手稿非空 → digested", res.get("digested") is True, str(res.get("digested")))

            # B 层 frontmatter 语义
            content = Path(res["path"]).read_text(encoding="utf-8")
            check("B 层 type=processed", "type: processed" in content)
            check("B 层含 tags", "tags:" in content)
            check("B 层含 ai_suggested_tags（可追溯）", "ai_suggested_tags:" in content)
            check("B 层含 confirmed_related", "confirmed_related:" in content)

        # 搜索命中
        status, found = call("POST", "/api/search", {"query": "第一性原理"})
        if isinstance(found, dict):
            check("搜索命中", found.get("total", 0) >= 1, f"total={found.get('total')}")
            titles = [r.get("title", "") for r in found.get("results", [])]
            check(
                "结果含刚入库的条目",
                any("第一性原理" in t for t in titles),
                str(titles[:3]),
            )
            check(
                "标题未被分词污染",
                all(" " not in t.strip() or len(t.strip()) < 3 for t in titles[:3]),
                str(titles[:3]),
            )

    # ---- 5. 队列与任务一致性 -----------------------------------------
    section("5. 队列一致性")
    status, items = call("GET", "/api/items?limit=200")
    if isinstance(items, dict):
        lst = items.get("items", [])
        check("条目可列出", len(lst) > 0, f"{len(lst)} 条")
        # 状态合法性
        legal = {
            "queued", "extracting", "extracted", "extract_failed",
            "ai_processing", "ai_failed", "pending_review", "reviewed",
        }
        bad = [i for i in lst if i.get("status") not in legal]
        check("所有状态合法", not bad, str([b.get("status") for b in bad[:5]]))
        # source_url 唯一
        urls = [i.get("source_url") for i in lst if i.get("source_url")]
        check(
            "source_url 唯一（精确去重生效）",
            len(urls) == len(set(urls)),
            f"{len(urls)} 条中有 {len(urls) - len(set(urls))} 个重复",
        )

    status, jobs = call("GET", "/api/jobs")
    if isinstance(jobs, dict):
        stats = jobs.get("stats", {})
        check("任务统计可用", "by_state" in stats, str(stats))
        # 滞留判定：running 且超过 5 分钟没更新 = 卡死（不是"正在忙"）
        from datetime import datetime, timedelta

        stale_cut = (datetime.now() - timedelta(minutes=5)).isoformat(sep=" ", timespec="seconds")
        stale = [
            j
            for j in jobs.get("jobs", [])
            if j.get("state") == "running"
            and str(j.get("updated_at") or "") < stale_cut
        ]
        check("无卡死的 running 任务（>5 分钟未更新）", not stale,
              f"running={stats.get('running')}，卡死 {len(stale)} 个")

    # ---- 6. 索引一致性 ------------------------------------------------
    section("6. 索引与 md 一致性")
    status, reidx = call("POST", "/api/settings/reindex")
    check("重建索引成功", status == 200, f"HTTP {status}")
    if isinstance(reidx, dict):
        check(
            "重建耗时 < 60s",
            reidx.get("elapsed_ms", 999999) < 60000,
            f"{reidx.get('elapsed_ms')}ms",
        )
        check("重建索引数 > 0", reidx.get("indexed", 0) > 0, str(reidx.get("indexed")))

    status, after = call("POST", "/api/search", {"query": "第一性原理"})
    if isinstance(after, dict):
        check("重建后仍能搜到", after.get("total", 0) >= 1, f"total={after.get('total')}")

    # ---- 7. 汇总 ------------------------------------------------------
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
