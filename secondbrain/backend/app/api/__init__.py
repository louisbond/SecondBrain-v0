"""API 路由注册。

按 v0.5.0 的页面动线组织：
- /api/collect   采集页（批量粘贴、预览、提交、速记）
- /api/items     队列 / A 层条目
- /api/jobs      任务队列
- /api/review    核准页（B 层闭环）
- /api/search    搜索与筛选
- /api/notes     C 层笔记
- /api/partitions 分区树（层级分区）
- /api/tags      标签治理
- /api/recap     回顾卡片 / 周报 / 日报
- /api/settings  设置（provider / vault / LAN / 重建索引）
- /api/upload    截图上传
- /api/workbench 工作区（合并采集+队列：实时状态 / 解析模板 / 配图）
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api.routes import (
    collect,
    items,
    jobs,
    notes,
    partitions,
    recap,
    review,
    search,
    settings,
    tags,
    upload,
    vault,
    workbench,
)


def register_routes(app: FastAPI) -> None:
    app.include_router(collect.router)
    app.include_router(items.router)
    app.include_router(jobs.router)
    app.include_router(review.router)
    app.include_router(search.router)
    app.include_router(notes.router)
    app.include_router(partitions.router)
    app.include_router(tags.router)
    app.include_router(recap.router)
    app.include_router(settings.router)
    app.include_router(upload.router)
    app.include_router(vault.router)
    app.include_router(workbench.router)
