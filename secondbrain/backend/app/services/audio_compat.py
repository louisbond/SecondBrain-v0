"""第三方库的版本兼容处理。

**当前唯一的一处**：PyAV 与 faster-whisper 的参数不兼容。

- faster-whisper 1.2.x 的 `audio.decode_audio()` 调用
  `av.open(path, mode="r", metadata_errors="ignore")`；
- PyAV 14 起移除了 `open()` 的 `metadata_errors` 参数（PyAV 19 已彻底移除）；
- 而 PyAV 13 及更早版本没有 Python 3.13 的 wheel，装不上。

所以在 Python 3.13 + 新版 PyAV 的组合下，只能自己做一次兼容：
把 `metadata_errors` 参数在调用前摘掉。这个参数的作用是
"忽略音频元数据里的解析错误"，去掉后行为在正常音频上没有区别。
"""

from __future__ import annotations


def patch_av_open_compat() -> bool:
    """给 av.open 打兼容补丁。返回是否真的打了补丁。

    幂等；已是新版本（没有 metadata_errors 参数）时返回 False。
    """
    try:
        import av  # type: ignore
    except Exception:
        return False

    if getattr(av, "_sb_av_open_patched", False):
        return False

    original = av.open

    def open_compat(file, mode="r", *args, **kwargs):
        # 摘掉新版 PyAV 已移除的参数
        kwargs.pop("metadata_errors", None)
        return original(file, mode, *args, **kwargs)

    av.open = open_compat  # type: ignore[assignment]
    av._sb_av_open_patched = True  # type: ignore[attr-defined]
    return True


def av_open_supports_metadata_errors() -> bool:
    """当前 PyAV 是否还支持 metadata_errors 参数。"""
    try:
        import av  # type: ignore
    except Exception:
        return False
    try:
        import inspect

        sig = inspect.signature(av.open)
        return "metadata_errors" in sig.parameters
    except (TypeError, ValueError):
        # 内建函数拿不到签名——用试调用判断代价太大，按"不支持"处理
        return False
