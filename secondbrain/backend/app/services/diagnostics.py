"""自检诊断（v39）。

以前这套检查逻辑**写在路由函数里**（`/api/workbench/diagnose`），
于是出现两个问题：

1. `/api/health` 是另一套写法，恒返回 `ok: true` —— 哪怕数据库坏了、
   vault 目录被删了，它也说"健康"。外部看门狗/用户自检会被骗。
2. 想在别处复用检查项，只能复制一份，改一处漏一处。

成熟软件的做法：**诊断是领域逻辑，不该住在路由里**。
现在统一收进本模块：
  · `collect_checks()` —— 跑一遍所有检查，返回明细
  · `critical_checks()` —— 哪些项不过就算"不健康"（vault / 数据库 / 数据可读）
  · `/api/health` 与 `/api/workbench/diagnose` 用同一份逻辑
"""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# 检查项名 → 是否属于"关键项"（关键项不健康 → health.ok = false）
CRITICAL = ("vault 存在", "数据库存在", "数据可读")

# 自检页按这两组排版：内部项横排成一行，外部网络项竖排逐条看。
GROUP_INTERNAL = "内部"
GROUP_EXTERNAL = "外部"


def _count(db: Any, sql: str) -> int:
    """COUNT(*) 必有一行；query_one 类型是 Row | None，这里显式收窄。"""
    row = db.query_one(sql)
    if row is None:
        raise RuntimeError(f"COUNT 查询没有返回行：{sql}")
    return int(row["c"])


def collect_checks() -> list[dict[str, Any]]:
    """跑一遍自检，返回 [{name, ok, detail, group}]。任何一项出错都不影响其它项。"""
    from app.core.config import get_config
    from app.core.db import get_db

    cfg = get_config()
    checks: list[dict[str, Any]] = []

    def _add(name: str, ok: bool, detail: str = "", group: str = GROUP_INTERNAL) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "group": group})

    # ---- 存储 ----
    _add("vault 存在", Path(cfg.vault.path).exists(), str(cfg.vault.path))
    _add("数据库存在", Path(cfg.db_path).exists(), str(cfg.db_path))

    # ---- 数据可读 ----
    try:
        db = get_db()
        n_items = _count(db, "SELECT COUNT(*) AS c FROM items")
        n_notes = _count(db, "SELECT COUNT(*) AS c FROM notes")
        n_parts = _count(db, "SELECT COUNT(*) AS c FROM partitions")
        _add("数据可读", True, f"条目 {n_items} / 笔记 {n_notes} / 分区 {n_parts}")
    except Exception as exc:  # noqa: BLE001
        _add("数据可读", False, str(exc))

    # ---- 索引 ----
    try:
        from app.services.index_service import get_index_service

        get_index_service()  # 确保实例可用
        n_indexed = _count(db, "SELECT COUNT(*) AS c FROM notes")
        _add("索引已建立", n_indexed > 0, f"已索引笔记 {n_indexed} 篇")
    except Exception as exc:  # noqa: BLE001
        _add("索引已建立", False, str(exc))

    # ---- AI 主链 ----
    try:
        from app.providers.ai_providers import get_router

        router = get_router()
        _add(
            "AI 主链",
            True,
            f"{router.current_name}（降级中：{router.fallback_active}）",
        )
    except Exception as exc:  # noqa: BLE001
        _add("AI 主链", False, str(exc))

    # ---- ASR ----
    try:
        from app.services.asr_service import get_asr_service

        info = get_asr_service().info()
        _add(
            "ASR 可用",
            bool(info.get("available")),
            f"引擎 {info.get('engine')} / 模型 {info.get('model_size')}",
        )
    except Exception as exc:  # noqa: BLE001
        _add("ASR 可用", False, str(exc))

    # ---- 磁盘 ----
    try:
        usage = shutil.disk_usage(str(cfg.data_dir))
        _add(
            "磁盘空间",
            usage.free > 500 * 1024 * 1024,
            f"剩余 {usage.free // (1024 * 1024)} MB",
        )
    except Exception as exc:  # noqa: BLE001
        _add("磁盘空间", False, str(exc))

    # ---- 外部网络（并行跑，单项都有超时，别让自检页挂住）----
    checks.extend(_external_checks())
    return checks


