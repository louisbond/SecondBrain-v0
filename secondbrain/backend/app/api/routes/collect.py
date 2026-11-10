"""采集页 API：批量粘贴解析、预览、提交、速记。

对齐 v0.5.0 §5.1 批量采集动线。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.ingest_service import get_ingest_service

router = APIRouter(prefix="/api/collect", tags=["collect"])


class ParseRequest(BaseModel):
    text: str = Field(default="", description="粘贴的分享文本/链接")


class SubmitEntry(BaseModel):
    # url 允许为空/None：粘贴文本里常有"只有标题没有链接"的行，
    # 直接 422 会让前端显示看不懂的 [object Object]。
    # 空的条目在 submit 里被**跳过**（不是错误）。
    url: str | None = None
    platform: str | None = None
    title: str | None = None
    force: bool = False
    # 采集时是否强制下载音频转写（默认只抓文本）
    collect_audio: bool = False


class SubmitRequest(BaseModel):
    entries: list[SubmitEntry | None] = Field(default_factory=list)
    force_similar: bool = False


class QuickNoteRequest(BaseModel):
    text: str
    title: str | None = None


@router.post("/parse")
async def parse(req: ParseRequest) -> dict[str, Any]:
    """解析粘贴文本 → 预览列表（含已存在/相似标记）。"""
    svc = get_ingest_service()
    return svc.preview(req.text)


@router.post("/submit")
async def submit(req: SubmitRequest) -> dict[str, Any]:
    """提交批量采集：精确去重 + 向量软提示 → 入队。"""
    svc = get_ingest_service()
    entries = []
    skipped: list[dict[str, Any]] = []
    for e in req.entries:
        if e is None:
            skipped.append({"url": "", "reason": "空条目"})
            continue
        d = e.model_dump()
        url = str(d.get("url") or "").strip()
        if not url:
            # 没链接的条目（只有标题）→ 跳过并说明，不当错误
            skipped.append(
                {
                    "url": "",
                    "title": str(d.get("title") or "")[:60],
                    "reason": "没有可用的链接（只有文字描述）",
                }
            )
            continue
        d["url"] = url
        entries.append(d)
    if not entries:
        return {
            "accepted": [],
            "duplicates": [],
            "similar": [],
            "rejected": [],
            "skipped": skipped,
        }
    result = svc.submit(entries, force_similar=req.force_similar)
    out = result.to_dict()
    out["skipped"] = skipped
    return out


@router.post("/quick-note")
async def quick_note(req: QuickNoteRequest) -> dict[str, Any]:
    """内部兼容入口：纯文本直接入 A 层。

    v0.9 起采集页的「速记」按钮已删除（用户反馈没什么实际作用）。
    保留这个路由是因为多个 E2E 测试用它造带正文的条目，删掉会让
    渲染/工作台走查失去数据准备手段。界面上不再有入口。
    """
    svc = get_ingest_service()
    try:
        return svc.quick_note(req.text, req.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/stats")
async def stats() -> dict[str, Any]:
    """采集统计。"""
    svc = get_ingest_service()
    return svc.stats()
