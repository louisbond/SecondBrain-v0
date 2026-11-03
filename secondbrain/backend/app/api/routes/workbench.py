"""工作区 API：解析模板 CRUD + 模板化解析 + 配图插入 + 实时状态。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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
    """自检诊断：配置 / 数据库 / 索引 / Provider / ASR / 磁盘。"""
    import shutil
    from pathlib import Path

    from app.core.config import get_config
    from app.core.db import get_db

    cfg = get_config()
    db = get_db()
    checks: list[dict[str, Any]] = []

    def _add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    _add("vault 存在", Path(cfg.vault.path).exists(), str(cfg.vault.path))
    _add("数据库存在", Path(cfg.db_path).exists(), str(cfg.db_path))
    try:
        n_items = db.query_one("SELECT COUNT(*) AS c FROM items")["c"]
        n_notes = db.query_one("SELECT COUNT(*) AS c FROM notes")["c"]
        n_parts = db.query_one("SELECT COUNT(*) AS c FROM partitions")["c"]
        _add("数据可读", True, f"条目 {n_items} / 笔记 {n_notes} / 分区 {n_parts}")
    except Exception as exc:  # noqa: BLE001
        _add("数据可读", False, str(exc))

    try:
        from app.core.db import get_db
        from app.services.index_service import get_index_service

        idx = get_index_service()
        n_indexed = int(
            get_db().query_one("SELECT COUNT(*) AS c FROM notes")["c"]
        )
        _add("索引已建立", n_indexed > 0, f"已索引笔记 {n_indexed} 篇")
    except Exception as exc:  # noqa: BLE001
        _add("索引已建立", False, str(exc))

    try:
        from app.providers.ai_providers import get_router

        router = get_router()
        _add(
            "AI 主链",
            True,
            f"{router.current_name}（降级中：{router.fallback_active}）",
        )
    except Exception as exc:  # noqa: BLE001
        _add("AI 主链", False, str(exc))

    try:
        from app.services.asr_service import get_asr_service

        info = get_asr_service().info()
        _add(
            "ASR 可用",
            bool(info.get("available")),
            f"引擎 {info.get('engine')} / 模型 {info.get('model_size')}",
        )
    except Exception as exc:  # noqa: BLE001
        _add("ASR 可用", False, str(exc))

    usage = shutil.disk_usage(str(cfg.data_dir))
    _add(
        "磁盘空间",
        usage.free > 500 * 1024 * 1024,
        f"剩余 {usage.free // (1024 * 1024)} MB",
    )

    return {"ok": all(c["ok"] for c in checks), "checks": checks, "version": "0.5.0"}


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
