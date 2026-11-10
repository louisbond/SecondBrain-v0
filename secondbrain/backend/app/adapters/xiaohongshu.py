"""小红书适配器（分享链解析 → 图文帖正文与标题）。

对齐 v0.5.0 §5.2：
| 小红书 | 分享链解析（web 页面） | 图文帖正文、标题 | 失败→手动粘贴 |

策略：
1. 展开 xhslink.com 短链 → 拿到 note_id；
2. 抓取笔记页 HTML，尝试从 __INITIAL_STATE__ / meta 标签里提取正文与标题；
3. 提取不到 → 走通用网页抽取（trafilatura）作为兜底；
4. 仍失败 → extract_failed + 手动粘贴。
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.adapters.base import Adapter, ExtractResult, register
from app.core.http import make_client

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_META_TITLE_RE = re.compile(
    r'<meta[^>]+name=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']', re.I
)
_META_CONTENT_RE = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']og:title["\']', re.I
)
_INITIAL_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", re.S)


@register
class XiaohongshuAdapter(Adapter):
    name = "xiaohongshu"
    label = "小红书"
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
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            if self.cookie:
                headers["Cookie"] = self.cookie
            self._client = make_client(
                "https://www.xiaohongshu.com",
                timeout=self.timeout,
                headers=headers,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def expand(self, url: str) -> str:
        if "xhslink.com" not in url and "xhs.link" not in url:
            return url
        try:
            resp = self.client.get(url)
            final = str(resp.url)
            m = re.search(r"/(explore|discovery/item)/([0-9a-f]{24})", final)
            if m:
                return f"https://www.xiaohongshu.com/explore/{m.group(2)}"
            return final
        except Exception:
            return url

    def extract(self, url: str) -> ExtractResult:
        try:
            url = self.expand(url)
            resp = self.client.get(url)
            html = resp.text
        except httpx.HTTPError as exc:
            return ExtractResult(
                ok=False,
                extractor="xhs_api",
                error=f"网络错误：{exc}（可手动粘贴正文）",
            )
        except Exception as exc:  # noqa: BLE001
            return ExtractResult(
                ok=False, extractor="xhs_api", error=f"抓取异常：{exc}"
            )

        title, description = self._parse_meta(html)
        state_title, state_desc = self._parse_initial_state(html)
        title = title or state_title
        description = description or state_desc

        if not description:
            # 兜底：通用网页抽取
            from app.adapters.web import WebAdapter

            web = WebAdapter(cookie=self.cookie, timeout=self.timeout)
            result = web.extract(url)
            if result.ok:
                return ExtractResult(
                    ok=True,
                    title=title or result.title,
                    text=result.text,
                    extractor="trafilatura",
                )

        if not description:
            return ExtractResult(
                ok=False,
                extractor="xhs_api",
                error="未取到正文（小红书对未登录访问有反爬，请手动粘贴正文）",
            )

        body = f"标题：{title}\n\n{description}" if title else description
        return ExtractResult(
            ok=True,
            title=title or "小红书笔记",
            text=body,
            extractor="xhs_api",
            extra={"note_id": self._note_id(url)},
        )

    @staticmethod
    def _note_id(url: str) -> str:
        m = re.search(r"([0-9a-f]{24})", url)
        return m.group(1) if m else ""

    def _parse_meta(self, html: str) -> tuple[str, str]:
        title = ""
        desc = ""
        m = _META_TITLE_RE.search(html) or _META_CONTENT_RE.search(html)
        if m:
            title = m.group(1).strip()
        m = _META_DESC_RE.search(html)
        if m:
            desc = m.group(1).strip()
        return title, desc

    def _parse_initial_state(self, html: str) -> tuple[str, str]:
        """从 __INITIAL_STATE__ 提取标题与正文。"""
        m = _INITIAL_STATE_RE.search(html)
        if not m:
            return "", ""
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return "", ""

        note = self._find_note(data)
        if not isinstance(note, dict):
            return "", ""

        title = str(note.get("title") or note.get("displayTitle") or "").strip()
        desc = str(note.get("desc") or note.get("description") or "").strip()

        # 图文帖正文可能在 imageList 里
        if not desc:
            parts: list[str] = []
            for image in note.get("imageList") or []:
                if isinstance(image, dict):
                    parts.append(str(image.get("url") or image.get("traceId") or ""))
            desc = "\n".join(p for p in parts if p)
        return title, desc

    def _find_note(self, node: Any, depth: int = 0) -> Any:
        """在初始状态里找到 note 对象。"""
        if depth > 6:
            return None
        if isinstance(node, dict):
            if "noteDetailMap" in node or "note" in node and isinstance(node.get("note"), dict):
                candidate = node.get("note")
                if isinstance(candidate, dict):
                    return candidate
            if node.get("noteDetailMap"):
                detail = node["noteDetailMap"]
                if isinstance(detail, dict):
                    for value in detail.values():
                        if isinstance(value, dict) and isinstance(value.get("note"), dict):
                            return value["note"]
            for value in node.values():
                found = self._find_note(value, depth + 1)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = self._find_note(item, depth + 1)
                if found:
                    return found
        return None
