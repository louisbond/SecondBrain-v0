"""真实端到端：用户把一条素材从链接变成 C 层笔记的完整流程。

用真实 AI + 真实 ASR（真 B站链接），全程在隔离实例上跑，不碰用户库。
每一步都断言"用户真能办成事"。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = ""
PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))
    return ok


def api(method: str, path: str, body=None, timeout=120):
    global BASE
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def wait_status(item_id: int, targets, timeout: int = 420):
    """等条目进入目标状态，返回最后一次读到的条目。"""
    deadline = time.time() + timeout
    item = {}
    while time.time() < deadline:
        code, item = api("GET", f"/api/items/{item_id}")
        if code == 200 and item.get("status") in targets:
            return item
        time.sleep(5)
    return item


def main() -> int:
    global BASE
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from harness import IsolatedServer

    srv = IsolatedServer()
    srv.__enter__()
    BASE = srv.base
    print(f"隔离实例：{BASE}\n")

    # ---------- 1. 用户在工作区贴链接 ----------
    print("── 1. 用户贴链接（真实 B站）──")
    # 用有真实语音的视频（纯音乐 MV 会让 ASR 空转写，测不出正常路径）
    url = "https://www.bilibili.com/video/BV1xx411c7mD"
    code, d = api("POST", "/api/collect/submit", {"entries": [{"url": url, "platform": "bilibili"}]})
    if code != 200:
        check("提交链接", False, str(d)[:160])
        srv.__exit__()
        return 1
    acc = d.get("accepted") or []
    dup = d.get("duplicates") or []
    if acc:
        item_id = acc[0]["id"]
        check("提交链接", True, f"新条目 #{item_id}")
    elif dup:
        item_id = dup[0].get("id")
        check("提交链接（命中去重）", bool(item_id), f"条目 #{item_id}")
    else:
        check("提交链接", False, str(d)[:160])
        srv.__exit__()
        return 1

    # ---------- 2. 抓取阶段 ----------
    print("\n── 2. 抓取阶段 ──")
    item = wait_status(item_id, ("extracted", "extract_failed", "ai_processing", "pending_review", "ai_failed"), 300)
    check("抓取完成", item.get("status") != "extract_failed",
          f"状态={item.get('status')} extractor={item.get('extractor')}")
    body_len = len(item.get("raw_text") or "")
    check("有正文内容", body_len > 0, f"{body_len} 字")
    if body_len < 50:
        print(f"     （简介本身极短（{body_len} 字）——这类视频 AI 摘要质量有限，"
              f"属正常；真实内容应走 ASR 或手动补充）")

    # ---------- 3. ASR（视频无字幕时）----------
    print("\n── 3. 语音转写（ASR）──")
    if item.get("asr"):
        text = item.get("raw_text") or ""
        check("ASR 已产出转写", len(text) > 200, f"{len(text)} 字")
    else:
        print(f"     （本条有字幕，无需 ASR：extractor={item.get('extractor')}）")

    # ---------- 4. AI 摘要 ----------
    print("\n── 4. AI 摘要 ──")
    item = wait_status(item_id, ("pending_review", "ai_failed"), 600)
    check("AI 处理完成", item.get("status") == "pending_review",
          f"状态={item.get('status')} 原因={item.get('error_reason') or '-'}")
    summary = item.get("ai_summary") or ""
    check("摘要有实质内容", len(summary) > 80, f"{len(summary)} 字")
    check("摘要不是空话", not any(p in summary for p in ("无法确认", "暂无内容", "待补充")))

    # ---------- 5. 核准页拿得到建议 ----------
    print("\n── 5. 核准页的建议 ──")
    code, d = api("GET", f"/api/review/{item_id}")
    check("核准页可读", code == 200)
    check("有分区建议或待定分区",
          bool(d.get("suggested_partitions")) or bool(d.get("pending_partitions")),
          f"词表内={d.get('suggested_partitions')} 待定="
          f"{[p.get('name') for p in (d.get('pending_partitions') or [])]}")
    check("有待定标签（AI 可建议新标签）", bool(d.get("pending_tags")),
          str([t.get("name") for t in (d.get("pending_tags") or [])])[:90])

    # ---------- 6. 用户确认入库 ----------
    print("\n── 6. 确认入库 ──")
    parts = list(d.get("suggested_partitions") or []) or [
        p.get("name") for p in (d.get("pending_partitions") or [])
    ]
    code, d = api("POST", f"/api/review/{item_id}/confirm", {
        "title": item.get("title") or "未命名",
        "personal_summary": "用户手稿：这段内容对我有用。",
        "tags": list(d.get("suggested_tags") or []),
        "partitions": parts,
        "confirmed_related": [],
        "ai_summary": summary,
        "summary_type": item.get("ai_summary_type") or "bullets",
    })
    check("确认入库成功", code == 200 and d.get("ok") is not False, str(d)[:140])

    # ---------- 7. B 层原文落盘 ----------
    print("\n── 7. 产物落盘 ──")
    code, item = api("GET", f"/api/items/{item_id}")
    b_path = item.get("processed_path") or ""
    check("有 B 层产物路径（条目里能查到）", bool(b_path), b_path)
    if b_path:
        p = Path(b_path)
        check("B 层文件存在", p.exists(), str(p))
        if p.exists():
            txt = p.read_text(encoding="utf-8", errors="replace")
            check("B 层含 AI 摘要", (summary[:40] in txt) or ("ai_summary" in txt))
            check("B 层含用户手稿", "用户手稿" in txt)

    # ---------- 8. 搜索能命中 ----------
    print("\n── 8. 检索命中 ──")
    kw = (summary or "内容")[:6]
    code, d = api("POST", "/api/search", {"query": kw, "limit": 5})
    check("搜索有命中", (d.get("total") or 0) > 0, f"关键词「{kw}」total={d.get('total')}")

    # ---------- 9. 图谱出现 ----------
    print("\n── 9. 图谱 ──")
    code, d = api("GET", "/api/workbench/graph?limit=200")
    check("图谱节点非空", len(d.get("nodes") or []) > 0, f"{len(d.get('nodes') or [])} 节点")
    check("图谱有连线", len(d.get("edges") or []) > 0, f"{len(d.get('edges') or [])} 边")

    # ---------- 10. 创作：C 层笔记 ----------
    print("\n── 10. 创作 C 层笔记 ──")
    code, d = api("POST", "/api/notes", {
        "title": "端到端测试笔记",
        "content": "这是端到端流程产出的正文内容。\n\n## 要点\n- 第一条\n- 第二条",
        "tags": [],
        "partitions": [],
        "source_path": b_path or "",
    })
    ok = code == 200 and d.get("ok") is not False
    check("C 层笔记创建成功", ok, str(d)[:140])
    if ok:
        note_path = d.get("note_path") or ""
        check("C 层文件落盘", bool(note_path) and Path(note_path).exists(), note_path)
        code, d2 = api("POST", "/api/search", {"query": "端到端", "limit": 5})
        check("新笔记可搜到", (d2.get("total") or 0) > 0, f"total={d2.get('total')}")

    # ---------- 11. 导出备份 ----------
    print("\n── 11. 备份导出 ──")
    code, d = api("POST", "/api/workbench/backup")
    check("备份导出成功", code == 200 and d.get("ok"), str(d)[:120])
    if d.get("path"):
        check("备份文件存在", Path(d["path"]).exists(), f"{d.get('size')} 字节")

    srv.__exit__()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗", f)
    print("=" * 60)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
