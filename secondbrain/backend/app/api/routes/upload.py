"""截图上传 API（附加项，手动 + 上传）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.core.config import get_config

router = APIRouter(prefix="/api/upload", tags=["upload"])

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_SIZE = 20 * 1024 * 1024  # 20MB


def _safe_name(name: str) -> str:
    stem = Path(name).stem
    suffix = Path(name).suffix.lower()
    cleaned = re.sub(r"[^\w\-一-鿿]+", "_", stem).strip("_")[:60] or "shot"
    return f"{cleaned}{suffix}"


@router.post("")
async def upload_screenshot(file: UploadFile = File(...)) -> dict[str, Any]:
    """上传截图到 assets/，返回 md 引用 [[assets/xxx.png]]。"""
    cfg = get_config()
    original = file.filename or "shot.png"
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_EXT:
        raise HTTPException(
            status_code=400, detail=f"不支持的文件类型 {suffix}（仅 png/jpg/webp/gif）"
        )

    data = await file.read()
    if len(data) > MAX_SIZE:
        raise HTTPException(status_code=413, detail="文件超过 20MB")
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")

    cfg.vault.ensure()
    assets_dir = cfg.vault.dir("assets")
    path = assets_dir / _safe_name(original)
    counter = 2
    while path.exists():
        path = assets_dir / f"{path.stem}-{counter}{suffix}"
        counter += 1

    path.write_bytes(data)
    rel = path.relative_to(cfg.vault.path).as_posix()
    return {
        "ok": True,
        "path": path.as_posix(),
        "relative": rel,
        "embed": f"![[{rel}]]",
        "size": len(data),
    }
