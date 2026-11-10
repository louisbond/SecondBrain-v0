"""重置实验数据（保留 vault 里的 md 文件）。

用法：
    python scripts/reset_data.py --yes

做三件事：
1. 清空数据库里的实验痕迹（条目 / 索引 / 词表 / 模板 / 任务…）
2. 从 A 层 md 重建条目（你的视频原文不重抓、不重转写）
3. 重新冷启动：目录树 → 分区树、frontmatter → 标签词表，并重建索引

**不会删除 vault 里的任何 md 文件。**
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

TABLES = [
    "note_tags",
    "note_partitions",
    "notes_fts",
    "links",
    "notes",
    "ai_suggestions",
    "embeddings",
    "jobs",
    "items",
    "tag_merges",
    "tags",
    "partitions",
    "parse_templates",
    "deleted_templates",
]


def main() -> int:
    if "--yes" not in sys.argv:
        print("这会清空数据库里的实验数据（vault 的 md 文件不动）。")
        print("确认请加 --yes")
        return 1

    from app.core.config import get_config
    from app.core.db import get_db

    cfg = get_config()
    db = get_db()

    print(f"vault: {cfg.vault.path}")
    print(f"数据库: {cfg.db_path}")

    def _wipe(conn) -> None:
        for table in TABLES:
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception as exc:  # noqa: BLE001
                print(f"  跳过 {table}: {exc}")

    db.write(_wipe)
    print("✓ 实验数据已清空")

    # 从 A 层 md 重建条目
    from app.services.ingest_service import get_ingest_service

    rebuilt = get_ingest_service().rebuild_items_from_inbox()
    print(f"✓ 从 A 层重建条目：新增 {rebuilt['created']}，跳过 {rebuilt['skipped']}")

    # 冷启动：目录树 → 分区树
    from app.services.partition_service import get_partition_service

    created = get_partition_service().seed_from_vault_dirs(cfg.vault.path)
    print(f"✓ 分区树冷启动：新建 {created} 个分区")

    # 标签词表（从存量 frontmatter）
    from app.services.index_service import get_index_service

    seeded = get_index_service().seed_tag_vocabulary()
    print(f"✓ 标签词表冷启动：{seeded.get('tags')} 个标签")

    # 解析模板（内置 3 个）
    from app.services.template_service import get_template_service

    tpls = get_template_service().seed_defaults()
    print(f"✓ 解析模板：新建 {tpls} 个")

    # 重建索引
    stats = get_index_service().reindex_all()
    print(f"✓ 索引重建：{stats}")

    # 汇总
    print("\n最终状态：")
    for table in ("items", "notes", "tags", "partitions", "parse_templates"):
        try:
            n = db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"]
            print(f"  {table}: {n}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {table}: 查询失败 {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
