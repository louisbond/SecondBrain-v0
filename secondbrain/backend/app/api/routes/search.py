"""搜索与阅读 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.search_service import get_search_service

router = APIRouter(prefix="/api/search", tags=["search"])


class SearchRequest(BaseModel):
    query: str = ""
    levels: list[str] | None = None
    tags: list[str] = Field(default_factory=list)
    partitions: list[str] = Field(default_factory=list)
    include_inbox: bool = False
    include_archived: bool = False
    limit: int = 30
    offset: int = 0


@router.post("")
async def search(req: SearchRequest) -> dict[str, Any]:
    """全文搜索（C 层权重 > B 层）+ 标签 AND 筛选。"""
    svc = get_search_service()
    return svc.search(
        query=req.query,
        levels=req.levels,
        tags=req.tags,
        partitions=req.partitions,
        include_inbox=req.include_inbox,
        include_archived=req.include_archived,
        limit=req.limit,
        offset=req.offset,
    )


@router.get("/vocabulary")
async def vocabulary() -> dict[str, Any]:
    """受控词表（侧栏）。"""
    svc = get_search_service()
    return svc.list_tags()


@router.get("/similar")
async def similar(note_path: str, top_k: int = 10) -> dict[str, Any]:
    """找相似（embedding 优先，不可用则降级关键词）。"""
    svc = get_search_service()
    return svc.similar(note_path, top_k=top_k)


@router.get("/backlinks")
async def backlinks(note_path: str) -> dict[str, Any]:
    """反链查询。"""
    svc = get_search_service()
    return {"note_path": note_path, "backlinks": svc.backlinks(note_path)}


@router.get("/graph")
async def graph(note_path: str) -> dict[str, Any]:
    """反链视图数据（echarts 树图）。"""
    svc = get_search_service()
    return svc.link_graph(note_path)
