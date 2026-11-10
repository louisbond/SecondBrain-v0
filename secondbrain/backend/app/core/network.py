"""网络环境探测（自检诊断 + 局域网访问提示共用）。

只回答三个问题，不掺业务逻辑：

- 这台机器在局域网里叫什么 —— `lan_ip()`。手机访问要用它，
  **不是 127.0.0.1**：回环地址在手机上指向手机自己，永远连不到电脑。
- 系统代理开着吗、连得上吗 —— `system_proxy()` / `tcp_reachable()`。
  之前快手采集报 SSL 错，真因就是 Clash 代理 TLS 拦截，这类问题要能自查。
- DNS 能不能解析 —— `dns_resolves()`。「内网通、外网不通」最常见就是它。

所有函数都必须**有超时**：自检页不能因为一个外网请求挂住整个页面。
"""

from __future__ import annotations

import os
import socket
import threading
from typing import Any
from urllib.parse import urlparse

_DEFAULT_DNS_HOST = "www.baidu.com"


def lan_ip() -> str:
    """出口网卡的局域网 IPv4（UDP connect 不实际发包，只让内核选路由）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(1.0)
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        try:
            return str(socket.gethostbyname(socket.gethostname()))
        except OSError:
            return ""
    finally:
        sock.close()


def system_proxy() -> dict[str, Any]:
    """读 Windows 系统代理设置；非 Windows/读不到时退回环境变量。"""
    enabled = False
    server = ""
    if os.name == "nt":
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                enabled = bool(winreg.QueryValueEx(key, "ProxyEnable")[0])
                server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
        except (OSError, ImportError):
            pass
    if not server:
        server = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or ""
        )
        if server:
            enabled = True
    return {"enabled": enabled, "server": server.strip()}


def parse_proxy_host(server: str) -> tuple[str, int] | None:
    """从 `127.0.0.1:26561` 或 `http=host:port;https=...` 里取出 host/port。"""
    raw = (server or "").strip()
    if not raw:
        return None
    # 分协议写法：http=127.0.0.1:7890;https=127.0.0.1:7890
    if "=" in raw and ";" in raw:
        raw = next(
            (seg.split("=", 1)[1] for seg in raw.split(";") if seg.startswith("http=")),
            raw.split(";")[0],
        )
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urlparse(raw)
        if not parsed.hostname:
            return None
        return parsed.hostname, parsed.port or 8080
    except ValueError:
        return None


def tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def dns_resolves(host: str = _DEFAULT_DNS_HOST, timeout: float = 3.0) -> tuple[str, str]:
    """返回 (解析到的 IP, 错误文本)。getaddrinfo 在 Windows 上没有超时，用线程兜。"""
    result: dict[str, str] = {}

    def _work() -> None:
        try:
            result["addr"] = str(socket.getaddrinfo(host, 443)[0][4][0])
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=_work, daemon=True, name="sb-dns-check")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return "", f"超时（>{timeout:g}s）"
    return result.get("addr", ""), result.get("error", "")
