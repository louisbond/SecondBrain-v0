"""工作区 API：解析模板 CRUD + 模板化解析 + 配图插入 + 实时状态。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.config import APP_VERSION
from app.services.ingest_service import get_ingest_service
from app.services.template_service import get_template_service
from app.services.workbench_service import get_workbench_service

router = APIRouter(prefix="/api/workbench", tags=["workbench"])


class TemplateIn(BaseModel):
    name: str
    body: str = ""
    description: str = ""


class TemplatePatch(BaseModel):
    name: str | None = None
    body: str | None = None
    description: str | None = None


class ParseIn(BaseModel):
    item_id: int
    template_id: int | None = None
    min_chars: int = Field(default=800, ge=1, le=20000)


class ScreenshotIn(BaseModel):
    position: int = 0
    image_ref: str
    caption: str = ""


# ---------------------------------------------------------------- 实时状态
@router.get("/status")
async def status(limit: int = 50) -> dict[str, Any]:
    """工作区实时状态（阶段 / 进度 / 成败）。"""
    return get_workbench_service().snapshot(limit=limit)


@router.get("/progress")
async def progress() -> dict[str, Any]:
    """顶栏全局进度：不用跳到状态页也能看到「正在做什么」。"""
    return get_workbench_service().progress()


# ---------------------------------------------------------------- 模板 CRUD
@router.post("/queue/{item_id}/toggle")
async def toggle_queue(item_id: int) -> dict[str, Any]:
    """未加工 ↔ 待加工：点一下进队列，再点一下退回。

    · raw_ready（未加工）→ ready_to_parse（待加工）
    · ready_to_parse（待加工）→ raw_ready（未加工）
    """
    svc = get_ingest_service()
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    st = str(item.get("status") or "")
    if st == "raw_ready":
        svc.transition(item_id, "ready_to_parse")
        target = "ready_to_parse"
    elif st == "ready_to_parse":
        svc.transition(item_id, "raw_ready")
        target = "raw_ready"
    else:
        raise HTTPException(
            status_code=409, detail=f"当前状态「{st}」不能切换加工队列"
        )
    return {"ok": True, "status": target, "item": svc.get_item(item_id)}


@router.get("/templates")
async def list_templates() -> dict[str, Any]:
    svc = get_template_service()
    # 总是补齐缺失的内置模板（用户删过就不再补，但没删过就应该在）
    svc.seed_defaults()
    return {"templates": svc.list()}


@router.post("/templates")
async def create_template(req: TemplateIn) -> dict[str, Any]:
    svc = get_template_service()
    try:
        return {"ok": True, "template": svc.create(req.name, req.body, req.description)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/templates/{template_id}")
async def update_template(template_id: int, req: TemplatePatch) -> dict[str, Any]:
    svc = get_template_service()
    try:
        return {
            "ok": True,
            "template": svc.update(
                template_id, name=req.name, body=req.body, description=req.description
            ),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/templates/{template_id}")
async def delete_template(template_id: int) -> dict[str, Any]:
    svc = get_template_service()
    if not svc.delete(template_id):
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"ok": True}


# ---------------------------------------------------------------- 模板化解析
@router.post("/parse")
async def parse_with_template(req: ParseIn) -> dict[str, Any]:
    """按模板生成解析结果（≥min_chars 字，不足则重新生成）。"""
    ingest = get_ingest_service()
    item = ingest.get_item(req.item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")

    text = str(item.get("raw_text") or "")
    if not text.strip():
        # 没有正文时回落到 A 层 md 全文
        from pathlib import Path

        from app.core import vault as vault_mod

        inbox_path = str(item.get("inbox_path") or "")
        if inbox_path:
            note = vault_mod.read_note(Path(inbox_path))
            if note is not None:
                text = note.body

    from app.core import ai_activity
    from app.providers.ai_service import get_ai_service

    # 必须丢进线程池：analyze_with_template 内部是**阻塞**的 HTTP 调用
    # （本地 14B 模型生成 800 字要 1-3 分钟）。直接在 async 路由里同步调用
    # 会把整个 uvicorn 事件循环冻住 —— 期间连 /api/health、顶栏进度轮询都
    # 收不到响应，前端 30 秒超时后报"后端在执行长任务"，用户只能看着卡死。
    ai_service = get_ai_service()
    # 预计耗时只用于顶栏进度条的进度/ETA，估不准不影响生成本身。
    # 用**实际生效**的字数（素材短时会被下调）来估，否则 ETA 差好几倍。
    from app.providers.ai_service import _effective_min_chars

    effective_min = _effective_min_chars(req.min_chars, len(text))
    ai_activity.start(
        "生成解析",
        item_id=req.item_id,
        est_s=ai_service.estimate_create_seconds(effective_min),
    )
    try:
        result = await asyncio.to_thread(
            ai_service.analyze_with_template,
            text,
            template_id=req.template_id,
            min_chars=req.min_chars,
        )
    finally:
        ai_activity.finish()
    if result.get("template"):
        result["template"] = {
            "id": result["template"].get("id"),
            "name": result["template"].get("name"),
            "description": result["template"].get("description"),
        }
    # 生成成功就把 AI 稿落到 ai_summary：生成标签时优先用它（比原文更贴用户
    # 自己认可的表达），也方便用户离开加工区后再回来看到上次的产物。
    if result.get("ok") and str(result.get("content") or "").strip():
        try:
            ingest.update_item(
                req.item_id,
                ai_summary=str(result.get("content") or "").strip(),
                # 生成成功 → 清掉上一次的失败标记（界面上的红色 ✗ 要消失）
                error_reason="",
                error_detail="",
            )
            # 待加工 → 待入库（加工完成，等用户手核确认）
            item_now = ingest.get_item(req.item_id)
            if item_now and item_now.get("status") == "ready_to_parse":
                ingest.transition(req.item_id, "pending_review")
        except Exception:  # noqa: BLE001 - 落库失败不影响本次返回
            pass
    elif not result.get("ok"):
        # 加工失败：状态不动（留着可重试），只给 A 层文件名加标识
        try:
            ingest.mark_parse_failed(req.item_id)
        except Exception:  # noqa: BLE001
            pass
    return result


# ---------------------------------------------------------------- 配图
@router.post("/items/{item_id}/screenshots")
async def insert_screenshot(item_id: int, req: ScreenshotIn) -> dict[str, Any]:
    """在正文指定位置插入配图。"""
    svc = get_ingest_service()
    try:
        return svc.insert_screenshot(item_id, req.position, req.image_ref, req.caption)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/items/{item_id}/screenshots/{index}")
async def remove_screenshot(item_id: int, index: int) -> dict[str, Any]:
    svc = get_ingest_service()
    try:
        return svc.remove_screenshot(item_id, index)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------- 图谱
@router.get("/graph")
async def graph(
    limit: int = 400, include_tags: bool = True, include_partitions: bool = True
) -> dict[str, Any]:
    """图谱数据（前端用自研 3D 力导向引擎渲染）。"""
    from app.services.graph_service import get_graph_service

    return get_graph_service().build(
        limit=limit,
        include_tags=include_tags,
        include_partitions=include_partitions,
    )


@router.get("/vault-tree")
async def vault_tree() -> dict[str, Any]:
    """vault 目录树（快速跳转）。"""
    from app.services.graph_service import get_graph_service

    return {"tree": get_graph_service().vault_tree()}


@router.get("/aggregate")
async def aggregate(limit: int = 50) -> dict[str, Any]:
    """整合区一次性数据：回顾卡片 + 周报 + 图谱统计。"""
    from app.services.graph_service import get_graph_service
    from app.services.recap_service import get_recap_service

    recap = get_recap_service()
    graph_data = get_graph_service().build(limit=300)
    try:
        card = recap.recap_card()
    except Exception as exc:  # noqa: BLE001
        card = {"error": str(exc)}
    try:
        weekly = recap.weekly_report(0)
    except Exception as exc:  # noqa: BLE001
        weekly = {"error": str(exc)}
    stats = {}
    if hasattr(recap, "stats"):
        try:
            stats = recap.stats()
        except Exception:  # noqa: BLE001
            stats = {}
    return {
        "card": card,
        "weekly": weekly,
        "graph_stats": graph_data.get("stats", {}),
        "stats": stats,
    }


@router.get("/chars-presets")
async def chars_presets() -> dict[str, Any]:
    """解析结果的字数档位。"""
    from app.providers.ai_service import DEFAULT_MIN_CHARS, MIN_CHARS_PRESETS

    return {"presets": MIN_CHARS_PRESETS, "default": DEFAULT_MIN_CHARS}


# ---------------------------------------------------------------- 功能区
@router.get("/diagnose")
async def diagnose() -> dict[str, Any]:
    """自检诊断：配置 / 数据库 / 索引 / Provider / ASR / 磁盘。

    v39：实现搬到 `app/services/diagnostics.py` —— 诊断是领域逻辑，
    不该住在路由里。`/api/health` 现在复用同一份检查，
    不会再出现"health 说好、diagnose 说坏"的两套结论。
    """
    from app.services.diagnostics import collect_checks, summarize

    checks = collect_checks()
    state = summarize(checks)
    return {
        "ok": state["ok"],
        "degraded": state["degraded"],
        "checks": checks,
        "version": APP_VERSION,
    }


@router.post("/cache/clean")
async def clean_cache(keep_audio: bool = False) -> dict[str, Any]:
    """清理缓存（音频缓存、临时目录），不动 md 与数据库。"""
    import shutil
    from pathlib import Path

    from app.core.config import get_config

    cfg = get_config()
    removed: dict[str, Any] = {}

    audio_dir = Path(cfg.data_dir) / "cache" / "audio"
    if audio_dir.exists() and not keep_audio:
        files = list(audio_dir.glob("*"))
        for f in files:
            f.unlink(missing_ok=True)
        removed["audio_files"] = len(files)

    cache_dir = Path(cfg.data_dir) / "cache"
    for sub in ("images", "tmp"):
        target = cache_dir / sub
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            removed[sub] = True

    return {"ok": True, "removed": removed}


@router.post("/backup")
async def backup() -> dict[str, Any]:
    """导出备份：数据库副本（md 是真相源，留在 vault 里不动）。"""
    import shutil
    import time
    from pathlib import Path

    from app.core.config import get_config

    cfg = get_config()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(cfg.data_dir) / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    db_file = Path(cfg.db_path)
    target = out_dir / f"secondbrain-{stamp}.db"
    if db_file.exists():
        shutil.copy2(db_file, target)
    return {
        "ok": True,
        "path": target.as_posix(),
        "size": target.stat().st_size if target.exists() else 0,
    }
