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


# ---------------------------------------------------------------------------
# 预设目录
# ---------------------------------------------------------------------------

# 为什么要有预设：模板地址、是否要密钥、返回里有没有正文，这三件事让用户
# 自己去翻文档挑服务，比配置本身还费劲。
#
# 收录标准（硬性）：**GET + query 传链接**。本模块的调用方式就是 GET，
# 收 POST/JSON 的（cobalt 之类）填进来也跑不通，列出来只会让人白试一场。
#
# v0.7.8：删掉了三个已经实测失效的公共接口（douyin.wtf 演示站 404、
# api.nfiii.com 返回的是网页不是 JSON、dy.nfiii.com DNS 直接解析不了）。
# 教训：预设清单里的公共免费服务会随时跑路，**列出来之前必须逐个实测**，
# 与其给用户一排点了就报错的选项，不如只留能跑通的 + 明确标注限制。
# 现在只保留一个实测仍活着的接口（需自己的免费 KEY）和自建/自定义出口。
PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "apihz",
        "name": "接口盒子 · 抖音视频/图集（需免费 KEY）",
        "kind": "keyed",
        "platforms": "抖音 / 图集",
        "url_template": "https://cn.apihz.cn/api/fun/douyin.php?id=88888888&key={key}&url={url}",
        "note": "实测接口可用。模板里的 {key} 会用下面「密钥」栏的值替换；公共 ID(88888888) 有频率限制，建议去官网注册免费账号换成自己的 ID（把地址里的 id 改掉即可）。返回 title/视频直链，正文通常只有标题。",
        "doc_url": "https://www.apihz.cn/api/fundouyin.html",
        "needs_key": True,
    },
    {
        "id": "selfhost",
        "name": "本机自建解析服务（推荐）",
        "kind": "selfhost",
        "platforms": "取决于你部署的项目",
        "url_template": "http://127.0.0.1:8080/parse?url={url}",
        "note": "最稳的一条路：自己跑开源解析项目（如 Douyin_TikTok_Download_API），链接不出内网、不受别人限流。把地址和端口改成你的实际配置。",
        "doc_url": "https://github.com/Evil0ctal/Douyin_TikTok_Download_API",
    },
    {
        "id": "custom",
        "name": "自定义（手填地址）",
        "kind": "custom",
        "platforms": "",
        "url_template": "",
        "note": "有自己的解析服务就填这里：地址里用 {url} 占位（会自动 URL 编码），需要密钥用 {key} 占位。不写占位符则自动拼 ?url=。",
        "doc_url": "",
    },
)


def preset_list() -> list[dict[str, Any]]:
    """给设置页的预设清单（返回副本，防止调用方改到模块常量）。"""
    return [dict(p) for p in PRESETS]


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
    # {key} 走密钥库（secrets.json），不把密钥写进可能被分享的 config.toml。
    # 没有值时保留原样并在 parse() 里给出可操作的错误，而不是发一个坏请求。
    if "{key}" in template and str(cfg.auth_value or "").strip():
        template = template.replace("{key}", quote(str(cfg.auth_value).strip(), safe=""))
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


def parse(target: str, timeout: int | None = None, force: bool = False) -> ExtractResult:
    """调用第三方接口解析一条链接。

    force=True 只给设置页的「测试」用：开关还没打开也能先验证服务通不通，
    否则用户的流程是「填地址 → 保存 → 开开关 → 再测」，多一步纯粹是摩擦。
    采集链路永远走默认的 force=False，不会因为测试功能而偷偷外发。
    """
    cfg = get_parser_config()
    if not cfg.enabled and not force:
        return ExtractResult(ok=False, extractor="parser_api", error="第三方解析未启用")
    if not str(cfg.url_template or "").strip():
        return ExtractResult(
            ok=False, extractor="parser_api", error="第三方解析已开启，但没填接口地址"
        )
    if "{key}" in cfg.url_template and not str(cfg.auth_value or "").strip():
        return ExtractResult(
            ok=False,
            extractor="parser_api",
            error="这个接口需要密钥：请在设置页「密钥」栏填好并保存",
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
        # normalize() 在「没有正文」时会拿标题顶替。那不是完整内容，
        # 不能让它冒充原文入库。
        complete=text.strip() != title.strip(),
    )


def probe(target: str) -> dict[str, Any]:
    """设置页的「测试」：返回可读的调试信息（开关未开也放行，见 parse 的 force）。"""
    result = parse(target, timeout=get_parser_config().timeout, force=True)
    return {
        "ok": result.ok,
        "title": result.title,
        "text_len": len(result.text or ""),
        "text_head": (result.text or "")[:300],
        "extractor": result.extractor,
        "error": result.error,
        "extra": result.extra,
        "enabled": bool(get_parser_config().enabled),
    }
