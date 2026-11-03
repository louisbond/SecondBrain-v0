"""AI Provider 层。

v34：**workbuddy 链路已整体移除**（原设计 v0.5.0 §6.1 的三链之一）。

为什么删：那条链靠调用 WorkBuddy 桌面版自带的 `codebuddy` CLI（`-p` 非交互），
本想"白借"桌面端已登录的授权来省 API key。但 CLI 在 SecondBrain 进程里
**读不到桌面端的认证文件**，永远返回 `auth_required` —— 挂着只会让用户
看到一条永远失败的链。连同 `cli_locator`（420 行 CLI 定位/登录脚本）一起删。

现存两条链：
| 链路 | 配置 | 角色 |
| openai_compat（主） | 任意 OpenAI 兼容端点（现配火山方舟豆包） | 默认 |
| ollama（备） | `http://127.0.0.1:11434/v1`，本地模型 | 自动降级 |

健康检查与超时：health prompt 30s；正式调用 180s。
自动降级触发：主链连续 ≥3 次失败 / 限流 429 / 额度耗尽 → 自动切 ollama 并 toast 告知；
恢复后手动切回（避免来回抖动）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import ClassVar
from typing import Any, Protocol

import httpx

from app.core.config import AppConfig, get_config
from app.core.http import make_client


class ProviderError(Exception):
    """Provider 调用失败。

    auth_required：认证类错误（主链本身健康，只是需要用户登录/配置密钥）。
    这类错误**不触发自动降级**——降级到一个同样没配置的备链只会更难排查。
    """

    def __init__(
        self,
        message: str,
        *,
        rate_limited: bool = False,
        quota: bool = False,
        auth_required: bool = False,
    ):
        super().__init__(message)
        self.rate_limited = rate_limited
        self.quota = quota
        self.auth_required = auth_required


@dataclass
class ProviderHealth:
    ok: bool
    detail: str = ""
    latency_ms: int = 0
    model: str = ""


class Provider(Protocol):
    """Provider 协议。"""

    name: str

    def health(self) -> ProviderHealth: ...

    def complete(self, prompt: str, *, system: str = "") -> str: ...


# --------------------------------------------------------------------------
# OpenAI 兼容端点（豆包 / ollama / 任意兼容服务）
# --------------------------------------------------------------------------

@dataclass
class OpenAICompatProvider:
    """OpenAI 兼容端点（Ollama 的 /v1 也走这个协议）。"""

    base_url: str
    model: str
    timeout: int = 300
    name: str = "ollama"
    api_key: str = ""

    def health(self) -> ProviderHealth:
        if not self.base_url:
            return ProviderHealth(ok=False, detail="未配置 base_url", model=self.model)
        start = time.time()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            with make_client(self.base_url, timeout=10) as client:
                resp = client.get(f"{self.base_url.rstrip('/')}/models", headers=headers)
                if resp.status_code >= 400:
                    detail = f"HTTP {resp.status_code}"
                    if resp.status_code in (401, 403):
                        detail += (
                            "（密钥无效或未配置——带密钥的端点必须设置 API Key）"
                            if self.api_key
                            else "（该端点需要 API Key，但当前未配置）"
                        )
                    return ProviderHealth(
                        ok=False,
                        detail=detail,
                        latency_ms=int((time.time() - start) * 1000),
                        model=self.model,
                    )
                return ProviderHealth(
                    ok=True,
                    detail="ok",
                    latency_ms=int((time.time() - start) * 1000),
                    model=self.model,
                )
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(
                ok=False,
                detail=str(exc)[:200],
                latency_ms=int((time.time() - start) * 1000),
                model=self.model,
            )

    def complete(
        self, prompt: str, *, system: str = "", timeout: float | None = None
    ) -> str:
        if not self.base_url:
            raise ProviderError("未配置 base_url")
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            with make_client(
                self.base_url, timeout=timeout or self.timeout
            ) as client:
                resp = client.post(
                    f"{self.base_url.rstrip('/')}/chat/completions",
                    json={"model": self.model, "messages": messages, "stream": False},
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"网络错误：{exc}") from exc

        if resp.status_code == 429:
            raise ProviderError("HTTP 429 限流", rate_limited=True)
        if resp.status_code in (401, 403):
            # 认证类错误：**不触发降级**（备链多半也没配），直接提示去配密钥
            raise ProviderError(
                f"HTTP {resp.status_code}：API Key 无效或未配置"
                f"（base_url={self.base_url}）",
                auth_required=True,
            )
        if resp.status_code >= 400:
            body = resp.text[:300]
            quota = any(
                k in body.lower() for k in ("quota", "insufficient", "余额", "额度")
            )
            raise ProviderError(f"HTTP {resp.status_code}: {body}", quota=quota)

        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(f"响应不是合法 JSON：{exc}") from exc

        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError("响应缺少 choices")
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "").strip()
        if not content:
            raise ProviderError("响应内容为空")
        return content


# --------------------------------------------------------------------------
# Provider 路由（自动降级）
# --------------------------------------------------------------------------

@dataclass
class ProviderRouter:
    """Provider 选择与自动降级。"""

    cfg: AppConfig
    _failure_streak: int = 0
    _current: str = ""
    _fallback_active: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _last_error: str = ""
    _on_fallback: Any = None

    def __post_init__(self) -> None:
        self._current = self.cfg.ai.provider
        self._fallback_active = False

    # -- 构造具体 provider ------------------------------------------------
    def _sync_cfg(self) -> AppConfig:
        """始终使用当前配置。

        `store.reload()` 是**整体替换** AppConfig 对象，路由器如果一直握着
        启动时那份旧对象，设置页新填的 api_key / base_url 永远不生效
        （表现为 401：密钥没带上）。
        """
        current = get_config()
        if current is not self.cfg:
            self.cfg = current
        return self.cfg

    def build(self, name: str) -> Provider | None:
        cfg = self._sync_cfg()
        if name == "ollama":
            o = cfg.ai.ollama
            return OpenAICompatProvider(
                base_url=o.base_url, model=o.model, timeout=o.timeout, name="ollama"
            )
        if name == "openai_compat":
            o = cfg.ai.openai_compat
            return OpenAICompatProvider(
                base_url=o.base_url,
                model=o.model,
                timeout=o.timeout,
                name="openai_compat",
                api_key=o.api_key,
            )
        return None

    @property
    def current_name(self) -> str:
        with self._lock:
            return self._current

    @property
    def fallback_active(self) -> bool:
        with self._lock:
            return self._fallback_active

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def set_fallback_listener(self, callback: Any) -> None:
        """降级时回调（用于 toast 通知）。"""
        self._on_fallback = callback

    def switch(self, name: str, manual: bool = True) -> None:
        """手动切换 provider（手动切回以避免抖动）。"""
        with self._lock:
            self._current = name
            self._fallback_active = False
            self._failure_streak = 0
            self._last_error = ""

    def _trigger_fallback(self, reason: str) -> None:
        """触发自动降级到 ollama。"""
        with self._lock:
            if not self._sync_cfg().ai.auto_fallback:
                return
            if self._fallback_active or self._current == "ollama":
                return
            self._fallback_active = True
            self._current = "ollama"
        if self._on_fallback:
            try:
                self._on_fallback(reason)
            except Exception:
                pass

    # -- 调用 -------------------------------------------------------------
    def complete(
        self, prompt: str, *, system: str = "", timeout: float | None = None
    ) -> str:
        with self._lock:
            name = self._current
        provider = self.build(name)
        if provider is None:
            raise ProviderError(f"未知的 provider：{name}")

        try:
            try:
                result = provider.complete(prompt, system=system, timeout=timeout)
            except TypeError:
                # 老式 provider 实现不接受 timeout 参数 → 退回不带 timeout 的调用
                result = provider.complete(prompt, system=system)
        except ProviderError as exc:
            # 认证类错误**不触发降级**：
            # 主链本身是好的，只是需要用户去登录一次。
            # 若把它当成"主链故障"降级到备链，备链多半也没起——反而更难排查。
            if exc.auth_required:
                with self._lock:
                    self._last_error = str(exc)
                    self._failure_streak = 0
                raise

            with self._lock:
                self._failure_streak += 1
                self._last_error = str(exc)
                streak = self._failure_streak
                threshold = self._sync_cfg().ai.fallback_threshold
                critical = exc.rate_limited or exc.quota

            if critical or streak >= threshold:
                self._trigger_fallback(
                    f"{name} 连续失败 {streak} 次（{exc}），自动降级到 ollama"
                )
                # 降级后立即用备链重试一次
                if name != "ollama":
                    backup = self.build("ollama")
                    if backup is not None:
                        try:
                            out = backup.complete(prompt, system=system)
                            with self._lock:
                                self._failure_streak = 0
                            return out
                        except ProviderError as exc2:
                            with self._lock:
                                self._failure_streak += 1
                                self._last_error = str(exc2)
                            raise ProviderError(
                                f"主链与备链均失败。主链：{exc}；备链：{exc2}"
                            ) from exc2
            raise

        with self._lock:
            self._failure_streak = 0
        return result

    # 健康检查缓存：网络探测不该每次都真打（设置页会反复调）
    # 注意：dataclass 里带注解的类属性会被当成字段 → 必须用 ClassVar
    _health_cache: ClassVar[dict[str, Any]] = {}
    _health_cache_at: ClassVar[float] = 0.0
    HEALTH_TTL: ClassVar[float] = 60.0

    def health(self, force: bool = False) -> dict[str, Any]:
        """所有 provider 的健康状态（设置页展示）。

        带 60 秒缓存——每次调用都打 /models 会把日志和网络刷爆，
        而健康状态本来就不需要秒级新鲜。设置页可传 force=True 强制刷新。
        """
        now = time.time()
        if (
            not force
            and self._health_cache
            and now - self._health_cache_at < self.HEALTH_TTL
        ):
            return dict(self._health_cache)

        out: list[dict[str, Any]] = []
        for name in ("ollama", "openai_compat"):
            provider = self.build(name)
            if provider is None:
                continue
            h = provider.health()
            out.append(
                {
                    "name": name,
                    "ok": h.ok,
                    "detail": h.detail,
                    "latency_ms": h.latency_ms,
                    "model": h.model,
                    "active": name == self.current_name,
                }
            )
        payload = {
            "providers": out,
            "current": self.current_name,
            "fallback_active": self.fallback_active,
            "last_error": self.last_error,
            "cached": bool(self._health_cache) and not force,
        }
        self._health_cache = payload
        self._health_cache_at = now
        return dict(payload)


_router: ProviderRouter | None = None


def get_router() -> ProviderRouter:
    global _router
    if _router is None:
        _router = ProviderRouter(cfg=get_config())
    return _router
