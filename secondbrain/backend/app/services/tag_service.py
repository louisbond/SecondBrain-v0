"""标签治理服务：两级受控词表、合并、停用。

对齐 v0.5.0 §7：
- 两级受控词表：partitions（大类）+ tags（细粒度）；AI prompt 动态注入；
- 冷启动：扫描存量 vault 全部 frontmatter 标签，去重合并为初始词表；
- 增长规则：AI 只能从词表选；新标签必须经用户确认；
- 管理页：词表浏览、合并（`A→B` 自动改写受影响 md 的 frontmatter）、停用、分区管理；
- 每次合并/停用触发受影响笔记的 FTS 与 embedding 增量更新。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from app.core import vault as vault_mod
from app.core.config import get_config
from app.core.db import get_db
from app.core.tokenizer import build_fts_payload, normalize_tag


class TagService:
    """标签治理。"""

    def __init__(self) -> None:
        self.cfg = get_config()
        self.db = get_db()

    # -- 查询 -------------------------------------------------------------
    def vocabulary(self) -> dict[str, Any]:
        """词表总览。"""
        tags = self.db.query(
            "SELECT t.id, t.name, t.source, "
            "(SELECT COUNT(*) FROM note_tags nt WHERE nt.tag_id = t.id) AS count "
            "FROM tags t ORDER BY count DESC, t.name"
        )
        partitions = self.db.query(
            "SELECT p.id, p.name, "
            "(SELECT COUNT(*) FROM note_partitions np WHERE np.partition_id = p.id) AS count "
            "FROM partitions p ORDER BY count DESC, p.name"
        )
        return {
            "tags": [dict(r) for r in tags],
            "partitions": [dict(r) for r in partitions],
        }

    def list_notes_with_tag(self, tag: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT n.note_path, n.title, n.level FROM note_tags nt "
            "JOIN tags t ON t.id = nt.tag_id JOIN notes n ON n.id = nt.note_id "
            "WHERE t.name = ?",
            (tag,),
        )
        return [dict(r) for r in rows]

    # -- 合并 -------------------------------------------------------------
    def _affected_notes_by_md(self, field: str, name: str) -> list[dict[str, Any]]:
        """按 md 文件内容找受影响笔记。

        不能只依赖 note_tags/note_partitions 表——用户可能手改过 md，
        或索引尚未建立（§10 明确"重建索引"是用户的兜底手段）。
        直接扫 md 保证合并一定能改写 frontmatter。
        """
        affected: list[dict[str, Any]] = []
        for path in self.cfg.vault.path.rglob("*.md"):
            note = vault_mod.read_note(path)
            if note is None:
                continue
            values = note.frontmatter.get(field) or []
            if any(normalize_tag(str(v)) == name for v in values):
                affected.append({"note_path": path.as_posix(), "title": note.title})
        return affected

    def merge_tags(self, src: str, dst: str) -> dict[str, Any]:
        """合并标签 A→B：改写受影响 md 的 frontmatter + 更新索引。"""
        src = normalize_tag(src)
        dst = normalize_tag(dst)
        if not src or not dst:
            raise ValueError("标签名不能为空")
        if src == dst:
            raise ValueError("源标签与目标标签相同")

        # 目标标签必须存在（不存在则创建）
        def _ensure(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO tags(name, source) VALUES(?, 'merged') "
                "ON CONFLICT(name) DO NOTHING",
                (dst,),
            )

        self.db.write(_ensure)

        affected = self._affected_notes_by_md("tags", src)
        changed = 0

        for note_info in affected:
            path = Path(note_info["note_path"])
            if not path.exists():
                continue
            note = vault_mod.read_note(path)
            if note is None:
                continue
            fm = dict(note.frontmatter)
            raw_tags = list(fm.get("tags") or [])
            new_tags: list[str] = []
            for tag in raw_tags:
                norm = normalize_tag(str(tag))
                if norm == src:
                    if dst not in new_tags:
                        new_tags.append(dst)
                elif norm not in new_tags:
                    new_tags.append(str(tag))
            fm["tags"] = new_tags
            vault_mod.write_note(path, fm, note.body)
            changed += 1

        # 更新索引与 note_tags
        def _reindex(conn: sqlite3.Connection) -> None:
            src_row = conn.execute("SELECT id FROM tags WHERE name = ?", (src,)).fetchone()
            dst_row = conn.execute("SELECT id FROM tags WHERE name = ?", (dst,)).fetchone()
            if src_row is None or dst_row is None:
                return
            # 迁移 note_tags
            conn.execute(
                "UPDATE OR IGNORE note_tags SET tag_id = ? WHERE tag_id = ?",
                (dst_row["id"], src_row["id"]),
            )
            conn.execute("DELETE FROM note_tags WHERE tag_id = ?", (src_row["id"],))
            # 删除源标签
            conn.execute("DELETE FROM tags WHERE id = ?", (src_row["id"],))
            conn.execute(
                "INSERT INTO tag_merges(src_name, dst_name, affected, created_at) "
                "VALUES(?,?,?,?)",
                (src, dst, changed, vault_mod.now_iso()),
            )

        self.db.write(_reindex)
        self._reindex_affected(affected)
        return {"ok": True, "src": src, "dst": dst, "affected": changed}

    def merge_partitions(self, src: str, dst: str) -> dict[str, Any]:
        """合并分区 A→B。"""
        src = normalize_tag(src)
        dst = normalize_tag(dst)
        if not src or not dst or src == dst:
            raise ValueError("分区名无效")

        from app.services.partition_service import get_partition_service

        get_partition_service().ensure_by_name(dst)
        affected = self._affected_notes_by_md("partitions", src)
        changed = 0
        for row in affected:
            path = Path(str(row["note_path"]))
            if not path.exists():
                continue
            note = vault_mod.read_note(path)
            if note is None:
                continue
            fm = dict(note.frontmatter)
            parts = [str(p) for p in (fm.get("partitions") or [])]
            new_parts = []
            for part in parts:
                if normalize_tag(part) == src:
                    if dst not in new_parts:
                        new_parts.append(dst)
                elif part not in new_parts:
                    new_parts.append(part)
            fm["partitions"] = new_parts
            vault_mod.write_note(path, fm, note.body)
            changed += 1

        def _reindex(conn: sqlite3.Connection) -> None:
            src_row = conn.execute(
                "SELECT id FROM partitions WHERE name = ?", (src,)
            ).fetchone()
            dst_row = conn.execute(
                "SELECT id FROM partitions WHERE name = ?", (dst,)
            ).fetchone()
            if src_row is None or dst_row is None:
                return
            conn.execute(
                "UPDATE OR IGNORE note_partitions SET partition_id = ? "
                "WHERE partition_id = ?",
                (dst_row["id"], src_row["id"]),
            )
            conn.execute(
                "DELETE FROM note_partitions WHERE partition_id = ?", (src_row["id"],)
            )
            conn.execute("DELETE FROM partitions WHERE id = ?", (src_row["id"],))

        self.db.write(_reindex)
        self._reindex_affected([{"note_path": r["note_path"]} for r in affected])
        return {"ok": True, "src": src, "dst": dst, "affected": changed}
    def _reindex_affected(self, affected: list[dict[str, Any]]) -> None:
        """合并/停用后触发受影响笔记的 FTS 与 embedding 增量更新。"""
        for info in affected:
            path = Path(str(info.get("note_path") or ""))
            if not path.exists():
                continue
            note = vault_mod.read_note(path)
            if note is None:
                continue
            rel = path.as_posix()
            from app.core import fts as fts_mod

            tags_text = " ".join(note.tags)
            columns = fts_mod.build_columns(
                title=note.title,
                summary=note.summary,
                manuscript=note.manuscript,
                body=note.body,
                tags=tags_text,
            )
            snippet_src = fts_mod.build_snippet_src(
                title=note.title,
                summary=note.summary,
                manuscript=note.manuscript,
                body=note.body,
                tags=tags_text,
            )

            def _run(conn: sqlite3.Connection, rel=rel, note=note, columns=columns,
                     snippet_src=snippet_src) -> None:
                conn.execute("DELETE FROM notes_fts WHERE note_path = ?", (rel,))
                conn.execute(
                    "INSERT INTO notes_fts(title, summary, manuscript, body, tags, level, "
                    "note_path, snippet_src) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        columns["title"],
                        columns["summary"],
                        columns["manuscript"],
                        columns["body"],
                        columns["tags"],
                        note.level,
                        rel,
                        snippet_src,
                    ),
                )
                row = conn.execute(
                    "SELECT id FROM notes WHERE note_path = ?", (rel,)
                ).fetchone()
                if row is None:
                    return
                conn.execute("DELETE FROM note_tags WHERE note_id = ?", (row["id"],))
                for tag in note.tags:
                    trow = conn.execute(
                        "SELECT id FROM tags WHERE name = ?", (normalize_tag(tag),)
                    ).fetchone()
                    if trow is not None:
                        conn.execute(
                            "INSERT OR IGNORE INTO note_tags(note_id, tag_id) VALUES(?,?)",
                            (row["id"], trow["id"]),
                        )

            self.db.write(_run)

            # embedding 增量更新
            try:
                from app.services.embedding_service import get_embedding_service

                get_embedding_service().embed_note(rel)
            except Exception:
                pass

    # -- 停用 / 新增 ------------------------------------------------------
    def deactivate_tag(self, name: str) -> dict[str, Any]:
        """停用标签：从词表移除，并从所有 md 的 frontmatter 中剔除该标签。"""
        name = normalize_tag(name)
        affected = self._affected_notes_by_md("tags", name)
        changed = 0
        for info in affected:
            path = Path(info["note_path"])
            if not path.exists():
                continue
            note = vault_mod.read_note(path)
            if note is None:
                continue
            fm = dict(note.frontmatter)
            fm["tags"] = [
                str(t)
                for t in (fm.get("tags") or [])
                if normalize_tag(str(t)) != name
            ]
            vault_mod.write_note(path, fm, note.body)
            changed += 1

        def _drop(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
            if row is None:
                return
            conn.execute("DELETE FROM note_tags WHERE tag_id = ?", (row["id"],))
            conn.execute("DELETE FROM tags WHERE id = ?", (row["id"],))

        self.db.write(_drop)
        self._reindex_affected(affected)
        return {"ok": True, "tag": name, "affected": changed}

    def add_tag(self, name: str, source: str = "manual") -> dict[str, Any]:
        """手动新增标签到受控词表。"""
        name = normalize_tag(name)
        if not name:
            raise ValueError("标签名不能为空")

        def _run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO tags(name, source) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET source = excluded.source",
                (name, source),
            )

        self.db.write(_run)
        return {"ok": True, "name": name}

    def add_partition(self, name: str) -> dict[str, Any]:
        name = normalize_tag(name)
        if not name:
            raise ValueError("分区名不能为空")
        from app.services.partition_service import get_partition_service

        return get_partition_service().ensure_by_name(name)
        return {"ok": True, "name": name}

    def rename_tag(self, old: str, new: str) -> dict[str, Any]:
        """重命名标签（等价于合并到新标签 + 停用旧标签）。"""
        old = normalize_tag(old)
        new = normalize_tag(new)
        if not old or not new:
            raise ValueError("标签名不能为空")
        if old == new:
            return {"ok": True, "affected": 0}
        self.add_tag(new, source="manual")
        return self.merge_tags(old, new)


_tags: TagService | None = None


def get_tag_service() -> TagService:
    global _tags
    if _tags is None:
        _tags = TagService()
    return _tags
