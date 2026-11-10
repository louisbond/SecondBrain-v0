"""A 层条目 API：队列列表、详情、手动补充。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core import vault as vault_mod
from app.services.ingest_service import get_ingest_service

router = APIRouter(prefix="/api/items", tags=["items"])


class ManualFillRequest(BaseModel):
    text: str
    title: str | None = None


class ItemPatchRequest(BaseModel):
    """标题 / 简介的行内编辑。两个字段都不传 = 没有要改的东西。"""

    title: str | None = None
    ai_summary: str | None = None


@router.get("")
async def list_items(
    status: str | None = None,
    platform: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    svc = get_ingest_service()
    items = svc.list_items(status=status, platform=platform, limit=limit, offset=offset)
    return {"items": items, "count": len(items), "stats": svc.stats()}


@router.get("/{item_id}")
async def get_item(item_id: int) -> dict[str, Any]:
    svc = get_ingest_service()
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    return item


@router.get("/{item_id}/content")
async def get_item_content(item_id: int) -> dict[str, Any]:
    """回查原文：A 层 md 全文（含字幕/正文）。"""
    svc = get_ingest_service()
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")

    inbox_path = item.get("inbox_path")
    if not inbox_path:
        return {
            "item_id": item_id,
            "path": None,
            "text": item.get("raw_text") or "",
            "frontmatter": {},
            "body": item.get("raw_text") or "",
        }

    path = Path(inbox_path)
    if not path.exists():
        return {
            "item_id": item_id,
            "path": str(path),
            "text": item.get("raw_text") or "",
            "frontmatter": {},
            "body": item.get("raw_text") or "",
            "missing": True,
        }

    fm, body = vault_mod.parse_frontmatter(path.read_text(encoding="utf-8"))
    return {
        "item_id": item_id,
        "path": str(path),
        "frontmatter": fm,
        "body": body,
        "text": item.get("raw_text") or "",
    }


@router.get("/{item_id}/audio")
async def get_item_audio(item_id: int) -> Any:
    """归档音频（MP3）。

    用 FileResponse 是为了拿到 **Range 支持** —— 浏览器 `<audio>` 要边下边播、
    拖进度条，全靠 206 Partial Content。自己 read_bytes() 返回整个文件的话，
    10 分钟的音频要先下完 9.6MB 才能点播放。
    """
    svc = get_ingest_service()
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    audio_path = str(item.get("audio_path") or "")
    if not audio_path or not Path(audio_path).exists():
        raise HTTPException(status_code=404, detail="这条素材还没有归档音频")
    return FileResponse(
        audio_path,
        media_type="audio/mpeg",
        filename=f"item{item_id}.mp3",
    )


@router.get("/{item_id}/segments")
async def get_item_segments(item_id: int) -> dict[str, Any]:
    """带时间戳的转写分段 —— 播放器点击跳转、人工校对用。"""
    svc = get_ingest_service()
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")

    audio_hash = str(item.get("audio_hash") or "")
    base = {
        "item_id": item_id,
        "duration_ms": int(item.get("audio_ms") or 0),
        "has_audio": bool(item.get("audio_path")),
    }
    if not audio_hash:
        return {**base, "segments": []}

    from app.services.media_service import get_media_service

    data = get_media_service().load_segments(audio_hash)
    if data is None:
        return {**base, "segments": []}
    return {
        **base,
        "segments": data.get("segments") or [],
        "model": data.get("model") or "",
    }


@router.get("/{item_id}/export")
async def export_item(item_id: int, fmt: str = "srt") -> Response:
    """导出音频文本：SRT 字幕 / 纯文本。

    数据主权的一部分 —— 导出的 SRT 能直接进任意播放器、剪辑软件，
    不用被这个软件锁住。
    """
    svc = get_ingest_service()
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")

    audio_hash = str(item.get("audio_hash") or "")
    segments: list[dict[str, Any]] = []
    if audio_hash:
        from app.services.media_service import get_media_service

        data = get_media_service().load_segments(audio_hash) or {}
        segments = list(data.get("segments") or [])

    text = str(item.get("raw_text") or "")
    if fmt == "txt" or not segments:
        body = text
        media_type = "text/plain; charset=utf-8"
        suffix = "txt"
    elif fmt == "segments":
        import json as _json

        body = _json.dumps(segments, ensure_ascii=False, indent=1)
        media_type = "application/json; charset=utf-8"
        suffix = "json"
    else:
        body = _to_srt(segments)
        media_type = "application/x-subrip; charset=utf-8"
        suffix = "srt"

    return Response(
        content=body.encode("utf-8"),
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename=item{item_id}.{suffix}; "
                f"filename*=UTF-8''item{item_id}.{suffix}"
            )
        },
    )


def _to_srt(segments: list[dict[str, Any]]) -> str:
    """分段 → SRT。时间格式必须是 HH:MM:SS,mmm（逗号不是小数点）。"""
    blocks: list[str] = []
    for index, seg in enumerate(segments, start=1):
        start = _srt_time(float(seg.get("start") or 0))
        end = _srt_time(float(seg.get("end") or 0))
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        blocks.append(f"{index}\n{start} --> {end}\n{text}\n")
    return "\n".join(blocks)


def _srt_time(seconds: float) -> str:
    total = max(0.0, seconds)
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = int(total % 60)
    millis = int(round((total - int(total)) * 1000))
    if millis >= 1000:  # 四舍五入进位
        millis = 999
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


@router.post("/upload")
async def upload_new_item(file: UploadFile = File(...)) -> dict[str, Any]:
    """直接上传视频/音频建条目 —— 不需要先有一个链接。

    **为什么需要这个入口**：抖音/快手匿名请求不给视频直链，小红书图文
    也没有音轨。用户实际的做法是「自己在 App 里保存视频，然后导入」。
    老流程要求「先粘链接建条目，再上传文件」，可这些来源的链接抓不出
    东西、甚至连标题都拿不到 —— 等于逼用户造假链接。

    去重按**整个文件的 sha256**（不是音频哈希）：无声视频也能去重。
    `source_url` 记成 `file://<sha256>`，保证 UNIQUE 不撞、reindex 时
    能正确回填（空串会撞唯一约束）。
    """
    import hashlib

    from app.core.db import get_db

    suffix = Path(file.filename or "").suffix.lower()
    allowed = (".mp4", ".m4a", ".mp3", ".webm", ".mov", ".wav", ".flv", ".mkv", ".aac")
    if suffix not in allowed:
        raise HTTPException(status_code=400, detail=f"只支持音视频文件：{', '.join(allowed)}")

    from app.core.config import get_config

    cfg = get_config()
    incoming = cfg.cache_dir / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    import uuid

    stem = (file.filename or "upload").rsplit(".", 1)[0][:40]
    target = incoming / f"new_{uuid.uuid4().hex[:8]}{suffix}"

    digest = hashlib.sha256()
    written = 0
    limit_mb = max(1, int(cfg.media.max_file_mb))
    max_bytes = limit_mb * 1024 * 1024
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    fh.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=400,
                        detail=f"文件超过 {limit_mb}MB 上限，请剪辑后再试",
                    )
                digest.update(chunk)
                fh.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"保存失败：{exc}") from exc

    if written == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="文件是空的")

    file_hash = digest.hexdigest()
    source_url = f"file://{file_hash}"

    # 同一个文件传两次 → 直接返回已有条目，不新建
    db = get_db()
    existing = db.query_one(
        "SELECT id, title, status FROM items WHERE source_url = ?", (source_url,)
    )
    if existing is not None:
        target.unlink(missing_ok=True)
        return {
            "ok": True,
            "duplicate": True,
            "item_id": int(existing["id"]),
            "title": existing["title"],
            "status": existing["status"],
            "message": "这个文件已经导入过了，直接打开已有素材即可",
        }

    title = stem or f"本地视频-{file_hash[:8]}"
    cur = db.write(
        lambda conn: conn.execute(
            "INSERT INTO items(source_url, platform, title, status, extractor, "
            "source_kind, source_name, source_path, media_url) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_url,
                "local",
                title,
                "queued",
                "file_upload",
                "file",
                file.filename or "",
                "",
                f"file:{target.as_posix()}",
            ),
        )
    )
    item_id = int(cur.lastrowid or 0)
    if item_id <= 0:
        raise HTTPException(status_code=500, detail="创建素材失败")

    svc = get_ingest_service()
    # 先落 A 层占位，转写完被正文替换
    path = svc.write_inbox(
        url=source_url,
        platform="local",
        title=title,
        text="（等待本地视频转写）",
        extractor="file_upload",
        asr=False,
    )
    svc.update_item(item_id, inbox_path=path.as_posix())

    # 立刻抽音频归档：转写失败（无声视频）也有音频/封面文字可查
    archived_note = ""
    try:
        from app.services.media_service import get_media_service

        archived = get_media_service().archive(str(target))
        if archived.ok:
            svc.update_item(
                item_id,
                audio_path=archived.audio_path,
                audio_hash=archived.audio_hash,
                audio_ms=archived.duration_ms or None,
            )
            mb = Path(archived.audio_path).stat().st_size / 1024 / 1024
            archived_note = f"，音频已归档（{mb:.1f}MB MP3）"
    except Exception:  # noqa: BLE001
        pass

    svc.transition(item_id, "extracted")
    from app.workers.queue import get_queue

    get_queue().enqueue(item_id, "asr", priority=4)
    return {
        "ok": True,
        "duplicate": False,
        "item_id": item_id,
        "title": title,
        "size": written,
        "message": f"已导入并排队转写{archived_note}",
    }


@router.post("/{item_id}/manual-fill")
async def manual_fill(item_id: int, req: ManualFillRequest) -> dict[str, Any]:
    """extract_failed 的行内手动粘贴兜底 → extracted → 排 ai job。"""
    svc = get_ingest_service()
    ok = svc.manual_fill(item_id, req.text, req.title)
    if not ok:
        raise HTTPException(status_code=400, detail="手动补充失败（内容为空或条目不存在）")
    return {"ok": True, "item": svc.get_item(item_id)}


@router.post("/{item_id}/upload-media")
async def upload_media(item_id: int, file: UploadFile = File(...)) -> dict[str, Any]:
    """上传本地视频/音频 → 排队转写成口播台词。

    100% 可靠的兜底：解析 API 会失效、分享链接会过期、Cookie 会掉登录，
    但用户从 App 保存到电脑的视频文件一定能转。转写全在本机 faster-whisper，
    文件不外发。适用于 extract_failed / raw_ready / extracted 的条目。
    """
    svc = get_ingest_service()
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    # 已入库/已归档的条目被静默换掉正文，会和笔记库脱节 —— 明确拒绝。
    if str(item.get("status") or "") in ("reviewed", "archived"):
        raise HTTPException(status_code=400, detail="已入库/已归档的条目请先恢复为待核准再转写")

    suffix = Path(file.filename or "").suffix.lower()
    allowed = (".mp4", ".m4a", ".mp3", ".webm", ".mov", ".wav", ".flv", ".mkv", ".aac")
    if suffix not in allowed:
        raise HTTPException(status_code=400, detail=f"只支持音视频文件：{', '.join(allowed)}")

    from app.core.config import get_config

    cfg = get_config()
    # 落临时区（可清），不再用 item{id}_upload 命名 —— 那个名字会覆盖上一次
    # 上传的文件，而且语义上是「缓存」，用户清缓存就把原始素材丢了。
    incoming = cfg.cache_dir / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    import uuid

    target = incoming / f"upload{item_id}_{uuid.uuid4().hex[:8]}{suffix}"

    limit_mb = max(1, int(cfg.media.max_file_mb))
    max_bytes = limit_mb * 1024 * 1024
    written = 0
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    fh.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=400,
                        detail=f"文件超过 {limit_mb}MB 上限，请剪辑后再试",
                    )
                fh.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"保存失败：{exc}") from exc
    if written == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="文件是空的")

    # 没有 A 层文件时先补一个占位，转写完 handle_asr 会把正文替换掉
    if not item.get("inbox_path"):
        path = svc.write_inbox(
            url=str(item.get("source_url") or ""),
            platform=str(item.get("platform") or "web"),
            title=str(item.get("title") or f"本地视频-{item_id}"),
            text="（等待本地视频转写）",
            extractor="media_asr_pending",
            asr=False,
        )
        svc.update_item(item_id, inbox_path=path.as_posix())

    svc.update_item(item_id, media_url=f"file:{target.as_posix()}")

    # 上传后立刻抽音频归档：即使后面 ASR 失败（无声视频、纯 BGM），
    # 这段音频本身也有价值（人耳复核），不该等到转写成功才落盘。
    archived_note = ""
    try:
        from app.services.media_service import get_media_service

        archived = get_media_service().archive(str(target))
        if archived.ok:
            svc.update_item(
                item_id,
                audio_path=archived.audio_path,
                audio_hash=archived.audio_hash,
                audio_ms=archived.duration_ms or None,
            )
            mb = Path(archived.audio_path).stat().st_size / 1024 / 1024
            archived_note = f"，音频已归档（{mb:.1f}MB MP3）"
    except Exception:  # noqa: BLE001
        # 归档失败不阻塞转写：ASR 也能直接从原视频解码
        pass

    # 能回「已抓取」就回，转写完成后 handle_asr 会把状态带到「原文就绪」，
    # 旧摘要自然失效、等用户重新跑 AI。个别状态（如已取消）不允许迁移，
    # 这时不阻塞转写本身，只是状态停在原地。
    reset = svc.transition(item_id, "extracted")

    from app.workers.queue import get_queue

    get_queue().enqueue(item_id, "asr", priority=5)
    return {
        "ok": True,
        "item_id": item_id,
        "size": written,
        "state_reset": reset,
        "message": f"已排队转写{archived_note}，可在「任务状态」页看进度",
    }


@router.post("/{item_id}/retry")
async def retry_item(item_id: int) -> dict[str, Any]:
    """重试失败的条目（extract_failed / ai_failed / raw_ready）。"""
    svc = get_ingest_service()
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")

    status = item.get("status")
    extractor = str(item.get("extractor") or "")
    from app.workers.queue import get_queue

    queue = get_queue()

    # v31：**ASR 从没成功过的条目，重试要重跑 extract（那样才会走 ASR）**。
    #
    # 坑：这类条目 extractor 带 "+asr_failed" 后缀，正文只有一句简介。
    # 早先一律按 ai_failed → 重跑 AI，于是 AI 拿着 11 字的简介去摘要 ——
    # 摘要质量当然差，而且用户看到的还是"没转写"。
    #
    # 优先级最高：不管当前是什么状态，只要 ASR 失败过就重跑 extract。
    # （pending_review 也一样 —— 11 字正文生成的摘要没有意义。）
    if "asr_failed" in extractor and status in (
        "ai_failed",
        "extracted",
        "raw_ready",
        "pending_review",
    ) or status == "extract_failed":
        svc.transition(item_id, "queued")
        queue.enqueue(item_id, "extract")
    elif status == "ai_failed":
        svc.transition(item_id, "extracted")
        queue.enqueue(item_id, "ai")
    elif status == "pending_review":
        # 用户对摘要/分区/标签不满意 → 重新分析一次
        # （只重跑 AI 阶段，不动 A 层原文，也不写 B 层）
        svc.transition(item_id, "extracted")
        queue.enqueue(item_id, "ai")
    elif status == "raw_ready":
        # v31：原文就绪（采集完成后不再自动跑 AI）。
        # 用户点「AI 提取要点」→ 重跑 AI 阶段。
        svc.transition(item_id, "extracted")
        queue.enqueue(item_id, "ai")
    else:
        raise HTTPException(status_code=400, detail=f"当前状态 {status} 不可重试")
    return {"ok": True, "item": svc.get_item(item_id)}


def _sync_inbox_meta(item: Any, title: str | None, ai_summary: str | None) -> bool:
    """改标题/简介时同步 A 层 md，避免文件与数据库各说各话。"""
    raw = item.get("inbox_path")
    if not raw:
        return False
    path = Path(str(raw))
    if not path.exists():
        return False
    note = vault_mod.read_note(path)
    if note is None:
        return False
    fm = dict(note.frontmatter)
    body = note.body
    if title is not None:
        fm["title"] = title
        lines = body.splitlines(keepends=True)
        if lines and lines[0].startswith("# 原始内容："):
            lines[0] = f"# 原始内容：{title}\n"
            body = "".join(lines)
    if ai_summary is not None:
        fm["ai_summary"] = ai_summary
    try:
        vault_mod.write_note(path, fm, body)
    except OSError:
        return False
    return True


@router.post("/{item_id}/confirm-collect")
async def confirm_collect(item_id: int) -> dict[str, Any]:
    """已采集 → 未加工（入库）：采集队列核实无误后送进加工队列。"""
    svc = get_ingest_service()
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    st = str(item.get("status") or "")
    if st != "extracted":
        raise HTTPException(
            status_code=409, detail=f"当前状态「{st}」不是已采集，不能入库"
        )
    svc.transition(item_id, "raw_ready")
    return {"ok": True, "item": svc.get_item(item_id)}


@router.patch("/{item_id}")
async def patch_item(item_id: int, body: ItemPatchRequest) -> dict[str, Any]:
    """行内改标题 / 简介（采集阶段觉得标题抓错了不必重新采集）。"""
    svc = get_ingest_service()
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")

    title = body.title.strip() if body.title is not None else None
    if title is not None and not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    summary = body.ai_summary.strip() if body.ai_summary is not None else None
    if title is None and summary is None:
        raise HTTPException(status_code=400, detail="没有要修改的字段")

    svc.update_item(item_id, title=title, ai_summary=summary)
    synced = _sync_inbox_meta(item, title, summary)
    # 标题变了 → A 层 md 文件名跟着改，否则文件永远叫「日期-旧标题」
    renamed_to = None
    if title and title != str(item.get("title") or ""):
        renamed_to = svc.rename_inbox(item_id, title)
    return {
        "ok": True,
        "synced_inbox": synced,
        "renamed_to": renamed_to,
        "item": svc.get_item(item_id),
    }


@router.delete("/{item_id}")
async def delete_item(item_id: int) -> dict[str, Any]:
    """删除不满意的采集素材，不必等它入库。

    清理 items / jobs / ai_suggestions、A 层 md、临时上传文件、归档 MP3。
    MP3 是内容寻址共享的，只有没有别的条目引用同一 hash 才删。
    **已入库的 B/C 层笔记不动** —— 那是用户资产，不该因为删一条采集记录就没了。
    """
    from app.core.config import get_config
    from app.core.db import get_db

    svc = get_ingest_service()
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")

    db = get_db()
    audio_hash = str(item.get("audio_hash") or "")
    audio_shared = False
    if audio_hash:
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM items WHERE audio_hash = ? AND id <> ?",
            (audio_hash, item_id),
        )
        audio_shared = bool(row and row["n"])

    def _run(conn: Any) -> None:
        conn.execute("DELETE FROM jobs WHERE item_id = ?", (item_id,))
        conn.execute(
            "DELETE FROM ai_suggestions WHERE note_path = ?", (f"item:{item_id}",)
        )
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))

    db.write(_run)

    removed: list[str] = []
    candidates: list[Path] = []
    if item.get("inbox_path"):
        candidates.append(Path(str(item["inbox_path"])))
    media_url = str(item.get("media_url") or "")
    if media_url.startswith("file:"):
        candidates.append(Path(media_url[5:]))
    if item.get("source_path"):
        candidates.append(Path(str(item["source_path"])))
    if not audio_shared:
        if item.get("audio_path"):
            candidates.append(Path(str(item["audio_path"])))
        if audio_hash:
            media_dir = get_config().media_dir
            candidates.append(media_dir / "asr" / f"{audio_hash}.json")
            candidates.append(media_dir / "ocr" / f"{audio_hash}.json")
    candidates.append(get_config().media_dir / "ocr" / f"item{item_id}.json")

    for path in candidates:
        try:
            if path.is_file():
                path.unlink()
                removed.append(path.name)
        except OSError:
            pass

    return {
        "ok": True,
        "removed_files": len(removed),
        "audio_kept_shared": audio_shared,
        "kept_notes": bool(item.get("processed_path")),
    }
