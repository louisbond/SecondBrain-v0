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


def _extract_title_from_context(
    context: str, url: str, url_pos: int = -1
) -> str:
    """从上下文里猜标题。

    优先级：
    1. 【...】包裹的标题
    2. "标题：xxx" 形式
    3. URL 前紧邻的一段中文/字母（去掉"复制本条信息"这类尾句）
    4. 行首到 URL 之间的文本

    **不能把分享文案的数字前缀当成标题**：
    抖音/快手复制出来的文案开头是一串数字（"41 复制打开抖音…"、
    "7.90 复制打开快手…"），老逻辑会把 "41" 当标题 —— 于是采集列表里
    出现三条一模一样的「41」，完全没法区分。
    所以第 4 步里要**跳过纯数字的片段**，继续找下一段有意义的文字。

    `context` 是**本链接之前**的前文（调用方按链接顺序切片传进来），
    `url_pos` 是该链接在原块里的起始下标（-1 表示在 context 末尾）。
    —— 这样同块里的多个链接各拿各的前文，且规则 1/2 不会误匹配到
    **后面**那个链接的内容（早前"标题：xxx"会把后面的 URL 也吃进标题）。
    """
    if not context:
        return ""

    # 【标题】
    m = re.search(r"[【\[]([^】\]]{2,120})[】\]]", context)
    if m:
        return m.group(1).strip()

    # "标题：xxx" 形式（**只取到空白/换行为止**，别把后面的 URL 吃进来）
    m = re.search(r"(?:标题|题目)[:：]\s*([^\s\n]{2,120})", context)
    if m:
        return m.group(1).strip()

    # 行首 → URL 之间的文本（取第一段**有意义**的）
    idx = context.find(url) if url_pos < 0 else url_pos
    if idx < 0:
        idx = len(context)
    if idx > 0:
        head = context[:idx]
        # 去掉常见噪声后缀
        head = re.sub(
            r"(复制本条信息.*|打开.*?搜索.*?|点击链接.*|长按.*|【.*)$", "", head
        ).strip()
        segments = re.split(r"[\s，,。.!！?？;；\n]+", head)
        segments = [s for s in segments if len(s) >= 2]
        for seg in segments:
            # 跳过纯数字（"41" / "7.90" 这类分享前缀）和短哈希码
            if _is_numeric_prefix(seg):
                continue
            return seg[:120]
    return ""


def _is_numeric_prefix(seg: str) -> bool:
    """判断一段文本是不是分享文案里的**噪声前缀**（数字编号 / 分享码 / 平台话术）。

    抖音/快手的分享文案开头形如：
        "41 复制打开抖音…"      → "41"（数字编号）、"复制打开抖音"（平台话术）
        "7.90 复制打开快手…"     → "7.90"、"复制打开快手"
        "3.14 gPz:/ 复制…"      → "3.14"、"gPz:/"（分享码）
    这些都不是标题 —— 当成标题会出现三条一模一样的「41」，完全没法区分。
    """
    s = (seg or "").strip()
    if not s:
        return True
    # 纯数字（允许小数点）
    if re.fullmatch(r"[\d.]+", s):
        return True
    # 短字母/符号串（分享码），且不含中文
    if len(s) <= 8 and re.fullmatch(r"[A-Za-z0-9:/\-_.]+", s):
        # 但像 "AI" "OK" 这种两个字母的英文词仍可能是标题的一部分，
        # 这里只挡「带符号」或「长度>=3」的纯字母串
        return len(s) >= 3 or re.search(r"[^\w\s]", s)
    # 平台固定话术（"复制打开抖音" / "复制打开快手" / "打开抖音搜索"…）
    if _SHARE_PHRASE_RE.fullmatch(s):
        return True
    return False


# 平台分享话术：这些短语开头固定，不是用户写的标题
_SHARE_PHRASE_RE = re.compile(
    r"(复制|打开|长按|点击|复制此链接|复制本条信息|复制打开抖音|复制打开快手"
    r"|复制打开小红书|复制打开视频号|打开抖音搜索|打开快手搜索|长按识别"
    r"|更多精彩|扫码打开|点击链接|复制链接|搜索|看看)[^\n]{0,8}$"
)


