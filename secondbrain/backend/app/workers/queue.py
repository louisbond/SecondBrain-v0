"""后台任务队列（jobs）。

对齐 v0.5.0 §4.5 / §5.3：
- jobs 表持久化 → 笔记本休眠重启后队列自动续跑（§3.1 可用性边界）；
- kind: extract / asr / ai / embed / reindex；
- ASR worker 单独一条队列，CPU 并发=1，节流（§5.3）。

调度策略：
- 单个 dispatch 线程轮询 pending job，按 priority 排序；
- extract / ai / embed 走通用 worker 池（默认 2 并发）；
- asr 走独立 worker（并发 1，避免 CPU 打满）。
"""

from __future__ import annotations

import sqlite3
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from app.core.db import get_db
from app.core.vault import now_iso

JOB_KINDS = ("extract", "asr", "ai", "embed", "reindex")


class JobQueue:
    """持久化任务队列 + 后台调度。"""

    def __init__(self) -> None:
        self.db = get_db()
        self._stop = threading.Event()
        self._dispatcher: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sb-job")
        self._asr_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sb-asr")
        self._inflight: set[int] = set()
        self._lock = threading.RLock()
        self._handlers: dict[str, Callable[[int], Any]] = {}
        self._progress: dict[int, float] = {}

    # -- 注册处理器 -------------------------------------------------------
    def register(self, kind: str, handler: Callable[[int], Any]) -> None:
        self._handlers[kind] = handler

    # -- 入队 -------------------------------------------------------------
    def enqueue(
        self,
        item_id: int | None,
        kind: str,
        priority: int = 5,
        dedupe: bool = True,
    ) -> int:
        """入队。dedupe=True 时若同 item 同 kind 已有未完成任务则复用。"""

        def _run(conn: sqlite3.Connection) -> int:
            if dedupe and item_id is not None:
                row = conn.execute(
                    "SELECT id FROM jobs WHERE item_id = ? AND kind = ? "
                    "AND state IN ('pending','running') LIMIT 1",
                    (item_id, kind),
                ).fetchone()
                if row is not None:
                    return int(row["id"])
            cur = conn.execute(
                "INSERT INTO jobs(item_id, kind, state, priority, created_at, updated_at) "
                "VALUES(?,?,'pending',?,?,?)",
                (item_id, kind, priority, now_iso(), now_iso()),
            )
            return int(cur.lastrowid)

        return self.db.write(_run)

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> None:
        if self._dispatcher and self._dispatcher.is_alive():
            return
        self._stop.clear()
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="sb-job-dispatcher", daemon=True
        )
        self._dispatcher.start()
        self._recover_stale()

    def stop(self) -> None:
        self._stop.set()
        if self._dispatcher:
            self._dispatcher.join(timeout=3)
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._asr_pool.shutdown(wait=False, cancel_futures=True)

    def _recover_stale(self) -> None:
        """休眠唤醒后把残留 running 任务重置为 pending（断点续跑）。"""

        def _run(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "UPDATE jobs SET state='pending', updated_at=? WHERE state='running'",
                (now_iso(),),
            )
            return cur.rowcount

        count = self.db.write(_run)
        if count:
            print(f"[jobs] 恢复 {count} 个中断任务为 pending")

        self._recover_stuck_items()

    def _recover_stuck_items(self) -> None:
        """自愈：把「处理中」但对应任务已失败的条目拉回失败态。

        异常退出（例如 handler 抛异常）时，条目会永远停在 ai_processing /
        extracting —— 界面一直转圈，重试接口也因为状态不允许而直接拒绝。
        这里在启动时把这类残留条目落回可重试的失败态。
        """
        try:
            db = get_db()

            def _run(conn: sqlite3.Connection) -> int:
                rows = conn.execute(
                    "SELECT i.id, j.last_error FROM items i "
                    "JOIN jobs j ON j.item_id = i.id "
                    "WHERE i.status IN ('ai_processing','extracting','asr_processing') "
                    "AND j.state = 'failed' "
                    "AND j.id = (SELECT MAX(id) FROM jobs j2 "
                    "           WHERE j2.item_id = i.id AND j2.kind = "
                    "             CASE i.status WHEN 'ai_processing' THEN 'ai' "
                    "                          WHEN 'extracting' THEN 'extract' "
                    "                          ELSE 'asr' END) "
                ).fetchall()
                for row in rows:
                    detail = str(row["last_error"] or "")[:500] or "任务失败，未回写条目状态"
                    conn.execute(
                        "UPDATE items SET status='ai_failed', error_reason=?, "
                        "error_detail=? WHERE id=?",
                        ("ai_internal_error", detail, row["id"]),
                    )
                return len(rows)

            count = db.write(_run)
            if count:
                print(f"[jobs] 自愈 {count} 个卡在处理中的条目（任务已失败）")
        except Exception as exc:  # noqa: BLE001
            print(f"[jobs] 自愈失败（不影响启动）：{exc}")

    # -- 调度循环 ---------------------------------------------------------
    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._claim_next()
            except Exception:
                job = None
            if job is None:
                time.sleep(0.5)
                continue
            self._run_job(job)

    def _claim_next(self) -> dict[str, Any] | None:
        """原子地领取一个任务（asr 优先于普通任务，保证转写不被饿死）。"""

        def _run(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM jobs WHERE state='pending' "
                "ORDER BY CASE kind WHEN 'asr' THEN 0 ELSE 1 END, priority ASC, id ASC "
                "LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET state='running', attempts=attempts+1, updated_at=? "
                "WHERE id=? AND state='pending'",
                (now_iso(), row["id"]),
            )
            return dict(row)

        return self.db.write(_run)

    def _run_job(self, job: dict[str, Any]) -> None:
        kind = job.get("kind") or ""
        handler = self._handlers.get(kind)
        job_id = int(job["id"])
        item_id = job.get("item_id")

        if handler is None:
            self._finish(job_id, "failed", f"未注册的 job kind: {kind}")
            return

        pool = self._asr_pool if kind == "asr" else self._pool
        try:
            pool.submit(self._execute, job_id, item_id, kind, handler)
        except RuntimeError:
            self._finish(job_id, "failed", "任务池已关闭")

    def _execute(
        self,
        job_id: int,
        item_id: int | None,
        kind: str,
        handler: Callable[[int], Any],
    ) -> None:
        try:
            result = handler(item_id) if item_id is not None else handler(0)
            self._finish(job_id, "done", None)
            self._progress.pop(job_id, None)
            return result
        except Exception as exc:  # noqa: BLE001
            detail = f"{exc}\n{traceback.format_exc(limit=6)}"
            self._finish(job_id, "failed", detail[:2000])
            self._progress.pop(job_id, None)
            return None

    def _finish(self, job_id: int, state: str, error: str | None) -> None:
        def _run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE jobs SET state=?, last_error=?, updated_at=? WHERE id=?",
                (state, error, now_iso(), job_id),
            )

        try:
            self.db.write(_run)
        except Exception:
            pass

    # -- 进度 -------------------------------------------------------------
    def set_progress(self, job_id: int, value: float) -> None:
        self._progress[job_id] = max(0.0, min(1.0, value))

        def _run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE jobs SET progress=?, updated_at=? WHERE id=?",
                (value, now_iso(), job_id),
            )

        try:
            self.db.write(_run)
        except Exception:
            pass

    def set_item_progress(self, item_id: int | None, value: float) -> None:
        """按条目更新进度（worker 只知道 item_id，不知道 job_id）。

        工作区的实时进度条读的就是 jobs.progress。
        """
        if not item_id:
            return

        def _run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE jobs SET progress=?, updated_at=? "
                "WHERE item_id=? AND state='running'",
                (float(value), now_iso(), item_id),
            )

        try:
            self.db.write(_run)
        except Exception:
            pass

    # -- 查询 -------------------------------------------------------------
    def list_jobs(
        self, state: str | None = None, kind: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if state:
            where.append("state = ?")
            params.append(state)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        rows = self.db.query(
            f"SELECT j.*, i.title AS item_title, i.source_url AS item_url, "
            f"i.platform AS item_platform, i.status AS item_status "
            f"FROM jobs j LEFT JOIN items i ON i.id = j.item_id "
            f"{clause} ORDER BY j.id DESC LIMIT ?",
            params,
        )
        return [dict(r) for r in rows]

    def queue_stats(self) -> dict[str, Any]:
        rows = self.db.query("SELECT state, COUNT(*) c FROM jobs GROUP BY state")
        by_state = {r["state"]: r["c"] for r in rows}
        return {
            "by_state": by_state,
            "pending": by_state.get("pending", 0),
            "running": by_state.get("running", 0),
            "failed": by_state.get("failed", 0),
            "done": by_state.get("done", 0),
        }

    def retry_failed(self) -> int:
        def _run(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "UPDATE jobs SET state='pending', last_error=NULL, updated_at=? "
                "WHERE state='failed'",
                (now_iso(),),
            )
            return cur.rowcount

        return self.db.write(_run)

    def cancel(self, job_id: int) -> bool:
        def _run(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "UPDATE jobs SET state='cancelled', updated_at=? "
                "WHERE id=? AND state='pending'",
                (now_iso(), job_id),
            )
            return cur.rowcount

        return bool(self.db.write(_run))


_queue: JobQueue | None = None


def get_queue() -> JobQueue:
    global _queue
    if _queue is None:
        _queue = JobQueue()
    return _queue
