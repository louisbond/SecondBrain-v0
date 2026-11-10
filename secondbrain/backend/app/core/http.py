"""HTTP 客户端工厂。

**为什么需要这个模块**：httpx 默认 `trust_env=True`，会读取
`HTTP_PROXY` / `HTTPS_PROXY` 环境变量。用户机器上常开着代理（Clash 等），
而本机的 Ollama(127.0.0.1:11434)、局域网内的 openai_compat 端点
**绝不能走代理**——否则会得到莫名其妙的 502 / 连接拒绝。

规则：
- 目标是回环地址（127.0.0.1 / localhost / ::1）→ 绕过代理；
- 目标是内网网段（10.x / 172.16-31.x / 192.168.x）→ 绕过代理；
- 其余（公网：B站、抖音、方舟等）→ 沿用环境代理。
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

import httpx

LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


def is_local_endpoint(url: str) -> bool:
    """判断 URL 是否指向本机/内网（应当绕过代理）。"""
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    if not host:
        return False

    # 去掉端口与用户信息
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if host.startswith("["):  # noqa: SIM108  # IPv6 字面量（三元表达式反而更难读）
        host = host[1:].split("]", 1)[0]
    else:
        host = host.split(":", 1)[0]

    if host in LOCAL_HOSTNAMES:
        return True

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def make_client(
    url: str = "",
    *,
    timeout: float | httpx.Timeout = 20.0,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = True,
    **kwargs: object,
) -> httpx.Client:
    """按目标地址创建 httpx 客户端——本地端点自动绕过代理。

    TLS 注入本来在 `main.py` 启动时做一次；但诊断、后台任务、单测都可能
    在 lifespan 之前就用这个工厂发请求，那时还是 certifi → 国内站点直接
    `CERTIFICATE_VERIFY_FAILED`。这里补一次幂等注入，让工厂自己就是对的。
    """
    from app.core.tls import enable_system_trust_store

    enable_system_trust_store()
    trust_env = not is_local_endpoint(url) if url else True
    return httpx.Client(
        timeout=timeout,
        headers=headers,
        follow_redirects=follow_redirects,
        trust_env=trust_env,
        **kwargs,  # type: ignore[arg-type]
    )
