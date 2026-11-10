"""数据库迁移（幂等）。

每个迁移在 `schema_migrations` 表留一行；已应用的不会重复执行。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

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


@register("2026-11-06-items-media-url")
def _items_media_url(conn: sqlite3.Connection) -> None:
    """items 补 media_url：短视频解析出的直链 / 用户上传的本地视频路径。

    以前解析器拿到 video_url 只塞进 extra 就丢了，ASR 仍拿原始分享链接去跑
    yt-dlp（抖音要登录、快手短链常失效），于是口播台词永远转不出来。
    """
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    if "media_url" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN media_url TEXT")


@register("2026-11-07-media-audio-archive")
def _media_audio_archive(conn: sqlite3.Connection) -> None:
    """音频归档 + 文件来源 + 进度文字。

    **为什么要存音频**：文本是降维产物，语气/停顿/重音/情绪全丢了。
    MP3 是「人耳复核」的唯一载体，也是换模型重转写的原料。
    无论来源是视频直链还是用户上传的本地文件，最终都统一成
    `media_dir/audio/<audio_hash>.mp3`。

    **为什么要 source_kind**：以前 source_url 是唯一锚点，纯文件入库
    只能造假链接。分成 url / file / paste 三类之后，本地文件用
    sha256 做锚点（改名、移动、删原片都不影响溯源）。

    **jobs.message**：进度条只有百分比时，用户看到 45% 卡两分钟
    完全不知道在干什么。这一列放「识别语音 3/13 段 · 预计还需 1m40s」
    这类实时文字。
    """
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    for name, decl in (
        ("audio_path", "TEXT"),  # 归档 MP3 的绝对路径
        ("audio_hash", "TEXT"),  # MP3 的 sha256（内容寻址 + 去重）
        ("audio_ms", "INTEGER"),  # 时长毫秒（进度 ETA 靠它算）
        ("source_kind", "TEXT"),  # url / file / paste
        ("source_name", "TEXT"),  # 原始文件名（文件来源时）
        ("source_path", "TEXT"),  # 原始文件在用户机器上的位置（仅供参考）
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {name} {decl}")

    job_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "message" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN message TEXT")

    # 音频哈希要去重：同一个视频从 URL 下载和用户上传，抽出的 MP3 相同。
    # 不建 UNIQUE —— 一条素材可能先失败后补，留空值也是常态。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_audio_hash ON items(audio_hash)"
    )


@register("2026-11-03-items-processed-path")
def _items_processed_path(conn: sqlite3.Connection) -> None:
    """items 表补 processed_path（条目入库后能查到 B 层产物在哪）。"""
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    if "processed_path" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN processed_path TEXT")


@register("2026-11-09-items-collect-audio")
def _items_collect_audio(conn: sqlite3.Connection) -> None:
    """items 补 collect_audio：采集时是否强制下载音频转写。

    默认只抓文本（标题/简介）；勾选「采集音频」后，即使文本够长也
    下载视频直链抽音频做 ASR，把口播台词和 MP3 一起归档。
    """
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    if "collect_audio" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN collect_audio INTEGER NOT NULL DEFAULT 0")


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
