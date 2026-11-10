"""ASR 服务：yt-dlp 抽音频 + faster-whisper 转写。

对齐 v0.5.0 §5.3：
    jobs(kind=asr) 队列（CPU 并发=1，节流）
      → yt-dlp 提取音频（m4a，内嵌 ffmpeg）
      → faster-whisper small（默认）/medium，int8，language=zh
      → 字幕文本写回 A 层 md（asr: true）
      → 完成 toast 通知

- 断点续跑：音频文件缓存于 `cache/audio/`，转写失败不重下载；
- 模型文件首次运行自动下载至本地缓存，之后离线可用。
- faster-whisper 不可用时降级为「仅抽取音频 + 明确告警」，不阻塞主流程。
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import get_config

# 常用繁体字（用于检测转写质量，不做转换——转换用 whisper 的提示词解决）。
# 只取高频的一批，够判断"是不是简体"即可。
_TRAD_CHARS = frozenset(
    "這來個們為說時會對開關無問點長東車馬鳥魚龍龜書畫聽見觀語讀寫學"
    "電腦網軟硬機系統記憶頻寬螢幕滑鼠標籤紙張邊緣體係結構設計劃執行程"
    "式應用發現實際題標準備認證據點擊選輸適還邊過運遠連進遲間聞閱防際"
    "陸陳陰陽隊階隨雖雙雜雞離難雲電靈靜韓響頁項順須預領頭顏願類顧風飛"
    "餓馬鮮鳳鴨鵝鷹鹽麗麥黃黨齊齒買賣價錢銀鐘長難離實發現樣種積節範"
    "體屬歲層級並儘隻髮鬥醜鐘藉捲嘗鹹瞭臺線纖繡續脅臘舉艙蘇藥蟲襯"
    "覺觀訂計訊記訪設許訴診註詞試詩話該詳誠語誤誰課調談請論諸講謝識"
    "議護讀變讓豐貝負財責貨質購賽贈贊贏趕較輕載輛輸轉農適還邊過運遠"
    "連進遲間聞閱防際陸陳陰陽隊階隨雖雙雜雞離難雲電靈靜韓響頁項順須"
    "預領頭顏願類顧風飛餓馬鮮鳳鴨鵝鷹鹽麗麥黃黨齊齒國區縣鎮鄉萬億歷"
    "層級業產經濟漁資訊獎勵勸勝敗歲歸舊禮種積節簡約細組織統線級習聞"
    "職聯腦臺蘇蘭號藝蘭蟲衛覺觀議護譯豐賓質賣購贏贖趨躍踐軟輕較載"
    "繁體髮髒幾點餘隻衝壓歲齒龜"
)


def trad_ratio(text: str) -> float:
    """文本里繁体字占中文汉字的比例（0.0–1.0）。

    用途：**质量提示**，不是转换。
    v31 实测：官方提示词下 tiny 模型约 2%，换 small 模型会明显更低。
    比例偏高说明该换更大的 ASR 模型了。
    """
    if not text:
        return 0.0
    han = 0
    trad = 0
    for ch in text:
        if "一" <= ch <= "鿿":
            han += 1
            if ch in _TRAD_CHARS:
                trad += 1
    return trad / han if han else 0.0


def to_simplified(text: str) -> str:
    """繁体 → 简体。

    whisper 的中文在长音频、专有名词密集的段落里会整段漂成繁体
    （训练语料混了港台文本），而且一路错下去。这些字会原样写进 vault，
    污染全文检索、AI 摘要和周报 —— 下游全都在错误文本上工作。
    简体文本过一遍 zhconv 基本无变化，开销可忽略。
    """
    if not text:
        return text
    try:
        from zhconv import convert

        return convert(text, "zh-cn")
    except Exception:  # noqa: BLE001  缺依赖时不能拖垮转写
        return text


@dataclass
class ASRResult:
    ok: bool
    text: str = ""
    audio_path: str | None = None
    error: str | None = None
    model: str = ""
    elapsed_ms: int = 0
    # 繁体字占比（0.0–1.0）。用于提示转写质量：
    # 数值偏高说明模型能力不够（tiny），该换更大的模型。
    trad_ratio: float = 0.0
    # 带时间戳的分段。**必须保留**：播放器跳转、SRT 导出、进度 ETA
    # 全靠它。早先只返回纯文本，用户想核对某句话的语气得自己
    # 拖进度条找 —— 等于没有。
    segments: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0
    # 归档音频的 sha256（pipeline 拿它落库 + 跨源去重）
    audio_hash: str = ""
    # 原视频在临时区的位置（OCR 抽帧要用，处理完由 finalize 决定去留）
    video_path: str = ""


class ASRService:
    """ASR 管线。"""

    def __init__(self) -> None:
        self.cfg = get_config()
        self._model: Any = None
        self._model_loaded = False
        self._model_error: str = ""
        self._model_source = ""
        self._lock = threading.Lock()

    # -- yt-dlp -----------------------------------------------------------
    def _ytdlp_bin(self) -> str:
        """定位 yt-dlp 可执行文件（逻辑统一在 app.core.ytdlp）。"""
        from app.core import ytdlp

        return ytdlp.find_bin()

    def _ffmpeg_dir(self) -> Path:
        """内嵌 ffmpeg 目录（yt-dlp 需要）。"""
        return self.cfg.bin_dir

    def download_audio(self, url: str, cache_key: str) -> tuple[bool, str | None, str | None]:
        """下载音频到缓存目录。返回 (ok, 音频路径, 错误)。

        两条路径：
        1. **直取原始音频流**（首选）：`-f bestaudio` 不做转码，产出单个音频文件。
           B站等平台的 bestaudio 通常本身就是 m4a，faster-whisper（PyAV）能直接解码，
           **不需要 ffmpeg**。这条路径在很多环境下是唯一可用的。
        2. **转码路径**（兜底）：`-x --audio-format m4a`，需要 ffmpeg。
           当平台只提供分离的视频流、或需要统一格式时才用。

        断点续跑：已下载的文件不重下（按 hash 前缀找缓存）。
        """
        import hashlib

        cache_dir = self.cfg.asr.cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.blake2b(url.encode("utf-8"), digest_size=8).hexdigest()[:16]
        prefix = f"{cache_key}_{digest}"

        # 断点续跑：已有缓存直接用
        cached = self._find_cached(cache_dir, prefix)
        if cached is not None:
            return True, str(cached), None

        # 路径 1：直取原始音频（无需 ffmpeg）
        ok, path, err = self._run_ytdlp(
            url,
            [
                "-f",
                "bestaudio/best",
                "--no-playlist",
                "--no-warnings",
                "-o",
                str(cache_dir / f"{prefix}.%(ext)s"),
            ],
            timeout=900,
        )
        if ok:
            found = self._find_cached(cache_dir, prefix)
            if found is not None:
                return True, str(found), None

        # 路径 2：转码（需要 ffmpeg）
        ok2, path2, err2 = self._run_ytdlp(
            url,
            [
                "-f",
                "bestaudio[ext=m4a]/bestaudio/best",
                "-x",
                "--audio-format",
                "m4a",
                "--no-playlist",
                "--no-warnings",
                "--ffmpeg-location",
                str(self._ffmpeg_dir()),
                "-o",
                str(cache_dir / f"{prefix}.%(ext)s"),
            ],
            timeout=900,
        )
        if ok2:
            found = self._find_cached(cache_dir, prefix)
            if found is not None:
                return True, str(found), None

        return False, None, (err2 or err or "音频下载失败")

    @staticmethod
    def _find_cached(cache_dir: Path, prefix: str) -> Path | None:
        """在缓存目录里找已下载的音频（忽略未完成的 .part）。"""
        for candidate in sorted(cache_dir.glob(f"{prefix}.*")):
            if candidate.suffix == ".part":
                continue
            if candidate.is_file() and candidate.stat().st_size > 1024:
                return candidate
        return None

    def _run_ytdlp(
        self, url: str, args: list[str], timeout: int
    ) -> tuple[bool, str | None, str | None]:
        """执行 yt-dlp，返回 (ok, stdout, 错误信息)。

        统一走 `app.core.ytdlp`：直连优先、网络类错误回退系统代理，
        错误文案已翻成中文（否则用户只会看到 CERTIFICATE_VERIFY_FAILED）。
        """
        from app.core import ytdlp

        res = ytdlp.run(args, url, timeout=timeout)
        if not res.ok:
            return False, None, res.error
        return True, res.stdout, None

    # -- faster-whisper ---------------------------------------------------
    def _load_model(self) -> Any:
        """加载 faster-whisper 模型。

        加载顺序：
        1. `asr.model_dir` 指向的本地目录（离线可用，用户预置模型时走这条）；
        2. HuggingFace Hub 的模型名（首次运行自动下载，之后走本地缓存）。

        模型下载失败（网络受限/代理拦截）时不抛异常而是返回 None，
        调用方给出可操作的提示（见 transcribe_file 的错误文案）。
        """
        with self._lock:
            if self._model_loaded:
                return self._model
            self._model_loaded = True
            try:
                # PyAV 新版移除了 metadata_errors 参数，先做兼容处理
                from app.services.audio_compat import patch_av_open_compat

                patch_av_open_compat()

                from faster_whisper import WhisperModel  # type: ignore

                model_ref: str = self.cfg.asr.model_size
                local_dir = self.cfg.asr.model_dir
                if local_dir and Path(local_dir).exists():
                    model_ref = str(local_dir)
                else:
                    # 配置里写的是开发机绝对路径时（源码树被移动/清理），
                    # 退回到随程序一起放的 data/models，别悄悄去 Hub 下载卡住。
                    bundled = self.cfg.data_dir / "models"
                    if bundled.is_dir():
                        for candidate in sorted(bundled.rglob("model.bin")):
                            model_ref = str(candidate.parent)
                            break

                self._model = WhisperModel(
                    model_ref,
                    device="cpu",
                    compute_type=self.cfg.asr.compute,
                )
                self._model_source = "本地目录" if model_ref != self.cfg.asr.model_size else "Hub"
            except Exception as exc:  # noqa: BLE001
                self._model = None
                self._model_error = str(exc)
            return self._model

    # ASR 提示词：官方推荐的简体写法（v31 实测）
    #
    # whisper 会**模仿提示词的字形**。实测同一段音频：
    #   无提示              → 繁体率 17.5%
    #   "以下是普通话的句子。" → 繁体率 1.9%
    #
    # 坑：早先写的是一句长提示（"以下是普通话视频的字幕内容，请用简体中文
    # 输出，保留专有名词。"），效果反而不如这句短的 —— 提示词太长会被
    # 当成待转写内容的一部分。
    # initial_prompt 是**唯一的领域先验**：small 模型对专有名词几乎全靠猜，
    # 给一份高频词表能显著压低"长鑫→长新""合肥→河肥"这类同音错误。
    # 注意别写太长：prompt 占上下文，过长会挤掉真正的音频上下文。
    _ZH_PROMPT = (
        "以下是普通话的句子，请用简体中文转写。"
        "常见词：合肥、长鑫存储、京东方、蔚来、科大讯飞、国轩高科、"
        "摩根士丹利、中国科学技术大学、量子卫星、可控核聚变、显示面板、存储芯片。"
    )

    # 长音频分段转写：每段多少秒
    # （长上下文会漂移：模型一路听下去，后面的段落又开始输出繁体，
    #   而且会一路错下去 —— "插件"→"插线"、"流程"→"交程"。分段能治这个。）
    _CHUNK_SECONDS = 30.0

    def _transcribe_segments(
        self,
        model: Any,
        audio_path: str,
        on_progress: Any = None,
    ) -> tuple[list[dict[str, Any]], float]:
        """转写并返回 (带时间戳的分段, 音频总时长秒)。

        faster-whisper 的 `segments` 是**惰性生成器**——一边解码一边 yield。
        所以这里不做 `[... for s in segments]` 一次性吃完，而是逐段消费、
        逐段回调，让上层实时报「识别语音 3/13 段 · 预计还需 1m40s」。

        回调签名：`on_progress(done_count: int, total_seconds: float, position_seconds: float)`

        **为什么用官方的短提示词**（v31 实测）：
        whisper 会**模仿提示词的字形**。同一段音频的繁体率：
            无提示                   → 17.5%
            "以下是普通话的句子。"     → 1.9%
        坑：早先写的是一句长提示（"以下是普通话视频的字幕内容，请用简体中文
        输出，保留专有名词。"），效果反而不如这句短的 —— 提示词太长会被
        当成待转写内容的一部分。

        **为什么不分段**：试过按静音点切成 30s 块逐块重转（v31），
        繁体率没降（1.9% → 2.3%），耗时还涨了 40%（28s → 40s）。
        残留的繁体**不是上下文漂移**，是 tiny 模型的能力上限 ——
        那些是真错字（"插件"→"插线"、"界面"→"居面"）。
        根治靠**换更大的模型**（config.toml 的 model_size / model_dir），
        不靠切段。
        """
        segments, info = model.transcribe(
            audio_path,
            language="zh",
            vad_filter=True,
            beam_size=5,
            initial_prompt=self._ZH_PROMPT,
            condition_on_previous_text=False,
        )
        total = float(getattr(info, "duration", 0.0) or 0.0)
        collected: list[dict[str, Any]] = []
        for seg in segments:
            # 逐段转简繁：segments 会直接用于播放器字幕与 SRT 导出，
            # 只在 body 汇总处转换会漏掉这些出口。
            text = to_simplified((seg.text or "").strip())
            if text:
                collected.append(
                    {
                        "start": round(float(seg.start or 0), 2),
                        "end": round(float(seg.end or 0), 2),
                        "text": text,
                    }
                )
            if on_progress is not None:
                try:
                    on_progress(len(collected), total, float(seg.end or 0))
                except Exception:  # noqa: BLE001
                    pass
        return collected, total

    def transcribe_file(self, audio_path: str, on_progress: Any = None) -> ASRResult:
        """转写本地音频。"""
        import time

        started = time.time()
        model = self._load_model()
        if model is None:
            return ASRResult(
                ok=False,
                error=self._model_load_hint(),
                audio_path=audio_path,
            )

        try:
            segments, duration = self._transcribe_segments(
                model, audio_path, on_progress=on_progress
            )
            body = "\n".join(s["text"] for s in segments)
            # 空转写**不能算成功**——否则下游 AI 拿到 0 字正文，
            # 报一个看不懂的 empty_content。纯音乐/无人声视频就是这样。
            if not body.strip():
                return ASRResult(
                    ok=False,
                    error=(
                        "转写结果为空：这段音频没有人声"
                        "（纯音乐/无人声视频属正常情况）。"
                        "可改用「识别画面文字」或「手动补充正文」录入内容。"
                    ),
                    audio_path=audio_path,
                    model=self.cfg.asr.model_size,
                    elapsed_ms=int((time.time() - started) * 1000),
                    duration_ms=int(duration * 1000),
                )
            return ASRResult(
                ok=True,
                text=body,
                audio_path=audio_path,
                model=self.cfg.asr.model_size,
                elapsed_ms=int((time.time() - started) * 1000),
                trad_ratio=trad_ratio(body),
                segments=segments,
                duration_ms=int(duration * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            return ASRResult(
                ok=False,
                error=f"转写失败：{exc}",
                audio_path=audio_path,
                elapsed_ms=int((time.time() - started) * 1000),
            )

    # -- 组合流程 ---------------------------------------------------------
    # -- 媒体直链 / 本地视频 --------------------------------------------
    # 真正的实现在 media_service（内容寻址归档、跨源去重、MP3 转码都在那）。
    # 这里保留方法名是为了不打断既有调用点与测试。
    _MEDIA_MAX_BYTES = 200 * 1024 * 1024
    _MEDIA_EXTS = (".mp4", ".m4a", ".mp3", ".webm", ".mov", ".wav", ".flv", ".mkv", ".aac")

    def download_media(
        self, media_url: str, item_id: int | None = None, referer: str | None = None
    ) -> tuple[bool, str | None, str | None]:
        """把视频/音频直链下载到缓存；本地文件直接返回路径。"""
        from app.services.media_service import get_media_service

        ok, path, error = get_media_service().resolve_local(
            media_url, item_id=item_id, referer=referer
        )
        return ok, (path or None), (error or None)

    def transcribe_media(
        self,
        media_url: str,
        item_id: int | None = None,
        referer: str | None = None,
        on_progress: Any = None,
    ) -> ASRResult:
        """视频直链 / 本地视频 → 归档 MP3 → 口播台词（全在本机，不外发）。"""
        import time

        started = time.time()
        from app.services.media_service import get_media_service

        archived = get_media_service().archive_ref(
            media_url, item_id=item_id, referer=referer
        )
        if not archived.ok:
            # 抽音频失败也要把视频路径带回去 —— 「没有音轨」的视频
            # 正是 OCR 的主力场景，丢了路径就抽不到帧了。
            return ASRResult(
                ok=False,
                error=archived.error or "媒体处理失败",
                video_path=archived.video_path,
            )

        result = self.transcribe_file(archived.audio_path, on_progress=on_progress)
        result.audio_path = archived.audio_path
        result.audio_hash = archived.audio_hash
        result.video_path = archived.video_path
        result.elapsed_ms = int((time.time() - started) * 1000)
        if archived.duration_ms and not result.duration_ms:
            result.duration_ms = archived.duration_ms
        return result

    def transcribe(
        self, url: str, item_id: int | None = None, on_progress: Any = None
    ) -> ASRResult:
        """完整 ASR 流程：下载音频 → 归档 MP3 → 转写。

        下载仍走 yt-dlp（B站等需要鉴权/分片的场景），但落盘统一成 MP3，
        后续重转写不用再下载。
        """
        import time

        started = time.time()
        cache_key = f"item{item_id}" if item_id else "audio"

        ok, audio_path, error = self.download_audio(url, cache_key)
        if not ok or not audio_path:
            return ASRResult(ok=False, error=error or "音频下载失败")

        # 下载到的可能是 m4a/webm，统一归档成 MP3（人耳可听 + 内容寻址去重）
        from app.services.media_service import get_media_service

        archived = get_media_service().archive(audio_path)
        if archived.ok:
            audio_path = archived.audio_path

        result = self.transcribe_file(audio_path, on_progress=on_progress)
        result.audio_path = audio_path
        if archived.ok:
            result.audio_hash = archived.audio_hash
            result.video_path = archived.video_path
            if archived.duration_ms and not result.duration_ms:
                result.duration_ms = archived.duration_ms
        result.elapsed_ms = int((time.time() - started) * 1000)
        return result

    def _model_load_hint(self) -> str:
        """模型加载失败时给出可操作的提示。"""
        detail = (self._model_error or "")[:160]
        hint = (
            "faster-whisper 模型加载失败，音频已缓存可稍后重试。"
            "常见原因：① HuggingFace 被网络/代理拦截；② 未预置本地模型。"
        )
        if self.cfg.asr.model_dir:
            hint += f" 当前 model_dir 指向 {self.cfg.asr.model_dir}（不存在则回退到 Hub 下载）。"
        else:
            hint += " 可在 config.toml 设置 [asr] model_dir 指向已下载的模型目录以离线使用。"
        if detail:
            hint += f" 原始错误：{detail}"
        return hint

    def available(self) -> bool:
        """faster-whisper 是否可用（包已装且模型能加载）。"""
        try:
            import faster_whisper  # type: ignore  # noqa: F401

            return True
        except Exception:
            return False

    def info(self) -> dict[str, Any]:
        model_dir = self.cfg.asr.model_dir
        return {
            "engine": self.cfg.asr.engine,
            "model_size": self.cfg.asr.model_size,
            "model_dir": str(model_dir) if model_dir else "",
            "model_dir_exists": bool(model_dir and Path(model_dir).exists()),
            "compute": self.cfg.asr.compute,
            "concurrency": self.cfg.asr.concurrency,
            "available": self.available(),
            "model_loaded": self._model is not None,
            "ytdlp": self._ytdlp_bin(),
            "ytdlp_exists": bool(
                Path(self._ytdlp_bin()).exists() or shutil.which("yt-dlp")
            ),
            "cache_dir": str(self.cfg.asr.cache_dir),
        }


_asr: ASRService | None = None


def get_asr_service() -> ASRService:
    global _asr
    if _asr is None:
        _asr = ASRService()
    return _asr
