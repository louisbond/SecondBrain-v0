"""平台适配器注册。

导入本包即完成全部适配器注册（`@register` 装饰器）。
"""

# 导入各适配器模块触发注册
from app.adapters import (
    bilibili,  # noqa: F401,E402
    social_video,  # noqa: F401,E402
    web,  # noqa: F401,E402
    xiaohongshu,  # noqa: F401,E402
)
from app.adapters.base import (  # noqa: F401
    Adapter,
    ExtractResult,
    all_adapters,
    get_adapter,
    register,
)

__all__ = [
    "Adapter",
    "ExtractResult",
    "all_adapters",
    "get_adapter",
    "register",
]