def _external_checks() -> list[dict[str, Any]]:
    """外部网络自检：区分「内网通 / 外网不通」这类最容易误判的情况。

    只在手动触发的 `/api/workbench/diagnose` 里跑，health 轮询不跑
    （见 collect_critical_checks），避免每 20 秒去敲一次外网。
    """
    from app.core import network
    from app.core.config import get_config

    cfg = get_config()
    ip = network.lan_ip()

    def item(name: str, ok: bool, detail: str) -> dict[str, Any]:
        return {"name": name, "ok": bool(ok), "detail": detail, "group": GROUP_EXTERNAL}

    def dns_check() -> dict[str, Any]:
        addr, err = network.dns_resolves()
        ok = bool(addr)
        return item("DNS 解析", ok, f"www.baidu.com → {addr}" if ok else f"解析失败：{err}")

    def web_check() -> dict[str, Any]:
        try:
            from app.core.http import make_client

            with make_client("https://www.baidu.com", timeout=4.0) as client:
                resp = client.get("https://www.baidu.com")
            return item("外网连通", resp.status_code < 500, f"HTTP {resp.status_code} · www.baidu.com")
        except Exception as exc:  # noqa: BLE001
            return item("外网连通", False, f"{type(exc).__name__}: {str(exc)[:120]}")

    def proxy_check() -> dict[str, Any]:
        proxy = network.system_proxy()
        if not proxy["enabled"]:
            return item("系统代理", True, "未启用（直连）")
        addr = network.parse_proxy_host(proxy["server"])
        if addr is None:
            return item("系统代理", True, f"已启用 {proxy['server']}（格式未识别）")
        reachable = network.tcp_reachable(*addr, timeout=2.0)
        hint = "可连接" if reachable else "连不上：SSL 报错 / 采集超时多半是它"
        return item("系统代理", reachable, f"{proxy['server']} · {hint}")

    def lan_check() -> dict[str, Any]:
        url = f"http://{ip}:{cfg.web.port}" if ip else "（取不到局域网 IP）"
        if cfg.web.allow_lan:
            return item("局域网入口", True, f"已开启 · 手机用 {url}（127.0.0.1 只在本机有效）")
        return item(
            "局域网入口",
            True,
            f"仅监听 127.0.0.1:{cfg.web.port}；手机要访问需开启 LAN 并重启，然后用 {url}",
        )

    out: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fn) for fn in (dns_check, web_check, proxy_check, lan_check)]
        for future, name in zip(
            futures, ("DNS 解析", "外网连通", "系统代理", "局域网入口"), strict=False
        ):
            try:
                out.append(future.result(timeout=8))
            except Exception as exc:  # noqa: BLE001
                out.append(item(name, False, f"检查异常：{type(exc).__name__} {exc}"))
    return out


def collect_critical_checks() -> list[dict[str, Any]]:
    """只跑**关键项**（vault / 数据库 / 数据可读）。

    `/api/health` 用它 —— 前端每 20 秒轮询一次健康，
    那里绝不能跑全套诊断（会碰 ASR、磁盘、Provider）。
    """
    from app.core.config import get_config
    from app.core.db import get_db

    cfg = get_config()
    checks: list[dict[str, Any]] = []

    def _add(name: str, ok: bool, detail: str = "") -> None:
        checks.append(
            {"name": name, "ok": bool(ok), "detail": detail, "group": GROUP_INTERNAL}
        )

    _add("vault 存在", Path(cfg.vault.path).exists(), str(cfg.vault.path))
    _add("数据库存在", Path(cfg.db_path).exists(), str(cfg.db_path))
    try:
        n_items = _count(get_db(), "SELECT COUNT(*) AS c FROM items")
        _add("数据可读", True, f"条目 {n_items}")
    except Exception as exc:  # noqa: BLE001
        _add("数据可读", False, str(exc))
    return checks


def summarize(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """把明细压成 {ok, degraded, failed}。

    · ok       —— 关键项全过（可以正常干活）
    · degraded —— 非关键项有问题（能用，但有功能不可用，比如 ASR 不可用）
    · failed   —— 关键项失败列表
    """
    failed = [c["name"] for c in checks if not c["ok"]]
    critical_failed = [c["name"] for c in checks if not c["ok"] and c["name"] in CRITICAL]
    return {
        "ok": not critical_failed,
        "degraded": bool(failed) and not critical_failed,
        "failed": failed,
        "critical_failed": critical_failed,
    }
