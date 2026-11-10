"""性能验证脚本（P10 量化指标）。

对齐 v0.5.0 §11：
- 搜索 P95 < 200ms @ 5000 条
- 反链查询 < 50ms
- embedding 单条查询编码 < 300ms（CPU）
- 全库 FTS/反链重建 < 60s @ 5000 条
- embedding 全库批处理 < 60min @ CPU

用法：
    python tests/perf_check.py [条数]
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

_TMP = tempfile.mkdtemp(prefix="sb-perf-")
os.environ["SECOND_BRAIN_HOME"] = _TMP
os.environ["SECOND_BRAIN_CONFIG"] = str(Path(_TMP) / "config.toml")

import app.core.config as config_mod  # noqa: E402

ROOT = Path(_TMP)
(ROOT / "config.toml").write_text(
    f"""
[vault]
path = "{ROOT.as_posix()}/vault"

[storage]
data_dir = "{ROOT.as_posix()}/data"
db_path = "{ROOT.as_posix()}/data/secondbrain.db"
prompts_dir = "{ROOT.as_posix()}/prompts"
""",
    encoding="utf-8",
)
config_mod.store.reload()

TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 5000

from app.core import vault as vault_mod  # noqa: E402
from app.core.db import get_db  # noqa: E402
from app.core.tokenizer import build_fts_payload  # noqa: E402

TOPICS = [
    "第一性原理", "还原论", "系统思维", "认知负荷", "刻意练习", "心流",
    "复利效应", "沉没成本", "机会成本", "边际效用", "二阶思维", "反脆弱",
    "熵增", "涌现", "反馈回路", "约束理论", "博弈论", "演化论",
]
VERBS = ["理解", "应用", "反思", "记录", "验证", "拆解", "关联", "延伸"]


def build_corpus(count: int) -> list[Path]:
    """造 count 篇 B 层笔记（含真实 frontmatter 结构）。"""
    cfg = config_mod.get_config()
    cfg.vault.ensure()
    processed_dir = cfg.vault.dir("processed")
    notes_dir = cfg.vault.dir("notes")
    rng = random.Random(20261101)

    paths: list[Path] = []
    for i in range(count):
        topic = rng.choice(TOPICS)
        verb = rng.choice(VERBS)
        title = f"{topic}的{verb}方式 {i}"
        summary = f"{topic}是本篇的核心主题。{verb}它需要把复杂问题拆解到基本事实，" * 3
        manuscript = f"我的理解是：{topic}的关键在于{verb}，并且要结合具体场景反复验证。" * 2
        tags = [rng.choice(TOPICS) for _ in range(rng.randint(1, 3))]
        body = f"# {title}\n\n{summary}\n\n{manuscript}\n\n相关：[[{rng.choice(TOPICS)}]]\n"

        is_note = i % 4 == 3
        target_dir = notes_dir if is_note else processed_dir
        fm: dict[str, Any] = {
            "title": title,
            "type": "note" if is_note else "processed",
            "tags": tags,
            "digested": True,
            "created": vault_mod.now_iso(),
        }
        if not is_note:
            fm.update(
                {
                    "source_url": f"https://example.com/{i}",
                    "platform": "bilibili",
                    "partitions": [rng.choice(TOPICS)],
                    "ai_summary": summary,
                    "personal_summary": manuscript,
                    "ai_model": "perf-fixture",
                }
            )
        path = target_dir / f"{i:05d}-{vault_mod.sanitize_filename(title)}.md"
        vault_mod.write_note(path, fm, body)
        paths.append(path)
    return paths


from typing import Any  # noqa: E402


def index_all(paths: list[Path]) -> float:
    """全库重建索引，返回耗时（秒）。"""
    from app.services.index_service import get_index_service

    started = time.time()
    get_index_service().reindex_all()
    return time.time() - started


def measure_search(queries: list[str], rounds: int = 5) -> dict[str, Any]:
    """搜索延迟统计。"""
    from app.services.search_service import get_search_service

    svc = get_search_service()
    samples: list[float] = []
    for _ in range(rounds):
        for q in queries:
            started = time.perf_counter()
            svc.search(q, limit=30)
            samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    return {
        "count": len(samples),
        "p50": round(samples[len(samples) // 2], 2),
        "p95": round(samples[int(len(samples) * 0.95)], 2),
        "max": round(samples[-1], 2),
    }


def measure_backlink(rounds: int = 200) -> dict[str, Any]:
    """反链查询延迟统计。"""
    from app.services.search_service import get_search_service

    svc = get_search_service()
    paths = [
        str(r["note_path"])
        for r in svc.db.query("SELECT note_path FROM notes LIMIT 200")
    ]
    if not paths:
        return {"count": 0, "p50": 0, "p95": 0, "max": 0}
    samples: list[float] = []
    for _ in range(rounds):
        for path in paths[:50]:
            started = time.perf_counter()
            svc.backlinks(path)
            samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    return {
        "count": len(samples),
        "p50": round(samples[len(samples) // 2], 3),
        "p95": round(samples[int(len(samples) * 0.95)], 3),
        "max": round(samples[-1], 3),
    }


def measure_embed(n: int = 30) -> dict[str, Any]:
    """embedding 单条编码延迟统计。"""
    from app.services.embedding_service import get_embedding_service

    svc = get_embedding_service()
    text = "第一性原理是把复杂问题拆解到最基本的事实与公理，然后从这些基本事实出发重新推导解决方案。" * 3
    svc.encode(text)  # 预热
    samples: list[float] = []
    for _ in range(n):
        started = time.perf_counter()
        svc.encode(text)
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    return {
        "backend": svc.backend,
        "count": len(samples),
        "p50": round(samples[len(samples) // 2], 2),
        "p95": round(samples[int(len(samples) * 0.95)], 2),
    }


def measure_embedding_batch() -> dict[str, Any]:
    """全库 embedding 批处理耗时。"""
    from app.services.embedding_service import get_embedding_service

    svc = get_embedding_service()
    started = time.time()
    result = svc.rebuild_all()
    return {
        "count": result.get("count", 0),
        "backend": result.get("backend", ""),
        "elapsed_s": round(time.time() - started, 2),
    }


def main() -> int:
    print(f"SecondBrain 性能验证 @ {TARGET} 条\n" + "=" * 60)

    print(f"[1/5] 生成 {TARGET} 篇语料…")
    started = time.time()
    paths = build_corpus(TARGET)
    print(f"      完成，用时 {time.time() - started:.1f}s\n")

    print("[2/5] 全库 FTS + 反链重建…")
    rebuild_s = index_all(paths)
    print(f"      用时 {rebuild_s:.2f}s\n")

    db = get_db()
    indexed = db.scalar("SELECT COUNT(*) FROM notes") or 0
    print(f"[3/5] 搜索延迟（{indexed} 条已索引）…")
    queries = ["第一性原理", "复利", "认知", "系统", "熵", "反馈", "还原", "思维"]
    search_stats = measure_search(queries)
    print(f"      {search_stats}\n")

    print("[4/5] 反链查询延迟…")
    backlink_stats = measure_backlink()
    print(f"      {backlink_stats}\n")

    print("[5/5] embedding 编码延迟…")
    embed_stats = measure_embed()
    print(f"      {embed_stats}\n")

    batch = measure_embedding_batch()
    print(f"      全库批处理：{batch}\n")

    # ---- 判定 --------------------------------------------------------
    print("=" * 60)
    print("指标判定（对照 v0.5.0 §11）")
    print("-" * 60)
    checks = [
        (
            f"搜索 P95 < 200ms @{indexed} 条",
            search_stats["p95"] < 200,
            f"P95={search_stats['p95']}ms",
        ),
        (
            "反链查询 P95 < 50ms",
            backlink_stats["p95"] < 50,
            f"P95={backlink_stats['p95']}ms",
        ),
        (
            "embedding 单条编码 P95 < 300ms",
            embed_stats["p95"] < 300,
            f"P95={embed_stats['p95']}ms (backend={embed_stats['backend']})",
        ),
        (
            f"全库 FTS/反链重建 < 60s @{indexed} 条",
            rebuild_s < 60,
            f"{rebuild_s:.2f}s",
        ),
        (
            "embedding 全库批处理 < 60min",
            batch["elapsed_s"] < 3600,
            f"{batch['elapsed_s']}s ({batch['count']} 条)",
        ),
    ]

    failed = 0
    for name, ok, detail in checks:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}  —  {detail}")
        if not ok:
            failed += 1

    print("-" * 60)
    if failed:
        print(f"{len(checks) - failed}/{len(checks)} 项达标")
        return 1
    print(f"全部 {len(checks)} 项达标")
    return 0


if __name__ == "__main__":
    sys.exit(main())