def _split_blocks(text: str) -> list[str]:
    """把粘贴文本切成若干块（每行一块，或按空行切）。"""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    return [ln for ln in lines if ln]


def pad_links(text: str) -> str:
    """给每个链接后面补两个空格（与前端粘贴处理保持一致）。

    分享文案里链接后面常常紧跟着中文（"…的作品 https://v.douyin.com/xxx 复制此链接"），
    不加分隔的话链接边界会被后面的字吃掉。

    规则：
      · 后面是空格/制表 → 归一成正好两个空格
      · 后面紧跟非空白   → 补两个空格
      · 后面是换行       → **不动**（换行是分块依据）

    实现注意：**不能用 lookahead 写法**。
    `re.sub(r"(url)(?=[^\\s])", ...)` 在匹配失败时会回溯 ——
    贪心的 `+` 会一格一格缩回去，于是 "https://x.com/1" 被拆成
    "https://x.com/" + "  " + "1"，URL 结尾的字符被甩到空格后面。
    必须用 finditer 拿到完整匹配后手工拼接。
    """
    if not text:
        return text
    out: list[str] = []
    last = 0
    for m in _URL_RE.finditer(text):
        s, e = m.span()
        out.append(text[last:s])
        out.append(m.group(0))
        rest = text[e:]
        ws_match = re.match(r"[ \t]*", rest)
        ws = ws_match.group(0) if ws_match else ""
        if ws:
            # 已有水平空白 → 归一成两个空格
            out.append("  ")
            last = e + len(ws)
        elif rest and not rest.startswith("\n"):
            # 紧跟非空白字符 → 补两个空格
            out.append("  ")
            last = e
        else:
            # 换行结尾（或文本结束）→ 不动
            last = e
    out.append(text[last:])
    return "".join(out)


def parse_links(text: str) -> list[ParsedLink]:
    """从混合文本中解析出全部链接（按出现顺序，去重）。"""
    results: list[ParsedLink] = []
    seen: set[str] = set()

    # 链接后面补两个空格：让链接边界清晰，标题提取不会被后缀文字干扰
    for block in _split_blocks(pad_links(text)):
        # 用 finditer 而不是 findall：要拿到每个链接在块里的**起始位置**，
        # 否则同一个块里第二个链接的标题会算成第一个链接前文（"https://v"）。
        matches = list(_URL_RE.finditer(block))
        if not matches:
            continue
        # 标题只认「本链接之前」的内容：逐个链接推进扫描起点，
        # 这样同块里的第 2、3 个链接拿到的是它们各自的前文。
        scan_from = 0
        for m in matches:
            raw_url = m.group(0)
            start_pos = m.start()
            # **链接粘连要拆开**：_URL_RE 的字符集里含 : / . & = ? 等，
            # 所以两个挨在一起的链接（中间没有空格/换行，比如从网页复制
            # 丢了换行）会被一次 findall 吃成一个长 URL —— 于是只识别出
            # 1 条而不是 2 条。按 "http(s):// 之前" 再切一刀。
            offset = 0
            for piece in re.split(r"(?=https?://)", raw_url):
                cleaned = piece.strip().strip(_TRAILING_JUNK)
                if not cleaned:
                    offset += len(piece)
                    continue
                normalized = normalize_url(cleaned)
                if not normalized:
                    offset += len(piece)
                    continue
                key = normalized.lower()
                if key in seen:
                    offset += len(piece)
                    continue
                seen.add(key)
                platform = detect_platform(normalized)
                # 本链接在块内的起始位置
                piece_pos = start_pos + offset
                # 前文 = 从上一个链接结束处到本链接开始处
                head_ctx = block[scan_from:piece_pos]
                results.append(
                    ParsedLink(
                        url=normalized,
                        platform=platform,
                        title=_extract_title_from_context(
                            head_ctx, cleaned, len(head_ctx)
                        ),
                        raw=block,
                        context=block,
                        is_short=is_short_link(normalized),
                        needs_expand=is_short_link(normalized),
                    )
                )
                # 下一个链接的前文从本链接之后开始
                scan_from = piece_pos + len(cleaned)
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
