"""TLS 信任库：优先用操作系统证书存储，缺失时回退 certifi。

**为什么需要这个模块**：部分 CDN 在握手时下发**交叉签名**的根证书。实测
`ark.cn-beijing.volces.com`（火山方舟）给出的链是：

    leaf(ark.*) → RapidSSL TLS RSA CA G1 → DigiCert Global Root G2(交叉签名版) → DigiCert Global Root CA

中间那层是"由老根签发"的版本，而 Python 自带的 certifi 里只有**自签版**的
DigiCert Global Root G2。OpenSSL 只会沿服务端给的这一条链往上走，走到
`DigiCert Global Root CA` 找不到，就报：

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    unable to get local issuer certificate (_ssl.c:1032)

浏览器和 curl 不报错，是因为它们用系统证书存储，并且会**构建多条候选链**——
能从自签版根出发找到另一条合法路径。`truststore` 把 Python 的默认 SSL 上下文
直接接到操作系统存储上，校验行为就与浏览器一致了（依赖 urllib3 官方推荐做法）。

这是**可选优化**：导入或注入失败时静默回退 certifi，绝不影响启动。
"""

from __future__ import annotations

import logging
from typing import Any

try:  # 顶层 import，便于 PyInstaller 静态收集；缺失时不报错
    import truststore as _truststore
except Exception:  # pragma: no cover - 环境缺少可选依赖
    _truststore = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_enabled = False


def enable_system_trust_store() -> bool:
    """把 TLS 校验切换到操作系统信任库（幂等）。

    返回是否已处于"系统信任库"模式。重复调用不会再注入一次。
    """
    global _enabled
    if _enabled:
        return True

    mod: Any = _truststore
    if mod is None:
        logger.info("未安装 truststore，TLS 校验继续使用内置 certifi")
        return False

    try:
        mod.inject_into_ssl()
    except Exception as exc:  # noqa: BLE001 - 任何异常都只降级，不阻断启动
        logger.warning("切换系统信任库失败，回退 certifi：%s", exc)
        return False

    _enabled = True
    logger.info("TLS 校验已切换到操作系统信任库")
    return True
