"""第三方解析 API 兜底（可选）。

**为什么需要**：抖音 / 快手 / 小红书现在不给未登录请求返回作品正文，
纯本地的平台适配器只能拿到标题和摘要。市面上「去水印」类工具之所以
覆盖上百平台，是因为它们有云端账号池、签名算法，或直接买聚合解析服务。

本模块把这层做成**可选外部依赖**：
- 默认关闭，用户不填地址就完全不影响现有链路；
- 只有平台适配器失败时才调用，成功路径不会外发链接；
- 响应做宽松归一化 —— 各家返回结构差异极大，硬编码某一家等于把产品
  绑死在一个随时可能跑路的服务上。

隐私边界写在设置页文案里：开启后链接会发给你填的那个服务。
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from app.adapters.base import ExtractResult
from app.core.config import ParserApiConfig, get_config
from app.core.http import make_client

# 文本类字段，按可信度从高到低。键名统一小写比较。
_TEXT_KEYS = (
    "content",
    "desc",
    "description",
    "caption",
    "text",
    "summary",
    "transcript",
    "subtitle",
    "subtitles",
    "文案",
)
_TITLE_KEYS = ("title", "caption", "desc", "name", "作品标题")
_AUTHOR_KEYS = ("nickname", "author", "username", "user_name", "name", "uid")
_TOPIC_KEYS = ("topics", "tags", "hashtags", "话题")
# 媒体直链：不进正文，但值得记进 extra，方便用户判断服务到底返回了什么
_MEDIA_KEYS = (
    "video_url",
    "play_url",
    "download_url",
    "nwm_video_url",
    "video",
    "play",
    "images",
    "pics",
)

_MAX_TEXT = 200_000


def get_parser_config() -> ParserApiConfig:
    return get_config().parser


def available() -> bool:
    """是否启用且配置完整。"""
    cfg = get_parser_config()
    return bool(cfg.enabled and str(cfg.url_template or "").strip())


def build_request_url(target: str) -> str:
    """把目标链接塞进用户配置的模板。

    支持 `{url}` 占位符（值已 URL 编码）；没有占位符时追加 `?url=`。
    """
    cfg = get_parser_config()
    template = str(cfg.url_template or "").strip()
    encoded = quote(target, safe="")
    if "{url}" in template:
        return template.replace("{url}", encoded)
    if "{url_raw}" in template:
        # 极少数服务要求不编码（整条已编码的短链放 query 会更好用，但留个口子）
        return template.replace("{url_raw}", target)
    sep = "&" if "?" in template else "?"
    return f"{template}{sep}url={encoded}"


def _headers() -> dict[str, str]:
    cfg = get_parser_config()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    name = str(cfg.auth_header or "").strip()
    if name:
        headers[name] = str(cfg.auth_value or "").strip()
    return headers


# ---------------------------------------------------------------------------
# 响应归一化
# ---------------------------------------------------------------------------


def _norm_key(key: str) -> str:
    return re.sub(r"[\s_\-]", "", str(key)).lower()


def _flatten_text(value: Any) -> str:
    """把任意值压成一段人类可读文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return ""
    if isinstance(value, list):
        parts = [_flatten_text(v) for v in value]
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        # 常见嵌套：{"text": "..."} / {"name": "..."} / {"url": "..."}
        for key in ("text", "content", "desc", "name", "title", "caption"):
            if key in value:
                return _flatten_text(value[key])
        return ""
    return ""


def _collect(obj: Any, found: dict[str, list[Any]], depth: int = 0) -> None:
    """递归收集所有键值（限制深度，避免超大响应把栈打穿）。"""
    if depth > 12:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            nk = _norm_key(key)
            found.setdefault(nk, []).append(value)
            _collect(value, found, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:50]:
            _collect(item, found, depth + 1)


def _pick(found: dict[str, list[Any]], keys: tuple[str, ...], limit: int = 1) -> list[str]:
    out: list[str] = []
    for key in keys:
        for value in found.get(_norm_key(key), []):
            text = _flatten_text(value)
            if text and text not in out:
                out.append(text)
                if len(out) >= limit:
                    return out
    return out


