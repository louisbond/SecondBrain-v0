"""快手适配器：分享链 → 页面 SSR JSON → 文案。

**为什么不再依赖 yt-dlp**：快手的分享页是服务端渲染的，文案就在 HTML 里，
用 yt-dlp 的 generic 反而只能拿到 og:description；而且 yt-dlp 会读 Windows
注册表代理，容易被代理拦成 `CERTIFICATE_VERIFY_FAILED`。

链路（只读公开页面，不逆向签名）：

1. `v.kuaishou.com/xxxx` 短链 → 302 → `/short-video/{photoId}` 等
2. 依次尝试 PC 分享页 / 移动端 `v.m.chenzhongtech.com/fw/photo/{id}`
3. 从 `window.__APOLLO_STATE__` / `window.INIT_STATE` 里取 `caption`、`userName`
4. 失败 → 通用网页抽取 → 手动粘贴
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.adapters.base import Adapter, ExtractResult, register
from app.core.http import make_client

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

_PHOTO_ID_RES = (
    re.compile(r"/short-video/([0-9A-Za-z_-]{6,40})"),
    re.compile(r"/fw/photo/([0-9A-Za-z_-]{6,40})"),
    re.compile(r"[?&]photoId=([0-9A-Za-z_-]{6,40})"),
)
_STATE_RES = (
    re.compile(r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S),
    re.compile(r"window\.INIT_STATE\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S),
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S),
)


def _find_key(obj: Any, key: str, depth: int = 0) -> Any:
    """递归找第一个同名 key（快手不同版本的 state 层级不一样）。"""
    if depth > 10:
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
class KuaishouAdapter(Adapter):
    name = "kuaishou"
    label = "快手"
    support_level = "partial"
    supports_audio = False
    supports_subtitle = False

    def __init__(self, cookie: str | None = None, timeout: int = 20) -> None:
        super().__init__(cookie=cookie, timeout=timeout)
        self._client: httpx.Client | None = None

    def _make(self, ua: str) -> httpx.Client:
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return make_client("https://www.kuaishou.com", timeout=self.timeout, headers=headers)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- 链接 -----------------------------------------------------------
    @staticmethod
    def _photo_id(url: str) -> str:
        for pattern in _PHOTO_ID_RES:
            m = pattern.search(url)
            if m:
                return m.group(1)
        return ""

    def expand(self, url: str) -> str:
        if self._photo_id(url):
            return url
        client = self._make(_UA_MOBILE)
        try:
            resp = client.get(url)
            return str(resp.url)
        except Exception:  # noqa: BLE001
            return url
        finally:
            client.close()

    # -- 解析 -----------------------------------------------------------
    @staticmethod
    def _parse_state(html: str) -> dict[str, Any]:
        for pattern in _STATE_RES:
            m = pattern.search(html)
            if not m:
                continue
            raw = m.group(1)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        return {}

    def extract(self, url: str) -> ExtractResult:
        try:
            target = self.expand(url)
            photo_id = self._photo_id(target)
            if not photo_id:
                # 短链失效时快手会 302 回首页（/ 或 /new-reco），
                # 这跟"主页/直播链接"是两回事，提示要分开，否则用户一直重试。
                path = urlparse(target).path.rstrip("/")
                if path in ("", "/new-reco", "/new-reco/"):
                    return ExtractResult(
                        ok=False,
                        extractor="kuaishou_ssr",
                        error="分享链接已失效（平台跳回了首页），请在 App 里重新复制链接。",
                    )
                return ExtractResult(
                    ok=False,
                    extractor="kuaishou_ssr",
                    error="链接里没有作品 ID（可能是主页/直播），请复制单个视频的分享链接。",
                )

            candidates = [
                f"https://www.kuaishou.com/short-video/{photo_id}",
                f"https://v.m.chenzhongtech.com/fw/photo/{photo_id}",
            ]

            for candidate in candidates:
                ua = _UA if "kuaishou.com" in candidate else _UA_MOBILE
                client = self._make(ua)
                try:
                    resp = client.get(candidate)
                except httpx.HTTPError:
                    continue
                finally:
                    client.close()

                html = resp.text or ""
                state = self._parse_state(html)
                result = self._build_result(state, photo_id)
                if result:
                    return result

                # 页面里没有 state，但 og 标签可能已经够用
                from app.adapters.web import WebAdapter

                web = WebAdapter(cookie=self.cookie, timeout=self.timeout)
                fallback = web.extract(candidate)
                if fallback.ok and len(fallback.text.strip()) >= 20:
                    return ExtractResult(
                        ok=True,
                        title=fallback.title or f"快手视频 {photo_id}",
                        text=fallback.text,
                        extractor="kuaishou_og",
                        extra={"photo_id": photo_id},
                    )

            return ExtractResult(
                ok=False,
                extractor="kuaishou_ssr",
                error=(
                    "快手页面没返回文案（可能需要登录或链接已失效）。"
                    "可填 Cookie 后重试，或直接粘贴文案。"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return ExtractResult(
                ok=False, extractor="kuaishou_ssr", error=f"抓取异常：{exc}"
            )

    def _build_result(self, state: dict[str, Any], photo_id: str) -> ExtractResult | None:
        if not state:
            return None
        caption = _find_key(state, "caption")
        author = _find_key(state, "userName") or _find_key(state, "name")
        if not isinstance(caption, str) and caption is not None:
            caption = str(caption)
        caption = (caption or "").strip()
        author = str(author or "").strip()

        if not caption and not author:
            return None

        parts: list[str] = []
        if author:
            parts.append(f"作者：{author}")
        if caption:
            parts.append(f"文案：{caption}")

        title = caption.splitlines()[0][:60] if caption else f"快手视频 {photo_id}"
        return ExtractResult(
            ok=True,
            title=title,
            text="\n\n".join(parts),
            extractor="kuaishou_ssr",
            extra={"photo_id": photo_id, "author": author},
        )
