"""索引服务：vault 扫描、建索引、重建索引、存量分层。

对齐 v0.5.0 §8.1 与 §10：
- 扫描全部 .md（排除工具目录）→ 解析 frontmatter → 按层归类建索引；
- 无结构的老笔记 → 归入"存量未分层"清单，用户批量指定层级或保持原样仅索引；
- FTS + 反链 + embedding 批量构建（进度条 + toast）；
- "重建索引"：全库 md → notes/links/FTS/embeddings 重建（<60s@5000 条）。

**md 是唯一真相源**：本服务任何时候都能从 vault 完整重建索引。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core import fts as fts_mod, vault as vault_mod
from app.core.config import get_config
from app.core.db import get_db
from app.core.tokenizer import normalize_tag
from app.services.link_resolver import LinkResolver

# 工具自有目录（不作为存量笔记扫描）
TOOL_DIRS = {"01_Inbox", "02_Processed", "03_Notes", "99_Archive", "_reports", "assets"}


@dataclass
class ScanResult:
    """一次扫描的统计。"""

    total_files: int = 0
    indexed: int = 0
    layered: dict[str, int] = field(default_factory=dict)
    unlayered: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0
    failed: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_files": self.total_files,
            "indexed": self.indexed,
            "layered": self.layered,
            "unlayered_count": len(self.unlayered),
            "unlayered": self.unlayered[:200],
            "elapsed_ms": self.elapsed_ms,
            "failed": self.failed[:50],
        }


class IndexService:
    """索引服务。"""

    def __init__(self) -> None:
        self.cfg = get_config()
        self.db = get_db()

    # -- 扫描 -------------------------------------------------------------
    def scan_vault(
        self,
        progress: Callable[[int, int], None] | None = None,
        include_tool_dirs: bool = False,
        replace: bool = True,
    ) -> ScanResult:
        """扫描 vault 全部 md → 建索引。

        include_tool_dirs：
        - False（默认）：只扫「存量」——排除工具自有目录（01_Inbox/02_Processed/…），
          用于 §8.1 的存量管线，把用户的老笔记纳入索引；
        - True：连工具目录一起扫——用于 §10 的「重建索引」，
          必须覆盖全部 md 才能真正重建 notes/links/FTS。

        replace：是否先删除同路径的旧索引行。表已清空时应传 False。
        """
        started = time.time()
        result = ScanResult()
        vault_path = self.cfg.vault.path

        if not vault_path.exists():
            return result

        files = [
            p
            for p in vault_path.rglob("*.md")
            if include_tool_dirs or not self._is_tool_path(p)
        ]
        result.total_files = len(files)

        layered_counts: dict[str, int] = {"processed": 0, "note": 0, "other": 0}
        unlayered: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        # 分批索引：每 BATCH 条一次写事务（性能关键路径）
        BATCH = 200
        pending: list[tuple[Path, vault_mod.Note, str]] = []

        def flush() -> None:
            nonlocal pending
            if pending:
                self._index_files(pending, replace=replace)
                pending = []

        for index, path in enumerate(files):
            try:
                note = vault_mod.read_note(path)
                if note is None:
                    failed.append({"path": path.as_posix(), "error": "读取失败"})
                    continue
                level = self._detect_level(path, note)
                if level == "inbox":
                    # A 层原文不是笔记，不进 notes/notes_fts
                    continue
                if level in ("processed", "note"):
                    pending.append((path, note, level))
                    if len(pending) >= BATCH:
                        flush()
                    layered_counts[level] += 1
                    result.indexed += 1
                else:
                    unlayered.append(
                        {
                            "path": path.as_posix(),
                            "rel_path": path.relative_to(vault_path).as_posix(),
                            "title": note.title,
                            "head": note.body.strip()[:120],
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                failed.append({"path": path.as_posix(), "error": str(exc)[:200]})
            if progress and index % 50 == 0:
                progress(index, len(files))

        flush()

        # 反链重建（需要所有 notes 先入库）
        self._rebuild_links()

        result.layered = layered_counts
        result.unlayered = unlayered
        result.failed = failed
        result.elapsed_ms = int((time.time() - started) * 1000)
        return result

    def _is_tool_path(self, path: Path) -> bool:
        """工具自有目录不作为存量扫描对象。"""
        try:
            rel = path.relative_to(self.cfg.vault.path)
        except ValueError:
            return False
        return bool(rel.parts and rel.parts[0] in TOOL_DIRS)

    def _detect_level(self, path: Path, note: vault_mod.Note) -> str:
        """判定层级：frontmatter type 优先，否则按目录推断。

        返回 processed / note / inbox / other。
        """
        ftype = str(note.frontmatter.get("type") or "").strip()
        if ftype in ("processed", "note", "inbox"):
            return ftype
        try:
            rel = path.relative_to(self.cfg.vault.path)
            top = rel.parts[0] if rel.parts else ""
            if top == self.cfg.vault.processed_dir:
                return "processed"
            if top == self.cfg.vault.notes_dir:
                return "note"
            if top == self.cfg.vault.inbox_dir:
                return "inbox"
        except ValueError:
            pass
        return "other"

    def _index_file(self, path: Path, note: vault_mod.Note, level: str) -> None:
        """索引单个文件。"""
        self._index_files([(path, note, level)])

    def reindex_one(self, path: Path) -> None:
        """重新索引单个 md 文件（层级自动判定）。

        用于「改完 frontmatter 后同步索引」——
        例如补写个人手稿：notes.digested / personal_summary_empty
        与 FTS 的 manuscript 列都必须跟着变，否则「待补手稿」清单永远刷不掉。
        """
        path = Path(path)
        note = vault_mod.read_note(path)
        if note is None:
            raise KeyError("笔记读取失败")
        level = self._detect_level(path, note)
        if level not in ("processed", "note"):
            return
        self._index_files([(path, note, level)])

    def _dir_partition_candidates(self, path: Path) -> list[str]:
        """从文件所在目录推导分区路径候选（由深到浅）。

        `成长树/思维逻辑/xxx.md` → ["成长树/思维逻辑", "成长树"]
        只返回**真实存在的分区**（避免给库里凭空造分区）。
        """
        try:
            rel = path.relative_to(self.cfg.vault.path)
        except ValueError:
            return []

        parts = [p for p in rel.parts[:-1] if p and not p.startswith(".")]
        # 工具目录不是分区
        parts = [p for p in parts if p not in TOOL_DIRS]

        candidates: list[str] = []
        for i in range(len(parts), 0, -1):
            candidate = "/".join(parts[:i])
            if candidate not in candidates:
                candidates.append(candidate)

        if not candidates:
            return []

        # 只保留库里存在的分区路径
        from app.services.partition_service import get_partition_service

        psvc = get_partition_service()
        out: list[str] = []
        for candidate in candidates:
            try:
                row = psvc.get_by_path(candidate)
            except Exception:  # noqa: BLE001
                row = None
            if row is not None:
                out.append(candidate)
                break  # 最深的那个命中即可（父分区通过子树展开覆盖）
        return out

    def _prepare_entry(
        self, path: Path, note: vault_mod.Note, level: str
    ) -> dict[str, Any]:
        """把一个 md 文件转成待写入的索引条目。"""
        rel = path.as_posix()
        title = note.title
        tags = [normalize_tag(str(t)) for t in note.tags]
        tags = [t for t in tags if t]
        partitions = [normalize_tag(str(p)) for p in note.partitions]
        partitions = [p for p in partitions if p]
        # 用户的 Obsidian 笔记没有分区 frontmatter——分区体现在**目录路径**里。
        # 例：成长树/思维逻辑.md → 分区「成长树/思维逻辑」（命中分区树里的叶子）
        for dir_path in self._dir_partition_candidates(path):
            if dir_path not in partitions:
                partitions.append(dir_path)
        manuscript = note.manuscript
        tags_text = " ".join(tags)

        columns = fts_mod.build_columns(
            title=title,
            summary=note.summary,
            manuscript=manuscript,
            body=note.body,
            tags=tags_text,
        )
        return {
            "rel": rel,
            "level": level,
            "title": title,
            "form": note.form,
            "tags": tags,
            "partitions": partitions,
            "columns": columns,
            "snippet_src": fts_mod.build_snippet_src(
                title=title,
                summary=note.summary,
                manuscript=manuscript,
                body=note.body,
                tags=tags_text,
            ),
            "manuscript_empty": 0 if manuscript.strip() else 1,
            "digested": 1 if note.digested else 0,
            # 归档状态由文件所在目录推导（§10：归档 = 移入 99_Archive/）。
            # 否则重建索引会把归档笔记的 archived 标志抹掉。
            "archived": 1 if self._is_archived_path(path) else 0,
            "created": str(
                note.frontmatter.get("created")
                or note.frontmatter.get("captured_at")
                or ""
            ),
            "reviewed": str(note.frontmatter.get("reviewed_at") or ""),
        }

    def _is_archived_path(self, path: Path) -> bool:
        """文件是否位于归档目录。"""
        try:
            rel = path.relative_to(self.cfg.vault.path)
        except ValueError:
            return False
        return bool(rel.parts and rel.parts[0] == self.cfg.vault.archive_dir)

    def _index_files(
        self,
        entries: list[tuple[Path, vault_mod.Note, str]],
        replace: bool = True,
    ) -> None:
        """批量索引（一次写事务，避免每文件一次队列往返）。

        5000 条重建要 <60s，逐文件写事务会成为瓶颈；批量后差距是数量级的。

        replace=False 用于"表已清空"的重建场景——此时无需再删旧行。
        note_path 是 FTS5 的 UNINDEXED 列，没有索引，
        `DELETE ... WHERE note_path = ?` 每次都是全表扫描，
        重建时逐行删除会退化成 O(n²)。
        """
        if not entries:
            return
        prepared = [self._prepare_entry(p, n, lv) for p, n, lv in entries]

        # 分区必须先存在于 partitions 表（note_partitions 靠 id 关联）
        from app.services.partition_service import get_partition_service

        psvc = get_partition_service()
        for e in prepared:
            for part in e.get("partitions") or []:
                try:
                    psvc.ensure_by_name(part)
                except Exception:  # noqa: BLE001
                    continue

        def _run(conn: sqlite3.Connection) -> None:
            # 1) 词表：收集所有标签一次性入库
            all_tags = sorted({t for e in prepared for t in e["tags"]})
            if all_tags:
                conn.executemany(
                    "INSERT INTO tags(name, source) VALUES(?, 'seed') "
                    "ON CONFLICT(name) DO NOTHING",
                    [(t,) for t in all_tags],
                )
            tag_ids = {
                str(r["name"]): int(r["id"])
                for r in conn.execute("SELECT id, name FROM tags").fetchall()
            }

            # 2) notes + notes_fts
            conn.executemany(
                "INSERT INTO notes(note_path, level, title, form, "
                "personal_summary_empty, digested, archived, created_at, reviewed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(note_path) DO UPDATE SET level=excluded.level, "
                "title=excluded.title, form=excluded.form, "
                "personal_summary_empty=excluded.personal_summary_empty, "
                "digested=excluded.digested, archived=excluded.archived",
                [
                    (
                        e["rel"],
                        e["level"],
                        e["title"],
                        e["form"],
                        e["manuscript_empty"],
                        e["digested"],
                        e["archived"],
                        e["created"],
                        e["reviewed"],
                    )
                    for e in prepared
                ],
            )
            if replace:
                conn.executemany(
                    "DELETE FROM notes_fts WHERE note_path = ?",
                    [(e["rel"],) for e in prepared],
                )
            conn.executemany(
                "INSERT INTO notes_fts(title, summary, manuscript, body, tags, level, "
                "note_path, snippet_src) VALUES(?,?,?,?,?,?,?,?)",
                [
                    (
                        e["columns"]["title"],
                        e["columns"]["summary"],
                        e["columns"]["manuscript"],
                        e["columns"]["body"],
                        e["columns"]["tags"],
                        e["level"],
                        e["rel"],
                        e["snippet_src"],
                    )
                    for e in prepared
                ],
            )

            # 3) note_tags
            id_rows = conn.execute(
                "SELECT id, note_path FROM notes WHERE note_path IN (%s)"  # noqa: UP031
                % ",".join("?" for _ in prepared),
                [e["rel"] for e in prepared],
            ).fetchall()
            id_by_path = {str(r["note_path"]): int(r["id"]) for r in id_rows}
            tag_links: list[tuple[int, int]] = []
            for e in prepared:
                note_id = id_by_path.get(e["rel"])
                if note_id is None:
                    continue
                for tag in e["tags"]:
                    tag_id = tag_ids.get(tag)
                    if tag_id is not None:
                        tag_links.append((note_id, tag_id))
            if tag_links:
                conn.executemany(
                    "INSERT OR IGNORE INTO note_tags(note_id, tag_id) VALUES(?,?)",
                    tag_links,
                )

            # 4) note_partitions —— 重建索引时也要恢复分区关联，
            #    否则「按分区搜索」与分区计数全是 0
            # 按 **path** 匹配（分区是树，name 可能在不同父级重复）
            part_ids = {
                str(r["path"] or r["name"]): int(r["id"])
                for r in conn.execute("SELECT id, name, path FROM partitions").fetchall()
            }
            part_ids.update(
                {
                    str(r["name"]): int(r["id"])
                    for r in conn.execute("SELECT id, name FROM partitions").fetchall()
                }
            )
            part_links: list[tuple[int, int]] = []
            for e in prepared:
                note_id = id_by_path.get(e["rel"])
                if note_id is None:
                    continue
                for part in e.get("partitions") or []:
                    pid = part_ids.get(part)
                    if pid is not None:
                        part_links.append((note_id, pid))
            if part_links:
                conn.executemany(
                    "INSERT OR IGNORE INTO note_partitions(note_id, partition_id) VALUES(?,?)",
                    part_links,
                )

        self.db.write_tx(_run)

    def _rebuild_links(self) -> None:
        """重建 links 表（confirmed + citation）。

        confirmed_related 只在 B 层 frontmatter 里；citation 在 C 层。

        性能要点：链接解析必须**一次性建索引**。
        `vault_mod.link_target_to_relpath()` 每次调用都会遍历整个 vault，
        逐条解析在 5000 篇时是 O(n²)（一次重建要几分钟）。
        这里先建 {标题/文件名 → 路径} 映射，再 O(1) 查表。
        """
        rows = self.db.query("SELECT id, note_path, level FROM notes")
        id_by_path = {str(r["note_path"]): int(r["id"]) for r in rows}

        # 一次性建索引：文件名 stem / 带日期前缀的 stem / frontmatter title
        resolver = LinkResolver(self.cfg.vault.path).build()

        edges: set[tuple[int, int, str]] = set()

        for row in rows:
            src_id = int(row["id"])
            path = Path(str(row["note_path"]))
            note = vault_mod.read_note(path)
            if note is None:
                continue

            if row["level"] == "processed":
                for raw in note.frontmatter.get("confirmed_related") or []:
                    target = resolver.resolve(str(raw))
                    dst = id_by_path.get(target)
                    if dst is not None and dst != src_id:
                        edges.add((src_id, dst, "confirmed"))
            else:
                citations = note.frontmatter.get("citations") or []
                if not citations:
                    citations = vault_mod.parse_wikilinks(note.body)
                for raw in citations:
                    target = resolver.resolve(str(raw))
                    dst = id_by_path.get(target)
                    if dst is not None and dst != src_id:
                        edges.add((src_id, dst, "citation"))

        def _run(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM links")
            conn.executemany(
                "INSERT OR IGNORE INTO links(src_note_id, dst_note_id, kind) VALUES(?,?,?)",
                sorted(edges),
            )

        self.db.write_tx(_run)

    def _resolve(self, raw: str) -> str:
        target = raw.strip()
        if target.startswith("[[") and target.endswith("]]"):
            target = target[2:-2]
        if not target:
            return ""
        return vault_mod.link_target_to_relpath(target, self.cfg.vault.path)

    # -- 重建索引 ---------------------------------------------------------
    def reindex_all(self, progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
        """全库重建索引（notes / links / FTS）。

        必须包含工具自有目录——「重建索引」的语义是"全库 md → 索引"，
        漏掉 02_Processed / 03_Notes 会导致 B/C 层笔记从索引中消失。
        """
        started = time.time()

        def _clear(conn: sqlite3.Connection) -> None:
            # 顺序重要：先删依赖表，再删 notes（避免留下孤儿引用）
            # 整表删除而非逐行 DELETE —— FTS5 的 UNINDEXED 列没有索引，
            # 逐行 DELETE WHERE note_path = ? 每次都要全表扫描。
            conn.execute("DROP TABLE IF EXISTS notes_fts")
            conn.execute(
                "CREATE VIRTUAL TABLE notes_fts USING fts5("
                "title, summary, manuscript, body, tags, "
                "level UNINDEXED, note_path UNINDEXED, snippet_src UNINDEXED, "
                "tokenize='unicode61')"
            )
            conn.execute("DELETE FROM links")
            conn.execute("DELETE FROM note_tags")
            conn.execute("DELETE FROM note_partitions")
            conn.execute("DELETE FROM notes")

        self.db.write_tx(_clear)
        result = self.scan_vault(
            progress=progress, include_tool_dirs=True, replace=False
        )
        return {
            "ok": True,
            "elapsed_ms": int((time.time() - started) * 1000),
            **result.to_dict(),
        }

    # -- 存量分层 ---------------------------------------------------------
    def assign_layer(self, paths: list[str], level: str) -> dict[str, Any]:
        """批量指定存量笔记层级（processed / note）。"""
        if level not in ("processed", "note"):
            raise ValueError("层级必须是 processed 或 note")

        updated = 0
        for rel in paths:
            path = Path(rel)
            if not path.exists():
                path = self.cfg.vault.path / rel
            if not path.exists():
                continue
            note = vault_mod.read_note(path)
            if note is None:
                continue
            fm = dict(note.frontmatter)
            fm["type"] = level
            fm.setdefault("title", note.title)
            vault_mod.write_note(path, fm, note.body)
            self._index_file(path, vault_mod.read_note(path) or note, level)
            updated += 1
        self._rebuild_links()
        return {"ok": True, "updated": updated}

    # -- 标签词表冷启动 ---------------------------------------------------
    def seed_tag_vocabulary(self) -> dict[str, Any]:
        """冷启动：扫描存量笔记，生成初始的「标签 / 分区」两级词表。

        - 标签：来自存量 frontmatter 的 tags；
        - 分区：来自存量 frontmatter 的 partitions，**以及 vault 的顶层目录**
          （目录天然就是"大类"，比让用户从零手填分区更合用；
          工具自有目录与隐藏目录除外）。
        """
        from app.core.tokenizer import normalize_tag as _norm

        collected: dict[str, int] = {}
        collected_parts: dict[str, int] = {}
        for path in self.cfg.vault.path.rglob("*.md"):
            if self._is_tool_path(path):
                continue
            try:
                fm, _ = vault_mod.parse_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            for raw in fm.get("tags") or []:
                name = _norm(str(raw))
                if name:
                    collected[name] = collected.get(name, 0) + 1
            for raw in fm.get("partitions") or []:
                name = _norm(str(raw))
                if name:
                    collected_parts[name] = collected_parts.get(name, 0) + 1

        # 注：分区冷启动已移到 partition_service.seed_from_vault_dirs（目录树 → 分区树）

        def _run(conn: sqlite3.Connection) -> None:
            for name, _count in collected.items():
                conn.execute(
                    "INSERT INTO tags(name, source) VALUES(?, 'seed') "
                    "ON CONFLICT(name) DO UPDATE SET source = "
                    "CASE WHEN tags.source = 'ai' THEN 'ai' ELSE 'seed' END",
                    (name,),
                )

        self.db.write(_run)
        return {
            "ok": True,
            "tags": len(collected),
            "partitions": len(collected_parts),
            "vocabulary": sorted(collected),
            "partition_vocabulary": sorted(collected_parts),
        }


_index: IndexService | None = None


def get_index_service() -> IndexService:
    global _index
    if _index is None:
        _index = IndexService()
    return _index
