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
    except PermissionError as exc:
        # 路径越界（不在知识库内）—— 明确拒绝，不要变成 500
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


class ManuscriptIn(BaseModel):
    note_path: str
    manuscript: str = ""


@router.post("/manuscript")
async def fill_manuscript(req: ManuscriptIn) -> dict[str, Any]:
    """补写个人手稿（已入库但手稿为空的 B 层笔记）。

    设计文档 §438 的软门槛：确认入库时手稿可以留空，
    但条目会进「待补手稿」清单 —— 这里就是那个清单的补写入口。

    note_path 走请求体而不是 URL 路径：`{note_path:path}` 是贪婪匹配，
    放在路径里会被前面的 PUT /{note_path:path} 抢先命中。
    """
    svc = get_notes_service()
    try:
        return svc.fill_manuscript(req.note_path, req.manuscript)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class MoveNoteRequest(BaseModel):
    note_path: str
    to_dir: str


@router.post("/move")
async def move_note(req: MoveNoteRequest) -> dict[str, Any]:
    """把笔记移动到另一个目录（首页文件树拖拽用）。

    只改物理位置 + notes.note_path，frontmatter 里若有相对引用需要用户自行确认
    （Obsidian 打开时会提示更新链接）。
    """
    from pathlib import Path

    from app.core.config import get_config

    cfg = get_config()
    root = cfg.vault.path.resolve()
    src = Path(req.note_path)
    dst_dir = Path(req.to_dir)

    try:
        dst_dir.resolve().relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="目标目录不在知识库内") from None
    if not src.exists():
        raise HTTPException(status_code=404, detail="笔记不存在")
    if src.suffix.lower() != ".md":
        raise HTTPException(status_code=400, detail="只能移动 .md 笔记")

    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.resolve() == src.resolve():
        return {"ok": True, "note_path": str(src), "moved": False}
    if dst.exists():
        raise HTTPException(
            status_code=409, detail=f"目标目录已有同名文件：{src.name}"
        )

    src.rename(dst)

    svc = get_notes_service()
    old_path, new_path = str(src), str(dst)

    def _run(conn):
        conn.execute(
            "UPDATE notes SET note_path = ? WHERE note_path = ?",
            (new_path, old_path),
        )

    svc.db.write(_run)
    return {"ok": True, "note_path": new_path, "moved": True}
