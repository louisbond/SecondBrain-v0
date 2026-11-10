"""Worker 流水线：把 jobs 队列的 kind 映射到具体业务动作。

kind → handler：
- extract：平台适配器抓取 → 落 A 层 md → 落 extracted → 排 ai job
- asr：yt-dlp 抽音频 → faster-whisper 转写 → 文本写回 A 层 md（asr=true）→ 排 ai job
- ai：AI 契约调用 → 写 ai_suggestions → 状态 pending_review
- embed：bge-m3 编码 → 写 embeddings
- reindex：全库 md 重建索引
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core import vault as vault_mod
from app.core.config import get_config
from app.core.db import get_db
from app.services.ingest_service import get_ingest_service
from app.workers.queue import JobQueue

# 短视频 CDN 常校验 Referer，按平台给下载请求带上（yt-dlp 走不到的直链场景）。
_MEDIA_REFERERS = {
    "douyin": "https://www.douyin.com/",
    "kuaishou": "https://www.kuaishou.com/",
    "xiaohongshu": "https://www.xiaohongshu.com/",
    "bilibili": "https://www.bilibili.com/",
}
_SHORT_VIDEO = frozenset(_MEDIA_REFERERS)
# 短于这个字数的「正文」基本只是标题/简介，真正有价值的是视频里的口播台词
_TRANSCRIPT_MIN_CHARS = 200
# 抓取失败时统一告诉用户真正能走通的路，别再让人白填 Cookie / 反复重试链接。
_IMPORT_HINT = (
    "建议用采集区「导入视频/音频」上传本地文件，程序会转写口播并归档 MP3；"
    "也可以直接粘贴完整文案。"
)


def _progress(item_id: int, value: float) -> None:
    """上报任务进度（工作区实时进度条）。"""
    try:
        from app.workers.queue import get_queue

        get_queue().set_item_progress(item_id, value)
    except Exception:  # noqa: BLE001
        pass


def _stage(item_id: int, message: str, progress: float | None = None) -> None:
    """上报「当前在做什么」的人话文字（进度条旁边那行）。

    和 `_progress` 分开：进度是给条子看的，文字是给人看的。
    只有百分比时，用户看到 45% 卡两分钟完全不知道在干什么。
    """
    try:
        from app.workers.queue import get_queue

        get_queue().set_item_stage(item_id, message, progress)
    except Exception:  # noqa: BLE001
        pass


def _fmt_eta(seconds: float) -> str:
    """'1m40s' / '35s' —— 估算剩余时间。"""
    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m{total % 60:02d}s"


def _local_media_path(media_ref: str) -> str:
    """`file:<路径>` 或裸绝对路径 → 本地文件路径；不是本地文件就返回空串。

    上传接口在收文件时就抽好 MP3 并写进了 `items.audio_path`，于是 ASR 走
    「复用已归档音频」这条捷径——`ASRResult` 里没有 `video_path`，
    收尾的 `finalize()` 拿到空串什么也删不掉，`cache/incoming` 里就堆下
    一份原始文件（每传一次漏一份，5.8MB 起）。这里把路径补回去。
    """
    raw = (media_ref or "").strip()
    if raw.startswith("file:"):
        candidate = Path(raw[5:])
    elif raw and not raw.startswith("http"):
        candidate = Path(raw)
    else:
        return ""
    return str(candidate) if candidate.is_file() else ""


def _read_cookie(platform: str) -> str | None:
    """按平台读取登录态（从 secrets.json 读，界面上的配置项已移除）。

    设置页的「登录态 Cookie」模块已按用户要求删掉，但**读取能力保留** ——
    已存进 secrets.json 的登录态继续生效，否则抖音会彻底抓不到内容
    （实测：无 Cookie 时抖音 SSR 只返回空壳，连一句文案都拿不到；
    有 Cookie 才能拿到标题 + 文案 + 视频直链，进而转写口播台词）。

    B站沿用 Netscape 格式的 cookie_file；抖音/快手/小红书的 Cookie 是浏览器里
    复制出来的原始请求头字符串，存在 secrets.json，按平台分开。
    之前所有平台共用 B站那份 cookie_file —— 对抖音等于没有，
    但错误提示却一直写着「可填 Cookie 后重试」，属于开空头支票。
    """
    cfg = get_config()
    if platform == "bilibili":
        # 优先用已存的 Cookie（secrets.json 里的 bilibili_cookie），
        # 没存再回退到 Netscape 格式的 cookie_file（插件导出的 cookies.txt）。
        from app.core.config import read_secret

        sec = (read_secret("bilibili_cookie") or "").strip()
        if sec:
            return sec
        if not cfg.collect.cookie_file:
            return None
        from app.adapters.bilibili import parse_cookie_file

        return parse_cookie_file(str(cfg.collect.cookie_file))
    if platform in ("douyin", "kuaishou", "xiaohongshu"):
        from app.core.config import read_secret

        return read_secret(f"{platform}_cookie") or None
    return None


def _try_parser_api(url: str) -> Any:
    """第三方解析兜底。未启用时返回 None（调用方据此区分「没试」和「试了失败」）。"""
    try:
        from app.services import parser_api

        if not parser_api.available():
            return None
        return parser_api.parse(url)
    except Exception as exc:  # noqa: BLE001
        from app.adapters.base import ExtractResult

        return ExtractResult(
            ok=False, extractor="parser_api", error=f"第三方解析异常：{exc}"[:200]
        )


def handle_extract(item_id: int) -> None:
    """抓取任务。"""
    ingest = get_ingest_service()
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
        platform, cookie=_read_cookie(platform), timeout=cfg.collect.web_timeout
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

    try:
        result = adapter.extract(url)
    finally:
        # 适配器内部的 httpx.Client 带连接池 + TLS 上下文，
        # 用完必须 close，否则每次采集泄漏一个 client（长时间跑会耗尽句柄）。
        try:
            adapter.close()
        except Exception:  # noqa: BLE001
            pass
    _progress(item_id, 0.35)

    if not result.ok:
        primary_error = result.error or "抓取失败"
        # 平台不给匿名请求返回正文时，可选地交给第三方解析兜底。
        # 没开启就什么都不做 —— 不能悄悄把用户链接发给外部服务。
        fallback = _try_parser_api(url)
        if fallback is not None and fallback.ok:
            result = fallback
        else:
            detail = primary_error
            if fallback is not None and fallback.error:
                detail = f"{primary_error}；第三方解析：{fallback.error}"
            detail = f"{detail} {_IMPORT_HINT}"
            ingest.transition(
                item_id,
                "extract_failed",
                error_reason="extract_failed",
                error_detail=detail,
            )
            _notify_failed(item, detail)
            return

    # 抓到了「标题 + 一句 og 摘要」也返回 ok=True 的老路径到此为止。
    # 这类结果不是完整正文，入库只会多一条点开空空的素材 —— 直接退出，
    # 明确告诉用户去导入视频/音频。平台 SSR 给出的完整文案（哪怕只有一句
    # caption）标 complete=True，不受影响。
    if not result.complete:
        detail = (
            f"这个链接只抓到标题/简介，没有完整正文（{result.extractor}）。"
            f"{_IMPORT_HINT}"
        )
        ingest.transition(
            item_id,
            "extract_failed",
            error_reason="thin_content",
            error_detail=detail,
        )
        _notify_failed(item, detail)
        return

    # 解析结果常带着无水印视频直链（extra.media）。以前它只被记下来就丢了，
    # ASR 仍拿原始分享链接去跑 yt-dlp，而抖音要登录、快手短链常失效 →
    # 口播台词永远转不出来。现在：有直链 + 正文只是标题 → 直接转写这条直链。
    media = [str(m) for m in (result.extra or {}).get("media") or [] if m]
    media_url = next((m for m in media if m.startswith("http")), None)
    thin_text = len((result.text or "").strip()) < _TRANSCRIPT_MIN_CHARS
    # 勾了「采集音频」→ 强制转写（即使文本够长也下载音频，把口播和 MP3 一起归档）
    collect_audio = bool(item.get("collect_audio"))
    need_transcript = collect_audio or bool(result.needs_asr) or (
        bool(media_url) and platform in _SHORT_VIDEO and thin_text
    )

    # 需要 ASR：先落 A 层（不含字幕），把媒体地址存好，再排 asr job
    if need_transcript:
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
            media_url=media_url,
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
    """采集完成 → 停在「已采集」等用户核实（点「入库」才进「未加工」）。

    v0.10.1：以前采集完直接进 raw_ready（未加工），用户没有核实环节；
    现在按用户要求拆成两段：已采集（可删除/重新解析/入库）→ 点入库 → 未加工。

    **坑**：`ingest.transition()` 会把 error_reason 清成 None（默认值）。
    所以这里**必须把已有的错误原因带过去** —— 否则 ASR 失败的原因
    （"这段音频没有人声"之类）在转场时就被抹掉了。
    """
    ingest = get_ingest_service()
    item = ingest.get_item(item_id) or {}
    cur = str(item.get("status") or "")
    if cur == "extracted":
        # 已经在「已采集」，不重复 transition（同状态不是合法迁移）
        _progress(item_id, 0.5)
        return
    try:
        ingest.transition(
            item_id,
            "extracted",
            error_reason=item.get("error_reason"),
            error_detail=item.get("error_detail"),
        )
    except Exception:  # noqa: BLE001
        pass
    _progress(item_id, 0.5)


def _back_to_pending(item_id: int, detail: str) -> None:
    """采集失败 → 退回「待采集」。

    本地模型读不到字幕/音频时，条目不能停在「已采集」——
    那会让用户以为采集成功了，点开却只有一句简介。
    这里把它落到 extract_failed（前端采集队列的「待采集」组），
    保留失败原因，界面上是红色失败态，可以重新采集或删除。
    """
    ingest = get_ingest_service()
    item = ingest.get_item(item_id) or {}
    try:
        ingest.transition(
            item_id,
            "extract_failed",
            error_reason=str(item.get("error_reason") or "asr_failed"),
            error_detail=(detail or "未能读取字幕/音频")[:500],
        )
    except Exception:  # noqa: BLE001
        pass
    _progress(item_id, 0.5)


def handle_asr(item_id: int) -> None:
    """ASR 转写任务：媒体 → 归档 MP3 → faster-whisper → 写回 A 层。

    三条路径，优先级从高到低：
    1. `items.audio_path` 已有归档 MP3 → 直接转写（重试不用重新下载/转码）
    2. `items.media_url` → 下载 + 抽 MP3 归档 → 转写（解析出的直链/用户上传）
    3. 原始链接 + yt-dlp（抖音/快手基本走不通：要登录、短链失效）

    转写过程中通过 `on_progress` 实时报「识别语音 N 段 · 预计还需 X」，
    而不是让进度条卡在 45% 一动不动。
    """
    ingest = get_ingest_service()

    item = ingest.get_item(item_id)
    if item is None:
        return

    url = str(item.get("source_url") or "")
    media_ref = str(item.get("media_url") or "").strip()
    archived_audio = str(item.get("audio_path") or "").strip()
    audio_hash = str(item.get("audio_hash") or "").strip()
    if not url and not media_ref and not archived_audio:
        return

    from app.services.asr_service import get_asr_service

    asr_svc = get_asr_service()

    def on_progress(done: int, total: float, pos: float) -> None:
        if total > 0:
            ratio = min(1.0, max(0.0, pos / total))
            _stage(
                item_id,
                f"识别语音 {done} 段 · {pos:.0f}/{total:.0f}s · "
                f"预计还需 {_fmt_eta(total - pos)}",
                0.45 + 0.25 * ratio,
            )
        else:
            _stage(item_id, f"识别语音 第 {done} 段…")

    if archived_audio and Path(archived_audio).exists():
        _stage(item_id, "复用已归档音频，开始识别…", 0.45)
        result = asr_svc.transcribe_file(archived_audio, on_progress=on_progress)
        result.audio_path = archived_audio
        result.audio_hash = audio_hash
        # 原始上传文件还在 cache/incoming 等着（OCR 抽帧要用），
        # 补回路径，收尾时 finalize 才知道该删谁。
        result.video_path = _local_media_path(media_ref)
    else:
        _stage(item_id, "下载并抽取音频（统一存 MP3）…", 0.43)
        if media_ref:
            referer = _MEDIA_REFERERS.get(str(item.get("platform") or ""))
            result = asr_svc.transcribe_media(
                media_ref, item_id=item_id, referer=referer, on_progress=on_progress
            )
        else:
            result = asr_svc.transcribe(url, item_id=item_id, on_progress=on_progress)

    platform = str(item.get("platform") or "")

    if not result.ok:
        # 转写失败（本地模型读不到字幕/音频）→ **退回「待采集」**。
        #
        # 以前这里直接 _mark_raw_ready，条目就停在「已采集」了 ——
        # 用户看到的是"采集成功"，但正文里只有一句简介，字幕根本没读出来，
        # 也不知道该怎么通过的。现在退回 extract_failed（前端归入「待采集」组），
        # 错误原因保留，界面上是红色失败态，可以重新采集/删除。
        #
        # 例外：原文本身有实质内容（平台 SSR 给的完整文案）时，
        # 转写失败不影响这条素材可用 —— 那就保留「已采集」，
        # 只是标记 asr_failed 让用户知道音频没读出来。
        #
        # 音频若已归档，照样落库：它是人耳复核的载体，转写失败也有价值。
        if result.audio_path and Path(result.audio_path).exists():
            ingest.update_item(
                item_id,
                audio_path=result.audio_path,
                audio_hash=result.audio_hash or None,
                audio_ms=result.duration_ms or None,
            )
        detail = (result.error or "ASR 转写失败")[:500]
        # 已有正文（平台 SSR 给的完整文案）才有保留价值 ——
        # 正文得看 item.raw_text，不是 result.text（失败时那是空的转写结果）。
        has_real_text = (
            len(str(item.get("raw_text") or "").strip()) >= _TRANSCRIPT_MIN_CHARS
        )
        ingest.update_item(
            item_id,
            extractor=f"{item.get('extractor')}+asr_failed",
            error_reason="asr_failed",
            error_detail=detail,
        )
        if has_real_text:
            _stage(item_id, "转写未成功，检查是否需要识别画面文字…", 0.7)
            if not enqueue_ocr_for_empty_asr(item_id, platform, result.video_path):
                _finish_media(item_id, result.video_path)
            _mark_raw_ready(item_id)
            return
        # 字幕也没读到 → 采集失败，退回待采集
        _stage(item_id, "转写未成功，退回待采集…", 0.7)
        if not enqueue_ocr_for_empty_asr(item_id, platform, result.video_path):
            _finish_media(item_id, result.video_path)
        _back_to_pending(item_id, detail)
        return

    # 文本写回 A 层 md（asr: true）
    # 双保险：空转写绝不覆盖已有正文（否则下游 AI 会拿到 0 字）
    if not str(result.text or "").strip():
        detail = "转写结果为空，未覆盖原有正文"
        has_real_text = len(str(item.get("raw_text") or "").strip()) >= _TRANSCRIPT_MIN_CHARS
        ingest.update_item(
            item_id,
            error_reason="asr_empty",
            error_detail=detail,
        )
        if has_real_text:
            _stage(item_id, "转写结果为空，检查是否需要识别画面文字…", 0.7)
            if not enqueue_ocr_for_empty_asr(item_id, platform, result.video_path):
                _finish_media(item_id, result.video_path)
            _mark_raw_ready(item_id)
            return
        _stage(item_id, "转写结果为空，退回待采集…", 0.7)
        if not enqueue_ocr_for_empty_asr(item_id, platform, result.video_path):
            _finish_media(item_id, result.video_path)
        _back_to_pending(item_id, detail)
        return

    _stage(item_id, f"识别完成（{len(result.segments)} 段 / {len(result.text)} 字），整理文本…", 0.7)

    # 归档元数据落库：音频路径、哈希（去重锚点）、时长（进度 ETA 用）
    if result.audio_path or result.audio_hash or result.duration_ms:
        ingest.update_item(
            item_id,
            audio_path=result.audio_path or None,
            audio_hash=result.audio_hash or None,
            audio_ms=result.duration_ms or None,
        )

    # 分段落盘（带时间戳）。播放器跳转 / SRT 导出 / 重新校对全靠它。
    if result.segments and result.audio_hash:
        try:
            from app.services.media_service import TranscribeSegment, get_media_service

            get_media_service().save_segments(
                result.audio_hash,
                [TranscribeSegment(**s) for s in result.segments],
                {
                    "title": str(item.get("title") or ""),
                    "item_id": item_id,
                    "model": result.model,
                    "text": result.text,
                },
            )
        except Exception:  # noqa: BLE001
            pass

    inbox_path = item.get("inbox_path")
    if inbox_path:
        path = Path(inbox_path)
        if path.exists():
            note = vault_mod.read_note(path)
            if note is not None:
                fm = dict(note.frontmatter)
                fm["asr"] = True
                fm["extractor"] = "media_asr" if media_ref else "ytdlp_asr"
                if result.audio_hash:
                    fm["audio_hash"] = result.audio_hash
                if result.duration_ms:
                    fm["audio_duration"] = f"{result.duration_ms // 60000}:{result.duration_ms // 1000 % 60:02d}"
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
        extractor="media_asr" if media_ref else "ytdlp_asr",
        asr=True,
    )
    _notify_asr_done(str(item.get("title") or ""))
    _stage(item_id, "写入笔记…", 0.74)
    # v31：转写完成 → 原文就绪，不自动跑 AI。
    # OCR 视配置决定是否补画面文字（默认只在 ASR 空结果时跑，这里不会触发）。
    if not _maybe_enqueue_ocr(item_id, platform, result.video_path):
        _finish_media(item_id, result.video_path)
    _mark_raw_ready(item_id)


def handle_ocr(item_id: int) -> None:
    """画面文字识别：视频帧 → 带时间码的文本，补进 A 层正文。

    只有在 ASR 没转出东西（或配置成 always）时才会排进来。
    结果作为独立段落 `## 画面文字（OCR）` 追加，**不覆盖** ASR 正文 ——
    OCR 有噪声（水印、UI、图表数字），混进正文会让下游 AI 摘要跑偏。
    """
    ingest = get_ingest_service()
    item = ingest.get_item(item_id)
    if item is None:
        return

    platform = str(item.get("platform") or "web")
    media_ref = str(item.get("media_url") or "").strip()
    if not media_ref:
        detail = "没有可用的视频来源，无法识别画面文字"
        ingest.update_item(
            item_id,
            error_reason="ocr_skipped",
            error_detail=detail,
        )
        if len(str(item.get("raw_text") or "").strip()) < _TRANSCRIPT_MIN_CHARS:
            _back_to_pending(item_id, detail)
            return
        _mark_raw_ready(item_id)
        return

    from app.services.media_service import get_media_service

    media = get_media_service()
    referer = _MEDIA_REFERERS.get(platform)
    _stage(item_id, "准备视频画面…", 0.76)
    ok, local, error = media.resolve_local(media_ref, item_id=item_id, referer=referer)
    if not ok or not local:
        detail = (error or "视频不可读")[:400]
        ingest.update_item(
            item_id,
            error_reason="ocr_failed",
            error_detail=detail,
        )
        if len(str(item.get("raw_text") or "").strip()) < _TRANSCRIPT_MIN_CHARS:
            _back_to_pending(item_id, detail)
            return
        _mark_raw_ready(item_id)
        return

    from app.services.ocr_service import get_ocr_service

    ocr_svc = get_ocr_service()

    def on_progress(done: int, total: int) -> None:
        if total > 0:
            _stage(
                item_id,
                f"识别画面文字 {done}/{total} 帧",
                0.76 + 0.12 * min(1.0, done / total),
            )

    _stage(item_id, "识别画面文字…", 0.76)
    result = ocr_svc.transcribe_video(
        local,
        duration_ms=int(item.get("audio_ms") or 0),
        on_progress=on_progress,
        asr_text=str(item.get("raw_text") or ""),
    )

    if not result.ok:
        detail = (result.error or "画面文字识别失败")[:400]
        ingest.update_item(
            item_id,
            error_reason="ocr_failed",
            error_detail=detail,
        )
        _finish_media(item_id, local)
        # 正文也是空的话，ASR 和 OCR 两条路都没读出东西 → 退回待采集
        if len(str(item.get("raw_text") or "").strip()) < _TRANSCRIPT_MIN_CHARS:
            _back_to_pending(item_id, detail)
            return
        _mark_raw_ready(item_id)
        return

    # 带时间码的结果落盘（工件目录按音频哈希归档；没有音频时按条目编号）
    bucket = str(item.get("audio_hash") or f"item{item_id}")
    try:
        import json

        target = media.dir("ocr") / f"{bucket}.json"
        target.write_text(
            json.dumps(
                {
                    "item_id": item_id,
                    "frames": result.frames,
                    "llm_fixed": result.llm_fixed,
                    "lines": [line.as_dict() for line in result.lines],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass

    existing = str(item.get("raw_text") or "").strip()
    section = "## 画面文字（OCR）\n" + result.text
    new_text = f"{existing}\n\n{section}" if existing else section

    _stage(item_id, "把画面文字写进笔记…", 0.9)

    inbox_path = item.get("inbox_path")
    written = False
    if inbox_path:
        note_file = Path(inbox_path)
        if note_file.exists():
            note = vault_mod.read_note(note_file)
            if note is not None:
                fm = dict(note.frontmatter)
                fm["ocr"] = True
                fm["extractor"] = f"{item.get('extractor') or 'media'}+ocr"
                body = note.body
                marker = "## 字幕/正文全文"
                if marker in body:
                    head, _, tail = body.partition(marker)
                    _, _, source_part = tail.partition("## 来源")
                    body = f"{head}{marker}\n{new_text}\n\n## 来源{source_part}"
                else:
                    body = f"{body}\n\n{section}\n"
                vault_mod.write_note(note_file, fm, body)
                written = True

    if not written:
        # 没有可写的 A 层文件时新建一个，保证正文可查
        note_file = ingest.write_inbox(
            url=str(item.get("source_url") or ""),
            platform=platform,
            title=str(item.get("title") or f"画面文字-{item_id}"),
            text=new_text,
            extractor="ocr",
            asr=False,
        )
        ingest.update_item(item_id, inbox_path=note_file.as_posix())

    ingest.update_item(
        item_id,
        raw_text=new_text,
        extractor=f"{item.get('extractor') or 'media'}+ocr",
        # **空串 = 清空**（update_item 里 None 表示"这个字段不动"）。
        # 原来传 None，OCR 明明成功了，error_reason 还留着 asr_failed，
        # 界面一直显示红色失败标记。
        error_reason="",
        error_detail="",
    )
    _finish_media(item_id, local)
    _mark_raw_ready(item_id)


def _finish_media(item_id: int, video_path: str) -> None:
    """收尾：按配置决定原视频去留（删掉 / 移到 media/video）。

    **必须在 ASR 和 OCR 都跑完之后调**。ASR 一完成就删，
    OCR 就没画面可抽帧了 —— 而且这个 bug 表现成「OCR 偶尔为空」，
    很难查。
    """
    try:
        from app.services.media_service import get_media_service

        item = get_ingest_service().get_item(item_id) or {}
        bucket = str(item.get("audio_hash") or f"item{item_id}")
        get_media_service().finalize(video_path, bucket)
    except Exception:  # noqa: BLE001
        pass


def _maybe_enqueue_ocr(item_id: int, platform: str, video_path: str) -> bool:
    """按配置决定是否排 OCR 任务。返回是否排了。

    默认 `auto_trigger = asr_empty`：只有 ASR 没转出东西才 OCR。
    抖音上大量视频是「口播 + 大字字幕」，那种情况下 OCR 拿到的是
    和 ASR 一模一样的话 —— 纯冗余，还多花十几秒。

    B站默认跳过：官方字幕 API 已经给了高质量文本，OCR 无增益。
    """
    try:
        cfg = get_config()
    except Exception:  # noqa: BLE001
        return False

    if not cfg.ocr.enabled or cfg.ocr.auto_trigger == "off":
        return False
    skip = {p.strip() for p in str(cfg.ocr.skip_platforms or "").split(",") if p.strip()}
    if platform in skip:
        return False
    if cfg.ocr.auto_trigger != "always":
        return False  # asr_empty 由失败分支单独触发

    # 没有视频就没有帧，OCR 无从下手
    if not video_path or not Path(video_path).exists():
        return False
    _enqueue(item_id, "ocr", priority=6)
    return True


def enqueue_ocr_for_empty_asr(item_id: int, platform: str, video_path: str) -> bool:
    """ASR 空结果/失败时的 OCR 入口（受 auto_trigger 约束）。"""
    try:
        cfg = get_config()
    except Exception:  # noqa: BLE001
        return False
    if not cfg.ocr.enabled or cfg.ocr.auto_trigger == "off":
        return False
    skip = {p.strip() for p in str(cfg.ocr.skip_platforms or "").split(",") if p.strip()}
    if platform in skip:
        return False
    if not video_path or not Path(video_path).exists():
        return False
    _enqueue(item_id, "ocr", priority=6)
    return True


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
    queue.register("ocr", handle_ocr)
    queue.register("ai", handle_ai)
    queue.register("embed", handle_embed)
    queue.register("reindex", handle_reindex)
