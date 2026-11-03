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
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import get_config


@dataclass
class ASRResult:
    ok: bool
    text: str = ""
    audio_path: str | None = None
    error: str | None = None
    model: str = ""
    elapsed_ms: int = 0


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
        """定位 yt-dlp 可执行文件。

        查找顺序：内嵌 bin/ → Python 解释器同目录的 Scripts/ → PATH。
        第二步很重要：pip 装的 console script 落在
        `<python-dir>/Scripts/`，该目录未必在 PATH 里，
        找不到就会让整条 ASR 管线直接失败。
        """
        cfg = self.cfg
        for name in ("yt-dlp.exe", "yt-dlp"):
            candidate = cfg.bin_dir / name
            if candidate.exists():
                return str(candidate)

        import sys

        # Windows 布局：<install>/python.exe 与 <install>/Scripts/ 是兄弟目录
        install_dir = Path(sys.executable).resolve().parent
        candidates = [
            install_dir,
            install_dir / "Scripts",
            install_dir / "bin",
        ]
        for base in candidates:
            for name in ("yt-dlp.exe", "yt-dlp"):
                candidate = base / name
                if candidate.exists():
                    return str(candidate)

        found = shutil.which("yt-dlp")
        return found or "yt-dlp"

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
        import time

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
        """执行 yt-dlp，返回 (ok, stdout, 错误信息)。"""
        cmd = [self._ytdlp_bin(), *args, url]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
        except FileNotFoundError:
            return False, None, "未找到 yt-dlp（请放到 bin/ 目录或安装到 PATH）"
        except subprocess.TimeoutExpired:
            return False, None, f"yt-dlp 下载超时（{timeout}s）"
        except Exception as exc:  # noqa: BLE001
            return False, None, f"yt-dlp 执行异常：{exc}"

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            # 提取 yt-dlp 的 ERROR 行，更可读
            for line in detail.splitlines():
                if line.startswith("ERROR:"):
                    detail = line
                    break
            return False, None, f"yt-dlp 失败（退出码 {proc.returncode}）：{detail[:300]}"
        return True, proc.stdout, None

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

    def transcribe_file(self, audio_path: str) -> ASRResult:
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
            segments, info = model.transcribe(
                audio_path,
                language="zh",
                vad_filter=True,
                beam_size=5,
            )
            lines: list[str] = []
            for seg in segments:
                text = (seg.text or "").strip()
                if text:
                    lines.append(text)
            body = "\n".join(lines)
            # 空转写**不能算成功**——否则下游 AI 拿到 0 字正文，
            # 报一个看不懂的 empty_content。纯音乐/无人声视频就是这样。
            if not body.strip():
                return ASRResult(
                    ok=False,
                    error=(
                        "转写结果为空：这段音频没有人声"
                        "（纯音乐/无人声视频属正常情况）。"
                        "可改用「手动补充正文」录入内容。"
                    ),
                    audio_path=audio_path,
                    model=self.cfg.asr.model_size,
                    elapsed_ms=int((time.time() - started) * 1000),
                )
            return ASRResult(
                ok=True,
                text=body,
                audio_path=audio_path,
                model=self.cfg.asr.model_size,
                elapsed_ms=int((time.time() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            return ASRResult(
                ok=False,
                error=f"转写失败：{exc}",
                audio_path=audio_path,
                elapsed_ms=int((time.time() - started) * 1000),
            )

    # -- 组合流程 ---------------------------------------------------------
    def transcribe(self, url: str, item_id: int | None = None) -> ASRResult:
        """完整 ASR 流程：下载音频 → 转写。"""
        import time

        started = time.time()
        cache_key = f"item{item_id}" if item_id else "audio"

        ok, audio_path, error = self.download_audio(url, cache_key)
        if not ok or not audio_path:
            return ASRResult(ok=False, error=error or "音频下载失败")

        result = self.transcribe_file(audio_path)
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
