"""工作区 API：解析模板 CRUD + 模板化解析 + 配图插入 + 实时状态。"""

from __future__ import annotations

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


# ---------------------------------------------------------------- 模板 CRUD
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
        from app.core import vault as vault_mod
        from pathlib import Path

        inbox_path = str(item.get("inbox_path") or "")
        if inbox_path:
            note = vault_mod.read_note(Path(inbox_path))
            if note is not None:
                text = note.body

    from app.providers.ai_service import get_ai_service

    result = get_ai_service().analyze_with_template(
        text, template_id=req.template_id, min_chars=req.min_chars
    )
    if result.get("template"):
        result["template"] = {
            "id": result["template"].get("id"),
            "name": result["template"].get("name"),
            "description": result["template"].get("description"),
        }
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
