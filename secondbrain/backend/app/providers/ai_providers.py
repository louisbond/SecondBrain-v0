"""AI Provider 层。

对齐 v0.5.0 §6.1：
| 链路 | 配置 | 角色 |
| workbuddy（主） | `wbuddy start/status` → `codebuddy -p ... -y --output-format json` → 方舟 API | 默认 |
| ollama（备） | `http://127.0.0.1:11434/v1`，Qwen-7B/MiniCPM | 自动降级 |
| openai_compat（通用） | 任意 OpenAI 兼容端点 | 手动切换 |

健康检查与超时：`wbuddy status` 轮询 5s/次总窗口 60s；health prompt 30s；正式调用 180s。
自动降级触发：方舟连续 ≥3 次失败 / 限流 429 / 额度耗尽 → 自动切 ollama 并 toast 告知；
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
# workbuddy / codebuddy → 方舟主链
# --------------------------------------------------------------------------

@dataclass
class WorkbuddyProvider:
    """CodeBuddy CLI（WorkBuddy 自带引擎）主链。

    流程：
    1. 定位 CLI（`cli_locator.locate_cli()`：WorkBuddy 安装目录 / 内置 bin/ / PATH）；
    2. `codebuddy daemon status` 检查；未运行则 `codebuddy daemon start` 并轮询；
    3. `codebuddy -p <prompt> -y --output-format json` 执行（走方舟 API 额度）；
    4. 解析 JSON 输出，剥离 CLI 的包装层，取出文本内容。

    与设计文档 v0.5.0 附录 A 的差异（实机核查后修正）：
    - `wbuddy start/status` → `codebuddy daemon start/status`（`wbuddy` 命令不存在）；
    - 子进程环境会剥离 `CODEBUDDY_SERVICE_PROXY_URL`（避免与 WorkBuddy 桌面端
      抢 5452 端口导致 EADDRINUSE）与代理变量（避免 CLI 内部 setProxy 崩溃）；
    - CLI 是 node 脚本，需带 node 解释器执行。
    """

    cfg: AppConfig
    name: str = "workbuddy"
    _cli_port: int = 0
    _last_auth_error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- CLI 定位 ---------------------------------------------------------
    def _cli(self):
        from app.core.cli_locator import locate_cli

        return locate_cli(self.cfg.ai.workbuddy.cli_path or None)

    def _run(self, args: list[str], timeout: float) -> tuple[int, str, str]:
        """执行 CLI（自动补 node 解释器、剥离冲突环境变量）。"""
        from app.core.cli_locator import run_cli

        info = self._cli()
        if info.path is None:
            return 127, "", "未找到 codebuddy CLI"
        return run_cli(args, timeout=int(timeout), cwd=info.path.parent, info=info)

    # -- 端口分配 ---------------------------------------------------------
    def _port(self) -> int:
        """给 CLI 分配一个空闲端口。

        这个 WorkBuddy 定制版 CLI 的 HTTP server 默认端口是**硬编码 5452**
        （见 dist/lazy-lite-wb/*.mjs），而 5452 正是 WorkBuddy 桌面端自己
        占用的服务代理端口 → EADDRINUSE。
        实测：主命令（`-p` / `--version`）接受 `--port`，但 `daemon` 子命令不接受。
        所以每次调用都显式指定一个空闲端口。
        """
        if self._cli_port:
            return self._cli_port
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", 0))
                self._cli_port = int(sock.getsockname()[1])
            except OSError:
                self._cli_port = 0
        return self._cli_port

    def _with_port(self, args: list[str]) -> list[str]:
        """给主命令参数注入 --port（子命令不能注入）。"""
        if args and args[0] in ("daemon", "ps", "logs", "attach", "kill", "stop", "rm",
                                "respawn", "agents", "mcp", "config", "sandbox",
                                "plugin", "project", "doctor", "update", "install",
                                "cleanup", "auto-mode"):
            return args
        port = self._port()
        if not port:
            return args
        return ["--port", str(port), *args]

    # -- 健康检查 ---------------------------------------------------------
    def health(self) -> ProviderHealth:
        """健康检查：`codebuddy --version`（快、免费、不需要认证）。"""
        start = time.time()
        info = self._cli()
        if info.path is None:
            return ProviderHealth(
                ok=False,
                detail=(
                    "未找到 codebuddy CLI。WorkBuddy 桌面版自带该引擎，"
                    "通常在 <安装目录>/resources/app.asar.unpacked/cli/bin/codebuddy"
                ),
                model="ark",
            )
        if info.is_node_script and not info.node:
            return ProviderHealth(
                ok=False, detail="codebuddy 是 node 脚本，但系统里找不到 node", model="ark"
            )

        code, out, err = self._run(["--version"], timeout=self.cfg.ai.workbuddy.health_timeout)
        detail = (out or err).strip()[:300]
        ok = code == 0
        if not ok:
            if "eaddrinuse" in detail.lower():
                detail = "端口冲突（EADDRINUSE）"
            return ProviderHealth(ok=False, detail=detail, model="ark")

        hint = ""
        if self._last_auth_error:
            hint = f"；⚠ {self._last_auth_error}"
        return ProviderHealth(
            ok=True,
            detail=f"CLI {detail}（{info.path}）{hint}",
            latency_ms=int((time.time() - start) * 1000),
            model="ark",
        )

    # -- 调用 -------------------------------------------------------------
    def complete(self, prompt: str, *, system: str = "") -> str:
        """非交互调用：codebuddy -p <prompt> -y --output-format json --tools "" """
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        args = self._with_port(
            [
                *self.cfg.ai.workbuddy.command,
                full_prompt,
                *self.cfg.ai.workbuddy.extra_args,
            ]
        )
        code, out, err = self._run(args, timeout=self.cfg.ai.workbuddy.timeout)
        text = (out or "").strip()

        if code != 0 or not text:
            blob = f"{out}\n{err}"
            low = blob.lower()
            if "authentication required" in low or "sign in" in low or "/login" in low:
                self._last_auth_error = (
                    "codebuddy CLI 未认证。两种可能："
                    "(1) 尚未登录——请在终端运行 `codebuddy`，选「Log in via Chinese Site」完成认证；"
                    "(2) 进程运行在受限环境里读不到认证文件"
                    "（日志特征：EPERM … auth/workbuddy-desktop.info）"
                    "——此时请让 SecondBrain 在沙箱外运行（自己双击 exe，或在自己终端里启动服务）"
                )
                raise ProviderError(self._last_auth_error, auth_required=True)
            if "eaddrinuse" in low:
                # 端口被抢：换一个端口重试一次
                self._cli_port = 0
                args = self._with_port(args[2:] if args[:2] == ["--port", args[1]] else args)
                code, out, err = self._run(args, timeout=self.cfg.ai.workbuddy.timeout)
                text = (out or "").strip()
                if code == 0 and text:
                    return self._extract_text(text)
                blob = f"{out}\n{err}"
                low = blob.lower()

            rate_limited = "429" in low or "rate limit" in low or "限流" in blob
            quota = any(
                k in low
                for k in ("quota", "insufficient", "余额不足", "额度", "credit exhausted")
            )
            raise ProviderError(
                f"codebuddy 调用失败（退出码 {code}）：{(err or out).strip()[:300]}",
                rate_limited=rate_limited,
                quota=quota,
            )

        self._last_auth_error = ""
        return self._extract_text(text)

    @staticmethod
    def _extract_text(raw: str) -> str:
        """从 codebuddy 输出中取文本内容。

        `--output-format json` 通常返回形如
          {"type":"result","result":"...","is_error":false}
        但不同版本结构有差异，这里做多级兜底解析。
        """
        raw = raw.strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # 可能是多行 JSON 或夹杂日志，取最后一个大括号块
            match = re.search(r"\{.*\}\s*$", raw, re.DOTALL)
            if not match:
                return raw
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError:
                return raw

        return WorkbuddyProvider._walk_json(payload)

    @staticmethod
    def _walk_json(node: Any) -> str:
        """在 JSON 结构里寻找文本内容。"""
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            parts = [WorkbuddyProvider._walk_json(x) for x in node]
            return "\n".join(p for p in parts if p)
        if isinstance(node, dict):
            for key in ("result", "text", "content", "message", "output", "response"):
                if key in node:
                    value = WorkbuddyProvider._walk_json(node[key])
                    if value:
                        return value
            for value in node.values():
                text = WorkbuddyProvider._walk_json(value)
                if text:
                    return text
        return ""


# --------------------------------------------------------------------------
# Ollama / OpenAI 兼容链
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
        if name == "workbuddy":
            return WorkbuddyProvider(cfg=cfg)
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
                # 不支持 timeout 参数的 provider（如 workbuddy CLI）走默认
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
        for name in ("workbuddy", "ollama", "openai_compat"):
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
