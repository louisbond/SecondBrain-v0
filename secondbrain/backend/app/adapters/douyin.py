"""抖音适配器：分享链 → 分享页 SSR JSON → 文案。

**为什么不再依赖 yt-dlp**：抖音没有官方 extractor，generic 只能从 og 标签
抠一句话；而且 yt-dlp 会读 Windows 注册表代理，国内站点经常被代理拦成
`CERTIFICATE_VERIFY_FAILED`（详见 app/core/ytdlp.py）。

链路（不逆向签名，只读服务端渲染的公开页面）：

1. `v.douyin.com/xxxx` 短链 → 302 → `/video/{aweme_id}` 或 `/share/video/{id}`
2. 请求 `https://www.iesdouyin.com/share/video/{id}/`（移动端 UA，SSR）
3. 页面里的 `window._ROUTER_DATA = {...}` 含完整作品信息
4. 取 `desc`（文案）/ `author.nickname` / `duration`

降级：SSR 拿不到 → 通用网页抽取（og:description）→ 手动粘贴。
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.adapters.base import Adapter, ExtractResult, register
from app.core.http import make_client

_UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

_AWEME_ID_RE = re.compile(r"/(?:video|note|share/video|share/note)/(\d{8,25})")
_ROUTER_DATA_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)
_ANY_ID_RE = re.compile(r"(?:aweme_id|item_id|modal_id)=(\d{8,25})")


def _find_key(obj: Any, key: str, depth: int = 0) -> Any:
    """在嵌套结构里递归找第一个同名 key（SSR JSON 层级不稳定）。"""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key(item, key, depth + 1)
            if found is not None:
                return found
    return None


# 文案短于这个字数就基本只是"标题/简介"，不是正文
_MIN_REAL_TEXT_CHARS = 200


def _extract_media(item: dict[str, Any]) -> list[str]:
    """从 SSR JSON 里取出视频直链（供 ASR 下载转写）。

    **这是抖音能不能转写到口播台词的关键。**
    SSR 的 `item.video` 里带着几个地址：
      · `download_addr` —— 无水印原画，优先用
      · `play_addr`     —— 带水印（URL 里是 `/playwm/`），兜底
      · `bit_rate[]`    —— 多码率，每个也有 url_list

    以前适配器只取 desc/author/duration，**完全没碰视频地址**，
    于是 `extra["media"]` 永远是空 —— pipeline 里
    `need_transcript = ... or (media_url and 短视频平台 and 正文太短)`
    那个分支永远进不去，抖音条目只能拿到一句文案就标"已采集"。

    返回值按优先级排序：download_addr → play_addr → bit_rate。
    """
    video = item.get("video")
    if not isinstance(video, dict):
        return []

    urls: list[str] = []

    def _collect(node: Any) -> None:
        if isinstance(node, dict):
            for u in node.get("url_list") or []:
                if isinstance(u, str) and u.startswith("http") and u not in urls:
                    urls.append(u)
        elif isinstance(node, list):
            for n in node:
                _collect(n)

    # 顺序即优先级
    _collect(video.get("download_addr"))
    _collect(video.get("play_addr"))
    _collect(video.get("play_addr_h264"))
    _collect(video.get("bit_rate"))
    return urls


@register
class DouyinAdapter(Adapter):
    name = "douyin"
    label = "抖音"
    support_level = "partial"
    supports_audio = False
    supports_subtitle = False

    def __init__(self, cookie: str | None = None, timeout: int = 20) -> None:
        super().__init__(cookie=cookie, timeout=timeout)
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            headers = {
                "User-Agent": _UA_MOBILE,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            if self.cookie:
                headers["Cookie"] = self.cookie
            self._client = make_client(
                "https://www.iesdouyin.com", timeout=self.timeout, headers=headers
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- 链接 -----------------------------------------------------------
    def _aweme_id(self, url: str) -> str:
        m = _AWEME_ID_RE.search(url) or _ANY_ID_RE.search(url)
        return m.group(1) if m else ""

    def expand(self, url: str) -> str:
        """短链跟一次 302，拿到带作品 ID 的地址。"""
        m = _AWEME_ID_RE.search(url)
        if m:
            return f"https://www.iesdouyin.com/share/video/{m.group(1)}/"
        try:
            resp = self.client.get(url)
            final = str(resp.url)
            found = self._aweme_id(final) or self._aweme_id(resp.text[:200000])
            if found:
                return f"https://www.iesdouyin.com/share/video/{found}/"
            return final
        except Exception:  # noqa: BLE001
            return url

    # -- 解析 -----------------------------------------------------------
    @staticmethod
    def _parse_router_data(html: str) -> dict[str, Any]:
        m = _ROUTER_DATA_RE.search(html)
        if not m:
            return {}
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _build_result(self, item: dict[str, Any], aweme_id: str) -> ExtractResult | None:
        desc = str(item.get("desc") or "").strip()
        author = str(
            (item.get("author") or {}).get("nickname")
            or (item.get("author") or {}).get("unique_id")
            or ""
        ).strip()
        duration = int(item.get("duration") or 0) // 1000  # 抖音给毫秒

        if not desc and not author:
            return None

        parts: list[str] = []
        if author:
            parts.append(f"作者：{author}")
        if desc:
            parts.append(f"文案：{desc}")
        if duration:
            parts.append(f"时长：{duration // 60} 分 {duration % 60} 秒")

        title = desc.splitlines()[0][:60] if desc else f"抖音视频 {aweme_id}"
        media = _extract_media(item)
        # **只有文案 ≠ 完整正文**。
        #
        # 抖音 SSR 给的 desc 就是用户发的那句话（几十字），不是视频里的口播内容。
        # 以前这里不设 complete（默认 True），pipeline 的"抓到完整正文"检查
        # 就永远不触发 —— 于是一条 73 字的文案被当成完整素材入库，显示"已采集"，
        # 用户点开才发现里面什么都没有。
        #
        # 现在按情况区分：
        #   · 拿到了视频直链 → 正文还能靠 ASR 转写出来，交给 pipeline 走 ASR
        #   · 只有文案、没有直链 → 确实抓不到正文，标 complete=False 让它判失败，
        #     并提示用「导入视频/音频」上传本地文件
        complete = bool(media) or len(desc) >= _MIN_REAL_TEXT_CHARS
        return ExtractResult(
            ok=True,
            title=title,
            text="\n\n".join(parts),
            extractor="douyin_ssr",
            complete=complete,
            extra={
                "aweme_id": aweme_id,
                "author": author,
                "duration": duration,
                "media": media,
            },
        )

    def extract(self, url: str) -> ExtractResult:
        try:
            target = self.expand(url)
            aweme_id = self._aweme_id(target)
            if not aweme_id:
                raise ValueError("链接里没有作品 ID（可能是主页/直播/已失效）")

            resp = self.client.get(target)
            html = resp.text or ""
            data = self._parse_router_data(html)

            item = _find_key(data, "item_list")
            if isinstance(item, list) and item:
                result = self._build_result(item[0], aweme_id)
                if result:
                    return result

            # 兜底：og:description 至少能留一句
            from app.adapters.web import WebAdapter

            web = WebAdapter(cookie=self.cookie, timeout=self.timeout)
            fallback = web.extract(target)
            if fallback.ok and fallback.text.strip():
                return ExtractResult(
                    ok=True,
                    title=fallback.title or f"抖音视频 {aweme_id}",
                    text=fallback.text,
                    extractor="douyin_og",
                    # og:description 就一句话，永远不是完整正文
                    complete=False,
                    extra={"aweme_id": aweme_id},
                )

            return ExtractResult(
                ok=False,
                extractor="douyin_ssr",
                error=(
                    "抖音分享页已不返回作品文案（拿到的只有空壳/登录页）。"
                    "最可靠的做法是用采集区右上角「导入视频」上传本地文件，"
                    "程序会转写口播并归档 MP3；或直接粘贴文案。"
                ),
            )
        except httpx.HTTPError as exc:
            return ExtractResult(
                ok=False, extractor="douyin_ssr", error=f"网络错误：{exc}（可手动粘贴）"
            )
        except Exception as exc:  # noqa: BLE001
            return ExtractResult(ok=False, extractor="douyin_ssr", error=f"抓取异常：{exc}")
