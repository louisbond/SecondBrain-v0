"""B站适配器（一等公民）。

对齐 v0.5.0 §5.2：
| 平台 | 路径 | 可得内容 | 兜底 |
| B站 | bilibili-api + 用户 cookie（含 AI 字幕获取） | 标题、简介、UP 主字幕/AI 字幕、封面 |
|      | 无字幕→ASR；接口失效→yt-dlp→手动粘贴 |

实现策略（不引入 bilibili-api 第三方依赖，直接走公开 web API，降低安装摩擦）：
1. 展开 b23.tv 短链 → 拿 BV 号；
2. `view` 接口拿标题/简介/cid；
3. `player/wbi/v2` 或 `x/player/v2` 接口拿字幕列表（含 AI 字幕）；
4. 字幕 URL 拿正文；
5. 无字幕 → 交给 yt-dlp 抽音频 → ASR 管线（needs_asr=True）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from app.adapters.base import Adapter, ExtractResult, register
from app.core.http import make_client
from app.core.parser import bvid_length_hint, extract_bvid

BILI_API = "https://api.bilibili.com"
BILI_WEB = "https://www.bilibili.com"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


@register
class BilibiliAdapter(Adapter):
    name = "bilibili"
    label = "B站"
    support_level = "full"
    supports_audio = True
    supports_subtitle = True

    def __init__(self, cookie: str | None = None, timeout: int = 20) -> None:
        super().__init__(cookie=cookie, timeout=timeout)
        self._client: httpx.Client | None = None

    # -- HTTP -------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": _UA,
            "Referer": f"{BILI_WEB}/",
            "Accept": "application/json, text/plain, */*",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client(
                BILI_WEB,
                timeout=self.timeout,
                headers=self._headers(),
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- 短链展开 ---------------------------------------------------------
    def expand(self, url: str) -> str:
        if "b23.tv" not in url:
            return url
        try:
            resp = self.client.get(url)
            final = str(resp.url)
            bvid = extract_bvid(final)
            if bvid:
                return f"{BILI_WEB}/video/{bvid}"
            return final
        except Exception:
            return url

    # -- 主流程 -----------------------------------------------------------
    def extract(self, url: str) -> ExtractResult:
        try:
            url = self.expand(url)
            bvid = extract_bvid(url)
            if not bvid:
                return ExtractResult(
                    ok=False,
                    extractor="bilibili_api",
                    error="无法从链接中解析 BV 号（链接里没有 BV 开头的那串）",
                )

            # BV 号位数不对 → 明确告诉用户「链接可能被截断」
            hint = bvid_length_hint(bvid)

            view = self._get_json(f"{BILI_API}/x/web-interface/view?bvid={bvid}")
            if not view or view.get("code") != 0:
                msg = (view or {}).get("message") or "B站 view 接口无响应"
                if hint:
                    return ExtractResult(
                        ok=False, extractor="bilibili_api", error=f"{hint}（{msg}）"
                    )
                return ExtractResult(
                    ok=False, extractor="bilibili_api", error=f"{msg}（可能需要 cookie）"
                )

            data = view.get("data") or {}
            title = str(data.get("title") or "").strip()
            desc = str(data.get("desc") or "").strip()
            cid = data.get("cid")
            owner = (data.get("owner") or {}).get("name") or ""
            duration = int(data.get("duration") or 0)

            subtitle_text = ""
            subtitle_type = ""
            if cid:
                subtitle_text, subtitle_type = self._fetch_subtitle(bvid, cid)

            body_parts: list[str] = []
            if owner:
                body_parts.append(f"UP主：{owner}")
            if desc:
                body_parts.append(f"简介：{desc}")
            if subtitle_text:
                body_parts.append(subtitle_text)
            elif desc:
                body_parts.append("（本视频未提供字幕，正文仅为简介）")

            text = "\n\n".join(body_parts)

            if not subtitle_text:
                # 无字幕 → 走 ASR 路径（yt-dlp 抽音频）
                return ExtractResult(
                    ok=True,
                    title=title,
                    text=text,
                    extractor="bilibili_api",
                    asr=False,
                    needs_asr=True,
                    extra={
                        "bvid": bvid,
                        "cid": cid,
                        "duration": duration,
                        "owner": owner,
                        "has_subtitle": False,
                    },
                )

            return ExtractResult(
                ok=True,
                title=title,
                text=text,
                extractor="bilibili_api",
                asr=False,
                extra={
                    "bvid": bvid,
                    "cid": cid,
                    "duration": duration,
                    "owner": owner,
                    "has_subtitle": True,
                    "subtitle_type": subtitle_type,
                },
            )
        except httpx.HTTPError as exc:
            return ExtractResult(
                ok=False, extractor="bilibili_api", error=f"网络错误：{exc}"
            )
        except Exception as exc:  # noqa: BLE001
            return ExtractResult(
                ok=False, extractor="bilibili_api", error=f"抓取异常：{exc}"
            )

    # -- 字幕 -------------------------------------------------------------
    def _get_json(self, url: str, retries: int = 2) -> dict[str, Any] | None:
        for attempt in range(retries + 1):
            try:
                resp = self.client.get(url)
                if resp.status_code >= 500 and attempt < retries:
                    time.sleep(1.0 + attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception:
                if attempt < retries:
                    time.sleep(1.0 + attempt)
                    continue
                return None
        return None

    def _fetch_subtitle(self, bvid: str, cid: int) -> tuple[str, str]:
        """取字幕正文。返回 (正文, 类型)。"""
        payload = self._get_json(
            f"{BILI_API}/x/player/v2?bvid={bvid}&cid={cid}"
        )
        if not payload or payload.get("code") != 0:
            return "", ""

        data = payload.get("data") or {}
        subtitle = data.get("subtitle") or {}
        subtitle_list = subtitle.get("subtitles") or []

        for item in subtitle_list:
            subtitle_url = str(item.get("subtitle_url") or "")
            if not subtitle_url:
                continue
            if subtitle_url.startswith("//"):
                subtitle_url = "https:" + subtitle_url
            lan = str(item.get("lan") or "")
            lan_doc = str(item.get("lan_doc") or "")
            body = self._download_subtitle(subtitle_url)
            if body:
                kind = "ai" if lan.startswith("ai") or "自动" in lan_doc else "up"
                return body, kind
        return "", ""

    def _download_subtitle(self, url: str) -> str:
        try:
            resp = self.client.get(url)
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            return ""

        body = payload.get("body") or []
        lines: list[str] = []
        for entry in body:
            content = str(entry.get("content") or "").strip()
            if content:
                lines.append(content)
        return "\n".join(lines)


def parse_cookie_file(path: str) -> str | None:
    """读取 Netscape 格式或纯文本 cookie 文件。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return None
    if not raw.strip():
        return None

    if "\t" in raw and "SESSDATA" in raw:
        pairs: list[str] = []
        for line in raw.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) >= 7:
                pairs.append(f"{cols[5]}={cols[6]}")
        return "; ".join(pairs) if pairs else None

    if "SESSDATA" in raw and "=" in raw:
        return raw.strip().replace("\n", "; ")
    return None
