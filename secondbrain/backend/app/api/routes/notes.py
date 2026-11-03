"""C 层笔记 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.notes_service import get_notes_service

router = APIRouter(prefix="/api/notes", tags=["notes"])


class CreateNoteRequest(BaseModel):
    title: str
    body: str = ""
    form: str = "fragment"
    tags: list[str] = Field(default_factory=list)
    partitions: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    digested: bool = False


class UpdateNoteRequest(BaseModel):
    title: str | None = None
    body: str | None = None
    form: str | None = None
    tags: list[str] | None = None
    partitions: list[str] | None = None
    digested: bool | None = None


class ImportRequest(BaseModel):
    content: str
    title: str | None = None


class InspirationRequest(BaseModel):
    title: str = ""
    body: str = ""
    top_k: int = 5


@router.get("")
async def list_notes(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    svc = get_notes_service()
    return {"notes": svc.list_notes(limit=limit, offset=offset)}


@router.post("")
async def create_note(req: CreateNoteRequest) -> dict[str, Any]:
    svc = get_notes_service()
    return svc.create_note(
        title=req.title,
        body=req.body,
        form=req.form,
        tags=req.tags,
        partitions=req.partitions,
        citations=req.citations,
        digested=req.digested,
    )


@router.get("/{note_path:path}")
async def get_note(note_path: str) -> dict[str, Any]:
    svc = get_notes_service()
    note = svc.get_note(note_path)
    if note is None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    return note


@router.put("/{note_path:path}")
async def update_note(note_path: str, req: UpdateNoteRequest) -> dict[str, Any]:
    svc = get_notes_service()
    try:
        return svc.update_note(
            note_path,
            title=req.title,
            body=req.body,
            form=req.form,
            tags=req.tags,
            partitions=req.partitions,
            digested=req.digested,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{note_path:path}")
async def delete_note(note_path: str) -> dict[str, Any]:
    svc = get_notes_service()
    ok = svc.delete_note(note_path)
    if not ok:
        raise HTTPException(status_code=404, detail="笔记不存在")
    return {"ok": True}


@router.post("/import")
async def import_external(req: ImportRequest) -> dict[str, Any]:
    """外部导入：粘贴 md 或纯文本 → 自动建 C 层笔记。"""
    svc = get_notes_service()
    try:
        return svc.import_external(req.content, req.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/inspiration")
async def inspiration(req: InspirationRequest) -> dict[str, Any]:
    """C 层编辑时的"你可能想引用"。"""
    svc = get_notes_service()
    return svc.inspiration(req.title, req.body, top_k=req.top_k)


@router.post("/{note_path:path}/archive")
async def archive(note_path: str) -> dict[str, Any]:
    """手动归档 → 99_Archive/。"""
    svc = get_notes_service()
    try:
        return svc.archive(note_path)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