def normalize(payload: Any) -> tuple[str, str, dict[str, Any]]:
    """把任意结构的解析结果压成 (title, text, extra)。

    extra 里带 author / topics / media，供 UI 展示与后续 ASR 判断。
    text 为空表示这个服务没给可用文案 —— 调用方应当按失败处理，
    而不是把一句标题当正文写进库。
    """
    found: dict[str, list[Any]] = {}
    _collect(payload, found)

    titles = _pick(found, _TITLE_KEYS, limit=1)
    title = titles[0] if titles else ""

    # 正文：按优先级取第一个非空，再追加其余不重复的较长文本
    texts: list[str] = []
    for key in _TEXT_KEYS:
        for value in found.get(_norm_key(key), []):
            text = _flatten_text(value)
            if len(text) < 4 or text in texts:
                continue
            if any(text in existing for existing in texts):
                continue
            texts.append(text)
    # 去掉被更长文本包含的短片段，避免「简介 + 简介前半段」重复入库
    texts.sort(key=len, reverse=True)
    kept: list[str] = []
    for text in texts:
        if not any(text != other and text in other for other in kept):
            kept.append(text)

    body = "\n\n".join(kept)[:_MAX_TEXT]

    extra: dict[str, Any] = {}
    authors = _pick(found, _AUTHOR_KEYS, limit=1)
    if authors:
        extra["author"] = authors[0]
    topics = _pick(found, _TOPIC_KEYS, limit=1)
    if topics:
        extra["topics"] = topics[0]
    media: list[str] = []
    for key in _MEDIA_KEYS:
        for value in found.get(_norm_key(key), []):
            text = _flatten_text(value)
            if not text:
                continue
            if text.startswith("http") and text not in media:
                media.append(text)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.startswith("http") and item not in media:
                        media.append(item)
    if media:
        extra["media"] = media[:10]

    if not body and title:
        body = title
    return title, body, extra


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------


def _error_for_status(status: int) -> str:
    if status in (401, 403):
        return f"解析服务拒绝访问（HTTP {status}），检查密钥或鉴权头"
    if status == 404:
        return "解析接口地址不存在（HTTP 404），检查 URL 模板"
    if status == 429:
        return "解析服务限流（HTTP 429），稍后再试或换一家"
    if status >= 500:
        return f"解析服务内部错误（HTTP {status}）"
    return f"解析服务返回 HTTP {status}"


def parse(target: str, timeout: int | None = None) -> ExtractResult:
    """调用第三方接口解析一条链接。"""
    cfg = get_parser_config()
    if not cfg.enabled:
        return ExtractResult(ok=False, extractor="parser_api", error="第三方解析未启用")
    if not str(cfg.url_template or "").strip():
        return ExtractResult(
            ok=False, extractor="parser_api", error="第三方解析已开启，但没填接口地址"
        )

    request_url = build_request_url(target)
    host = ""
    try:
        from urllib.parse import urlparse

        host = urlparse(request_url).netloc
    except Exception:  # noqa: BLE001
        host = ""

    try:
        client = make_client(request_url, timeout=timeout or cfg.timeout, headers=_headers())
        with client:
            resp = client.get(request_url)
    except Exception as exc:  # noqa: BLE001
        return ExtractResult(
            ok=False,
            extractor="parser_api",
            error=f"连接解析服务失败：{type(exc).__name__} {exc}".strip()[:200],
        )

    if resp.status_code != 200:
        return ExtractResult(
            ok=False, extractor="parser_api", error=_error_for_status(resp.status_code)
        )

    raw = resp.text or ""
    payload: Any = None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # 有些服务直接返回视频直链（302 已跟随，body 是二进制）或纯文本
        stripped = raw.strip()
        if stripped.startswith("http") and len(stripped) < 2000:
            return ExtractResult(
                ok=False,
                extractor="parser_api",
                error="解析服务只返回了媒体直链，没有文案",
                extra={"media": [stripped]},
            )
        payload = {"text": stripped}

    title, text, extra = normalize(payload)

    if not text.strip():
        snippet = raw.strip().replace("\n", " ")[:200]
        return ExtractResult(
            ok=False,
            extractor="parser_api",
            error=f"解析服务返回的内容里没有可用文案：{snippet}",
            extra={"raw": snippet},
        )

    if extra.get("author"):
        text = f"{text}\n\n作者：{extra['author']}"

    return ExtractResult(
        ok=True,
        title=title[:200],
        text=text,
        extractor=f"parser_api:{host}" if host else "parser_api",
        extra=extra,
    )


def probe(target: str) -> dict[str, Any]:
    """设置页的「测试」：返回可读的调试信息。"""
    result = parse(target, timeout=get_parser_config().timeout)
    return {
        "ok": result.ok,
        "title": result.title,
        "text_len": len(result.text or ""),
        "text_head": (result.text or "")[:300],
        "extractor": result.extractor,
        "error": result.error,
        "extra": result.extra,
    }
