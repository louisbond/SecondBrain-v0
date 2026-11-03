"""Worker 流水线：把 jobs 队列的 kind 映射到具体业务动作。

kind → handler：
- extract：平台适配器抓取 → 落 A 层 md → 落 extracted → 排 ai job
- asr：yt-dlp 抽音频 → faster-whisper 转写 → 文本写回 A 层 md（asr=true）→ 排 ai job
- ai：AI 契约调用 → 写 ai_suggestions → 状态 pending_review
- embed：bge-m3 编码 → 写 embeddings
- reindex：全库 md 重建索引
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

from app.core import vault as vault_mod
from app.core.config import get_config
from app.core.db import get_db
from app.services.ingest_service import get_ingest_service
from app.workers.queue import JobQueue


def _progress(item_id: int, value: float) -> None:
    """上报任务进度（工作区实时进度条）。"""
    try:
        from app.workers.queue import get_queue

        get_queue().set_item_progress(item_id, value)
    except Exception:  # noqa: BLE001
        pass


def _read_cookie() -> str | None:
    """读取 B站 cookie（配置了 cookie_file 时）。"""
    cfg = get_config()
    if not cfg.collect.cookie_file:
        return None
    from app.adapters.bilibili import parse_cookie_file

    return parse_cookie_file(str(cfg.collect.cookie_file))


def handle_extract(item_id: int) -> None:
    """抓取任务。"""
    ingest = get_ingest_service()
    db = get_db()
    cfg = get_config()

    item = ingest.get_item(item_id)
    if item is None:
        return

    url = str(item.get("source_url") or "")
    platform = str(item.get("platform") or "web")
    title = str(item.get("title") or "")

    # 速记条目不走抓取
    if platform == "quick_note":
        return

    ingest.transition(item_id, "extracting")
    _progress(item_id, 0.2)

    from app.adapters import get_adapter

    adapter = get_adapter(
        platform, cookie=_read_cookie(), timeout=cfg.collect.web_timeout
    )
    if adapter is None:
        ingest.transition(
            item_id,
            "extract_failed",
            error_reason="no_adapter",
            error_detail=f"无 {platform} 适配器",
        )
        _notify_failed(item, f"无 {platform} 适配器")
        return

    result = adapter.extract(url)
    _progress(item_id, 0.35)

    if not result.ok:
        ingest.transition(
            item_id,
            "extract_failed",
            error_reason="extract_failed",
            error_detail=result.error or "抓取失败",
        )
        _notify_failed(item, result.error or "抓取失败")
        return

    # 需要 ASR：先落 A 层（不含字幕），再排 asr job
    if result.needs_asr:
        path = ingest.write_inbox(
            url=url,
            platform=platform,
            title=result.title or title,
            text=result.text,
            extractor=result.extractor,
            asr=False,
        )
        ingest.update_item(
            item_id,
            title=result.title or title,
            raw_text=result.text,
            extractor=result.extractor,
            asr=False,
            inbox_path=path.as_posix(),
        )
        ingest.transition(item_id, "extracted")
        _enqueue(item_id, "asr")
        return

    # 有字幕/正文：直接落 A 层并排 ai job
    path = ingest.write_inbox(
        url=url,
        platform=platform,
        title=result.title or title,
        text=result.text,
        extractor=result.extractor,
        asr=result.asr,
        screenshots=result.screenshots,
    )
    ingest.update_item(
        item_id,
        title=result.title or title,
        raw_text=result.text,
        extractor=result.extractor,
        asr=result.asr,
        inbox_path=path.as_posix(),
    )
    ingest.transition(item_id, "extracted")
    # v31：不再自动排 ai job。
    # 采集区的定位是「经济工具拿原文」——抓取/转写完就交差。
    # AI 提取要点是**加工区**的活：要选模板、要指定字数档位（≥800 字）、
    # 要用户确认分区标签。放在采集阶段自动跑，会出现用户吐槽的
    # 「任务完成后突然开始 AI 提取要点，还没按 800 字要求」。
    _mark_raw_ready(item_id)


def _mark_raw_ready(item_id: int) -> None:
    """原文就绪：不再往下自动跑 AI，等用户在加工区触发。

    **坑**：`ingest.transition()` 会把 error_reason 清成 None（默认值）。
    所以这里**必须把已有的错误原因带过去** —— 否则 ASR 失败的原因
    （"这段音频没有人声"之类）在转场时就被抹掉了，界面上只看到
    「原文就绪」，用户完全不知道之前发生了什么。
    """
    ingest = get_ingest_service()
    item = ingest.get_item(item_id) or {}
    try:
        ingest.transition(
            item_id,
            "raw_ready",
            error_reason=item.get("error_reason"),
            error_detail=item.get("error_detail"),
        )
    except Exception:  # noqa: BLE001
        pass
    _progress(item_id, 0.5)


def handle_asr(item_id: int) -> None:
    """ASR 转写任务：yt-dlp 抽音频 → faster-whisper 转写 → 写回 A 层。"""
    ingest = get_ingest_service()
    cfg = get_config()

    item = ingest.get_item(item_id)
    if item is None:
        return

    url = str(item.get("source_url") or "")
    if not url:
        return

    from app.services.asr_service import get_asr_service

    _progress(item_id, 0.45)
    asr_svc = get_asr_service()
    result = asr_svc.transcribe(url, item_id=item_id)
    _progress(item_id, 0.7)
    if not result.ok:
        # 转写失败不阻塞：保留现有正文（简介），直接进 AI
        # 但**必须把原因记下来**——否则界面只显示 "asr_failed"，无从排查
        ingest.update_item(
            item_id,
            extractor=f"{item.get('extractor')}+asr_failed",
            error_reason="asr_failed",
            error_detail=(result.error or "ASR 转写失败")[:500],
        )
        # v31：不自动跑 AI（AI 加工是加工区的活）。
        # 转写失败也停在「原文就绪」——用户可以在加工区重试转写或手工补正文。
        _mark_raw_ready(item_id)
        return

    # 文本写回 A 层 md（asr: true）
    # 双保险：空转写绝不覆盖已有正文（否则下游 AI 会拿到 0 字）
    if not str(result.text or "").strip():
        ingest.update_item(
            item_id,
            error_reason="asr_empty",
            error_detail="转写结果为空，未覆盖原有正文",
        )
        _mark_raw_ready(item_id)
        return

    inbox_path = item.get("inbox_path")
    if inbox_path:
        path = Path(inbox_path)
        if path.exists():
            note = vault_mod.read_note(path)
            if note is not None:
                fm = dict(note.frontmatter)
                fm["asr"] = True
                fm["extractor"] = "ytdlp_asr"
                body = note.body
                marker = "## 字幕/正文全文"
                if marker in body:
                    head, _, tail = body.partition(marker)
                    # tail 形如 "\n<text>\n\n## 来源\n<url>"
                    _, _, source_part = tail.partition("## 来源")
                    body = f"{head}{marker}\n{result.text}\n\n## 来源{source_part}"
                else:
                    body = f"{body}\n\n## ASR 转写全文\n{result.text}\n"
                vault_mod.write_note(path, fm, body)

    ingest.update_item(
        item_id,
        raw_text=result.text,
        extractor="ytdlp_asr",
        asr=True,
    )
    _notify_asr_done(str(item.get("title") or ""))
    # v31：转写完成 → 原文就绪，不自动跑 AI。
    _mark_raw_ready(item_id)


def handle_ai(item_id: int) -> None:
    """AI 契约任务：调用 AI → 落 ai_suggestions → pending_review。"""
    from app.providers.ai_service import get_ai_service

    ingest = get_ingest_service()
    db = get_db()

    item = ingest.get_item(item_id)
    if item is None:
        return

    raw_text = str(item.get("raw_text") or "")
    if not raw_text.strip():
        ingest.transition(
            item_id,
            "ai_failed",
            error_reason="empty_content",
            error_detail="没有可摘要的内容",
        )
        return

    ingest.transition(item_id, "ai_processing")
    _progress(item_id, 0.8)

    try:
        result = get_ai_service().analyze(raw_text, title=str(item.get("title") or ""))
    except Exception as exc:  # noqa: BLE001
        # 任何未预期异常都必须把条目落回失败态，
        # 否则条目永远停在 ai_processing，界面一直转圈、也无法重试。
        ingest.transition(
            item_id,
            "ai_failed",
            error_reason="ai_internal_error",
            error_detail=f"{type(exc).__name__}: {exc}"[:500],
        )
        raise
    if not result.ok:
        ingest.transition(
            item_id,
            "ai_failed",
            error_reason="ai_contract_failed",
            error_detail=result.error or "AI 契约失败",
        )
        return

    import json

    def _run(conn) -> None:
        conn.execute(
            "DELETE FROM ai_suggestions WHERE note_path = ?", (f"item:{item_id}",)
        )
        for tag in result.tags:
            conn.execute(
                "INSERT INTO ai_suggestions(note_path, suggestion_type, suggestion_value, "
                "suggestion_reason, status, created_at) VALUES(?, 'tag', ?, '', 'pending', ?)",
                (f"item:{item_id}", tag, vault_mod.now_iso()),
            )
        for name in result.new_tags:
            conn.execute(
                "INSERT INTO ai_suggestions(note_path, suggestion_type, suggestion_value, "
                "suggestion_reason, status, created_at) VALUES(?, 'new_tag', ?, ?, 'pending', ?)",
                (
                    f"item:{item_id}",
                    name.get("name", ""),
                    name.get("reason", ""),
                    vault_mod.now_iso(),
                ),
            )
        for part in result.partitions:
            conn.execute(
                "INSERT INTO ai_suggestions(note_path, suggestion_type, suggestion_value, "
                "suggestion_reason, status, created_at) VALUES(?, 'partition', ?, '', 'pending', ?)",
                (f"item:{item_id}", part, vault_mod.now_iso()),
            )
        # 词表外的新分区建议（用户勾选确认后才建分区）
        for name in getattr(result, "new_partitions", None) or []:
            conn.execute(
                "INSERT INTO ai_suggestions(note_path, suggestion_type, suggestion_value, "
                "suggestion_reason, status, created_at) VALUES(?, 'partition_new', ?, ?, 'pending', ?)",
                (
                    f"item:{item_id}",
                    name.get("name", ""),
                    name.get("reason", ""),
                    vault_mod.now_iso(),
                ),
            )
        for kw in result.keywords:
            conn.execute(
                "INSERT INTO ai_suggestions(note_path, suggestion_type, suggestion_value, "
                "suggestion_reason, status, created_at) VALUES(?, 'related_keyword', ?, '', 'pending', ?)",
                (f"item:{item_id}", kw, vault_mod.now_iso()),
            )
        conn.execute(
            "UPDATE items SET title = COALESCE(NULLIF(?, ''), title), "
            "ai_summary = ?, ai_summary_type = ?, ai_model = ? WHERE id = ?",
            (
                result.title_suggestion,
                result.summary,
                result.summary_type,
                result.provider,
                item_id,
            ),
        )

    db.write(_run)
    ingest.transition(item_id, "pending_review")

    # 待核准提醒（隔天集中核核准动线）
    try:
        from app.services.notifier import notify_review_ready

        pending = (
            db.scalar("SELECT COUNT(*) FROM items WHERE status = 'pending_review'") or 0
        )
        notify_review_ready(int(pending))
    except Exception:
        pass


def handle_embed(item_id: int) -> None:
    """单条 embedding（bge-m3）。"""
    from app.services.embedding_service import get_embedding_service

    svc = get_embedding_service()
    if not svc.available():
        return
    svc.embed_note_by_item(item_id)


def handle_reindex(item_id: int) -> None:
    """全库重建索引。"""
    from app.services.index_service import get_index_service

    get_index_service().reindex_all()


def _enqueue(item_id: int, kind: str, priority: int = 5) -> None:
    from app.workers.queue import get_queue

    get_queue().enqueue(item_id, kind, priority=priority)


def _notify_failed(item: dict[str, Any], reason: str) -> None:
    """抓取失败 toast（§5.1）。"""
    try:
        from app.services.notifier import notify_item_failed

        notify_item_failed(str(item.get("title") or item.get("source_url") or ""), reason)
    except Exception:
        pass


def _notify_done(item: dict[str, Any], platform_label: str = "") -> None:
    try:
        from app.services.notifier import notify_item_done

        notify_item_done(
            str(item.get("title") or ""), platform_label or str(item.get("platform") or "")
        )
    except Exception:
        pass


def _notify_asr_done(title: str) -> None:
    try:
        from app.services.notifier import notify_asr_done

        notify_asr_done(title)
    except Exception:
        pass


def resume_pending_ai(queue: JobQueue) -> int:
    """服务启动时续跑未完成的 AI 任务。

    条件：条目处于 extracted（原文就绪）且还没有摘要，且没有在跑的 ai job。
    这样重启 / 从 A 层重建条目之后，素材会自动走到待核准，不用手动点重试。
    """
    from app.core.db import get_db

    db = get_db()
    rows = db.query(
        "SELECT i.id FROM items i "
        "WHERE i.status = 'extracted' AND (i.ai_summary IS NULL OR i.ai_summary = '') "
        "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.item_id = i.id "
        "AND j.kind = 'ai' AND j.state IN ('pending','running'))"
    )
    count = 0
    for row in rows:
        item_id = int(row["id"])
        try:
            queue.enqueue(item_id, "ai", priority=6)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


def register_workers(queue: JobQueue) -> None:
    """注册所有 job handler。"""
    queue.register("extract", handle_extract)
    queue.register("asr", handle_asr)
    queue.register("ai", handle_ai)
    queue.register("embed", handle_embed)
    queue.register("reindex", handle_reindex)
