"""工作区服务（采集 + 队列合并后的实时状态）。

把「采集任务」和「解析队列」的状态统一成一份数据：
每条任务给出 **当前阶段 / 进度 / 成败**，前端据此画实时状态表。
"""

from __future__ import annotations

from typing import Any

from app.core.db import get_db

# status → (阶段标签, 进度, 状态色)
#
# v31：新增 raw_ready（原文就绪）。
# 采集区的定位是「经济工具拿原文」——抓完/转写完就停在这儿，
# **不再自动跑 AI**。所以"原文就绪"是采集链路的**终点**，
# 进度条到 100%（绿），而不是 45% 一路卡在"run"状态。
STAGE_MAP: dict[str, tuple[str, float, str]] = {
    "queued": ("排队中", 0.05, "wait"),
    "extracting": ("抓取原文", 0.25, "run"),
    "extracted": ("原文就绪", 0.45, "run"),
    "raw_ready": ("原文就绪", 1.0, "done"),
    "ai_processing": ("AI 解析中", 0.75, "run"),
    "pending_review": ("待你核准", 1.0, "done"),
    "reviewed": ("已入库", 1.0, "done"),
    "extract_failed": ("抓取失败", 1.0, "fail"),
    "ai_failed": ("解析失败", 1.0, "fail"),
}

# 阶段序号（用于判断"当前在第几步"）
STAGE_ORDER = [
    "queued",
    "extracting",
    "extracted",
    "raw_ready",
    "ai_processing",
    "pending_review",
    "reviewed",
]


