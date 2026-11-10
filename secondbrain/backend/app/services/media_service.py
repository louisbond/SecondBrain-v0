"""媒体归档：视频/音频 → 统一 MP3 + 内容寻址存储。

## 为什么统一存 MP3

文本是降维产物：语气、停顿、重音、情绪、口音全部丢失。
MP3 是「人耳复核」的唯一载体，也是换模型重转写的原料。

10 分钟 128kbps 单声道约 9.6MB，是原视频的 5%，但换来：
- 换 medium 模型重转写不用求人重发视频
- 能在笔记页里直接听语气校对
- 能导出给任何播放器 / 剪辑软件
- 链接失效后内容仍然完整

## 为什么内容寻址

路径用 `<sha256>.mp3` 而不是 `item<N>_upload.mp3`：
- **跨源去重**：同一个视频从 URL 下载 vs 用户上传，抽出的音频字节相同
  → 哈希相同 → 命中同一条素材。按 URL 去重做不到（短链变体、不同分享入口）。
- **改名/移动不断链**：用户重命名本地文件不影响归档。
- **不会互相覆盖**：老代码用 item_id 命名，同一条目重传直接盖掉上一份。

## 目录约定

    data/media/audio/<hash>.mp3     归档音频（持久）
    data/media/asr/<hash>.json      ASR 分段（带时间戳）
    data/media/ocr/<hash>.json      OCR 结果（带时间戳）
    data/media/frames/<hash>/       采样帧（仅在调试/重跑时存在）
    data/media/video/<hash>.mp4     保留的原视频（keep_video = true 时）

`data/cache/incoming/` 是**临时**下载区，可随时清空；
`data/media/` 是**归档**区，清缓存不能碰。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import get_config

# 与 items 表保持一致的媒体后缀白名单
MEDIA_EXTS = (".mp4", ".m4a", ".mp3", ".webm", ".mov", ".wav", ".flv", ".mkv", ".aac")


def _max_bytes() -> int:
    """单文件上限（与上传共用 `[media] max_file_mb`）。"""
    return max(1, int(get_config().media.max_file_mb)) * 1024 * 1024


@dataclass
class AudioArchive:
    """一次「媒体 → MP3」的结果。"""

    ok: bool
    audio_path: str = ""
    audio_hash: str = ""
    duration_ms: int = 0
    # 原视频/音频在本地临时区的位置（OCR 抽帧要用，用完由 finalize 决定去留）
    video_path: str = ""
    error: str = ""
    # 命中了已有归档（同一段音频之前处理过）
    reused: bool = False


@dataclass
class TranscribeSegment:
    """一条带时间戳的转写结果。"""

    start: float
    end: float
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {"start": round(self.start, 2), "end": round(self.end, 2), "text": self.text}


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """流式计算文件 sha256（200MB 也不占内存）。"""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _parse_bitrate(value: str) -> int:
    """'128k' / '128000' → 128000。"""
    text = str(value or "").strip().lower()
    if text.endswith("k"):
        try:
            return int(float(text[:-1]) * 1000)
        except ValueError:
            return 128000
    try:
        return int(text)
    except ValueError:
        return 128000


class MediaService:
    """媒体下载、音频归档、内容寻址去重。"""

    def __init__(self) -> None:
        self.cfg = get_config()

    # -- 目录 -------------------------------------------------------------
    def dir(self, kind: str) -> Path:
        """取某类归档目录并确保存在。kind ∈ audio/asr/ocr/frames/video。"""
        target = self.cfg.media_dir / kind
        target.mkdir(parents=True, exist_ok=True)
        return target

    def incoming_dir(self) -> Path:
        target = self.cfg.cache_dir / "incoming"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def audio_path(self, audio_hash: str) -> Path:
        return self.dir("audio") / f"{audio_hash}.mp3"

    # -- 下载 -------------------------------------------------------------
    def resolve_local(
        self, media_ref: str, item_id: int | None = None, referer: str | None = None
    ) -> tuple[bool, str, str]:
        """把 media_ref 变成可读的本地文件。

        支持三种输入：
        - `file:<绝对路径>`：用户上传后落盘的位置，直通
        - 裸绝对路径：同上（兼容老数据）
        - http(s)：流式下载到 cache/incoming，带 Referer 和大小上限

        返回 (ok, 本地路径, 错误)。
        """
        import httpx

        raw = (media_ref or "").strip()
        if not raw:
            return False, "", "媒体地址为空"

        if raw.startswith("file:"):
            local = Path(raw[5:])
        elif not raw.startswith("http"):
            local = Path(raw)
        else:
            local = None

        if local is not None:
            if local.exists():
                return True, str(local), ""
            return False, "", f"本地媒体文件不存在：{local}"

        # 同一 URL 复用已下载的临时文件（断点续跑）
        digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=8).hexdigest()[:16]
        prefix = f"media{item_id}_{digest}" if item_id else f"media_{digest}"
        for candidate in sorted(self.incoming_dir().glob(f"{prefix}.*")):
            if candidate.is_file() and candidate.stat().st_size > 1024:
                return True, str(candidate), ""

        suffix = ".mp4"
        match = re.search(r"\.(mp4|m4a|mp3|webm|mov|wav|flv|mkv|aac)(?:\?|$)", raw, re.I)
        if match:
            suffix = "." + match.group(1).lower()

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        }
        if referer:
            headers["Referer"] = referer

        target = self.incoming_dir() / f"{prefix}{suffix}"
        try:
            from app.core.http import make_client

            with make_client(
                raw, timeout=httpx.Timeout(10.0, read=180.0), headers=headers
            ) as client, client.stream("GET", raw) as resp:
                if resp.status_code != 200:
                    return False, "", f"媒体下载失败：HTTP {resp.status_code}"
                written = 0
                limit_mb = max(1, int(get_config().media.max_file_mb))
                with target.open("wb") as fh:
                    for chunk in resp.iter_bytes(65536):
                        written += len(chunk)
                        if written > _max_bytes():
                            fh.close()
                            target.unlink(missing_ok=True)
                            return (
                                False,
                                "",
                                f"文件超过 {limit_mb}MB 上限，请剪辑后再试",
                            )
                        fh.write(chunk)
        except Exception as exc:  # noqa: BLE001
            target.unlink(missing_ok=True)
            return False, "", f"媒体下载失败：{type(exc).__name__} {str(exc)[:120]}"
        return True, str(target), ""

    # -- 抽音频 -----------------------------------------------------------
    def extract_audio(self, source: str) -> tuple[bool, str, int, str]:
        """任意媒体 → 单声道 MP3。返回 (ok, 临时mp3路径, 时长ms, 错误)。

        用 PyAV 直接解码 + libmp3lame 编码，**不需要 ffmpeg 二进制**。
        重采样到配置的采样率/声道，码率由 `[media] audio_bitrate` 决定。

        采样率不按 ASR 的 16kHz 存：识别要 16k，但人耳听会明显发闷。
        44.1kHz 是语音存档的甜点，10 分钟约 9.6MB。
        """
        import av

        src = Path(source)
        if not src.exists():
            return False, "", 0, f"媒体文件不存在：{source}"

        bitrate = _parse_bitrate(self.cfg.media.audio_bitrate)
        rate = int(self.cfg.media.audio_sample_rate or 44100)
        target = self.incoming_dir() / f"{src.stem}_audio.mp3"

        try:
            with av.open(str(src)) as inp:
                audio_streams = [s for s in inp.streams if s.type == "audio"]
                if not audio_streams:
                    return False, "", 0, "这个文件里没有音轨（无声视频或纯图片）"
                stream = audio_streams[0]

                with av.open(str(target), mode="w", format="mp3") as out:
                    encoder = out.add_stream("libmp3lame", rate=rate)
                    encoder.bit_rate = bitrate
                    encoder.layout = "mono"

                    resampler = av.AudioResampler(
                        format="s16p", layout="mono", rate=rate
                    )
                    samples = 0
                    for frame in inp.decode(stream):
                        # decode() 的静态类型是 VideoFrame|AudioFrame|SubtitleSet，
                        # 这里只要音频帧（字幕轨混进来时直接跳过）。
                        if not isinstance(frame, av.AudioFrame):
                            continue
                        # 容器没给 duration 时，用实际解出的采样数兜底
                        samples += int(frame.samples or 0)
                        for resampled in resampler.resample(frame):
                            for packet in encoder.encode(resampled):
                                out.mux(packet)
                    # 冲刷重采样器与编码器缓冲区
                    for resampled in resampler.resample(None):
                        for packet in encoder.encode(resampled):
                            out.mux(packet)
                    for packet in encoder.encode(None):
                        out.mux(packet)
        except Exception as exc:  # noqa: BLE001
            target.unlink(missing_ok=True)
            return False, "", 0, f"抽取音频失败：{type(exc).__name__} {str(exc)[:120]}"

        if not target.exists() or target.stat().st_size < 1024:
            target.unlink(missing_ok=True)
            return False, "", 0, "抽取音频失败：输出文件为空（可能是无声视频）"

        duration_ms = 0
        try:
            with av.open(str(target)) as probe:
                if probe.duration:
                    duration_ms = int(probe.duration / 1000)
        except Exception:  # noqa: BLE001
            pass
        if duration_ms <= 0 and samples and rate:
            duration_ms = int(samples / rate * 1000)
        return True, str(target), duration_ms, ""

    # -- 归档 -------------------------------------------------------------
    def archive(self, source: str) -> AudioArchive:
        """本地媒体 → 归档 MP3（内容寻址，已存在则复用）。"""
        ok, temp_mp3, duration_ms, error = self.extract_audio(source)
        if not ok:
            return AudioArchive(ok=False, video_path=str(source), error=error)

        temp = Path(temp_mp3)
        try:
            audio_hash = sha256_file(temp)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            return AudioArchive(
                ok=False, video_path=str(source), error=f"计算音频哈希失败：{exc}"
            )

        final = self.audio_path(audio_hash)
        reused = final.exists() and final.stat().st_size > 1024
        if reused:
            # 同一段音频之前就归档过（跨源去重命中），临时文件可以扔
            temp.unlink(missing_ok=True)
        else:
            try:
                temp.replace(final)
            except OSError:
                # 跨盘 replace 会失败，退回复制
                import shutil

                shutil.copy2(temp, final)
                temp.unlink(missing_ok=True)

        return AudioArchive(
            ok=True,
            audio_path=str(final),
            audio_hash=audio_hash,
            duration_ms=duration_ms,
            video_path=str(source),
            reused=reused,
        )

    def archive_ref(
        self, media_ref: str, item_id: int | None = None, referer: str | None = None
    ) -> AudioArchive:
        """media_ref（URL 或本地路径）→ 归档 MP3。"""
        ok, local, error = self.resolve_local(media_ref, item_id=item_id, referer=referer)
        if not ok:
            return AudioArchive(ok=False, error=error)
        return self.archive(local)

    # -- 去重 -------------------------------------------------------------
    def find_by_hash(self, audio_hash: str) -> dict[str, Any] | None:
        """按音频哈希找已有素材（跨 source_url 去重）。"""
        if not audio_hash:
            return None
        from app.core.db import get_db

        row = get_db().query_one(
            "SELECT id, title, status, inbox_path FROM items "
            "WHERE audio_hash = ? ORDER BY id LIMIT 1",
            (audio_hash,),
        )
        return dict(row) if row else None

    # -- 分段结果 ---------------------------------------------------------
    def segments_path(self, audio_hash: str) -> Path:
        return self.dir("asr") / f"{audio_hash}.json"

    def save_segments(
        self, audio_hash: str, segments: list[TranscribeSegment], meta: dict[str, Any]
    ) -> Path:
        """把带时间戳的分段落盘。

        **不能只存文本**：播放器跳转、SRT 导出、进度 ETA 全靠时间戳。
        存 JSON 而不是塞进 SQLite —— 一条 10 分钟视频有几百段，
        会把 DB 撑大且无查询需求。
        """
        target = self.segments_path(audio_hash)
        payload = {
            "audio_hash": audio_hash,
            "segments": [s.as_dict() for s in segments],
            **meta,
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return target

    def load_segments(self, audio_hash: str) -> dict[str, Any] | None:
        target = self.segments_path(audio_hash)
        if not target.exists():
            return None
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # -- 收尾 -------------------------------------------------------------
    def finalize(self, video_path: str, audio_hash: str) -> str:
        """处理全部完成后决定原视频去留。返回处理结果标签。

        - keep_video = true → 移到 media/video/<hash>.mp4 永久保留
        - 否则删除（MP3 + 分段 JSON 已经够了）

        **调用时机必须是 OCR 之后**。ASR 一完成就删，OCR 就没画面可抽帧了，
        而且这个 bug 表现成「OCR 偶尔为空」，很难查。
        """
        src = Path(video_path or "")
        # 空串会变成 `Path(".")`——存在且是个目录，unlink 必然失败。
        # 直接判 missing，别让调用方以为「删过了」。
        if not video_path or not src.is_file():
            return "missing"
        if self.cfg.media.keep_video:
            target = self.dir("video") / f"{audio_hash}{src.suffix or '.mp4'}"
            if not target.exists():
                try:
                    src.replace(target)
                    return "kept"
                except OSError:
                    return "kept-left"
            src.unlink(missing_ok=True)
            return "kept"
        try:
            src.unlink()
            return "deleted"
        except OSError:
            return "delete-failed"


_media: MediaService | None = None


def get_media_service() -> MediaService:
    global _media
    if _media is None:
        _media = MediaService()
    return _media
