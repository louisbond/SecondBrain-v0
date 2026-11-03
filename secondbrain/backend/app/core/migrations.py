"""数据库迁移（幂等）。

每个迁移在 `schema_migrations` 表留一行；已应用的不会重复执行。
"""

from __future__ import annotations

import sqlite3
from typing import Callable

# 版本 → 迁移函数
MIGRATIONS: list[tuple[str, Callable[[sqlite3.Connection], None]]] = []


def register(version: str):
    def deco(fn: Callable[[sqlite3.Connection], None]):
        MIGRATIONS.append((version, fn))
        return fn

    return deco


@register("2026-11-03-partitions-tree")
def _partitions_tree(conn: sqlite3.Connection) -> None:
    """分区表树化：去掉 name 全局唯一，加 parent_id / level / path。

    旧表里 name 有 UNIQUE 约束（表级），要改成「父+名」唯一必须重建表。
    id 保持不变，note_partitions 的外键引用不会断。
    """
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(partitions)").fetchall()}
    if "parent_id" in cols:
        return  # 已是新结构

    conn.execute(
        "CREATE TABLE partitions_new ("
        " id INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " parent_id INTEGER REFERENCES partitions(id) ON DELETE CASCADE,"
        " level INTEGER NOT NULL DEFAULT 1,"
        " path TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO partitions_new(id, name, parent_id, level, path) "
        "SELECT id, name, NULL, 1, name FROM partitions"
    )
    conn.execute("DROP TABLE partitions")
    conn.execute("ALTER TABLE partitions_new RENAME TO partitions")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_partitions_parent ON partitions(parent_id)"
    )


@register("2026-11-03-deleted-templates")
def _deleted_templates(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS deleted_templates ("
        " name TEXT PRIMARY KEY,"
        " deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )


@register("2026-11-03-items-processed-path")
def _items_processed_path(conn: sqlite3.Connection) -> None:
    """items 表补 processed_path（条目入库后能查到 B 层产物在哪）。"""
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    if "processed_path" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN processed_path TEXT")


def run_migrations(conn: sqlite3.Connection) -> list[str]:
    """执行未应用的迁移，返回本次应用的版本列表。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY,"
        " applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    applied = {
        str(r[0])
        for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    done: list[str] = []
    for version, fn in MIGRATIONS:
        if version in applied:
            continue
        fn(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version) VALUES(?)", (version,)
        )
        done.append(version)
    return done
