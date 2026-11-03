"""存量管线 API（P5）：扫描、未分层清单、分批补 AI。

对齐 v0.5.0 §8.1：
- 设置页选择 vault 根目录 → 扫描全部 .md；
- 无结构的老笔记 → 归入「存量未分层」清单，用户批量指定层级或保持原样仅索引；
- 可选：存量批量补 AI（分批 10 条/批，限速，预估 token 消耗并确认）；
- 断点续跑：进度持久化于 jobs；失败清单单独列出可重跑。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.backfill_service import get_backfill_service
from app.services.index_service import get_index_service

router = APIRouter(prefix="/api/vault", tags=["vault"])


class AssignLayerRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)
    level: str  # processed / note


class BackfillRequest(BaseModel):
    force: bool = False


@router.post("/scan")
async def scan() -> dict[str, Any]:
    """扫描 vault → 建索引 + 未分层清单。"""
    return get_index_service().scan_vault().to_dict()


@router.post("/assign-layer")
async def assign_layer(req: AssignLayerRequest) -> dict[str, Any]:
    """批量指定存量笔记层级。"""
    if req.level not in ("processed", "note"):
        raise HTTPException(status_code=400, detail="层级必须是 processed 或 note")
    try:
        return get_index_service().assign_layer(req.paths, req.level)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/backfill/progress")
async def backfill_progress() -> dict[str, Any]:
    """批量补 AI 进度（断点续跑状态）。"""
    return get_backfill_service().progress()


@router.get("/backfill/estimate")
async def backfill_estimate() -> dict[str, Any]:
    """预估 token 消耗（供用户确认后启动）。"""
    return get_backfill_service().estimate()


@router.post("/backfill/start")
async def backfill_start(req: BackfillRequest) -> dict[str, Any]:
    """启动存量批量补 AI。"""
    result = get_backfill_service().start(force=req.force)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "启动失败"))
    return result


@router.post("/backfill/retry")
async def backfill_retry() -> dict[str, Any]:
    """重跑失败清单。"""
    result = get_backfill_service().retry_failed()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "无失败项"))
    return result


@router.post("/backfill/reset")
async def backfill_reset() -> dict[str, Any]:
    """清空进度。"""
    return get_backfill_service().reset()
