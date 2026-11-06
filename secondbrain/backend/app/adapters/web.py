"""通用网页适配器：trafilatura → Playwright → 手动。

对齐 v0.5.0 §5.2：
| 通用网页 | trafilatura→Playwright→手动 | 正文 |
|          | extract_failed 行内粘贴 textarea→extracted（extractor=manual），不再重试 |

**降级为通用网页兜底路径**，不再是对视频平台的主路径（§3.2）。
"""

from __future__ import annotations

import logging
import re
from html import unescape as _unescape

import httpx

from app.adapters.base import Adapter, ExtractResult, register
from app.core.http import make_client

logger = logging.getLogger(__name__)

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

        # 3) meta 兜底：任何页面至少有 title / og:description。
        #
        # 「啥链接都能提取」的实际含义就在这里：文章类走正文，视频/产品页
        # 正文抽不出来时，也要把标题和摘要落成一条素材，而不是直接失败
        # 让用户去手动粘贴。
        meta_title, meta_desc = self._extract_meta(url)
        if meta_desc and len(meta_desc) >= 12:
            return ExtractResult(
                ok=True,
                title=meta_title or meta_desc[:40],
                text=(
                    f"标题：{meta_title}\n\n摘要：{meta_desc}"
                    if meta_title
                    else f"摘要：{meta_desc}"
                ),
                extractor="meta",
            )

        # 4) 失败 → 交给上层走 extract_failed + 行内手动粘贴
        reason = "正文过短或抽取失败"
        if not text:
            reason = "页面无法访问或需要 JS 渲染"
        return ExtractResult(
            ok=False,
            extractor="trafilatura",
            error=f"{reason}（可手动粘贴正文）",
        )

    def _extract_meta(self, url: str) -> tuple[str, str]:
        """抓 og/meta 标题与摘要（最后一道兜底，几乎不会空手）。"""
        try:
            resp = self.client.get(url)
            html = resp.text or ""
        except Exception:  # noqa: BLE001
            return "", ""

        def pick(patterns: tuple[str, ...]) -> str:
            for pattern in patterns:
                m = re.search(pattern, html, re.I | re.S)
                if m:
                    value = _unescape(m.group(1)).strip()
                    if value:
                        return value
            return ""

        title = pick(
            (
                r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)["\']',
                r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']*)["\']',
                r"<title[^>]*>(.*?)</title>",
            )
        )
        desc = pick(
            (
                r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']',
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']',
                r'<meta[^>]+name=["\']twitter:description["\'][^>]+content=["\']([^"\']*)["\']',
            )
        )
        if " - 知乎" in title:
            title = title.replace(" - 知乎", "").strip()
        return title, desc

    def _extract_trafilatura(self, url: str) -> tuple[str, str]:
        """trafilatura 正文抽取。

        两个坑：

        1. `use_config()` 读的是包内 `settings.cfg`。PyInstaller **不会**自动
           收集这个数据文件（只收 .py），打包后 config 为空 →
           `extract()` 读 `MIN_EXTRACTED_SIZE` 抛 NoOptionError。
           所以这里先用带 config 的调用，失败再退回默认配置。
        2. 异常绝不能静默吞掉。之前 `except: return "", ""` 让"打包版正文
           永远退化成 meta 摘要"这个问题完全不可观测，只能靠猜。
        """
        try:
            import trafilatura
            from trafilatura.settings import use_config
        except Exception as exc:  # noqa: BLE001
            logger.warning("trafilatura 不可用：%s", exc)
            return "", ""

        try:
            downloaded = trafilatura.fetch_url(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("trafilatura 抓取失败 %s：%s", url, exc)
            return "", ""
        if not downloaded:
            logger.info("trafilatura 未取到内容：%s", url)
            return "", ""

        text: str = ""
        try:
            config = use_config()
            config.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")
            text = (
                trafilatura.extract(
                    downloaded, config=config, include_links=False, include_tables=True
                )
                or ""
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("trafilatura 抽取失败（带 config）：%s", exc)
            text = ""

        if not text:
            # 打包缺 settings.cfg 时走这里：默认配置照样能抽出正文
            try:
                text = (
                    trafilatura.extract(
                        downloaded, include_links=False, include_tables=True
                    )
                    or ""
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("trafilatura 抽取失败（默认 config）：%s", exc)
                text = ""

        try:
            meta = trafilatura.extract_metadata(downloaded)
            title = str(getattr(meta, "title", "") or "")
        except Exception:  # noqa: BLE001
            title = ""
        return (text or "").strip(), title

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
