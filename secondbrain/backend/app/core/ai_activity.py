"""全局 AI 活动登记（给顶栏进度用）。

背景：AI 生成是**同步阻塞**调用（本地 14B 模型 1-3 分钟），不走 jobs 队列，
所以 `workbench.progress()` 从 jobs 表里根本看不到它 —— 用户点了「生成」之后
界面一片安静，只能等到超时才知道出了事。

这里用一个极轻的进程内单例记录"当前有没有长耗时 AI 任务在跑"，
由路由在调用前后 start()/finish()，`progress()` 读快照。
只保留最近一条：单机场景并发点两次没有意义，覆盖即可。
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_current: dict[str, Any] | None = None


def start(
    label: str,
    item_id: int | None = None,
    stage: str = "生成中",
    est_s: float | None = None,
) -> None:
    """登记一个 AI 长任务开始。

    est_s：预计总耗时（秒）。给了就能算进度百分比和剩余时间（ETA）；
    不给则前端只显示已用时长。
    """
    global _current
    with _lock:
        _current = {
            "label": label,
            "item_id": item_id,
            "stage": stage,
            "started_at": time.time(),
            "est_s": float(est_s) if est_s else None,
        }


def update(stage: str) -> None:
    """更新阶段文案（可选）。"""
    with _lock:
        if _current is not None:
            _current["stage"] = stage


def finish() -> None:
    """登记结束（无论成功失败都要调用）。"""
    global _current
    with _lock:
        _current = None


def snapshot() -> dict[str, Any]:
    """给 /api/workbench/progress 用的快照。"""
    with _lock:
        if _current is None:
            return {"active": False}
        elapsed = time.time() - _current["started_at"]
        est = _current.get("est_s")
        out: dict[str, Any] = {
            "active": True,
            "label": _current["label"],
            "item_id": _current["item_id"],
            "stage": _current["stage"],
            "elapsed_ms": int(elapsed * 1000),
            "elapsed_s": round(elapsed, 1),
            "est_s": None,
            "percent": None,
            "eta_s": None,
        }
        if est and est > 0:
            # 进度封顶 98%：没真正返回前不显示 100%，否则会"到了还在转"
            out["est_s"] = round(est, 1)
            out["percent"] = round(min(elapsed / est, 0.98) * 100, 1)
            out["eta_s"] = max(0.0, round(est - elapsed, 1))
        return out
