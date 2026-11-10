"""平台适配器基类与注册表。

对齐 v0.5.0 §5.2 能力诚实分级：
每个适配器必须诚实声明它能拿到什么、拿不到什么、抓不到时怎么办。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass
class ExtractResult:
    """抓取结果。"""

    ok: bool
    title: str = ""
    text: str = ""              # 字幕 / 正文全文
    extractor: str = "manual"
    asr: bool = False
    error: str | None = None
    audio_path: str | None = None   # 需要 ASR 时给出音频路径
    needs_asr: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    screenshots: list[str] = field(default_factory=list)
    # 是否拿到了「完整正文」。meta/og 摘要、只有标题这类结果必须标 False：
    # 以前它们也 ok=True，库里就会多出一条只有一句话的素材，用户看到的是
    # 「解析成功」但点开什么都没有。抓取门禁据此拒绝入库并提示导入文件。
    complete: bool = True


class Adapter:
    """适配器基类。

    子类需实现 `extract`。`capabilities` 用于 UI 明示平台支持等级（§13 降级路径）。
    """

    name: ClassVar[str] = "base"
    label: ClassVar[str] = "基础"
    # 能力诚实分级：full=完整可用 / partial=部分可用 / unstable=不稳定 / manual=仅手动
    support_level: ClassVar[str] = "manual"
    supports_audio: ClassVar[bool] = False
    supports_subtitle: ClassVar[bool] = False

    def __init__(self, cookie: str | None = None, timeout: int = 20) -> None:
        self.cookie = cookie
        self.timeout = timeout

    @abc.abstractmethod
    def extract(self, url: str) -> ExtractResult:
        """抓取内容。失败时返回 ok=False 并给出 error。"""

    def expand(self, url: str) -> str:
        """展开短链为最终 URL。默认原样返回。"""
        return url

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "support_level": self.support_level,
            "supports_audio": self.supports_audio,
            "supports_subtitle": self.supports_subtitle,
        }


_REGISTRY: dict[str, type[Adapter]] = {}


def register(cls: type[Adapter]) -> type[Adapter]:
    """装饰器：注册适配器。"""
    _REGISTRY[cls.name] = cls
    return cls


def get_adapter(
    platform: str, cookie: str | None = None, timeout: int = 20
) -> Adapter | None:
    cls = _REGISTRY.get(platform)
    if cls is None:
        return None
    return cls(cookie=cookie, timeout=timeout)


def all_adapters() -> list[dict[str, Any]]:
    """列出全部适配器及其能力（前端设置页展示）。"""
    out: list[dict[str, Any]] = []
    for cls in _REGISTRY.values():
        cls.__new__(cls)  # 仅读类属性，避免构造副作用（丢弃返回值）
        out.append(
            {
                "name": cls.name,
                "label": cls.label,
                "support_level": cls.support_level,
                "supports_audio": cls.supports_audio,
                "supports_subtitle": cls.supports_subtitle,
            }
        )
    return sorted(out, key=lambda x: x["name"])
