"""分区树 API（层级分区：增删改查 / 树 / 路径 / 子树）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.partition_service import get_partition_service

router = APIRouter(prefix="/api/partitions", tags=["partitions"])


class PartitionIn(BaseModel):
    name: str
    parent_id: int | None = None


class PartitionPatch(BaseModel):
    name: str | None = None
    parent_id: int | None = None


@router.get("")
async def list_partitions() -> dict[str, Any]:
    svc = get_partition_service()
    return {"partitions": svc.list_all(), "tree": svc.tree()}


@router.get("/tree")
async def partition_tree() -> dict[str, Any]:
    return {"tree": get_partition_service().tree()}


@router.post("")
async def create_partition(req: PartitionIn) -> dict[str, Any]:
    svc = get_partition_service()
    try:
        return {"ok": True, "partition": svc.create(req.name, req.parent_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{partition_id}")
async def update_partition(partition_id: int, req: PartitionPatch) -> dict[str, Any]:
    svc = get_partition_service()
    try:
        return {
            "ok": True,
            "partition": svc.update(partition_id, name=req.name, parent_id=req.parent_id),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{partition_id}")
async def delete_partition(partition_id: int) -> dict[str, Any]:
    svc = get_partition_service()
    try:
        return svc.delete(partition_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{partition_id}/subtree")
async def partition_subtree(partition_id: int) -> dict[str, Any]:
    svc = get_partition_service()
    if svc.get(partition_id) is None:
        raise HTTPException(status_code=404, detail="分区不存在")
    ids = svc.subtree_ids(partition_id)
    return {"ids": ids, "paths": list(svc.descendant_paths(partition_id))}


@router.post("/seed-from-vault")
async def seed_partitions() -> dict[str, Any]:
    """冷启动：把 vault 目录树导成分区树。"""
    from app.core.config import get_config

    cfg = get_config()
    created = get_partition_service().seed_from_vault_dirs(cfg.vault.path)
    return {"ok": True, "created": created, "tree": get_partition_service().tree()}
