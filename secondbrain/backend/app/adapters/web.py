"""通用网页适配器：trafilatura → Playwright → 手动。

对齐 v0.5.0 §5.2：
| 通用网页 | trafilatura→Playwright→手动 | 正文 |
|          | extract_failed 行内粘贴 textarea→extracted（extractor=manual），不再重试 |

**降级为通用网页兜底路径**，不再是对视频平台的主路径（§3.2）。
"""

from __future__ import annotations

import httpx

from app.adapters.base import Adapter, ExtractResult, register
from app.core.http import make_client

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


@register
class WebAdapter(Adapter):
    name = "web"
    label = "通用网页"
    support_level = "full"
    supports_audio = False
    supports_subtitle = False

    def __init__(self, cookie: str | None = None, timeout: int = 20) -> None:
        super().__init__(cookie=cookie, timeout=timeout)
        self._client: httpx.Client | None = None
        self._last_url = ""

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            headers = {"User-Agent": _UA}
            if self.cookie:
                headers["Cookie"] = self.cookie
            self._client = make_client(
                self._last_url or "https://example.com",
                timeout=self.timeout,
                headers=headers,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def extract(self, url: str) -> ExtractResult:
        # 目标地址变了要重建 client（代理策略随目标而定）
        if url != self._last_url:
            self.close()
            self._last_url = url

        # 1) trafilatura：静态正文抽取，质量最好
        text, title = self._extract_trafilatura(url)
        if text and len(text) >= 200:
            return ExtractResult(
                ok=True, title=title, text=text, extractor="trafilatura"
            )

        # 2) Playwright：JS 渲染页面兜底
        text, title = self._extract_playwright(url)
        if text and len(text) >= 200:
            return ExtractResult(
                ok=True, title=title, text=text, extractor="playwright"
            )

        # 3) 失败 → 交给上层走 extract_failed + 行内手动粘贴
        reason = "正文过短或抽取失败"
        if not text:
            reason = "页面无法访问或需要 JS 渲染"
        return ExtractResult(
            ok=False,
            extractor="trafilatura",
            error=f"{reason}（可手动粘贴正文）",
        )

    def _extract_trafilatura(self, url: str) -> tuple[str, str]:
        try:
            import trafilatura
            from trafilatura.settings import use_config

            config = use_config()
            config.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")
            downloaded = trafilatura.fetch_url(url)
            if not downloaded:
                return "", ""
            text = trafilatura.extract(
                downloaded, config=config, include_links=False, include_tables=True
            )
            meta = trafilatura.extract_metadata(downloaded)
            title = str(getattr(meta, "title", "") or "")
            return (text or "").strip(), title
        except Exception:
            return "", ""

    def _extract_playwright(self, url: str) -> tuple[str, str]:
        """Playwright 兜底（未安装则跳过）。"""
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except Exception:
            return "", ""

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent=_UA)
                page.goto(url, timeout=self.timeout * 1000, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                title = page.title()
                text = page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
                browser.close()
                return (text or "").strip(), title
        except Exception:
            return "", ""

    def expand(self, url: str) -> str:
        return url
