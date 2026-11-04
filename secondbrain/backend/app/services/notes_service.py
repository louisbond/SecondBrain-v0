"""C 层笔记服务：创作笔记 CRUD、引用解析、外部导入、灵感推荐。

对齐 v0.5.0 §9：
- 单一笔记类型 `type: note`，`form: fragment|article` 字段区分形式；
- 引用：编辑器插入 `[[]]`，`citations` 解析为 `links(kind=citation)`；
- 外部导入：粘贴 md 或纯文本，自动建 C 层笔记并解析已有 `[[]]`；
- 灵感推荐：编辑时取（标题+正文前 500 字）向量召回 Top-5。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core import vault as vault_mod
from app.core.config import get_config
from app.core.db import get_db
from app.core.tokenizer import normalize_tag
from app.services.link_resolver import LinkResolver


class NotesService:
    """C 层笔记服务。"""

    def __init__(self) -> None:
        self.cfg = get_config()
        self.db = get_db()

    # -- 读取 -------------------------------------------------------------
    def get_note(self, note_path: str) -> dict[str, Any] | None:
        path = Path(note_path)
        if not path.exists():
            # 尝试按文件名解析
            resolved = vault_mod.link_target_to_relpath(note_path, self.cfg.vault.path)
            path = Path(resolved)
            if not path.exists():
                return None

        note = vault_mod.read_note(path)
        if note is None:
            return None

        from app.services.search_service import get_search_service

        search = get_search_service()
        rel = path.as_posix()
        return {
            "note_path": rel,
            "title": note.title,
            "level": note.level,
            "form": note.form,
            "frontmatter": note.frontmatter,
            # 双摘要（AI 在上、个人手稿在下）——阅读页直接用这两个顶层字段，
            # 不要让前端去 frontmatter 里翻（v29 时前端读 note.summary / note.manuscript，
            # 后端没返回 → 两块永远不渲染）。
            "summary": note.summary,
            "manuscript": note.manuscript,
            "body": note.body,
            "tags": note.tags,
            "partitions": note.partitions,
            "digested": note.digested,
            "citations": vault_mod.parse_wikilinks(
                str(note.frontmatter.get("citations") or "")
            ),
            "backlinks": search.backlinks(rel),
            "outlinks": search.outlinks(rel),
        }

    def list_notes(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT n.note_path, n.title, n.form, n.digested, n.archived, n.created_at, "
            "n.reviewed_at FROM notes n WHERE n.level='note' "
            "ORDER BY n.created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(r) for r in rows]

    # -- 写入 -------------------------------------------------------------
    def create_note(
        self,
        title: str,
        body: str = "",
        form: str = "fragment",
        tags: list[str] | None = None,
        partitions: list[str] | None = None,
        citations: list[str] | None = None,
        digested: bool = False,
    ) -> dict[str, Any]:
        """新建 C 层笔记。"""
        self.cfg.vault.ensure()
        title = (title or "").strip() or "未命名"
        stem = vault_mod.sanitize_filename(
            f"{datetime.now().strftime('%Y-%m-%d')}-{title}"
        )
        path = vault_mod.unique_path(self.cfg.vault.dir("notes"), stem, ".md")

        final_tags = [normalize_tag(t) for t in (tags or [])]
        final_tags = [t for t in final_tags if t]
        final_parts = [normalize_tag(p) for p in (partitions or [])]
        final_parts = [p for p in final_parts if p]

        # 引用来源：显式 citations ∪ 正文中的 [[...]]
        citation_links = [c.strip() for c in (citations or []) if c.strip()]
        body_links = vault_mod.parse_wikilinks(body or "")
        for link in body_links:
            if link not in citation_links:
                citation_links.append(link)

        frontmatter: dict[str, Any] = {
            "title": title,
            "type": "note",
            "form": form if form in ("fragment", "article") else "fragment",
            "tags": final_tags,
            "partitions": final_parts,
            "citations": [f"[[{c}]]" for c in citation_links],
            "digested": bool(digested),
            "created": vault_mod.now_iso(),
        }
        vault_mod.write_note(path, frontmatter, body or "")
        self._upsert_tags(final_tags)
        self._upsert_partitions(final_parts)
        self._index_note(path, frontmatter, body or "")
        return {"note_path": path.as_posix(), "title": title}

    def update_note(
        self,
        note_path: str,
        title: str | None = None,
        body: str | None = None,
        form: str | None = None,
        tags: list[str] | None = None,
        partitions: list[str] | None = None,
        digested: bool | None = None,
    ) -> dict[str, Any]:
        """更新 C 层笔记。"""
        path = Path(note_path)
        if not path.exists():
            resolved = vault_mod.link_target_to_relpath(note_path, self.cfg.vault.path)
            path = Path(resolved)
            if not path.exists():
                raise KeyError("笔记不存在")

        note = vault_mod.read_note(path)
        if note is None:
            raise KeyError("笔记读取失败")

        fm = dict(note.frontmatter)
        if title is not None:
            fm["title"] = title.strip()
        if form is not None:
            fm["form"] = form
        if tags is not None:
            final_tags = [normalize_tag(t) for t in tags]
            fm["tags"] = [t for t in final_tags if t]
            self._upsert_tags(fm["tags"])
        if partitions is not None:
            final_parts = [normalize_tag(p) for p in partitions]
            fm["partitions"] = [p for p in final_parts if p]
            self._upsert_partitions(fm["partitions"])
        if digested is not None:
            fm["digested"] = bool(digested)

        new_body = note.body if body is None else body
        vault_mod.write_note(path, fm, new_body)

        # 引用从正文自动补全（[[...]] → citations）
        links = vault_mod.parse_wikilinks(new_body)
        if links:
            fm["citations"] = [f"[[{stem}]]" for stem in dict.fromkeys(links)]
            vault_mod.write_note(path, fm, new_body)

        self._index_note(path, fm, new_body)
        return {"note_path": path.as_posix(), "title": fm.get("title", "")}

    def fill_manuscript(self, note_path: str, manuscript: str) -> dict[str, Any]:
        """补写个人手稿（已入库但手稿为空的 B 层笔记）。

        设计文档 §438：手稿为空也可确认入库（软门槛不拦截），
        但条目进入「待补手稿」清单，回顾卡片持续提醒 —— 必须有个补写入口，
        否则软门槛就变成了「永远补不上」的死路。

        md 是唯一真相源：改 frontmatter 的 personal_summary 后重写 md，
        再重索引（notes.personal_summary_empty / digested / FTS 的 manuscript 列）。
        """
        path = Path(note_path)
        if not path.exists():
            resolved = vault_mod.link_target_to_relpath(note_path, self.cfg.vault.path)
            path = Path(resolved)
            if not path.exists():
                raise KeyError("笔记不存在")

        note = vault_mod.read_note(path)
        if note is None:
            raise KeyError("笔记读取失败")

        fm = dict(note.frontmatter)
        text = (manuscript or "").strip()
        fm["personal_summary"] = text
        # 有手稿即视为已消化（与 _write_processed 的 digested 规则一致）
        fm["digested"] = bool(text)
        vault_mod.write_note(path, fm, note.body)

        # 重索引：让 notes.digested / personal_summary_empty 与 FTS 同步
        from app.services.index_service import get_index_service

        get_index_service().reindex_one(path)

        return {
            "ok": True,
            "note_path": path.as_posix(),
            "title": fm.get("title", ""),
            "digested": bool(text),
        }

    def delete_note(self, note_path: str) -> bool:
        path = Path(note_path)
        if not path.exists():
            return False
        rel = path.as_posix()

        def _run(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT id FROM notes WHERE note_path = ?", (rel,)
            ).fetchone()
            if row is not None:
                conn.execute("DELETE FROM links WHERE src_note_id = ?", (row["id"],))
                conn.execute("DELETE FROM links WHERE dst_note_id = ?", (row["id"],))
                conn.execute("DELETE FROM note_tags WHERE note_id = ?", (row["id"],))
                conn.execute("DELETE FROM note_partitions WHERE note_id = ?", (row["id"],))
                conn.execute("DELETE FROM notes WHERE id = ?", (row["id"],))
            conn.execute("DELETE FROM notes_fts WHERE note_path = ?", (rel,))
            conn.execute("DELETE FROM embeddings WHERE note_path = ?", (rel,))

        self.db.write(_run)
        try:
            path.unlink()
        except OSError:
            return False
        return True

    # -- 外部导入 ---------------------------------------------------------
    def import_external(self, content: str, title: str | None = None) -> dict[str, Any]:
        """粘贴 md 或纯文本 → 自动建 C 层笔记并解析已有 [[]]。"""
        content = (content or "").strip()
        if not content:
            raise ValueError("导入内容为空")

        frontmatter: dict[str, Any] = {}
        body = content
        if content.startswith("---"):
            fm, body = vault_mod.parse_frontmatter(content)
            if fm:
                frontmatter = fm

        note_title = (
            (title or "").strip()
            or str(frontmatter.get("title") or "").strip()
            or self._derive_title(body)
        )
        links = vault_mod.parse_wikilinks(body)

        self.cfg.vault.ensure()
        stem = vault_mod.sanitize_filename(
            f"{datetime.now().strftime('%Y-%m-%d')}-{note_title}"
        )
        path = vault_mod.unique_path(self.cfg.vault.dir("notes"), stem, ".md")

        final_tags = [
            normalize_tag(str(t))
            for t in (frontmatter.get("tags") or [])
            if str(t).strip()
        ]
        merged = {
            "title": note_title,
            "type": "note",
            "form": str(frontmatter.get("form") or "fragment"),
            "tags": final_tags,
            "citations": [f"[[{stem}]]" for stem in dict.fromkeys(links)],
            "digested": bool(frontmatter.get("digested", False)),
            "created": vault_mod.now_iso(),
            "imported_from": "external",
        }
        vault_mod.write_note(path, merged, body)
        self._index_note(path, merged, body)
        self._upsert_tags(final_tags)
        return {
            "note_path": path.as_posix(),
            "title": note_title,
            "citations": links,
        }

    @staticmethod
    def _derive_title(body: str) -> str:
        for line in (body or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                return line.lstrip("#").strip()[:80] or "未命名"
            return line[:60] or "未命名"
        return "未命名"

    # -- 灵感推荐 ---------------------------------------------------------
    def inspiration(self, title: str, body: str, top_k: int = 5) -> dict[str, Any]:
        """C 层编辑时的"你可能想引用"。"""
        text = f"{title}\n{body[:500]}"
        try:
            from app.services.embedding_service import get_embedding_service

            svc = get_embedding_service()
            if svc.available():
                results = svc.similar_to_text(text, top_k=top_k)
                return {"results": results, "available": True}
        except Exception:
            pass
        return {"results": [], "available": False}

    # -- 归档 -------------------------------------------------------------
    def archive(self, note_path: str) -> dict[str, Any]:
        """手动归档：移入 99_Archive/（保留引用有效性）。"""
        path = Path(note_path)
        if not path.exists():
            raise KeyError("笔记不存在")
        rel = path.as_posix()

        # 引用该笔记的路径需要跟着更新
        self.cfg.vault.ensure()
        target_dir = self.cfg.vault.dir("archive")
        target = target_dir / path.name
        if target.exists():
            target = vault_mod.unique_path(target_dir, path.stem, ".md")

        # 找出所有指向旧路径的 md，改写引用
        self._rewrite_links(rel, target.as_posix())

        path.rename(target)
        new_rel = target.as_posix()

        def _run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE notes SET note_path = ?, archived = 1 WHERE note_path = ?",
                (new_rel, rel),
            )
            conn.execute(
                "UPDATE notes_fts SET note_path = ? WHERE note_path = ?", (new_rel, rel)
            )

        self.db.write(_run)
        return {"ok": True, "note_path": new_rel}

    def _rewrite_links(self, old_path: str, new_path: str) -> None:
        """把 vault 内指向旧路径的 [[...]] 链接改写为新路径。"""
        old_stem = Path(old_path).stem
        new_stem = Path(new_path).stem
        for md in self.cfg.vault.path.rglob("*.md"):
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            if old_stem not in text:
                continue
            updated = text.replace(f"[[{old_stem}]]", f"[[{new_stem}]]")
            updated = updated.replace(old_path, new_path)
            if updated != text:
                try:
                    md.write_text(updated, encoding="utf-8")
                except OSError:
                    continue

    # -- 索引 -------------------------------------------------------------
    def _upsert_tags(self, tags: list[str]) -> None:
        if not tags:
            return

        def _run(conn: sqlite3.Connection) -> None:
            for name in tags:
                conn.execute(
                    "INSERT INTO tags(name, source) VALUES(?, 'manual') "
                    "ON CONFLICT(name) DO NOTHING",
                    (name,),
                )

        self.db.write(_run)

    def _upsert_partitions(self, partitions: list[str]) -> None:
        """分区也要先入词表，否则 note_partitions 关联写不进去。

        分区是树（同名可存在于不同父级），所以走 partition_service 按路径去重。
        """
        if not partitions:
            return
        from app.services.partition_service import get_partition_service

        svc = get_partition_service()
        for name in partitions:
            try:
                svc.ensure_by_name(name)
            except ValueError:
                continue

    def _index_note(
        self, path: Path, frontmatter: dict[str, Any], body: str
    ) -> None:
        """写 notes / notes_fts / note_tags / links(kind=citation)。"""
        from app.core import fts as fts_mod

        rel = path.as_posix()
        title = str(frontmatter.get("title") or path.stem)
        tags = [str(t) for t in (frontmatter.get("tags") or [])]
        partitions = [str(p) for p in (frontmatter.get("partitions") or [])]
        citations = vault_mod.parse_wikilinks(str(frontmatter.get("citations") or ""))
        if not citations:
            citations = vault_mod.parse_wikilinks(body)
        digested = bool(frontmatter.get("digested"))

        tags_text = " ".join(tags)
        columns = fts_mod.build_columns(
            title=title, body=body, tags=tags_text
        )
        snippet_src = fts_mod.build_snippet_src(
            title=title, body=body, tags=tags_text
        )

        # 解析引用目标 → 目标笔记路径
        # 用 LinkResolver（一次性建索引）而不是逐条扫 vault——
        # 逐条扫在笔记多时是 O(n²)。
        resolver = LinkResolver(self.cfg.vault.path).build()
        targets: list[str] = []
        for target in citations:
            resolved = resolver.resolve(target)
            if resolved and resolved not in targets:
                targets.append(resolved)

        def _run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO notes(note_path, level, title, form, "
                "personal_summary_empty, digested, archived, created_at) "
                "VALUES(?, 'note', ?, ?, 1, ?, 0, ?) "
                "ON CONFLICT(note_path) DO UPDATE SET title=excluded.title, "
                "form=excluded.form, digested=excluded.digested",
                (
                    rel,
                    title,
                    str(frontmatter.get("form") or ""),
                    1 if digested else 0,
                    str(frontmatter.get("created") or vault_mod.now_iso()),
                ),
            )
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
                    "note",
                    rel,
                    snippet_src,
                ),
            )
            row = conn.execute(
                "SELECT id FROM notes WHERE note_path = ?", (rel,)
            ).fetchone()
            if row is None:
                return
            note_id = row["id"]
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            for tag in tags:
                trow = conn.execute(
                    "SELECT id FROM tags WHERE name = ?", (tag,)
                ).fetchone()
                if trow is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO note_tags(note_id, tag_id) VALUES(?,?)",
                        (note_id, trow["id"]),
                    )
            # note_partitions —— 不写的话「按分区搜索」对 C 层笔记永远命中 0 条
            conn.execute(
                "DELETE FROM note_partitions WHERE note_id = ?", (note_id,)
            )
            for part in partitions or []:
                prow = conn.execute(
                    "SELECT id FROM partitions WHERE name = ?", (part,)
                ).fetchone()
                if prow is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO note_partitions(note_id, partition_id) "
                        "VALUES(?,?)",
                        (note_id, prow["id"]),
                    )
            # citations → links(kind=citation)
            conn.execute(
                "DELETE FROM links WHERE src_note_id = ? AND kind = 'citation'",
                (note_id,),
            )
            for target in targets:
                trow = conn.execute(
                    "SELECT id FROM notes WHERE note_path = ?", (target,)
                ).fetchone()
                if trow is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO links(src_note_id, dst_note_id, kind) "
                        "VALUES(?,?,'citation')",
                        (note_id, trow["id"]),
                    )

        self.db.write(_run)


_notes: NotesService | None = None


def get_notes_service() -> NotesService:
    global _notes
    if _notes is None:
        _notes = NotesService()
    return _notes
