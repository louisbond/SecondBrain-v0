"""存量批量补 AI 管线（P5）。

对齐 v0.5.0 §8.1：
「可选：存量批量补 AI（分批 10 条/批，限速，预估 token 消耗并确认）」
「断点续跑：进度持久化于 jobs；失败清单单独列出可重跑」
「存量补 AI 产生的建议同样走核准页（不静默写入）」

关键约束：
- **不静默写入**——补 AI 只产出 ai_suggestions，条目进入 pending_review，
  必须经用户在核准页确认才写入 B 层；
- 断点续跑：进度记在 meta 表（batch_ai_progress），中断后重跑不重复处理；
- 限速：批间 sleep，避免打爆 AI 端点。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core import vault as vault_mod
from app.core.config import get_config
from app.core.db import get_db

BATCH_SIZE = 10
PROGRESS_KEY = "batch_ai_progress"


@dataclass
class BackfillProgress:
    """批量补 AI 进度（持久化在 meta 表）。"""

    running: bool = False
    total: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    current_batch: int = 0
    total_batches: int = 0
    failed_paths: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "total": self.total,
            "processed": self.processed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "current_batch": self.current_batch,
            "total_batches": self.total_batches,
            "failed_paths": self.failed_paths[:100],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "percent": (
                round(self.processed / self.total * 100, 1) if self.total else 0.0
            ),
        }


class BackfillService:
    """存量批量补 AI。"""

    def __init__(self) -> None:
        self.cfg = get_config()
        self.db = get_db()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- 进度存取 ---------------------------------------------------------
    def _load_progress(self) -> BackfillProgress:
        row = self.db.query_one(
            "SELECT value FROM meta WHERE key = ?", (PROGRESS_KEY,)
        )
        if row is None:
            return BackfillProgress()
        import json

        try:
            data = json.loads(row["value"])
        except json.JSONDecodeError:
            return BackfillProgress()
        return BackfillProgress(
            running=bool(data.get("running")),
            total=int(data.get("total", 0)),
            processed=int(data.get("processed", 0)),
            succeeded=int(data.get("succeeded", 0)),
            failed=int(data.get("failed", 0)),
            current_batch=int(data.get("current_batch", 0)),
            total_batches=int(data.get("total_batches", 0)),
            failed_paths=list(data.get("failed_paths", [])),
            started_at=str(data.get("started_at", "")),
            finished_at=str(data.get("finished_at", "")),
        )

    def _save_progress(self, progress: BackfillProgress) -> None:
        import json

        self.db.write(
            lambda conn: conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (PROGRESS_KEY, json.dumps(progress.to_dict(), ensure_ascii=False)),
            )
        )

    def progress(self) -> dict[str, Any]:
        return self._load_progress().to_dict()

    # -- 候选扫描 ---------------------------------------------------------
    def candidates(self, limit: int = 5000) -> list[dict[str, Any]]:
        """找出尚未补 AI 的存量笔记。

        判据：notes 表已索引，但对应 md 里没有 ai_summary 字段。
        """
        rows = self.db.query(
            "SELECT note_path, title, level FROM notes WHERE level IN ('processed','note') "
            "LIMIT ?",
            (limit,),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            path = Path(str(row["note_path"]))
            note = vault_mod.read_note(path)
            if note is None:
                continue
            # 已有 AI 摘要 → 跳过（断点续跑的依据）
            if note.frontmatter.get("ai_summary"):
                continue
            text = note.body.strip()
            if len(text) < self.cfg.collect.min_chars:
                continue
            out.append(
                {
                    "note_path": str(row["note_path"]),
                    "title": note.title,
                    "level": row["level"],
                    "length": len(text),
                }
            )
        return out

    def estimate(self) -> dict[str, Any]:
        """预估 token 消耗（供用户确认）。"""
        candidates = self.candidates()
        total_chars = sum(c["length"] for c in candidates)
        # 粗略估算：中文约 1.5 字符/token，输入 + 输出约 1:1
        estimated_tokens = int(total_chars / 1.5 * 2)
        return {
            "count": len(candidates),
            "total_chars": total_chars,
            "estimated_tokens": estimated_tokens,
            "batches": (len(candidates) + BATCH_SIZE - 1) // BATCH_SIZE,
            "note": "估算基于中文 1.5 字符/token、输入输出约 1:1，仅供量级参考",
        }

    # -- 执行 -------------------------------------------------------------
    def start(self, force: bool = False) -> dict[str, Any]:
        """启动批量补 AI（后台线程）。"""
        with self._lock:
            current = self._load_progress()
            if current.running:
                return {"ok": False, "message": "批量补 AI 正在运行中"}
            if not force and current.processed > 0 and current.processed < current.total:
                return {
                    "ok": False,
                    "message": (
                        f"上次运行中断（{current.processed}/{current.total}）。"
                        "继续运行将跳过已处理部分。"
                    ),
                    "progress": current.to_dict(),
                }

            candidates = self.candidates()
            if not candidates:
                return {"ok": False, "message": "没有需要补 AI 的存量笔记"}

            progress = BackfillProgress(
                running=True,
                total=len(candidates),
                total_batches=(len(candidates) + BATCH_SIZE - 1) // BATCH_SIZE,
                started_at=vault_mod.now_iso(),
            )
            self._save_progress(progress)

            self._thread = threading.Thread(
                target=self._run, args=(candidates,), daemon=True
            )
            self._thread.start()
            return {
                "ok": True,
                "message": f"已启动，将分 {progress.total_batches} 批处理 {len(candidates)} 条",
                "progress": progress.to_dict(),
            }

    def _run(self, candidates: list[dict[str, Any]]) -> None:
        from app.core.db import get_db
        from app.providers.ai_service import get_ai_service
        from app.services.ingest_service import get_ingest_service

        db = get_db()
        ai = get_ai_service()
        ingest = get_ingest_service()
        progress = self._load_progress()

        try:
            for index, candidate in enumerate(candidates):
                # 断点续跑：已处理过的跳过
                if index < progress.processed:
                    continue

                path = Path(candidate["note_path"])
                note = vault_mod.read_note(path)
                if note is None:
                    progress.failed += 1
                    progress.processed += 1
                    progress.failed_paths.append(candidate["note_path"])
                    self._save_progress(progress)
                    continue

                try:
                    result = ai.analyze(note.body, title=note.title)
                    if not result.ok:
                        raise RuntimeError(result.error or "AI 调用失败")

                    self._store_suggestions(db, ingest, candidate, result)
                    progress.succeeded += 1
                except Exception as exc:  # noqa: BLE001
                    progress.failed += 1
                    progress.failed_paths.append(candidate["note_path"])
                    print(f"[backfill] 失败 {candidate['note_path']}：{exc}")

                progress.processed += 1
                if progress.processed % BATCH_SIZE == 0:
                    progress.current_batch = progress.processed // BATCH_SIZE
                    self._save_progress(progress)
                    # 批间限速
                    time.sleep(2.0)
                else:
                    self._save_progress(progress)

            progress.finished_at = vault_mod.now_iso()
        finally:
            progress.running = False
            if not progress.finished_at:
                progress.finished_at = vault_mod.now_iso()
            self._save_progress(progress)

    def _store_suggestions(
        self, db, ingest, candidate: dict[str, Any], result: Any
    ) -> None:
        """把补 AI 的建议写入 ai_suggestions，并让该笔记进入待核准。

        **不静默写入 tags**——必须经核准页确认。
        """
        note_path = candidate["note_path"]

        def _run(conn) -> None:
            conn.execute(
                "DELETE FROM ai_suggestions WHERE note_path = ?", (note_path,)
            )
            for tag in result.tags:
                conn.execute(
                    "INSERT INTO ai_suggestions(note_path, suggestion_type, "
                    "suggestion_value, suggestion_reason, status, created_at) "
                    "VALUES(?, 'tag', ?, '', 'pending', ?)",
                    (note_path, tag, vault_mod.now_iso()),
                )
            for name in result.new_tags:
                conn.execute(
                    "INSERT INTO ai_suggestions(note_path, suggestion_type, "
                    "suggestion_value, suggestion_reason, status, created_at) "
                    "VALUES(?, 'new_tag', ?, ?, 'pending', ?)",
                    (
                        note_path,
                        name.get("name", ""),
                        name.get("reason", ""),
                        vault_mod.now_iso(),
                    ),
                )
            for part in result.partitions:
                conn.execute(
                    "INSERT INTO ai_suggestions(note_path, suggestion_type, "
                    "suggestion_value, suggestion_reason, status, created_at) "
                    "VALUES(?, 'partition', ?, '', 'pending', ?)",
                    (note_path, part, vault_mod.now_iso()),
                )
            for kw in result.keywords:
                conn.execute(
                    "INSERT INTO ai_suggestions(note_path, suggestion_type, "
                    "suggestion_value, suggestion_reason, status, created_at) "
                    "VALUES(?, 'related_keyword', ?, '', 'pending', ?)",
                    (note_path, kw, vault_mod.now_iso()),
                )

            # 缓存 AI 摘要到 md（不写 tags/personal_summary）
            md_path = Path(note_path)
            note = vault_mod.read_note(md_path)
            if note is not None:
                fm = dict(note.frontmatter)
                fm["ai_summary"] = result.summary
                fm["ai_summary_type"] = result.summary_type
                fm["ai_model"] = result.provider
                fm["ai_suggested_tags"] = result.tags
                fm["ai_suggested_partitions"] = result.partitions
                vault_mod.write_note(md_path, fm, note.body)

        db.write(_run)

    def retry_failed(self) -> dict[str, Any]:
        """重跑失败清单。"""
        progress = self._load_progress()
        if not progress.failed_paths:
            return {"ok": False, "message": "没有失败项"}

        candidates = []
        for path in progress.failed_paths:
            md = Path(path)
            note = vault_mod.read_note(md)
            if note is None:
                continue
            candidates.append(
                {
                    "note_path": path,
                    "title": note.title,
                    "level": "processed",
                    "length": len(note.body),
                }
            )

        new_progress = BackfillProgress(
            running=True,
            total=len(candidates),
            total_batches=(len(candidates) + BATCH_SIZE - 1) // BATCH_SIZE,
            started_at=vault_mod.now_iso(),
        )
        self._save_progress(new_progress)

        self._thread = threading.Thread(
            target=self._run, args=(candidates,), daemon=True
        )
        self._thread.start()
        return {"ok": True, "count": len(candidates)}

    def reset(self) -> dict[str, Any]:
        """清空进度（下次从头开始）。"""
        self.db.write(
            lambda conn: conn.execute("DELETE FROM meta WHERE key = ?", (PROGRESS_KEY,))
        )
        return {"ok": True}


_backfill: BackfillService | None = None


def get_backfill_service() -> BackfillService:
    global _backfill
    if _backfill is None:
        _backfill = BackfillService()
    return _backfill
