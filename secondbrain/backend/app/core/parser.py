"""分享文本智能解析：从混合文本中提取 URL、识别平台、去噪。

对齐 v0.5.0 §5.1：
「采集页 textarea 粘贴 N 段分享文本/链接 → 智能解析：正则提取 URL（含 v.douyin.com
等短链）+ 平台识别 + 去噪」

真实场景里用户粘贴的是 APP 的分享文案，形如：

    【标题】xxx https://b23.tv/AbCdEf 复制本条信息，打开Dou音搜索，直接观看视频！

因此解析必须容忍：中文文案、emoji、多个 URL 混杂、短链、以及结尾的噪声。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

# URL 主正则：http/https 开头，到空白或中文标点为止
_URL_RE = re.compile(
    r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
    re.IGNORECASE,
)

# 需要在 URL 尾部剥掉的常见噪声字符
_TRAILING_JUNK = ".,;:!?、。，；：！？）)】]》>\"'"

# 短链域名 → 需要展开的平台
SHORTENERS: dict[str, str] = {
    "b23.tv": "bilibili",
    "bili2233.cn": "bilibili",
    "m.bilibili.com": "bilibili",
    "v.douyin.com": "douyin",
    "www.iesdouyin.com": "douyin",
    "iesdouyin.com": "douyin",
    "douyin.com": "douyin",
    "v.kuaishou.com": "kuaishou",
    "kuaishou.com": "kuaishou",
    "chenzhongtech.com": "kuaishou",
    "xhslink.com": "xiaohongshu",
    "xhs.link": "xiaohongshu",
    "xiaohongshu.com": "xiaohongshu",
    "bilibili.com": "bilibili",
    "bili2233.com": "bilibili",
}

# 域名 → 平台
DOMAIN_PLATFORM: dict[str, str] = {
    "bilibili.com": "bilibili",
    "b23.tv": "bilibili",
    "douyin.com": "douyin",
    "iesdouyin.com": "douyin",
    "kuaishou.com": "kuaishou",
    "gifshow.com": "kuaishou",
    "xiaohongshu.com": "xiaohongshu",
    "xhslink.com": "xiaohongshu",
    "zhihu.com": "web",
    "weixin.qq.com": "web",
    "mp.weixin.qq.com": "web",
    "jianshu.com": "web",
    "sspai.com": "web",
    "36kr.com": "web",
    "youtube.com": "web",
    "youtu.be": "web",
    "twitter.com": "web",
    "x.com": "web",
    "medium.com": "web",
    "github.com": "web",
}

PLATFORM_LABELS: dict[str, str] = {
    "bilibili": "B站",
    "douyin": "抖音",
    "kuaishou": "快手",
    "xiaohongshu": "小红书",
    "web": "网页",
    "manual": "手动",
    "quick_note": "速记",
}

# 平台徽标颜色（前端用）
PLATFORM_COLORS: dict[str, str] = {
    "bilibili": "#fb7299",
    "douyin": "#000000",
    "kuaishou": "#ff6600",
    "xiaohongshu": "#ff2e4d",
    "web": "#6b7280",
    "manual": "#8b5cf6",
    "quick_note": "#10b981",
}


@dataclass
class ParsedLink:
    """解析出的一条链接。"""

    url: str
    platform: str
    title: str = ""
    raw: str = ""              # 该 URL 所在的原始文本块
    context: str = ""          # 上下文（用于提取标题）
    is_short: bool = False
    needs_expand: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "platform": self.platform,
            "platform_label": PLATFORM_LABELS.get(self.platform, self.platform),
            "platform_color": PLATFORM_COLORS.get(self.platform, "#6b7280"),
            "title": self.title,
            "raw": self.raw,
            "context": self.context,
            "is_short": self.is_short,
            "needs_expand": self.needs_expand,
            "extra": self.extra,
        }


def normalize_url(url: str) -> str:
    """URL 规范化：去尾噪、去 fragment、统一 scheme 与 www。

    注意：保留 query（B站 BV 号之外的 utm 等参数会被剥掉）。
    """
    url = (url or "").strip().strip(_TRAILING_JUNK)
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url

    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()

    # 去掉 tracking 参数
    drop_prefixes = ("utm_", "spm_", "vd_source", "share_source", "share_medium")
    query_pairs = []
    for chunk in (parsed.query or "").split("&"):
        if not chunk:
            continue
        key = chunk.split("=", 1)[0].lower()
        if key.startswith(drop_prefixes) or key in {
            "timestamp",
            "unique_k",
            "share_from",
            "appuid",
            "iid",
        }:
            continue
        query_pairs.append(chunk)

    return urlunparse(
        (
            scheme,
            netloc,
            parsed.path,
            parsed.params,
            "&".join(query_pairs),
            "",
        )
    )


def detect_platform(url: str) -> str:
    """按域名识别平台。"""
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return "web"
    host = host.split(":")[0]
    # 逐级匹配（支持 xxx.bilibili.com）
    parts = host.split(".")
    for i in range(len(parts)):
        candidate = ".".join(parts[i:])
        if candidate in DOMAIN_PLATFORM:
            return DOMAIN_PLATFORM[candidate]
        if candidate in SHORTENERS:
            return SHORTENERS[candidate]
    return "web"


def is_short_link(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return host.split(":")[0] in SHORTENERS and detect_platform(url) != "web"


def _extract_title_from_context(context: str, url: str) -> str:
    """从上下文里猜标题。

    优先级：
    1. 【...】包裹的标题
    2. URL 前紧邻的一段中文/字母（去掉"复制本条信息"这类尾句）
    3. 行首到 URL 之间的文本
    """
    if not context:
        return ""

    # 【标题】
    m = re.search(r"[【\[]([^】\]]{2,120})[】\]]", context)
    if m:
        return m.group(1).strip()

    # "标题：xxx" 形式
    m = re.search(r"(?:标题|题目)[:：]\s*([^\n]{2,120})", context)
    if m:
        return m.group(1).strip()

    # 行首 → URL 之间的文本（取最长的一段中文）
    idx = context.find(url)
    if idx > 0:
        head = context[:idx]
        # 去掉常见噪声后缀
        head = re.sub(
            r"(复制本条信息.*|打开.*?搜索.*?|点击链接.*|长按.*|【.*)$", "", head
        ).strip()
        segments = re.split(r"[\s，,。.!！?？;；\n]+", head)
        segments = [s for s in segments if len(s) >= 2]
        if segments:
            return segments[0][:120]
    return ""


def _split_blocks(text: str) -> list[str]:
    """把粘贴文本切成若干块（每行一块，或按空行切）。"""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    return [ln for ln in lines if ln]


def parse_links(text: str) -> list[ParsedLink]:
    """从混合文本中解析出全部链接（按出现顺序，去重）。"""
    results: list[ParsedLink] = []
    seen: set[str] = set()

    for block in _split_blocks(text):
        urls = _URL_RE.findall(block)
        if not urls:
            continue
        for raw_url in urls:
            cleaned = raw_url.strip().strip(_TRAILING_JUNK)
            if not cleaned:
                continue
            normalized = normalize_url(cleaned)
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            platform = detect_platform(normalized)
            results.append(
                ParsedLink(
                    url=normalized,
                    platform=platform,
                    title=_extract_title_from_context(block, raw_url),
                    raw=block,
                    context=block,
                    is_short=is_short_link(normalized),
                    needs_expand=is_short_link(normalized),
                )
            )
    return results


def extract_bvid(url: str) -> str | None:
    """从 B站 URL 中提取 BV 号。

    标准格式是 BV + 10 位（共 12 字符）。但用户复制链接时**常被截断**
    （少一位），此时宁可提取出来让后端给出明确提示，也不要直接说
    "无法解析" —— 用户看到的是自己贴的链接，莫名其妙。

    长度下限取 8 位（BV + 6），避免把无关 token 当 BV 号。
    """
    m = re.search(r"(BV[0-9A-Za-z]{8,})", url, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"(av\d+)", url, re.IGNORECASE)
    if m:
        return m.group(1)
    # 有 "BV" 但位数不对 → 提取出来，让上层给出"链接可能被截断"的提示
    m = re.search(r"(BV[0-9A-Za-z]{1,20})", url, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def bvid_length_hint(bvid: str) -> str:
    """BV 号长度异常的诊断提示（给用户看）。"""
    if not bvid:
        return ""
    if bvid.lower().startswith("av"):
        return ""
    n = len(bvid)
    if n < 12:
        return (
            f"BV 号只有 {n} 位（标准是 12 位）——"
            "链接复制时可能被截断了，请重新复制完整链接"
        )
    if n > 12:
        return f"BV 号有 {n} 位（标准是 12 位），请检查链接是否带了多余字符"
    return ""


def extract_platform_ids(url: str, platform: str) -> dict[str, str]:
    """提取平台特有的 ID（供各适配器使用）。"""
    ids: dict[str, str] = {}
    if platform == "bilibili":
        bvid = extract_bvid(url)
        if bvid:
            ids["bvid"] = bvid
    elif platform == "douyin":
        m = re.search(r"/video/(\d+)", url)
        if m:
            ids["aweme_id"] = m.group(1)
        m = re.search(r"/share/video/(\d+)", url)
        if m:
            ids["aweme_id"] = m.group(1)
    elif platform == "kuaishou":
        m = re.search(r"/short-video/([\w\-]+)", url)
        if m:
            ids["photo_id"] = m.group(1)
        m = re.search(r"photoId=([\w\-]+)", url)
        if m:
            ids["photo_id"] = m.group(1)
    elif platform == "xiaohongshu":
        m = re.search(r"/explore/([0-9a-f]{24})", url)
        if m:
            ids["note_id"] = m.group(1)
        m = re.search(r"/discovery/item/([0-9a-f]{24})", url)
        if m:
            ids["note_id"] = m.group(1)
        m = re.search(r"([0-9a-f]{24})", url)
        if m:
            ids["note_id"] = m.group(1)
    return ids


def canonical_source_url(url: str, platform: str) -> str:
    """生成入库用的规范化 source_url（精确去重的依据）。"""
    normalized = normalize_url(url)
    ids = extract_platform_ids(normalized, platform)
    if platform == "bilibili" and ids.get("bvid"):
        return f"https://www.bilibili.com/video/{ids['bvid']}"
    if platform == "douyin" and ids.get("aweme_id"):
        return f"https://www.douyin.com/video/{ids['aweme_id']}"
    if platform == "kuaishou" and ids.get("photo_id"):
        return f"https://www.kuaishou.com/short-video/{ids['photo_id']}"
    if platform == "xiaohongshu" and ids.get("note_id"):
        return f"https://www.xiaohongshu.com/explore/{ids['note_id']}"
    return normalized


def platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, platform)
