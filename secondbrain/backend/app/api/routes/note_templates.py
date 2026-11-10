"""创作模板 API（原创区「空模板」）。

与 `/api/workbench/templates`（解析模板）是两件事：
那个是给 AI 的提示词，这个是给人写的 Markdown 骨架。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.note_template_service import get_note_template_service

router = APIRouter(prefix="/api/note-templates", tags=["note-templates"])


class NoteTemplateIn(BaseModel):
    name: str
    body: str = ""
    description: str = ""
    suggest_tags: list[str] | None = None
    suggest_partition: str = ""


class NoteTemplatePatch(BaseModel):
    name: str | None = None
    body: str | None = None
    description: str | None = None
    suggest_tags: list[str] | None = None
    suggest_partition: str | None = None
    sort_order: int | None = None


@router.get("")
async def list_templates() -> dict[str, Any]:
    svc = get_note_template_service()
    svc.ensure_seeded()
    return {"templates": svc.list()}


@router.post("")
async def create_template(req: NoteTemplateIn) -> dict[str, Any]:
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="模板名不能为空")
    svc = get_note_template_service()
    tpl = svc.create(
        name=req.name.strip(),
        body=req.body,
        description=req.description,
        suggest_tags=req.suggest_tags,
        suggest_partition=req.suggest_partition,
    )
    if tpl is None:
        raise HTTPException(status_code=400, detail=f"模板名「{req.name}」已存在")
    return {"ok": True, "template": tpl}


@router.put("/{template_id}")
async def update_template(template_id: int, req: NoteTemplatePatch) -> dict[str, Any]:
    svc = get_note_template_service()
    tpl = svc.update(
        template_id,
        name=req.name,
        body=req.body,
        description=req.description,
        suggest_tags=req.suggest_tags,
        suggest_partition=req.suggest_partition,
        sort_order=req.sort_order,
    )
    if tpl is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"ok": True, "template": tpl}


@router.delete("/{template_id}")
async def delete_template(template_id: int) -> dict[str, Any]:
    svc = get_note_template_service()
    if not svc.delete(template_id):
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"ok": True}


@router.post("/restore-defaults")
async def restore_defaults() -> dict[str, Any]:
    """把内置模板补回来（删错了想要回）。"""
    svc = get_note_template_service()
    added = svc.restore_defaults()
    return {"ok": True, "added": added, "templates": svc.list()}
