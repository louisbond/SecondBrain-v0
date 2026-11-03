"""标签治理 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.tag_service import get_tag_service

router = APIRouter(prefix="/api/tags", tags=["tags"])


class MergeRequest(BaseModel):
    src: str
    dst: str


class TagRequest(BaseModel):
    name: str


@router.get("")
async def vocabulary() -> dict[str, Any]:
    """词表总览。"""
    svc = get_tag_service()
    return svc.vocabulary()


@router.get("/{name}/notes")
async def notes_with_tag(name: str) -> dict[str, Any]:
    svc = get_tag_service()
    return {"notes": svc.list_notes_with_tag(name)}


@router.post("/merge")
async def merge(req: MergeRequest) -> dict[str, Any]:
    """合并标签 A→B（改写受影响 md 的 frontmatter）。"""
    svc = get_tag_service()
    try:
        return svc.merge_tags(req.src, req.dst)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/merge-partition")
async def merge_partition(req: MergeRequest) -> dict[str, Any]:
    svc = get_tag_service()
    try:
        return svc.merge_partitions(req.src, req.dst)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/rename")
async def rename(req: MergeRequest) -> dict[str, Any]:
    svc = get_tag_service()
    try:
        return svc.rename_tag(req.src, req.dst)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/deactivate")
async def deactivate(req: TagRequest) -> dict[str, Any]:
    svc = get_tag_service()
    return svc.deactivate_tag(req.name)


@router.post("")
async def add_tag(req: TagRequest) -> dict[str, Any]:
    svc = get_tag_service()
    try:
        return svc.add_tag(req.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/partitions")
async def add_partition(req: TagRequest) -> dict[str, Any]:
    svc = get_tag_service()
    try:
        return svc.add_partition(req.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/seed")
async def seed() -> dict[str, Any]:
    """冷启动：扫描存量标签合并为初始词表。"""
    from app.services.index_service import get_index_service

    return get_index_service().seed_tag_vocabulary()