class WorkbenchService:
    """工作区实时状态。"""

    def __init__(self) -> None:
        self.db = get_db()

    # -- 单条状态 ---------------------------------------------------------
    def item_state(self, item: dict[str, Any]) -> dict[str, Any]:
        status = str(item.get("status") or "queued")
        label, progress, color = STAGE_MAP.get(status, (status, 0.1, "wait"))

        # 已转写（asr=1）说明原文阶段真的跑完了
        if status == "extracted" and int(item.get("asr") or 0) == 1:
            label = "转写完成"
            color = "done"
            progress = 1.0

        job = self._latest_job(int(item["id"]))
        stage_text = ""
        if job is not None:
            state = str(job.get("state") or "")
            if state == "running":
                # 只在跑的时候显示过程文字。任务一结束就清空 ——
                # 否则「已完成」的行旁边还挂着「写入笔记…」，看着像卡住了。
                stage_text = str(job.get("message") or "")
                label = {
                    "extract": "抓取原文",
                    "asr": "语音转写",
                    "ocr": "识别画面文字",
                    "ai": "AI 解析",
                    "embed": "生成向量",
                }.get(str(job.get("kind") or ""), "处理中")
                color = "run"
                try:
                    p = float(job.get("progress") or 0)
                except (TypeError, ValueError):
                    p = 0.0
                if p > 0:
                    progress = max(progress, p)
            elif state == "failed":
                color = "fail"
                label = "任务失败"

        raw_len = len(str(item.get("raw_text") or ""))
        summary_len = len(str(item.get("ai_summary") or ""))

        return {
            "id": item.get("id"),
            "title": item.get("title") or "（无标题）",
            "platform": item.get("platform") or "web",
            "source_url": item.get("source_url") or "",
            "status": status,
            "stage": label,
            "stage_text": stage_text,
            "progress": round(min(max(progress, 0.0), 1.0), 3),
            "state": color,
            "ok": color != "fail",
            "extractor": item.get("extractor") or "",
            "asr": bool(int(item.get("asr") or 0)),
            "has_audio": bool(str(item.get("audio_path") or "")),
            "audio_hash": item.get("audio_hash") or "",
            "duration_ms": int(item.get("audio_ms") or 0),
            "source_kind": item.get("source_kind") or ("url" if item.get("source_url") else ""),
            "error_reason": item.get("error_reason") or "",
            "error_detail": (item.get("error_detail") or "")[:300],
            "raw_len": raw_len,
        # 别名：加工区的 review 接口返回的是 raw_length，
        # StatusStrip 组件在两个页面复用，统一成 raw_length 免得两套叫法。
        "raw_length": raw_len,
            "summary_len": summary_len,
            "inbox_path": item.get("inbox_path") or "",
            "processed_path": item.get("processed_path") or "",
            "created_at": item.get("created_at") or "",
            "processed_at": item.get("processed_at") or "",
            "job": job,
        }

    def _latest_job(self, item_id: int) -> dict[str, Any] | None:
        # message 必须在这里查出来：上面 item_state 靠它渲染「识别语音 3/13 段」。
        # 漏掉这一列的表现是进度文字永远是空 —— 而且不报错，很难发现。
        row = self.db.query_one(
            "SELECT id, kind, state, progress, attempts, last_error, message, updated_at "
            "FROM jobs WHERE item_id = ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        )
        return dict(row) if row else None

    # -- 全量状态 ---------------------------------------------------------
    def snapshot(self, limit: int = 50) -> dict[str, Any]:
        from app.services.ingest_service import get_ingest_service

        ingest = get_ingest_service()
        items = ingest.list_items(limit=limit)
        states = [self.item_state(it) for it in items]

        counts = {"running": 0, "done": 0, "fail": 0, "wait": 0}
        for st in states:
            counts[st["state"]] = counts.get(st["state"], 0) + 1

        # 平均进度**只算未完成的条目**（v31）。
        #
        # 坑：早先对**所有**条目求平均 —— 已完成(1.0)和失败(1.0)都算进去。
        # 于是 5 条里 4 条完成 + 1 条失败时，「平均进度 100%」而「进行中 0」，
        # 界面自相矛盾（用户反馈"任务进度条太不准"）。
        # 现在：没有未完成条目时显示 100%（全部搞定），否则只对未完成的求平均。
        pending = [s for s in states if s["state"] in ("run", "wait")]
        if pending:
            avg_progress = sum(s["progress"] for s in pending) / len(pending)
        else:
            avg_progress = 1.0 if states else 0.0

        return {
            "items": states,
            "stats": {
                "total": len(states),
                "running": counts.get("run", 0),
                "done": counts.get("done", 0),
                "fail": counts.get("fail", 0),
                "wait": counts.get("wait", 0),
                "avg_progress": round(avg_progress, 3),
                # 进行中/等待的条目数（前端用来判断"还有活要干吗"）
                "pending": len(pending),
            },
        }

    def progress(self) -> dict[str, Any]:
        """给顶栏用的轻量汇总 —— 不用把全部 items 捞出来。

        用户不想为了看一眼「识别语音 31 段」而跳到状态页，所以顶栏每 2 秒
        打一次这个接口。只查 jobs 表和当前在跑的那条 item 标题。
        """
        from app.core.db import get_db

        db = get_db()
        counts = db.query_one(
            "SELECT "
            "COALESCE(SUM(CASE WHEN state='running' THEN 1 ELSE 0 END), 0) AS running,"
            "COALESCE(SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END), 0) AS waiting,"
            "COALESCE(AVG(CASE WHEN state='running' THEN progress END), 0) AS avg_progress "
            "FROM jobs"
        )
        active = db.query_one(
            "SELECT j.item_id, j.kind, j.progress, j.message, i.title "
            "FROM jobs j JOIN items i ON i.id = j.item_id "
            "WHERE j.state = 'running' ORDER BY j.updated_at DESC LIMIT 1"
        )
        result: dict[str, Any] = {
            "running": int(counts["running"]) if counts else 0,
            "waiting": int(counts["waiting"]) if counts else 0,
            "avg_progress": round(float(counts["avg_progress"] or 0), 3)
            if counts
            else 0.0,
            "active": None,
        }
        if active:
            result["active"] = {
                "item_id": active["item_id"],
                "title": active["title"] or "",
                "kind": active["kind"] or "",
                "message": active["message"] or "",
                "progress": round(float(active["progress"] or 0), 3),
            }
        # AI 生成不走 jobs 队列，单独登记在 ai_activity 里；
        # 顶栏据此显示「AI 生成中 42s」，否则用户看不到任何进展。
        from app.core import ai_activity

        result["ai"] = ai_activity.snapshot()
        return result


_service: WorkbenchService | None = None


def get_workbench_service() -> WorkbenchService:
    global _service
    if _service is None:
        _service = WorkbenchService()
    return _service
