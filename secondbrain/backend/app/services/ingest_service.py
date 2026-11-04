"""A 层采集服务：解析 → 去重 → 入队 → 落盘。

对齐 v0.5.0 §5.1 / §5.4：
    批量粘贴 → 智能解析 → 预览 → 精确去重 + 向量软提示 → 生成 extract job
    → 后台处理（抓取/转写/AI）→ 状态机流转 → 隔天集中核准

状态机（§5.4）：
    queued → extracting ─┬→ extracted → ai_processing ─┬→ pending_review → reviewed
                         └→ extract_failed（手动粘贴→extracted）  └→ ai_failed
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.core import vault as vault_mod
from app.core.config import get_config
from app.core.db import get_db
from app.core.parser import (
    PLATFORM_LABELS,
    ParsedLink,
    canonical_source_url,
    detect_platform,
    parse_links,
)

# 状态机合法迁移
VALID_TRANSITIONS: dict[str, set[str]] = {
    # queued 也允许直达 extracted（用户手动提供内容时）
    "queued": {"extracting", "extract_failed", "extracted", "cancelled"},
    "extracting": {"extracted", "extract_failed"},
    # v31：采集完成后**不再自动排 AI job**（AI 加工是加工区的活，
    # 要模板/字数档位/用户确认）。原文就绪 → 待用户触发 AI。
    "extracted": {"ai_processing", "ai_failed", "raw_ready"},
    "raw_ready": {
        "ai_processing",
        "pending_review",
        "ai_failed",
        "extracting",
        # 重试路径：raw_ready → extracted → ai_processing
        # （retry 接口先回 extracted 再排 ai job）
        "extracted",
    },
    "extract_failed": {"extracted", "queued", "cancelled"},
    "ai_processing": {"pending_review", "ai_failed"},
    "ai_failed": {"ai_processing", "queued"},
    "pending_review": {
        "reviewed",
        "pending_review",
        # v31：ASR 失败过的条目点「重试」要能回队列重跑抓取+转写
        # （正文只有一句简介时，pending_review 的摘要是没意义的）。
        "queued",
    },
    "reviewed": {"archived"},
    "archived": {"pending_review"},
}

STATUS_LABELS: dict[str, str] = {
    "queued": "排队中",
    "extracting": "抓取中",
    "extracted": "已抓取",
    "raw_ready": "原文就绪",
    "extract_failed": "抓取失败",
    "ai_processing": "AI 处理中",
    "ai_failed": "AI 失败",
    "pending_review": "待核准",
    "reviewed": "已入库",
    "archived": "已归档",
    "cancelled": "已取消",
}


@dataclass
class SubmitResult:
    """一次批量提交的结果汇总。"""

    accepted: list[dict[str, Any]] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    similar: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    batch_limit: int = 100

    def summary(self) -> str:
        return (
            f"本轮 {len(self.accepted) + len(self.duplicates) + len(self.rejected)} 条："
            f"成功 {len(self.accepted)}、重复 {len(self.duplicates)}、"
            f"拒绝 {len(self.rejected)}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "similar": self.similar,
            "rejected": self.rejected,
            "truncated": self.truncated,
            "batch_limit": self.batch_limit,
            "summary": self.summary(),
        }


class IngestService:
    """采集入队服务。"""

    def __init__(self) -> None:
        self.cfg = get_config()
        self.db = get_db()

    # -- 解析 -------------------------------------------------------------
    def parse(self, text: str) -> list[ParsedLink]:
        return parse_links(text)

    def preview(self, text: str) -> dict[str, Any]:
        """采集页预览：解析 + 标记已存在/相似。"""
        links = self.parse(text)
        existing = self._existing_map()
        items: list[dict[str, Any]] = []
        for link in links:
            canonical = canonical_source_url(link.url, link.platform)
            info = link.to_dict()
            info["canonical_url"] = canonical
            info["existing"] = existing.get(canonical.lower())
            info["existing_id"] = existing.get(canonical.lower(), {}).get("id")
            items.append(info)
        return {
            "items": items,
            "count": len(items),
            "batch_limit": self.cfg.collect.batch_limit,
            "over_limit": len(items) > self.cfg.collect.batch_limit,
        }

    def _existing_map(self) -> dict[str, dict[str, Any]]:
        rows = self.db.query(
            "SELECT id, source_url, title, status, platform FROM items"
        )
        return {
            str(r["source_url"]).lower(): {
                "id": r["id"],
                "title": r["title"],
                "status": r["status"],
                "platform": r["platform"],
            }
            for r in rows
            if r["source_url"]
        }

    # -- 提交 -------------------------------------------------------------
    def _expand_if_needed(self, url: str, platform: str) -> str:
        """短链展开（b23.tv / v.douyin.com 等）。

        短链不展开则无法得到规范化 source_url，精确去重会失效，
        因此在提交前展开。展开失败时保留原链接（后续抓取阶段会再试）。
        """
        from app.core.parser import is_short_link

        if not is_short_link(url):
            return url
        try:
            from app.adapters import get_adapter

            adapter = get_adapter(platform, timeout=self.cfg.collect.web_timeout)
            if adapter is not None:
                return adapter.expand(url) or url
        except Exception:
            pass
        return url

    def submit(
        self,
        entries: list[dict[str, Any]],
        force_similar: bool = False,
    ) -> SubmitResult:
        """提交批量采集。

        entries 每项：{url, platform?, title?, force?}
        精确去重（UNIQUE 冲突）→ 已存在则拒绝并提示；
        向量相似度 > 0.85 → 软提示（不阻断，除非 force）。
        """
        result = SubmitResult(batch_limit=self.cfg.collect.batch_limit)
        limit = self.cfg.collect.batch_limit

        for entry in entries[:limit]:
            url = str(entry.get("url") or "").strip()
            if not url:
                result.rejected.append(
                    {"url": url, "reason": "empty_url", "message": "URL 为空"}
                )
                continue

            platform = str(entry.get("platform") or detect_platform(url))
            url = self._expand_if_needed(url, platform)
            canonical = canonical_source_url(url, platform)
            title = str(entry.get("title") or "").strip()

            # 精确去重
            row = self.db.query_one(
                "SELECT id, title, status FROM items WHERE source_url = ?",
                (canonical,),
            )
            if row is not None:
                result.duplicates.append(
                    {
                        "url": canonical,
                        "id": row["id"],
                        "title": row["title"],
                        "status": row["status"],
                        "reason": "exact_duplicate",
                        "message": f"已存在（{STATUS_LABELS.get(row['status'], row['status'])}）",
                    }
                )
                continue

            # 向量软提示
            similar = self._similar_candidates(canonical, title)
            if similar and not (force_similar or entry.get("force")):
                result.similar.append(
                    {
                        "url": canonical,
                        "platform": platform,
                        "title": title,
                        "candidates": similar,
                        "message": "已有一条很像的，可强制入库",
                    }
                )
                continue

            item_id = self._insert_item(canonical, platform, title)
            if item_id is None:
                result.rejected.append(
                    {
                        "url": canonical,
                        "reason": "duplicate_conflict",
                        "message": "并发写入冲突，URL 已存在",
                    }
                )
                continue

            self._enqueue(item_id, "extract", priority=5)
            result.accepted.append(
                {
                    "id": item_id,
                    "url": canonical,
                    "platform": platform,
                    "platform_label": PLATFORM_LABELS.get(platform, platform),
                    "title": title,
                    "status": "queued",
                }
            )

        if len(entries) > limit:
            result.truncated = True
            for entry in entries[limit:]:
                result.rejected.append(
                    {
                        "url": str(entry.get("url") or ""),
                        "reason": "batch_limit",
                        "message": f"单次提交上限 {limit} 条，超出部分未入队",
                    }
                )
        return result

    def _similar_candidates(self, url: str, title: str) -> list[dict[str, Any]]:
        """向量软提示：相似度 > 阈值则提示（embedding 不可用时静默跳过）。"""
        try:
            from app.services.embedding_service import get_embedding_service

            svc = get_embedding_service()
            if not svc.available():
                return []
            hits = svc.similar_to_incoming(url, title, top_k=3)
            threshold = self.cfg.duplicate.vector_threshold
            return [h for h in hits if h.get("score", 0) > threshold]
        except Exception:
            return []

    def _insert_item(self, url: str, platform: str, title: str) -> int | None:
        def _run(conn: sqlite3.Connection) -> int | None:
            try:
                cur = conn.execute(
                    "INSERT INTO items(source_url, platform, title, status, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (url, platform, title or None, "queued", vault_mod.now_iso()),
                )
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

        return self.db.write(_run)

    # -- 队列 -------------------------------------------------------------
    def _enqueue(self, item_id: int, kind: str, priority: int = 5) -> int:
        def _run(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO jobs(item_id, kind, state, priority, created_at, updated_at) "
                "VALUES(?,?,'pending',?,?,?)",
                (item_id, kind, priority, vault_mod.now_iso(), vault_mod.now_iso()),
            )
            return int(cur.lastrowid)

        return self.db.write(_run)

    # -- 状态流转 ---------------------------------------------------------
    def transition(
        self,
        item_id: int,
        to_status: str,
        error_reason: str | None = None,
        error_detail: str | None = None,
    ) -> bool:
        """状态机流转（非法迁移拒绝）。"""

        def _run(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT status FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                return False
            current = row["status"]
            if current == to_status:
                return True
            if to_status not in VALID_TRANSITIONS.get(current, set()):
                return False
            conn.execute(
                "UPDATE items SET status = ?, error_reason = ?, error_detail = ?, "
                "processed_at = ? WHERE id = ?",
                (
                    to_status,
                    error_reason,
                    error_detail,
                    vault_mod.now_iso(),
                    item_id,
                ),
            )
            return True

        return self.db.write(_run)

    def update_item(
        self,
        item_id: int,
        title: str | None = None,
        raw_text: str | None = None,
        extractor: str | None = None,
        asr: bool | None = None,
        inbox_path: str | None = None,
        processed_path: str | None = None,
        status: str | None = None,
        error_reason: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if raw_text is not None:
            sets.append("raw_text = ?")
            params.append(raw_text)
        if extractor is not None:
            sets.append("extractor = ?")
            params.append(extractor)
        if asr is not None:
            sets.append("asr = ?")
            params.append(1 if asr else 0)
        if inbox_path is not None:
            sets.append("inbox_path = ?")
            params.append(inbox_path)
        if processed_path is not None:
            sets.append("processed_path = ?")
            params.append(processed_path)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if error_reason is not None:
            sets.append("error_reason = ?")
            params.append(error_reason)
        if error_detail is not None:
            sets.append("error_detail = ?")
            params.append(error_detail)
        if not sets:
            return
        params.append(item_id)

        def _run(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"UPDATE items SET {', '.join(sets)} WHERE id = ?", params
            )

        self.db.write(_run)

    # -- 手动补充（extract_failed 兜底）----------------------------------
    def manual_fill(self, item_id: int, text: str, title: str | None = None) -> bool:
        """行内手动粘贴 → 直接落 A 层，extractor=manual，不再重试。"""
        text = (text or "").strip()
        if not text:
            return False

        row = self.db.query_one(
            "SELECT source_url, platform, title FROM items WHERE id = ?", (item_id,)
        )
        if row is None:
            return False

        title = title or row["title"] or f"手动补充-{item_id}"

        # 已有 A 层文件就**更新它**，别再新建一个
        # （否则同一条目会出现两个文件：一个旧的空壳 + 一个新的正文）
        path = self._update_inbox_body(item_id, title, text)
        if path is None:
            path = self._write_inbox(
                url=row["source_url"] or "",
                platform=row["platform"] or "web",
                title=title,
                text=text,
                extractor="manual",
                asr=False,
            )
        self.update_item(
            item_id,
            title=title,
            raw_text=text,
            extractor="manual",
            asr=False,
            inbox_path=path.as_posix(),
        )
        self.transition(item_id, "extracted")
        self._enqueue(item_id, "ai", priority=5)
        return True

    # -- A 层落盘 ---------------------------------------------------------
    def _update_inbox_body(self, item_id: int, title: str, text: str) -> Path | None:
        """把已有 A 层 md 的正文替换为新内容；没有可用文件时返回 None。"""
        row = self.db.query_one(
            "SELECT inbox_path FROM items WHERE id = ?", (item_id,)
        )
        if row is None or not row["inbox_path"]:
            return None
        path = Path(str(row["inbox_path"]))
        if not path.exists():
            return None
        note = vault_mod.read_note(path)
        if note is None:
            return None
        fm = dict(note.frontmatter)
        fm["extractor"] = "manual"
        fm["status"] = "extracted"
        body = note.body
        marker = "## 字幕/正文全文"
        head = f"# 原始内容：{title}"
        if marker in body:
            _, _, tail = body.partition(marker)
            _, _, source_part = tail.partition("## 来源")
            body = f"{head}\n\n{marker}\n{text}\n\n## 来源{source_part}"
        else:
            body = f"{head}\n\n{marker}\n{text}\n"
        vault_mod.write_note(path, fm, body)
        return path

    def _write_inbox(
        self,
        url: str,
        platform: str,
        title: str,
        text: str,
        extractor: str,
        asr: bool,
        screenshots: list[str] | None = None,
    ) -> Path:
        """写 A 层 md（§4.2 frontmatter）。"""
        cfg = get_config()
        cfg.vault.ensure()
        date_str = datetime.now().strftime("%Y-%m-%d")
        stem = vault_mod.sanitize_filename(f"{date_str}-{title or '未命名'}")
        path = vault_mod.unique_path(cfg.vault.dir("inbox"), stem, ".md")

        frontmatter: dict[str, Any] = {
            "type": "inbox",
            "source_url": url,
            "platform": platform,
            "captured_at": vault_mod.now_iso(),
            "extractor": extractor,
            "asr": bool(asr),
            "status": "extracted",
            "screenshots": screenshots or [],
        }
        body = (
            f"# 原始内容：{title}\n\n"
            f"## 字幕/正文全文\n{text}\n\n"
            f"## 来源\n{url}\n"
        )
        return vault_mod.write_note(path, frontmatter, body)

    def write_inbox(
        self,
        url: str,
        platform: str,
        title: str,
        text: str,
        extractor: str,
        asr: bool,
        screenshots: list[str] | None = None,
    ) -> Path:
        """公开版本：供适配器调用。"""
        return self._write_inbox(url, platform, title, text, extractor, asr, screenshots)

    # -- 配图 -------------------------------------------------------------
    def insert_screenshot(
        self,
        item_id: int,
        position: int,
        image_ref: str,
        caption: str = "",
    ) -> dict[str, Any]:
        """在正文指定位置插入一张配图。

        position = 段落序号（0 表示第一段之前；-1 / 超界则追加到末尾）。
        插完把图片登记到 A 层 md 的 frontmatter.screenshots 里。
        """
        image_ref = (image_ref or "").strip()
        if not image_ref:
            raise ValueError("图片地址不能为空")

        item = self.get_item(item_id)
        if item is None:
            raise KeyError("条目不存在")
        inbox_path = str(item.get("inbox_path") or "")
        if not inbox_path:
            raise ValueError("该条目没有 A 层原文文件，无法插入配图")

        path = Path(inbox_path)
        note = vault_mod.read_note(path)
        if note is None:
            raise ValueError("A 层原文读取失败")

        body = note.body
        marker = "## 字幕/正文全文"
        head, _, tail = body.partition(marker)
        content, _, source_part = tail.partition("## 来源")

        paragraphs = content.split("\n\n")
        # 去掉首尾空段，落点更符合直觉
        while paragraphs and not paragraphs[0].strip():
            paragraphs.pop(0)
        while paragraphs and not paragraphs[-1].strip():
            paragraphs.pop()

        caption_text = (caption or "").strip()
        img_md = f"![{caption_text}]({image_ref})" if caption_text else f"![]({image_ref})"

        idx = int(position)
        if idx < 0 or idx > len(paragraphs):
            idx = len(paragraphs)
        paragraphs.insert(idx, img_md)

        new_content = "\n\n".join(paragraphs)
        new_body = f"{head}{marker}\n{new_content}\n\n## 来源{source_part}"

        fm = dict(note.frontmatter)
        shots = fm.get("screenshots") or []
        if not isinstance(shots, list):
            shots = []
        # frontmatter 的 YAML 只支持标量列表（嵌套对象会被写成字符串），
        # 所以配图登记成 "说明｜图片地址" 这样的字符串
        shots.append(f"{caption_text}｜{image_ref}" if caption_text else image_ref)
        fm["screenshots"] = shots
        vault_mod.write_note(path, fm, new_body)

        return {
            "ok": True,
            "item_id": item_id,
            "position": idx,
            "paragraphs": len(paragraphs),
            "screenshots": shots,
            "path": path.as_posix(),
        }

    def remove_screenshot(self, item_id: int, index: int) -> dict[str, Any]:
        """按 frontmatter.screenshots 的序号移除一张配图（含正文里的图片行）。"""
        item = self.get_item(item_id)
        if item is None:
            raise KeyError("条目不存在")
        inbox_path = str(item.get("inbox_path") or "")
        path = Path(inbox_path)
        note = vault_mod.read_note(path)
        if note is None:
            raise ValueError("A 层原文读取失败")

        fm = dict(note.frontmatter)
        shots = list(fm.get("screenshots") or [])
        if index < 0 or index >= len(shots):
            raise ValueError("配图序号越界")
        removed = shots.pop(index)

        body = note.body
        marker = "## 字幕/正文全文"
        head, _, tail = body.partition(marker)
        content, _, source_part = tail.partition("## 来源")
        ref = removed.split("｜")[-1] if isinstance(removed, str) else str(removed)
        kept = [
            p
            for p in content.split("\n\n")
            if not (p.strip().startswith("![") and ref and ref in p)
        ]
        new_body = f"{head}{marker}\n" + "\n\n".join(kept) + f"\n\n## 来源{source_part}"
        fm["screenshots"] = shots
        vault_mod.write_note(path, fm, new_body)
        return {"ok": True, "removed": removed, "screenshots": shots}

    # -- 从 A 层重建条目（md 是真相源）----------------------------------
    def rebuild_items_from_inbox(self) -> dict[str, Any]:
        """扫描 A 层 md，为丢失的条目补建记录。

        md 是唯一真相源：DB 被清空后，靠这里的扫描把"原文 → 条目"这条链
        重新接上（不重抓、不重转写）。
        """
        cfg = get_config()
        inbox = cfg.vault.dir("inbox")
        if not inbox.exists():
            return {"created": 0, "skipped": 0}

        existing = {
            str(r["source_url"]): int(r["id"])
            for r in self.db.query(
                "SELECT id, source_url FROM items WHERE source_url IS NOT NULL AND source_url != ''"
            )
        }

        created = 0
        skipped = 0
        for path in sorted(inbox.glob("*.md")):
            note = vault_mod.read_note(path)
            if note is None:
                continue
            fm = note.frontmatter
            url = str(fm.get("source_url") or "")
            platform = str(fm.get("platform") or "web")
            title = str(fm.get("title") or "").strip()
            if not title:
                # 文件名形如 2026-11-02-标题 → 去掉日期前缀
                title = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", path.stem).strip() or path.stem
            body = note.body
            if "## 字幕/正文全文" in body:
                body = body.split("## 字幕/正文全文", 1)[1]
                body = body.split("## 来源")[0].strip()
            if url and url in existing:
                skipped += 1
                continue

            def _run(conn, url=url, platform=platform, title=title, body=body,
                     extractor=str(fm.get("extractor") or "manual"),
                     asr=int(bool(fm.get("asr"))),
                     inbox_path=path.as_posix()) -> None:
                conn.execute(
                    "INSERT OR IGNORE INTO items(source_url, platform, raw_text, title, "
                    "status, extractor, asr, inbox_path) VALUES(?,?,?,?,?,?,?,?)",
                    (url or None, platform, body, title, "extracted", extractor, asr, inbox_path),
                )

            self.db.write(_run)
            created += 1
        return {"created": created, "skipped": skipped}

    # -- 速记 -------------------------------------------------------------
    def quick_note(self, text: str, title: str | None = None) -> dict[str, Any]:
        """页面速记入口：纯文本直接入 A 层（extracted），并排队 AI。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("速记内容不能为空")
        title = (title or text[:40]).strip()
        path = self._write_inbox(
            url="",
            platform="quick_note",
            title=title,
            text=text,
            extractor="manual",
            asr=False,
        )

        def _run(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO items(source_url, platform, raw_text, title, status, "
                "extractor, asr, inbox_path, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"quicknote:{vault_mod.now_iso()}",
                    "quick_note",
                    text,
                    title,
                    "extracted",
                    "manual",
                    0,
                    path.as_posix(),
                    vault_mod.now_iso(),
                ),
            )
            return int(cur.lastrowid)

        item_id = self.db.write(_run)
        self._enqueue(item_id, "ai", priority=6)
        return {"id": item_id, "path": path.as_posix(), "title": title}

    # -- 查询 -------------------------------------------------------------
    def list_items(
        self,
        status: str | None = None,
        platform: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if platform:
            where.append("platform = ?")
            params.append(platform)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.extend([limit, offset])
        rows = self.db.query(
            f"SELECT * FROM items {clause} ORDER BY id DESC LIMIT ? OFFSET ?", params
        )
        return [self._item_dict(r) for r in rows]

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM items WHERE id = ?", (item_id,))
        return self._item_dict(row) if row else None

    def _item_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["status_label"] = STATUS_LABELS.get(row["status"], row["status"])
        data["platform_label"] = PLATFORM_LABELS.get(
            row["platform"] or "", row["platform"] or ""
        )
        raw = row["raw_text"] or ""
        data["raw_preview"] = raw[:200]
        data["raw_length"] = len(raw)
        return data
    def stats(self) -> dict[str, Any]:
        """队列统计（供回顾卡片 / 队列页）。"""
        rows = self.db.query("SELECT status, COUNT(*) c FROM items GROUP BY status")
        by_status = {r["status"]: r["c"] for r in rows}
        total = sum(by_status.values())
        pending_review = by_status.get("pending_review", 0)

        # 待补手稿（B 层已入库但 personal_summary 空）
        need_manuscript = self.db.scalar(
            "SELECT COUNT(*) FROM notes WHERE level='processed' AND personal_summary_empty=1"
        ) or 0

        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        today_count = self.db.scalar(
            "SELECT COUNT(*) FROM items WHERE date(created_at) = ?", (today,)
        ) or 0
        yesterday_count = self.db.scalar(
            "SELECT COUNT(*) FROM items WHERE date(created_at) = ?", (yesterday,)
        ) or 0

        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        week_count = self.db.scalar(
            "SELECT COUNT(*) FROM items WHERE date(created_at) >= ?", (week_ago,)
        ) or 0

        return {
            "total": total,
            "by_status": by_status,
            "pending_review": pending_review,
            "need_manuscript": need_manuscript,
            "today": today_count,
            "yesterday": yesterday_count,
            "week": week_count,
        }


_ingest: IngestService | None = None


def get_ingest_service() -> IngestService:
    global _ingest
    if _ingest is None:
        _ingest = IngestService()
    return _ingest
