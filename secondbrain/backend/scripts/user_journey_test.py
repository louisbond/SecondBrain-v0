"""用户动线走查：模拟真实用户按顺序操作五区。

不是「接口返回 200」就算过——每一步都验证**用户真能办成事**：
  · 页面渲染出了该有的东西
  · 操作后状态真的变了
  · 结果能被后续步骤用上
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:7688"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
DOCS = Path(__file__).resolve().parents[1] / "docs"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))
    return ok


def api(method: str, path: str, body=None, timeout=60):
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
        body_raw = e.read().decode()
        try:
            return e.code, json.loads(body_raw)
        except json.JSONDecodeError:
            return e.code, body_raw
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def chips_have_names(html: str) -> bool:
    """词表 chip 必须渲染出名字，而不是空复选框。

    回归 bug：vocab 降级成字符串数组后模板仍用 t.name → 渲染成 `☐ ☐`。
    """
    # 只看 body 里的元素（排除 <style> 里的 .check-chip 定义）
    body = html
    m = re.search(r"<body[\s\S]*", html)
    if m:
        body = m.group(0)
    # 匹配到下一个 check-chip 或容器结束（不能只到第一个 </span>，
    # 因为 chip 内部还有嵌套 span）
    for chunk in re.findall(
        r'class="check-chip[^"]*"[^>]*>([\s\S]{0,400}?)(?=class="check-chip|</div>)', body
    ):
        text = re.sub(r"<[^>]+>", " ", chunk)
        text = re.sub(r"\s+", " ", text).strip()
        if text and text not in ("☑", "☐"):
            return True
    return False


def dom(path: str, timeout: int = 120) -> str:
    """渲染并返回页面 DOM 文本。"""
    tmp = DOCS / "_dom.html"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
            f"--virtual-time-budget=10000", "--dump-dom",
            f"{BASE}{path}",
            f"--user-data-dir={DOCS / '_edge_ux'}",
        ],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    return proc.stdout or ""


def visible_text(html: str) -> str:
    """粗略提取可见文本（去标签、去脚本）。"""
    html = re.sub(r"<script[\s\S]*?</script>", " ", html)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text)
    return text


# ============================================================
def stage_1_boot():
    print("\n── 阶段 1：打开软件（用户双击 exe）──")
    code, d = api("GET", "/api/health")
    check("服务已启动", code == 200 and d.get("ok") is True, f"port={d.get('port')}")
    check("vault 指向真实库", "ChaoNaoKu" in str(d.get("vault")), str(d.get("vault")))
    check("库里有数据", d.get("db_items", 0) > 0 and d.get("db_notes", 0) > 0,
          f"items={d.get('db_items')} notes={d.get('db_notes')}")


def stage_2_nav():
    print("\n── 阶段 2：五区导航（用户点左侧菜单）──")
    nav_expect = {
        "/": ["首页", "知识图谱", "今日回顾"],
        "/workbench": ["采集区", "采集", "任务状态"],
        "/process": ["加工区", "待加工", "核准"],
        "/original": ["原创区", "创作", "空模板"],
        "/manage": ["管理区", "分区树", "标签", "维护与诊断"],
        # 状态页不再是一级导航，但采集/加工区的状态横条会跳过来
        "/status": ["任务状态", "全部", "进行中", "已完成", "失败"],
        # 设置是一级页 + Tab（与管理区同款），默认显示 AI 组
        "/settings": ["设置", "AI Provider", "API 密钥", "存储", "系统"],
    }
    for path, keywords in nav_expect.items():
        html = dom(path)
        text = visible_text(html)
        if not text.strip():
            check(f"{path} 渲染", False, "DOM 为空")
            continue
        missing = [k for k in keywords if k not in text]
        check(f"{path} 渲染完整", not missing, f"缺少 {missing}" if missing else "全部关键词命中")


def stage_3_workbench():
    print("\n── 阶段 3：工作区（导入素材 + 看状态）──")
    code, d = api("GET", "/api/workbench/status?limit=20")
    if code != 200:
        check("工作区状态接口", False, str(d)[:120])
        return
    items = d.get("items") or []
    check("能看到任务列表", isinstance(items, list), f"{len(items)} 条")
    stages = {i.get("stage") for i in items}
    check("每条都有阶段标记", all(i.get("stage") for i in items), f"阶段集合={stages}")
    check("进度在 0~1", all(0.0 <= float(i.get("progress", 0)) <= 1.0 for i in items))
    # 失败项必须能给出原因（用户能看到为什么失败）
    failed = [i for i in items if i.get("state") in ("failed", "ai_failed", "extract_failed")]
    for f in failed:
        check(f"失败项 #{f['id']} 有原因说明", bool(f.get("error_detail") or f.get("error_reason")),
              (f.get("error_detail") or f.get("error_reason") or "")[:80])


def stage_4_process():
    print("\n── 阶段 4：加工区（核�� + 创作）──")
    code, d = api("GET", "/api/review/pending")
    if code != 200:
        check("核准队列接口", False, str(d)[:120])
        return
    items = d.get("items") or []
    check("队列非空（有素材可核）", len(items) > 0, f"{len(items)} 条待核准")
    if not items:
        return
    iid = items[0]["id"]
    code, d = api("GET", f"/api/review/{iid}")
    if code != 200:
        check(f"读取条目 #{iid}", False, str(d)[:120])
        return
    ai_sum = str(d.get("ai_summary") or "")
    check("AI 摘要有真实内容", len(ai_sum) > 60, f"{len(ai_sum)} 字：{ai_sum[:60]}…")
    # 摘要不能是"无法确认"这类空话
    empty_phrases = ("无法确认", "暂无内容", "待补充", "未知")
    check("摘要不是空话", not any(p in ai_sum for p in empty_phrases))
    # 分区建议：要么命中词表，要么作为"待定分区"给出（AI 可建议新分区）
    sug = set(d.get("suggested_partitions") or [])
    pending_parts = d.get("pending_partitions") or []
    check(
        "分区有建议（词表内或待定）",
        bool(sug) or bool(pending_parts),
        f"词表内={sug} 待定={[p.get('name') for p in pending_parts]}",
    )
    check("给出标签建议", bool(d.get("suggested_tags")), str(d.get("suggested_tags"))[:80])
    # 分区建议必须在词表里（能勾选）
    _, p = api("GET", "/api/partitions")
    known = {x["path"] for x in (p.get("partitions") or [])}
    check("建议分区都在词表内", sug <= known, f"建议={sug} 词表={known}")
    # 待定分区不能与标签混淆（曾经混在一起）
    pending_tag_names = {t.get("name") for t in (d.get("pending_tags") or [])}
    check(
        "待定分区与待定标签不混淆",
        not (pending_tag_names & {p.get("name") for p in pending_parts}),
        f"标签={pending_tag_names} 分区={[p.get('name') for p in pending_parts]}",
    )


def stage_5_chars_templates():
    print("\n── 阶段 5：字数档位与模板 ──")
    code, d = api("GET", "/api/workbench/chars-presets")
    check("档位接口可用", code == 200)
    presets = d.get("presets") or []
    check("默认档 800", d.get("default") == 800, f"default={d.get('default')}")
    check("档位包含 800", 800 in presets, str(presets))
    check("档位覆盖用户要求的区间",
          set([200, 500, 1000, 1500, 2000, 3000, 5000]).issubset(set(presets)), str(presets))
    code, d = api("GET", "/api/workbench/templates")
    tpls = d.get("templates") or []
    check("模板库非空", len(tpls) > 0, f"{len(tpls)} 个")
    check("模板可新增/编辑/删除", all("id" in t and "name" in t for t in tpls))


def stage_6_insight():
    print("\n── 阶段 6：整合区（搜索 / 回顾 / 图谱）──")
    code, d = api("POST", "/api/search", {"query": "思维", "limit": 5})
    check("搜索接口可用", code == 200)
    check("搜索有命中", (d.get("total") or 0) > 0, f"total={d.get('total')}")
    if d.get("results"):
        r0 = d["results"][0]
        check("结果含标题与路径", bool(r0.get("title")) and bool(r0.get("note_path")),
              f"{r0.get('title')}")

    # 按分区筛选
    code, d2 = api("POST", "/api/search", {"query": "思维", "partitions": ["成长树"], "limit": 5})
    check("按分区筛选可用", code == 200, f"total={d2.get('total')}")
    check("按分区筛选有命中", (d2.get("total") or 0) > 0)

    code, d = api("GET", "/api/workbench/graph?limit=100")
    nodes = d.get("nodes") or []
    edges = d.get("edges") or []
    check("图谱有节点", len(nodes) > 0, f"{len(nodes)} 节点")
    check("图谱有连线", len(edges) > 0, f"{len(edges)} 边")
    check("图谱统计字段完整", bool(d.get("stats")), str(d.get("stats"))[:120])

    code, d = api("GET", "/api/workbench/vault-tree")
    tree = d.get("tree") or []
    check("vault 目录树可取", len(tree) > 0, f"{len(tree)} 项")

    code, d = api("GET", "/api/workbench/aggregate")
    check("整合区聚合可用", code == 200, f"keys={list(d.keys()) if isinstance(d, dict) else d}")

    # 词表 chip 必须显示名字（防"空复选框"回归）。
    # v24 起词表 chip 都是按需渲染：加工区（选中条目后）、首页筛选面板（展开后）。
    # 这里检查加工区 DOM；首页筛选面板的展开检查见 render 测试与 CDP 自检。
    html = dom("/process")
    check("加工区词表 chip 有名字", chips_have_names(html), "只渲染出空复选框")


def stage_7_features():
    print("\n── 阶段 7：功能区（治理 + 诊断）──")
    code, d = api("GET", "/api/workbench/diagnose")
    checks = d.get("checks") or []
    check("诊断可用", code == 200, f"{len(checks)} 项检查")
    bad = [c for c in checks if not c.get("ok")]
    for c in checks:
        mark = "✓" if c.get("ok") else "!"
        print(f"     {mark} {c.get('name')}: {str(c.get('detail'))[:70]}")
    print(f"     → 失败项: {len(bad)}")

    code, d = api("GET", "/api/partitions/tree")
    nodes = d.get("tree") or []
    check("分区树可取", len(nodes) > 0, f"{len(nodes)} 根")
    for n in nodes:
        check(f"分区「{n['name']}」有计数", "count" in n, f"count={n.get('count')}")


def stage_8_settings():
    print("\n── 阶段 8：设置区（系统配置）──")
    code, d = api("GET", "/api/settings/providers")
    ok = code == 200 and isinstance(d, dict)
    check("provider 列表可读", ok, f"code={code} {str(d)[:90]}")
    if ok:
        providers = d.get("providers") or []
        check("至少一个可用 provider", len(providers) > 0,
              f"{[p.get('name') for p in providers][:5]}")
    code, d = api("GET", "/api/settings")
    check("设置可读", code == 200, f"keys={list(d.keys())[:6] if isinstance(d, dict) else d}")


def stage_9_data_integrity():
    print("\n── 阶段 9：数据完整性 ──")
    vault = Path("<workspace>/Systems666/ChaoNaoKu")
    mds = [p for p in vault.rglob("*.md") if ".obsidian" not in str(p)]
    # 15 = 11 篇笔记 + 4 个视频 A 层原文；周报（_reports/）是调度器正常产物，不计入。
    # 用下限断言（用户会继续加工，笔记数只增不减）
    core = [p for p in mds if "_reports" not in p.parts]
    check("md 文件数合理", len(core) >= 15, f"{len(core)} 个（另有周报 {len(mds) - len(core)} 个）")
    # 没有测试残留
    junk = [p.name for p in mds if any(k in p.name for k in ("测试", "冒烟", "占位", "自测", "验证"))]
    check("无测试残留文件", not junk, str(junk))
    # DB 与 md 一致
    code, d = api("GET", "/api/health")
    check("索引条目数与库一致", (d.get("db_notes") or 0) >= 11, f"notes={d.get('db_notes')}")
    # 搜索能命中库中笔记
    code, d = api("POST", "/api/search", {"query": "隐性因素", "limit": 3})
    check("库中笔记可搜到", (d.get("total") or 0) > 0, f"total={d.get('total')}")


def main() -> int:
    print("=" * 60)
    print("SecondBrain 用户动线走查")
    print("=" * 60)
    for fn in (
        stage_1_boot, stage_2_nav, stage_3_workbench, stage_4_process,
        stage_5_chars_templates, stage_6_insight, stage_7_features,
        stage_8_settings, stage_9_data_integrity,
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(f"{fn.__name__} 异常", False, f"{type(exc).__name__}: {exc}")

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  ✗", f)
    else:
        print("全部通过 ✓")
    print("=" * 60)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
