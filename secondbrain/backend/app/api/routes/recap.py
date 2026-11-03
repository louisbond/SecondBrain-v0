"""回顾 / 报表 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.recap_service import get_recap_service

router = APIRouter(prefix="/api/recap", tags=["recap"])


class DailyRequest(BaseModel):
    start: str
    end: str


@router.get("/card")
async def card() -> dict[str, Any]:
    """回顾卡片（启动首页）。"""
    svc = get_recap_service()
    return svc.recap_card()


@router.post("/weekly")
async def weekly(week_offset: int = 0) -> dict[str, Any]:
    """生成周报（手动触发）。"""
    svc = get_recap_service()
    return svc.weekly_report(week_offset=week_offset)


@router.post("/daily")
async def daily(req: DailyRequest) -> dict[str, Any]:
    """生成日报（按需手动）。"""
    svc = get_recap_service()
    return svc.daily_report(req.start, req.end)


@router.get("/reports")
async def list_reports() -> dict[str, Any]:
    """已生成的报表列表。"""
    from app.core.config import get_config

    cfg = get_config()
    reports_dir = cfg.vault.dir("reports")
    files: list[dict[str, Any]] = []
    if reports_dir.exists():
        for path in sorted(reports_dir.glob("*.md"), reverse=True):
            files.append(
                {
                    "name": path.name,
                    "path": path.as_posix(),
                    "size": path.stat().st_size,
                    "modified": path.stat().st_mtime,
                }
            )
    return {"reports": files}
