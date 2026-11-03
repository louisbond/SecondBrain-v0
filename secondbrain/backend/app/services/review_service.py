"""B 层核准服务：最简驾驶舱 → confirmed → B 层 md 落盘。

对齐 v0.5.0 §6.3 / §4.3：
- 核准页数据：AI 摘要 + 手稿编辑器 + A 层回查链接 + 标签/分区勾选 + 相关笔记勾选；
- **AI 摘要在上、个人手稿在下垂直排布**（不并排）；
- 逐条确认，无批量全收；
- 手稿为空也可确认入库（软门槛不拦截），但进入"待补手稿"清单；
- 确认后 personal_summary 非空 → digested=true。

字段语义（§4.3 修订）：
- `tags` 仅在核准确认后写入 = AI 建议勾选 ∪ 用户手动新增；
- 用户拒绝全部建议 → `tags` 为空数组，不继承任何 AI 值；
- `ai_suggested_tags` / `ai_suggested_partitions` 永远保留 AI 原始建议供追溯；
- `confirmed_related` 是唯一参与反链/图谱的链接字段。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.core import vault as vault_mod
from app.core.config import get_config
from app.core.db import get_db
from app.core.tokenizer import normalize_tag
from app.services.ingest_service import get_ingest_service

# 允许"人工直接核准"的状态：
# pending_review 是正常路径；其余是 AI 挂了/没跑时的兜底——
# 用户手写摘要即可入库，不必卡在反复重试 AI 上（§6.3 的"以 AI 摘要为底稿"
# 本就允许没有 AI 底稿时从零手写）。
MANUAL_CONFIRMABLE = {
    "pending_review",
    "ai_failed",
    "extracted",
    "extract_failed",
}


class ReviewService:
    """核准闭环服务。"""

    def __init__(self) -> None:
        self.cfg = get_config()
        self.db = get_db()
        self.ingest = get_ingest_service()

    # -- 读取核准页数据 ---------------------------------------------------
    def get_review_item(self, item_id: int) -> dict[str, Any]:
        """构造核准页的完整数据。"""
        item = self.ingest.get_item(item_id)
        if item is None:
            raise KeyError("条目不存在")

        db = self.db
        suggestions = db.query(
            "SELECT suggestion_type, suggestion_value, suggestion_reason FROM "
            "ai_suggestions WHERE note_path = ? ORDER BY id",
            (f"item:{item_id}",),
        )

        suggested_tags: list[str] = []
        pending_tags: list[dict[str, str]] = []
        suggested_partitions: list[str] = []
        pending_partitions: list[dict[str, str]] = []
        keywords: list[str] = []

        for row in suggestions:
            stype = row["suggestion_type"]
            value = str(row["suggestion_value"] or "")
            if not value:
                continue
            if stype == "tag":
                suggested_tags.append(value)
            elif stype == "new_tag":
                pending_tags.append(
                    {
                        "name": value,
                        "reason": str(row["suggestion_reason"] or ""),
                    }
                )
            elif stype == "partition":
                suggested_partitions.append(value)
            elif stype == "partition_new":
                pending_partitions.append(
                    {
                        "name": value,
                        "reason": str(row["suggestion_reason"] or ""),
                    }
                )
            elif stype == "related_keyword":
                keywords.append(value)

        # embedding 召回的相关笔记（P9 启用后才有内容）
        related = self._embedding_related(item_id)

        return {
            "item": {
                "id": item_id,
                "title": item.get("title") or "",
                "source_url": item.get("source_url") or "",
                "platform": item.get("platform") or "",
                "platform_label": item.get("platform_label") or "",
                "status": item.get("status") or "",
                "status_label": item.get("status_label") or "",
                "inbox_path": item.get("inbox_path") or "",
                "asr": bool(item.get("asr")),
                "extractor": item.get("extractor") or "",
                "raw_length": item.get("raw_length") or 0,
                "created_at": item.get("created_at") or "",
            },
            "ai_summary": self._ai_summary(item),
            "ai_model": self._ai_model(),
            "suggested_tags": suggested_tags,
            "pending_tags": pending_tags,
            "suggested_partitions": suggested_partitions,
            "pending_partitions": pending_partitions,
            "keywords": keywords,
            "embedding_related": related,
            "vocab": self._vocab(),
        }

    def _ai_summary(self, item: dict[str, Any]) -> str:
        """AI 摘要正文（items.ai_summary，由 AI worker 写入）。"""
        return str(item.get("ai_summary") or "")

    def _ai_model(self) -> str:
        row = self.db.query_one(
            "SELECT ai_model FROM items WHERE ai_model IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"
        )
        return str(row["ai_model"]) if row else ""

    def _vocab(self) -> dict[str, list[str]]:
        partitions = [
            str(r["name"]) for r in self.db.query("SELECT name FROM partitions ORDER BY name")
        ]
        tags = [str(r["name"]) for r in self.db.query("SELECT name FROM tags ORDER BY name")]
        return {"partitions": partitions, "tags": tags}

    def _embedding_related(self, item_id: int) -> list[dict[str, Any]]:
        try:
            from app.services.embedding_service import get_embedding_service

            svc = get_embedding_service()
            if not svc.available():
                return []
            return svc.related_for_item(item_id, top_k=5)
        except Exception:
            return []

    # -- 确认入库 ---------------------------------------------------------
    def confirm(
        self,
        item_id: int,
        title: str | None = None,
        personal_summary: str = "",
        tags: list[str] | None = None,
        partitions: list[str] | None = None,
        confirmed_related: list[str] | None = None,
        ai_summary: str | None = None,
        summary_type: str = "bullets",
    ) -> dict[str, Any]:
        """逐条确认 → B 层 md 落盘 → 索引更新。

        允许的状态：
        - pending_review：AI 已完成，走正常核准；
        - ai_failed：AI 挂了，用户手写摘要直接入库（不必反复重试）；
        - extracted / extract_failed：内容已就绪但未跑 AI，也可直接手工核准。
        """
        item = self.ingest.get_item(item_id)
        if item is None:
            raise KeyError("条目不存在")

        current_status = str(item.get("status") or "")
        if current_status not in MANUAL_CONFIRMABLE:
            raise ValueError(
                f"当前状态 {current_status} 不能核准（可核准状态："
                f"{', '.join(sorted(MANUAL_CONFIRMABLE))}）"
            )

        final_title = (title or item.get("title") or "").strip()
        if not final_title:
            final_title = "未命名"

        manuscript = (personal_summary or "").strip()
        summary = (ai_summary or self._ai_summary(item) or "").strip()
        summary = summary or self._fallback_summary(item)

        # 标签：用户确认的 = AI 建议勾选 ∪ 手动新增；拒绝全部则为空数组
        final_tags = [normalize_tag(t) for t in (tags or [])]
        final_tags = [t for t in final_tags if t]
        final_partitions = [normalize_tag(p) for p in (partitions or [])]
        final_partitions = [p for p in final_partitions if p]

        related = [r for r in (confirmed_related or []) if r]

        # 词表 upsert（用户确认的新标签入受控词表，source=ai 或 manual）
        self._upsert_vocab(final_tags, final_partitions)

        # B 层 md 落盘
        ai_suggested_tags, ai_suggested_partitions, llm_keywords = self._ai_originals(
            item_id
        )
        path = self._write_processed(
            title=final_title,
            source_url=str(item.get("source_url") or ""),
            platform=str(item.get("platform") or ""),
            inbox_path=str(item.get("inbox_path") or ""),
            partitions=final_partitions,
            tags=final_tags,
            ai_summary=summary,
            personal_summary=manuscript,
            summary_type=summary_type,
            related=related,
            ai_suggested_tags=ai_suggested_tags,
            ai_suggested_partitions=ai_suggested_partitions,
            llm_keywords=llm_keywords,
        )

        # 索引 + 链接 + 建议状态
        self._index_processed(
            path,
            final_title,
            summary,
            manuscript,
            final_tags,
            related,
            item_id=item_id,
            partitions=final_partitions,
        )

        # 状态流转 → reviewed
        # 非 pending_review 的来源状态（ai_failed / extracted / extract_failed）
        # 不在状态机的合法迁移表里，这里直接落库（用户已显式确认）。
        if current_status == "pending_review":
            self.ingest.transition(item_id, "reviewed")
        else:
            self.ingest.update_item(item_id, status="reviewed")
        self.ingest.update_item(item_id, title=final_title)
        # B 层产物路径写回条目（之后能在工作区/详情里看到产物在哪）
        self.ingest.update_item(item_id, processed_path=path.as_posix())

        # 排 embedding job（P9）
        from app.workers.queue import get_queue

        get_queue().enqueue(item_id, "embed", priority=7)

        return {
            "ok": True,
            "path": path.as_posix(),
            "digested": bool(manuscript),
            "title": final_title,
            "tags": final_tags,
        }

    def _fallback_summary(self, item: dict[str, Any]) -> str:
        """AI 摘要缺失时的兜底（用原文前 500 字）。"""
        raw = str(item.get("raw_text") or "")
        if not raw:
            return ""
        return raw[:500]

    def _upsert_vocab(self, tags: list[str], partitions: list[str]) -> None:
        if not tags and not partitions:
            return

        def _run(conn: sqlite3.Connection) -> None:
            for name in tags:
                conn.execute(
                    "INSERT INTO tags(name, source) VALUES(?, 'ai') "
                    "ON CONFLICT(name) DO NOTHING",
                    (name,),
                )
        # 分区是树：走 partition_service 按路径去重（同名可存在于不同父级）
        from app.services.partition_service import get_partition_service

        psvc = get_partition_service()
        for name in partitions:
            try:
                psvc.ensure_by_name(name)
            except ValueError:
                continue

        self.db.write(_run)

    def _ai_originals(self, item_id: int) -> tuple[list[str], list[str], list[str]]:
        """取回 AI 的原始建议（供 frontmatter 追溯，永不被用户勾选结果覆盖）。"""
        rows = self.db.query(
            "SELECT suggestion_type, suggestion_value FROM ai_suggestions "
            "WHERE note_path = ? ORDER BY id",
            (f"item:{item_id}",),
        )
        tags: list[str] = []
        partitions: list[str] = []
        keywords: list[str] = []
        for row in rows:
            value = str(row["suggestion_value"] or "")
            if not value:
                continue
            stype = row["suggestion_type"]
            if stype == "tag":
                tags.append(value)
            elif stype == "new_tag":
                tags.append(value)
            elif stype == "partition":
                partitions.append(value)
            elif stype == "related_keyword":
                keywords.append(value)
        return tags, partitions, keywords

    def _write_processed(
        self,
        title: str,
        source_url: str,
        platform: str,
        inbox_path: str,
        partitions: list[str],
        tags: list[str],
        ai_summary: str,
        personal_summary: str,
        summary_type: str,
        related: list[str],
        ai_suggested_tags: list[str] | None = None,
        ai_suggested_partitions: list[str] | None = None,
        llm_keywords: list[str] | None = None,
    ) -> Path:
        """写 B 层 md（§4.3 frontmatter）。"""
        self.cfg.vault.ensure()
        date_str = vault_mod.datetime.now().strftime("%Y-%m-%d")
        stem = vault_mod.sanitize_filename(f"{date_str}-{title}")
        path = vault_mod.unique_path(self.cfg.vault.dir("processed"), stem, ".md")

        # A 层引用（回查原文）
        source_inbox = ""
        if inbox_path:
            try:
                rel = Path(inbox_path).relative_to(self.cfg.vault.path).as_posix()
                source_inbox = f"[[{rel[:-3]}]]" if rel.endswith(".md") else f"[[{rel}]]"
            except (ValueError, OSError):
                source_inbox = f"[[{Path(inbox_path).stem}]]"

        frontmatter: dict[str, Any] = {
            "title": title,
            "type": "processed",
            "source_inbox": source_inbox,
            "source_url": source_url,
            "platform": platform,
            "partitions": partitions,
            "tags": tags,
            "ai_summary": ai_summary,
            "personal_summary": personal_summary,
            "ai_model": self._ai_model(),
            "ai_suggested_tags": ai_suggested_tags or [],
            "ai_suggested_partitions": ai_suggested_partitions or [],
            "embedding_related": related,
            "llm_related_keywords": llm_keywords or [],
            "confirmed_related": related,
            "digested": bool(personal_summary.strip()),
            "created": vault_mod.now_iso(),
            "reviewed_at": vault_mod.now_iso(),
        }
        body = f"回查原文：{source_inbox}\n" if source_inbox else ""
        return vault_mod.write_note(path, frontmatter, body)

    def _index_processed(
        self,
        path: Path,
        title: str,
        summary: str,
        manuscript: str,
        tags: list[str],
        related: list[str],
        item_id: int | None = None,
        partitions: list[str] | None = None,
    ) -> None:
        """写 notes 表 / FTS / links / suggestions 状态。"""
        from app.core import fts as fts_mod

        tags_text = " ".join(tags)
        columns = fts_mod.build_columns(
            title=title,
            summary=summary,
            manuscript=manuscript,
            body="",
            tags=tags_text,
        )
        snippet_src = fts_mod.build_snippet_src(
            title=title, summary=summary, manuscript=manuscript, tags=tags_text
        )
        rel_path = path.as_posix()
        digested = 1 if manuscript.strip() else 0

        # 解析 related 中的 [[...]] → 目标笔记路径
        targets: list[str] = []
        for rel in related:
            target = self._resolve_link(rel)
            if target:
                targets.append(target)

        def _run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO notes(note_path, level, title, form, "
                "personal_summary_empty, digested, archived, created_at, reviewed_at) "
                "VALUES(?, 'processed', ?, '', ?, ?, 0, ?, ?) "
                "ON CONFLICT(note_path) DO UPDATE SET title=excluded.title, "
                "personal_summary_empty=excluded.personal_summary_empty, "
                "digested=excluded.digested, reviewed_at=excluded.reviewed_at",
                (
                    rel_path,
                    title,
                    0 if digested else 1,
                    digested,
                    vault_mod.now_iso(),
                    vault_mod.now_iso(),
                ),
            )
            conn.execute(
                "DELETE FROM notes_fts WHERE note_path = ?", (rel_path,)
            )
            conn.execute(
                "INSERT INTO notes_fts(title, summary, manuscript, body, tags, level, "
                "note_path, snippet_src) VALUES(?,?,?,?,?,?,?,?)",
                (
                    columns["title"],
                    columns["summary"],
                    columns["manuscript"],
                    columns["body"],
                    columns["tags"],
                    "processed",
                    rel_path,
                    snippet_src,
                ),
            )
            # note_tags
            conn.execute("DELETE FROM note_tags WHERE note_id IN "
                         "(SELECT id FROM notes WHERE note_path = ?)", (rel_path,))
            row = conn.execute(
                "SELECT id FROM notes WHERE note_path = ?", (rel_path,)
            ).fetchone()
            if row is not None:
                for tag in tags:
                    trow = conn.execute(
                        "SELECT id FROM tags WHERE name = ?", (tag,)
                    ).fetchone()
                    if trow is not None:
                        conn.execute(
                            "INSERT OR IGNORE INTO note_tags(note_id, tag_id) VALUES(?,?)",
                            (row["id"], trow["id"]),
                        )
                # note_partitions —— 漏写会让「按分区搜索」永远命中 0 条
                conn.execute(
                    "DELETE FROM note_partitions WHERE note_id = ?", (row["id"],)
                )
                for part in (partitions or []):
                    prow = conn.execute(
                        "SELECT id FROM partitions WHERE name = ?", (part,)
                    ).fetchone()
                    if prow is not None:
                        conn.execute(
                            "INSERT OR IGNORE INTO note_partitions(note_id, partition_id) "
                            "VALUES(?,?)",
                            (row["id"], prow["id"]),
                        )
            # confirmed_related → links
            if row is not None:
                conn.execute(
                    "DELETE FROM links WHERE src_note_id = ? AND kind = 'confirmed'",
                    (row["id"],),
                )
                for target in targets:
                    trow = conn.execute(
                        "SELECT id FROM notes WHERE note_path = ?", (target,)
                    ).fetchone()
                    if trow is not None:
                        conn.execute(
                            "INSERT OR IGNORE INTO links(src_note_id, dst_note_id, kind) "
                            "VALUES(?,?,'confirmed')",
                            (row["id"], trow["id"]),
                        )

            # 建议状态落库：只标记用户实际确认的为 accepted，其余为 rejected
            conn.execute(
                "UPDATE ai_suggestions SET status = 'rejected' WHERE note_path = ? "
                "AND suggestion_type IN ('tag','partition')",
                (f"item:{item_id}",),
            )
            for name in tags:
                conn.execute(
                    "UPDATE ai_suggestions SET status = 'accepted' WHERE note_path = ? "
                    "AND suggestion_type IN ('tag','new_tag') AND suggestion_value = ?",
                    (f"item:{item_id}", name),
                )
            for name in partitions:
                conn.execute(
                    "UPDATE ai_suggestions SET status = 'accepted' WHERE note_path = ? "
                    "AND suggestion_type = 'partition' AND suggestion_value = ?",
                    (f"item:{item_id}", name),
                )

        self.db.write(_run)

    def _resolve_link(self, raw: str) -> str:
        """把 [[目标]] 或路径解析成 vault 内绝对路径（找不到则跳过）。"""
        target = raw.strip()
        if target.startswith("[[") and target.endswith("]]"):
            target = target[2:-2]
        if not target:
            return ""
        from app.services.link_resolver import LinkResolver

        return LinkResolver(self.cfg.vault.path).build().resolve(target)

    # -- 待核准列表 -------------------------------------------------------
    def list_pending(self, limit: int = 50) -> list[dict[str, Any]]:
        """待核准队列（AI 已完成）。

        另见 list_manual——AI 挂了时用户仍需能手工核准那些条目。
        """
        rows = self.db.query(
            "SELECT id, title, platform, source_url, created_at, status, inbox_path, asr "
            "FROM items WHERE status = 'pending_review' ORDER BY id ASC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def list_manual(self, limit: int = 50) -> list[dict[str, Any]]:
        """需要人工介入的条目（AI 失败 / 内容就绪未跑 AI）。

        这些条目不阻塞在"反复重试 AI"上——用户可以直接手写摘要并入库。

        **不含 pending_review**：那批已经在「待核准」队列里了，
        再放一遍会让同一条目在界面出现两次，剩余条数也会算重。
        """
        placeholders = ",".join("?" for _ in MANUAL_CONFIRMABLE)
        rows = self.db.query(
            f"SELECT id, title, platform, source_url, created_at, status, inbox_path, "
            f"asr, error_detail FROM items WHERE status IN ({placeholders}) "
            f"AND status != 'pending_review' "
            f"ORDER BY id ASC LIMIT ?",
            [*sorted(MANUAL_CONFIRMABLE), limit],
        )
        return [dict(r) for r in rows]

    def list_ai_failed(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT id, title, platform, source_url, created_at, status, error_detail "
            "FROM items WHERE status = 'ai_failed' ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def skip(self, item_id: int) -> dict[str, Any]:
        """放弃核准：退回 pending_review 或标记跳过（§6.3）。"""
        item = self.ingest.get_item(item_id)
        if item is None:
            raise KeyError("条目不存在")
        return {"ok": True, "item": item}

    def list_digest_pending(self, limit: int = 100) -> list[dict[str, Any]]:
        """待补手稿清单（digested 软门槛）。"""
        rows = self.db.query(
            "SELECT note_path, title, created_at FROM notes "
            "WHERE level='processed' AND personal_summary_empty=1 AND archived=0 "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]


_review: ReviewService | None = None


def get_review_service() -> ReviewService:
    global _review
    if _review is None:
        _review = ReviewService()
    return _review
