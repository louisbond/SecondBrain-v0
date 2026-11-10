"""端到端 API 冒烟测试（对着运行中的服务打）。

用法：python tests/e2e_smoke.py [base_url]
覆盖：健康检查 → 采集解析 → 提交 → 队列 → 速记 → 核准 → 搜索 → 笔记 → 标签 → 回顾
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# v39：允许从**任意目录**跑（以前只能 cd backend 跑，否则 import app 失败）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = os.environ.get("SB_BASE", "http://127.0.0.1:7688")

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def call(method: str, path: str, body: dict | None = None):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, _maybe_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, _maybe_json(raw)
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def _maybe_json(raw: str):
    """响应可能是 JSON，也可能是 HTML（前端页面）。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append((name, detail))
        print(f"  ✗ {name}  {detail}")


def section(title: str) -> None:
    print(f"\n── {title} ──")


def main() -> int:
    print(f"SecondBrain E2E 冒烟测试 @ {BASE}\n" + "=" * 56)

    # ---- 1. 健康检查 --------------------------------------------------
    section("健康检查")
    status, data = call("GET", "/api/health")
    check("GET /api/health 返回 200", status == 200, f"got {status}")
    if isinstance(data, dict):
        # v39：版本号**不写死** —— 断言它等于后端唯一源 APP_VERSION。
        # 以前这里写死 "0.5.0"，每次升版本都要改测试，忘了就红。
        from app.core.config import APP_VERSION

        check("版本与 APP_VERSION 一致", data.get("version") == APP_VERSION,
              f"got {data.get('version')}, expect {APP_VERSION}")
        check("搜索模式已启用", data.get("search_mode") in ("jieba_bigram", "bigram"))
        check(
            "jieba 可用（无降级告警）",
            data.get("search_mode_warning") is None,
            str(data.get("search_mode_warning")),
        )

    # ---- 2. 采集解析 --------------------------------------------------
    section("采集解析（P0）")
    paste = (
        "【第一性原理是什么】 https://www.bilibili.com/video/BV1SmokeTest01 "
        "复制本条信息，打开B站搜索，直接观看视频！\n"
        "https://www.douyin.com/video/7123456789012345678\n"
        "https://www.xiaohongshu.com/explore/64f0a1b2c3d4e5f600112233"
    )
    status, data = call("POST", "/api/collect/parse", {"text": paste})
    check("POST /api/collect/parse 返回 200", status == 200, f"got {status}")
    if isinstance(data, dict):
        check("解析出 3 条链接", data.get("count") == 3, f"got {data.get('count')}")
        platforms = {i["platform"] for i in data.get("items", [])}
        check(
            "平台识别正确（bilibili/douyin/xiaohongshu）",
            platforms == {"bilibili", "douyin", "xiaohongshu"},
            str(platforms),
        )
        titles = [i.get("title") for i in data.get("items", [])]
        check("从【】中提取标题", "第一性原理是什么" in titles, str(titles))

    # ---- 3. 提交与去重 ------------------------------------------------
    section("提交与去重（P0）")
    # 用运行标识保证 URL 唯一，使测试可重复执行（去重逻辑本身仍被单独验证）
    run_id = str(int(time.time()))[-8:]
    url = f"https://www.bilibili.com/video/BV1Smoke{run_id}"
    status, data = call(
        "POST",
        "/api/collect/submit",
        {"entries": [{"url": url, "platform": "bilibili", "title": "冒烟测试视频"}]},
    )
    check("POST /api/collect/submit 返回 200", status == 200, f"got {status}")
    item_id = None
    if isinstance(data, dict):
        accepted = data.get("accepted", [])
        check("首次提交被接受", len(accepted) == 1, str(data.get("summary")))
        if accepted:
            item_id = accepted[0]["id"]

    status, data = call(
        "POST",
        "/api/collect/submit",
        {"entries": [{"url": url, "platform": "bilibili", "title": "重复提交"}]},
    )
    if isinstance(data, dict):
        check(
            "重复 URL 被精确去重",
            len(data.get("duplicates", [])) == 1,
            str(data.get("summary")),
        )

    # ---- 4. 队列与状态机 ----------------------------------------------
    section("队列与状态机（P0）")
    status, data = call("GET", "/api/items?limit=20")
    check("GET /api/items 返回 200", status == 200, f"got {status}")
    if isinstance(data, dict) and item_id:
        items = data.get("items", [])
        check("新条目在队列中", any(i["id"] == item_id for i in items))
        found = next((i for i in items if i["id"] == item_id), None)
        if found:
            check(
                "条目状态为 queued/extracting/extract_failed 之一",
                found["status"]
                in ("queued", "extracting", "extract_failed", "extracted"),
                found["status"],
            )

    status, data = call("GET", "/api/jobs")
    check("GET /api/jobs 返回 200", status == 200, f"got {status}")
    if isinstance(data, dict):
        check("jobs 统计可用", "stats" in data)

    # ---- 5. 速记（P7） ------------------------------------------------
    section("速记（P7）")
    status, data = call(
        "POST",
        "/api/collect/quick-note",
        {"text": "冒烟测试的速记内容：这是一个随手想法。"},
    )
    check("POST /api/collect/quick-note 返回 200", status == 200, f"got {status}")
    if isinstance(data, dict):
        check("速记返回条目 id", bool(data.get("id")))

    # ---- 6. 手动补充兜底（P3）----------------------------------------
    section("失败兜底（P3）")
    status, data = call(
        "POST",
        "/api/collect/submit",
        {
            "entries": [
                {"url": f"https://example.com/smoke-fail-{run_id}", "platform": "web"}
            ]
        },
    )
    fail_id = None
    if isinstance(data, dict) and data.get("accepted"):
        fail_id = data["accepted"][0]["id"]
    if fail_id:
        status, data = call(
            "POST",
            f"/api/items/{fail_id}/manual-fill",
            {"text": "手动粘贴的正文内容。" * 30, "title": "手动补充"},
        )
        check("行内手动补充返回 200", status == 200, f"got {status}")
        if isinstance(data, dict):
            check("补充后状态为 extracted", data.get("item", {}).get("status") == "extracted",
                  str(data.get("item", {}).get("status")))

    # ---- 7. 标签词表 --------------------------------------------------
    section("标签治理（P8）")
    status, data = call("POST", "/api/tags", {"name": "冒烟标签"})
    check("POST /api/tags 返回 200", status == 200, f"got {status}")
    status, data = call("GET", "/api/tags")
    if isinstance(data, dict):
        names = [t["name"] for t in data.get("tags", [])]
        check("标签进入受控词表", "冒烟标签" in names, str(names[:8]))
        check("分区列表存在", "partitions" in data)

    # ---- 8. C 层笔记（P7）---------------------------------------------
    section("C 层笔记（P7）")
    status, data = call(
        "POST",
        "/api/notes",
        {
            "title": "冒烟测试笔记",
            "body": "这是端到端冒烟测试创建的创作笔记，内容足够长以便被索引到。",
            "form": "fragment",
            "tags": ["冒烟标签"],
        },
    )
    check("POST /api/notes 返回 200", status == 200, f"got {status}")
    note_path = None
    if isinstance(data, dict):
        note_path = data.get("note_path")
        check("笔记已落盘", bool(note_path))
        if note_path:
            check(
                "笔记在 03_Notes 目录下",
                "/03_Notes/" in note_path,
                note_path,
            )

    # ---- 9. 外部导入（P7）--------------------------------------------
    section("外部导入（P7）")
    status, data = call(
        "POST",
        "/api/notes/import",
        {"content": "---\ntitle: 导入的冒烟笔记\ntags: [冒烟标签]\n---\n\n导入的正文内容。"},
    )
    check("POST /api/notes/import 返回 200", status == 200, f"got {status}")
    if isinstance(data, dict):
        check("导入标题正确", data.get("title") == "导入的冒烟笔记", str(data.get("title")))

    # ---- 10. 搜索（P6）------------------------------------------------
    section("搜索（P6）")
    status, data = call("POST", "/api/search", {"query": "冒烟"})
    check("POST /api/search 返回 200", status == 200, f"got {status}")
    if isinstance(data, dict):
        check("搜索命中冒烟笔记", data.get("total", 0) >= 1, f"total={data.get('total')}")
        check("返回耗时信息", "elapsed_ms" in data)

    status, data = call("POST", "/api/search", {"query": "两字词"})
    if isinstance(data, dict):
        check("两字词查询不报错", "results" in data)

    status, data = call("GET", "/api/search/vocabulary")
    check("GET /api/search/vocabulary 返回 200", status == 200, f"got {status}")

    # ---- 11. 回顾（P8）------------------------------------------------
    section("回顾（P8）")
    status, data = call("GET", "/api/recap/card")
    check("GET /api/recap/card 返回 200", status == 200, f"got {status}")
    if isinstance(data, dict):
        check("回顾卡片含采集统计", "collected_yesterday" in data)
        check("回顾卡片含待补手稿", "need_manuscript" in data)

    status, data = call("POST", "/api/recap/weekly?week_offset=0")
    check("POST /api/recap/weekly 返回 200", status == 200, f"got {status}")
    if isinstance(data, dict):
        check("周报已生成", bool(data.get("path")), str(data))

    # ---- 12. 设置（P1/P10）--------------------------------------------
    section("设置（P1 / P10）")
    status, data = call("GET", "/api/settings")
    check("GET /api/settings 返回 200", status == 200, f"got {status}")
    if isinstance(data, dict):
        adapters = data.get("adapters", [])
        names = {a["name"] for a in adapters}
        check(
            "5 个平台适配器已注册",
            names == {"bilibili", "douyin", "kuaishou", "xiaohongshu", "web"},
            str(names),
        )
        levels = {a["name"]: a["support_level"] for a in adapters}
        check("B站标为完整可用", levels.get("bilibili") == "full", str(levels))
        check("抖音诚实标为不稳定", levels.get("douyin") == "unstable", str(levels))

    status, data = call("GET", "/api/settings/providers")
    check("GET /api/settings/providers 返回 200", status == 200, f"got {status}")
    if isinstance(data, dict):
        check("Provider 矩阵可用", len(data.get("providers", [])) >= 2)

    status, data = call("GET", "/api/settings/autostart")
    check("GET /api/settings/autostart 返回 200", status == 200, f"got {status}")
    if isinstance(data, dict):
        check("电源策略建议存在", "power" in data)

    # ---- 13. 索引（P5/P10）-------------------------------------------
    section("索引（P5 / P10）")
    status, data = call("POST", "/api/settings/reindex")
    check("POST /api/settings/reindex 返回 200", status == 200, f"got {status}")
    if isinstance(data, dict):
        check(
            "重建索引在 60s 内完成（5000 条目标）",
            data.get("elapsed_ms", 999999) < 60000,
            f"{data.get('elapsed_ms')}ms",
        )

    status, data = call("GET", "/api/vault/backfill/estimate")
    check("GET /api/vault/backfill/estimate 返回 200", status == 200, f"got {status}")
    if isinstance(data, dict):
        check("补 AI 预估可用", "count" in data)

    # ---- 14. SPA -------------------------------------------------------
    section("前端")
    status, data = call("GET", "/")
    check("GET / 返回 200", status == 200, f"got {status}")

    # ---- 汇总 ---------------------------------------------------------
    print("\n" + "=" * 56)
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
