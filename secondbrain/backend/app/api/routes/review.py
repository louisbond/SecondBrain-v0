"""B 层核准 API：最简驾驶舱。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.review_service import get_review_service

router = APIRouter(prefix="/api/review", tags=["review"])


class ConfirmRequest(BaseModel):
    title: str | None = None
    personal_summary: str = ""
    tags: list[str] = Field(default_factory=list)
    partitions: list[str] = Field(default_factory=list)
    confirmed_related: list[str] = Field(default_factory=list)
    ai_summary: str | None = None
    summary_type: str = "bullets"


@router.get("/pending")
async def pending(limit: int = 50) -> dict[str, Any]:
    """待核准队列 + 需人工介入的条目（AI 失败等）。"""
    svc = get_review_service()
    return {
        "items": svc.list_pending(limit=limit),
        "manual": svc.list_manual(limit=limit),
        "ai_failed": svc.list_ai_failed(limit=limit),
    }


@router.get("/need-manuscript")
async def need_manuscript(limit: int = 100) -> dict[str, Any]:
    """待补手稿清单（digested 软门槛）。"""
    svc = get_review_service()
    return {"items": svc.list_digest_pending(limit=limit)}


@router.get("/{item_id}")
async def get_review(item_id: int) -> dict[str, Any]:
    """核准页数据：AI 摘要 + 手稿 + 标签建议 + 相关笔记。"""
    svc = get_review_service()
    try:
        return svc.get_review_item(item_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{item_id}/confirm")
async def confirm(item_id: int, req: ConfirmRequest) -> dict[str, Any]:
    """逐条确认入库（B 层 md 落盘 + 索引更新）。

    pending_review 是正常路径；ai_failed / extracted 等状态也允许手工核准，
    用于 AI 不可用时直接手写摘要入库。
    """
    svc = get_review_service()
    try:
        return svc.confirm(
            item_id,
            title=req.title,
            personal_summary=req.personal_summary,
            tags=req.tags,
            partitions=req.partitions,
            confirmed_related=req.confirmed_related,
            ai_summary=req.ai_summary,
            summary_type=req.summary_type,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{item_id}/skip")
async def skip(item_id: int) -> dict[str, Any]:
    """放弃本条（退回 pending_review）。"""
    svc = get_review_service()
    try:
        return svc.skip(item_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
