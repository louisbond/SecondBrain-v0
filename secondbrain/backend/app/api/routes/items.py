"""A 层条目 API：队列列表、详情、手动补充。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core import vault as vault_mod
from app.services.ingest_service import get_ingest_service

router = APIRouter(prefix="/api/items", tags=["items"])


class ManualFillRequest(BaseModel):
    text: str
    title: str | None = None


@router.get("")
async def list_items(
    status: str | None = None,
    platform: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    svc = get_ingest_service()
    items = svc.list_items(status=status, platform=platform, limit=limit, offset=offset)
    return {"items": items, "count": len(items), "stats": svc.stats()}


@router.get("/{item_id}")
async def get_item(item_id: int) -> dict[str, Any]:
    svc = get_ingest_service()
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    return item


@router.get("/{item_id}/content")
async def get_item_content(item_id: int) -> dict[str, Any]:
    """回查原文：A 层 md 全文（含字幕/正文）。"""
    svc = get_ingest_service()
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")

    inbox_path = item.get("inbox_path")
    if not inbox_path:
        return {
            "item_id": item_id,
            "path": None,
            "text": item.get("raw_text") or "",
            "frontmatter": {},
            "body": item.get("raw_text") or "",
        }

    path = Path(inbox_path)
    if not path.exists():
        return {
            "item_id": item_id,
            "path": str(path),
            "text": item.get("raw_text") or "",
            "frontmatter": {},
            "body": item.get("raw_text") or "",
            "missing": True,
        }

    fm, body = vault_mod.parse_frontmatter(path.read_text(encoding="utf-8"))
    return {
        "item_id": item_id,
        "path": str(path),
        "frontmatter": fm,
        "body": body,
        "text": item.get("raw_text") or "",
    }


@router.post("/{item_id}/manual-fill")
async def manual_fill(item_id: int, req: ManualFillRequest) -> dict[str, Any]:
    """extract_failed 的行内手动粘贴兜底 → extracted → 排 ai job。"""
    svc = get_ingest_service()
    ok = svc.manual_fill(item_id, req.text, req.title)
    if not ok:
        raise HTTPException(status_code=400, detail="手动补充失败（内容为空或条目不存在）")
    return {"ok": True, "item": svc.get_item(item_id)}


@router.post("/{item_id}/retry")
async def retry_item(item_id: int) -> dict[str, Any]:
    """重试失败的条目（extract_failed / ai_failed）。"""
    svc = get_ingest_service()
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")

    status = item.get("status")
    from app.workers.queue import get_queue

    queue = get_queue()
    if status == "extract_failed":
        svc.transition(item_id, "queued")
        queue.enqueue(item_id, "extract")
    elif status == "ai_failed":
        svc.transition(item_id, "extracted")
        queue.enqueue(item_id, "ai")
    elif status == "pending_review":
        # 用户对摘要/分区/标签不满意 → 重新分析一次
        # （只重跑 AI 阶段，不动 A 层原文，也不写 B 层）
        svc.transition(item_id, "extracted")
        queue.enqueue(item_id, "ai")
    else:
        raise HTTPException(status_code=400, detail=f"当前状态 {status} 不可重试")
    return {"ok": True, "item": svc.get_item(item_id)}


@router.delete("/{item_id}")
async def delete_item(item_id: int) -> dict[str, Any]:
    """删除条目（同时删 A 层 md）。"""
    svc = get_ingest_service()
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")

    import sqlite3

    from app.core.db import get_db

    db = get_db()
    inbox_path = item.get("inbox_path")

    def _run(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM jobs WHERE item_id = ?", (item_id,))
        conn.execute("DELETE FROM ai_suggestions WHERE note_path = ?", (f"item:{item_id}",))
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))

    db.write(_run)
    if inbox_path:
        path = Path(inbox_path)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
    return {"ok": True}
