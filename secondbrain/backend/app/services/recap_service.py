"""D 层运维服务：回顾卡片、周报、日报、归档。

对齐 v0.5.0 §10：
- 回顾卡片：昨日/本周采集数、待补手稿数（点击直达）、加权未引用旧笔记 1 条
  （权重=距上次被引用/查看时间，纯随机兜底）；
- 周报：每周日 21:00 自动生成 `_reports/2026-W45.md`；
- 日报：按需手动（回顾页选日期范围生成），不自动产出；
- 去重：URL 精确（拒绝）+ 向量 >0.85（软提示可强制）；
- 归档：仅手动。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.core import vault as vault_mod
from app.core.config import get_config
from app.core.db import get_db


class RecapService:
    """回顾与报表。"""

    def __init__(self) -> None:
        self.cfg = get_config()
        self.db = get_db()

    # -- 回顾卡片 ---------------------------------------------------------
    def recap_card(self) -> dict[str, Any]:
        """启动首页回顾卡片。"""
        today = datetime.now()
        yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        today_str = today.strftime("%Y-%m-%d")
        week_ago = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        _month_ago = (today - timedelta(days=30)).strftime("%Y-%m-%d")

        collected_yesterday = (
            self.db.scalar(
                "SELECT COUNT(*) FROM items WHERE date(created_at) = ?", (yesterday,)
            )
            or 0
        )
        collected_today = (
            self.db.scalar(
                "SELECT COUNT(*) FROM items WHERE date(created_at) = ?", (today_str,)
            )
            or 0
        )
        collected_week = (
            self.db.scalar(
                "SELECT COUNT(*) FROM items WHERE date(created_at) >= ?", (week_ago,)
            )
            or 0
        )
        pending_review = (
            self.db.scalar(
                "SELECT COUNT(*) FROM items WHERE status = 'pending_review'"
            )
            or 0
        )
        need_manuscript = (
            self.db.scalar(
                "SELECT COUNT(*) FROM notes WHERE level='processed' "
                "AND personal_summary_empty=1 AND archived=0"
            )
            or 0
        )
        created_week = (
            self.db.scalar(
                "SELECT COUNT(*) FROM notes WHERE level='note' "
                "AND date(created_at) >= ?",
                (week_ago,),
            )
            or 0
        )

        return {
            "collected_yesterday": collected_yesterday,
            "collected_today": collected_today,
            "collected_week": collected_week,
            "pending_review": pending_review,
            "need_manuscript": need_manuscript,
            "created_notes_week": created_week,
            "unreferenced_note": self._pick_unreferenced(),
            "generated_at": vault_mod.now_iso(),
        }

    def _pick_unreferenced(self) -> dict[str, Any] | None:
        """加权未引用旧笔记（权重=距上次被引用/查看时间，纯随机兜底）。"""
        rows = self.db.query(
            "SELECT n.note_path, n.title, n.level, n.created_at, n.reviewed_at "
            "FROM notes n "
            "WHERE n.archived = 0 AND n.level IN ('processed','note') "
            "AND NOT EXISTS (SELECT 1 FROM links l WHERE l.dst_note_id = n.id) "
            "ORDER BY n.created_at ASC LIMIT 200"
        )
        if not rows:
            return None

        now = datetime.now()
        weighted: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            stamp = str(row["reviewed_at"] or row["created_at"] or "")
            age_days = 30.0
            if stamp:
                try:
                    parsed = datetime.fromisoformat(stamp)
                    age_days = max(0.0, (now - parsed).total_seconds() / 86400)
                except ValueError:
                    pass
            weight = age_days + 1.0
            weighted.append((weight, dict(row)))

        # 加权随机：按 weight 累积
        total = sum(w for w, _ in weighted)
        target = random.random() * total
        cursor = 0.0
        for weight, item in weighted:
            cursor += weight
            if cursor >= target:
                return {
                    "note_path": item["note_path"],
                    "title": item["title"],
                    "level": item["level"],
                    "age_days": round(
                        (now - datetime.fromisoformat(str(item["created_at"])))
                        .total_seconds()
                        / 86400
                    )
                    if item.get("created_at")
                    else None,
                    "reason": "加权未引用旧笔记",
                }
        item = weighted[-1][1]
        return {"note_path": item["note_path"], "title": item["title"], "reason": "加权未引用旧笔记"}

    # -- 周报 -------------------------------------------------------------
    def weekly_report(self, week_offset: int = 0) -> dict[str, Any]:
        """生成周报（默认本周；week_offset=-1 → 上周）。"""
        now = datetime.now()
        # ISO 周一为一周起点
        monday = now - timedelta(days=now.weekday() + 7 * week_offset)
        monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        sunday = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)

        start = monday.strftime("%Y-%m-%d")
        end = sunday.strftime("%Y-%m-%d")

        collected = (
            self.db.scalar(
                "SELECT COUNT(*) FROM items WHERE date(created_at) BETWEEN ? AND ?",
                (start, end),
            )
            or 0
        )
        reviewed = (
            self.db.scalar(
                "SELECT COUNT(*) FROM items WHERE status = 'reviewed' "
                "AND date(processed_at) BETWEEN ? AND ?",
                (start, end),
            )
            or 0
        )
        created = (
            self.db.scalar(
                "SELECT COUNT(*) FROM notes WHERE level='note' "
                "AND date(created_at) BETWEEN ? AND ?",
                (start, end),
            )
            or 0
        )
        new_tags = [
            str(r["name"])
            for r in self.db.query(
                "SELECT name FROM tags WHERE source IN ('ai','manual') ORDER BY id DESC LIMIT 50"
            )
        ]
        need_manuscript = [
            {
                "note_path": str(r["note_path"]),
                "title": str(r["title"] or ""),
            }
            for r in self.db.query(
                "SELECT note_path, title FROM notes WHERE level='processed' "
                "AND personal_summary_empty=1 ORDER BY created_at DESC LIMIT 50"
            )
        ]

        iso_year, iso_week, _ = monday.isocalendar()
        filename = f"{iso_year}-W{iso_week:02d}.md"
        path = self.cfg.vault.dir("reports") / filename

        body = self._render_weekly(
            start, end, collected, reviewed, created, new_tags, need_manuscript
        )
        self.cfg.vault.ensure()
        vault_mod.write_note(
            path,
            {
                "type": "report",
                "kind": "weekly",
                "period_start": start,
                "period_end": end,
                "generated": vault_mod.now_iso(),
            },
            body,
        )
        return {
            "ok": True,
            "path": path.as_posix(),
            "period": [start, end],
            # v32：前端要直接展示周报正文（不用再单独读文件），
            # 也给一个可读的周标签（2026-W45）。
            "week": f"{iso_year}-W{iso_week:02d}",
            "week_label": f"{start} ~ {end}",
            "body": body,
            "collected": collected,
            "reviewed": reviewed,
            "created": created,
            "need_manuscript_count": len(need_manuscript),
        }

    def _render_weekly(
        self,
        start: str,
        end: str,
        collected: int,
        reviewed: int,
        created: int,
        new_tags: list[str],
        need_manuscript: list[dict[str, Any]],
    ) -> str:
        lines = [
            f"# 周报 {start} ~ {end}",
            "",
            "## 本周统计",
            "",
            f"- 采集：{collected} 条",
            f"- 核准入库：{reviewed} 条",
            f"- 创作笔记：{created} 篇",
            "",
            "## 待补手稿",
            "",
        ]
        if need_manuscript:
            for item in need_manuscript:
                stem = Path(item["note_path"]).stem
                lines.append(f"- [[{stem}]] {item['title']}")
        else:
            lines.append("（无）")

        lines.extend(["", "## 新入词表标签", ""])
        if new_tags:
            lines.extend(f"- {tag}" for tag in new_tags)
        else:
            lines.append("（无）")
        lines.append("")
        return "\n".join(lines)

    # -- 日报 -------------------------------------------------------------
    def daily_report(self, start: str, end: str) -> dict[str, Any]:
        """按需手动生成日报（日期范围）。"""
        collected = (
            self.db.scalar(
                "SELECT COUNT(*) FROM items WHERE date(created_at) BETWEEN ? AND ?",
                (start, end),
            )
            or 0
        )
        items = self.db.query(
            "SELECT title, platform, status, source_url FROM items "
            "WHERE date(created_at) BETWEEN ? AND ? ORDER BY id DESC LIMIT 100",
            (start, end),
        )
        lines = [
            f"# 日报 {start} ~ {end}",
            "",
            f"## 采集（{collected} 条）",
            "",
        ]
        for row in items:
            lines.append(
                f"- [{row['platform'] or 'web'}] {row['title'] or row['source_url'] or '（无标题）'}"
                f" — {row['status']}"
            )
        if not items:
            lines.append("（无）")
        lines.append("")

        path = self.cfg.vault.dir("reports") / f"daily-{start}_{end}.md"
        self.cfg.vault.ensure()
        vault_mod.write_note(
            path,
            {
                "type": "report",
                "kind": "daily",
                "period_start": start,
                "period_end": end,
                "generated": vault_mod.now_iso(),
            },
            "\n".join(lines),
        )
        return {"ok": True, "path": path.as_posix(), "collected": collected}

    # -- 自动周报调度 -----------------------------------------------------
    def should_generate_weekly(self) -> bool:
        """判断当前是否到了周报生成时间（每周日 21:00）。"""
        if not self.cfg.maintain.weekly_report:
            return False
        now = datetime.now()
        if now.weekday() != self.cfg.maintain.weekly_report_weekday:
            return False
        if now.hour < self.cfg.maintain.weekly_report_hour:
            return False
        # 同一天只生成一次
        key = f"weekly_report_{now.strftime('%Y-%m-%d')}"
        # 已生成过（meta 里有这个 key）→ 不用再生成
        return not self.db.query_one("SELECT value FROM meta WHERE key = ?", (key,))

    def mark_weekly_generated(self) -> None:
        now = datetime.now()
        key = f"weekly_report_{now.strftime('%Y-%m-%d')}"
        self.db.write(
            lambda conn: conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (key, vault_mod.now_iso()),
            )
        )


_recap: RecapService | None = None


def get_recap_service() -> RecapService:
    global _recap
    if _recap is None:
        _recap = RecapService()
    return _recap
