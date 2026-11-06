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
        return ExtractResult(
            ok=True,
            title=title,
            text="\n\n".join(parts),
            extractor="douyin_ssr",
            extra={"aweme_id": aweme_id, "author": author, "duration": duration},
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
                    extra={"aweme_id": aweme_id},
                )

            return ExtractResult(
                ok=False,
                extractor="douyin_ssr",
                error=(
                    "抖音页面没返回文案（可能是图文/直播/需要登录）。"
                    "可填 Cookie 后重试，或直接粘贴文案。"
                ),
            )
        except httpx.HTTPError as exc:
            return ExtractResult(
                ok=False, extractor="douyin_ssr", error=f"网络错误：{exc}（可手动粘贴）"
            )
        except Exception as exc:  # noqa: BLE001
            return ExtractResult(ok=False, extractor="douyin_ssr", error=f"抓取异常：{exc}")
