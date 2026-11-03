"""任务队列 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.workers.queue import get_queue

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
async def list_jobs(
    state: str | None = None, kind: str | None = None, limit: int = 50
) -> dict[str, Any]:
    queue = get_queue()
    return {
        "jobs": queue.list_jobs(state=state, kind=kind, limit=limit),
        "stats": queue.queue_stats(),
    }


@router.post("/retry-failed")
async def retry_failed() -> dict[str, Any]:
    queue = get_queue()
    count = queue.retry_failed()
    return {"ok": True, "count": count}


@router.post("/{job_id}/cancel")
async def cancel(job_id: int) -> dict[str, Any]:
    queue = get_queue()
    ok = queue.cancel(job_id)
    return {"ok": ok}
