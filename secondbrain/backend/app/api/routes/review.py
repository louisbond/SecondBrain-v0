"""B 层核准 API：最简驾驶舱。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.review_service import get_review_service

router = APIRouter(prefix="/api/review", tags=["review"])


class SuggestTagsIn(BaseModel):
    manuscript: str = ""


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


@router.post("/{item_id}/suggest-tags")
async def suggest_tags(item_id: int, req: SuggestTagsIn) -> dict[str, Any]:
    """用户点了才生成标签推荐：优先看个人手稿，手稿空了才回退 AI 原文。

    以前是 AI 阶段自动基于 raw_text 产出建议、进加工区就铺一屏，
    但那时用户还没写手稿，推荐跟他自己真正想记的东西常常对不上。
    只算不写库，勾选与否由 confirm() 决定。
    """
    from app.services.ingest_service import get_ingest_service

    item = get_ingest_service().get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")

    manuscript = (req.manuscript or "").strip()
    ai_draft = str(item.get("ai_summary") or "").strip()
    # 三档原料，越靠前越贴近用户自己的表达：
    #   手稿 > AI 总结稿（加工区生成的、用户认可后的产物）> AI 原文
    if manuscript:
        source, text = "manuscript", manuscript
    elif ai_draft:
        source, text = "ai_summary", ai_draft
    else:
        source, text = "raw_text", str(item.get("raw_text") or "").strip()
    if not text:
        raise HTTPException(
            status_code=400, detail="个人手稿和 AI 原文都是空的，先写点内容再生成标签"
        )

    tags: list[str] = []
    engine = "ai"
    try:
        from app.providers.ai_service import get_ai_service

        # 同 workbench.parse：阻塞的 LLM 调用必须走线程池，
        # 否则事件循环被冻住，连健康检查都要等它跑完。
        from app.core import ai_activity

        ai_activity.start("生成标签", item_id=item_id)
        try:
            result = await asyncio.to_thread(
                get_ai_service().analyze, text, title=str(item.get("title") or "")
            )
        finally:
            ai_activity.finish()
        if result.ok:
            tags = [
                *[str(t) for t in result.tags],
                *[
                    str(t.get("name") or "")
                    for t in result.new_tags
                    if isinstance(t, dict)
                ],
                *[str(k) for k in result.keywords],
            ]
    except Exception:  # noqa: BLE001 - Ollama 没起来也要能给出可用建议
        pass

    if not tags:
        engine = "keyword"
        try:
            import jieba.analyse

            tags = [str(t) for t in jieba.analyse.extract_tags(text, topK=10)]
        except Exception:  # noqa: BLE001
            tags = []

    seen: set[str] = set()
    ordered: list[str] = []
    for raw in tags:
        name = raw.strip()
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return {
        "tags": ordered[:12],
        "source": source,
        "engine": engine,
        "item_id": item_id,
    }


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
