"""后台调度器：自动周报 + 定时任务。

对齐 v0.5.0 §10：
「周报：每周日 21:00 自动生成 `_reports/2026-W45.md`」

调度器用独立线程轮询，每分钟检查一次；周报当天只生成一次
（用 meta 表记录，标记在 `mark_weekly_generated`）。
"""

from __future__ import annotations

import threading

from app.core.config import get_config


class Scheduler:
    """轻量定时调度器。"""

    def __init__(self) -> None:
        self.cfg = get_config()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_check = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="sb-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._stop.wait(60)

    def _tick(self) -> None:
        """每分钟执行一次调度检查。"""
        from app.services.recap_service import get_recap_service

        svc = get_recap_service()
        if svc.should_generate_weekly():
            result = svc.weekly_report()
            svc.mark_weekly_generated()
            try:
                from app.services.notifier import notify_weekly_report

                notify_weekly_report(str(result.get("path", "")))
            except Exception:
                pass


_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
