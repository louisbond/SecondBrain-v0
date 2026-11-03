"""抖音 / 快手适配器（yt-dlp 路径）。

对齐 v0.5.0 §5.2：
| 抖音 | yt-dlp（分享短链）+ cookie 可配 | 标题、简介、音频（→ASR） |
|      | 支持不稳是已知事实→手动粘贴兜底为常态路径而非异常 |
| 快手 | yt-dlp（分享短链） | 同抖音 |

§13 降级路径：
「yt-dlp 对抖音/快手支持失效 → 触发条件 spike 或运行期解析失败率 >50%
  → '简介+ASR+手动粘贴'为常态路径；UI 明示平台支持等级」

因此这两个适配器的 support_level 标为 **unstable**，抓取失败时明确提示
"手动粘贴是常规路径"，而不是把它当异常。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.adapters.base import Adapter, ExtractResult, register
from app.core.config import get_config


class YtDlpAdapter(Adapter):
    """基于 yt-dlp 的通用视频平台适配器基类。"""

    support_level = "unstable"
    supports_audio = True
    supports_subtitle = False

    def _ytdlp_bin(self) -> str:
        cfg = get_config()
        for name in ("yt-dlp.exe", "yt-dlp"):
            candidate = cfg.bin_dir / name
            if candidate.exists():
                return str(candidate)
        return shutil.which("yt-dlp") or "yt-dlp"

    def _ffmpeg_dir(self) -> Path:
        return get_config().bin_dir

    def _probe(self, url: str) -> tuple[dict, str | None]:
        """用 yt-dlp 拿元数据（不下载）。"""
        cmd = [
            self._ytdlp_bin(),
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
            "--no-playlist",
            "--ffmpeg-location",
            str(self._ffmpeg_dir()),
        ]
        if self.cookie:
            cmd.extend(["--add-header", f"Cookie:{self.cookie}"])
        cmd.append(url)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout * 3,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
        except FileNotFoundError:
            return {}, "未找到 yt-dlp（请放到 bin/ 目录或安装到 PATH）"
        except subprocess.TimeoutExpired:
            return {}, "yt-dlp 探测超时"
        except Exception as exc:  # noqa: BLE001
            return {}, f"yt-dlp 执行异常：{exc}"

        if proc.returncode != 0:
            return {}, (proc.stderr or proc.stdout or "yt-dlp 探测失败").strip()[:300]

        import json

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {}, "yt-dlp 输出解析失败"

        return payload, None

    def extract(self, url: str) -> ExtractResult:
        payload, error = self._probe(url)
        if error:
            return ExtractResult(
                ok=False,
                extractor="ytdlp",
                error=f"{error}（手动粘贴简介与字幕是常规路径）",
            )

        title = str(payload.get("title") or "").strip()
        description = str(payload.get("description") or "").strip()
        uploader = str(payload.get("uploader") or payload.get("channel") or "").strip()
        duration = int(payload.get("duration") or 0)

        body_parts: list[str] = []
        if uploader:
            body_parts.append(f"作者：{uploader}")
        if description:
            body_parts.append(f"简介：{description}")
        if not description and not uploader:
            body_parts.append(
                f"（平台未返回简介；视频时长 {duration // 60} 分 {duration % 60} 秒，"
                "需 ASR 转写或手动粘贴）"
            )

        return ExtractResult(
            ok=True,
            title=title or f"{self.label}视频",
            text="\n\n".join(body_parts),
            extractor="ytdlp",
            asr=False,
            needs_asr=True,  # 视频正文靠 ASR
            extra={
                "uploader": uploader,
                "duration": duration,
                "thumbnail": payload.get("thumbnail"),
                "webpage_url": payload.get("webpage_url"),
            },
        )

    def expand(self, url: str) -> str:
        """展开分享短链。"""
        payload, error = self._probe(url)
        if error:
            return url
        return str(payload.get("webpage_url") or url)


@register
class DouyinAdapter(YtDlpAdapter):
    name = "douyin"
    label = "抖音"


@register
class KuaishouAdapter(YtDlpAdapter):
    name = "kuaishou"
    label = "快手"
