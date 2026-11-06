"""设置 API：provider 切换、vault 路径、重建索引、适配器能力、关机。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import get_config, store
from app.providers.ai_providers import get_router

router = APIRouter(prefix="/api/settings", tags=["settings"])


class ProviderRequest(BaseModel):
    provider: str


class LanRequest(BaseModel):
    allow_lan: bool
    token: str | None = None


class VaultRequest(BaseModel):
    path: str


class AutoFallbackRequest(BaseModel):
    enabled: bool


class AIKeyRequest(BaseModel):
    """OpenAI 兼容端点（火山方舟等）的接入参数。"""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    activate: bool = False
    clear: bool = False


class ParserApiRequest(BaseModel):
    """第三方解析 API（可选兜底）。"""

    enabled: bool = False
    url_template: str = ""
    auth_header: str = ""
    auth_value: str = ""
    clear_auth: bool = False
    timeout: int = 30


class ParserTestRequest(BaseModel):
    url: str = ""


@router.get("/ai-key")
async def get_ai_key() -> dict[str, Any]:
    """当前 OpenAI 兼容链的配置状态（密钥脱敏）。"""
    from app.core.config import mask_secret

    cfg = get_config()
    o = cfg.ai.openai_compat
    return {
        "configured": bool(o.api_key),
        "masked": mask_secret(o.api_key),
        "base_url": o.base_url,
        "model": o.model,
        "provider": cfg.ai.provider,
    }


@router.post("/ai-key")
async def set_ai_key(req: AIKeyRequest) -> dict[str, Any]:
    """保存 OpenAI 兼容链的密钥与端点（密钥写 secrets.json，不进 config.toml）。"""
    from app.core.config import (
        active_config_path,
        active_secret_path,
        mask_secret,
        save_secret,
        set_toml_value,
    )

    cfg = get_config()
    toml_path = active_config_path()

    if req.clear:
        secret_path = active_secret_path()
        if secret_path.exists():
            try:
                data = json.loads(secret_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            data.pop("ark_api_key", None)
            data.pop("openai_api_key", None)
            if data:
                secret_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            else:
                secret_path.unlink(missing_ok=True)
    elif req.api_key:
        save_secret("ark_api_key", req.api_key.strip())

    if req.base_url:
        set_toml_value(toml_path, "ai.openai_compat", "base_url", req.base_url.strip())
    if req.model:
        set_toml_value(toml_path, "ai.openai_compat", "model", req.model.strip())

    store.reload()
    cfg = get_config()

    result: dict[str, Any] = {
        "ok": True,
        "configured": bool(cfg.ai.openai_compat.api_key),
        "masked": mask_secret(cfg.ai.openai_compat.api_key),
        "base_url": cfg.ai.openai_compat.base_url,
        "model": cfg.ai.openai_compat.model,
    }

    if req.activate:
        router_ = get_router()
        router_.switch("openai_compat", manual=True)
        # 落盘：重启后仍用这条链
        from app.core.config import set_toml_value

        set_toml_value(toml_path, "ai", "provider", "openai_compat")
        store.set_runtime(provider="openai_compat")
        result["provider"] = router_.current_name

    return result


@router.get("")
async def get_settings() -> dict[str, Any]:
    """设置页总览。"""
    cfg = get_config()
    from app.adapters.base import all_adapters
    from app.services.asr_service import get_asr_service
    from app.services.embedding_service import get_embedding_service

    return {
        "vault": {
            "path": str(cfg.vault.path),
            "inbox": str(cfg.vault.dir("inbox")),
            "processed": str(cfg.vault.dir("processed")),
            "notes": str(cfg.vault.dir("notes")),
            "reports": str(cfg.vault.dir("reports")),
        },
        "web": {
            "host": cfg.web.host,
            "port": cfg.web.port,
            "allow_lan": cfg.web.allow_lan,
            "token_set": bool(cfg.web.token),
        },
        "ai": {
            "provider": cfg.ai.provider,
            "auto_fallback": cfg.ai.auto_fallback,
            "auto_start_ollama": cfg.ai.auto_start_ollama,
            "fallback_threshold": cfg.ai.fallback_threshold,
            "embedding_model": cfg.ai.embedding.model,
            "embedding_backend": get_embedding_service().backend,
            "embedding_real": get_embedding_service().available(),
        },
        "asr": get_asr_service().info(),
        "collect": {
            "min_chars": cfg.collect.min_chars,
            "web_timeout": cfg.collect.web_timeout,
            "batch_limit": cfg.collect.batch_limit,
            "cookie_file": str(cfg.collect.cookie_file or ""),
        },
        "adapters": all_adapters(),
        "parser": {
            "enabled": cfg.parser.enabled,
            "url_template": cfg.parser.url_template,
            "auth_header": cfg.parser.auth_header,
            "auth_value_set": bool(cfg.parser.auth_value),
            "timeout": cfg.parser.timeout,
        },
        "maintain": {
            "weekly_report": cfg.maintain.weekly_report,
            "weekly_report_weekday": cfg.maintain.weekly_report_weekday,
            "weekly_report_hour": cfg.maintain.weekly_report_hour,
        },
        "paths": {
            "data_dir": str(cfg.data_dir),
            "db_path": str(cfg.db_path),
            "bin_dir": str(cfg.bin_dir),
            "prompts_dir": str(cfg.prompts_dir),
        },
    }


@router.get("/providers")
async def providers() -> dict[str, Any]:
    """Provider 健康状态。

    v34：不再返回 `cli` 字段 —— workbuddy（codebuddy CLI）链路已整体移除，
    `cli_locator` 也一并删了。
    """
    return get_router().health()


@router.post("/provider")
async def switch_provider(req: ProviderRequest) -> dict[str, Any]:
    """手动切换 provider（手动切回以避免抖动）。

    同时写入 config.toml——否则重启后又回到旧链，用户会以为"切换没生效"。
    """
    if req.provider not in ("ollama", "openai_compat"):
        raise HTTPException(status_code=400, detail="未知 provider")

    from app.core.config import active_config_path, set_toml_value

    set_toml_value(active_config_path(), "ai", "provider", req.provider)
    get_router().switch(req.provider, manual=True)
    store.set_runtime(provider=req.provider)
    return {"ok": True, "provider": req.provider, "persisted": True}


@router.post("/provider/reset")
async def reset_provider() -> dict[str, Any]:
    """清除降级状态（恢复主链）。"""
    router_ = get_router()
    router_.switch(get_config().ai.provider, manual=True)
    return {"ok": True, "provider": router_.current_name}


@router.post("/auto-fallback")
async def set_auto_fallback(req: AutoFallbackRequest) -> dict[str, Any]:
    store.set_runtime(auto_fallback=req.enabled)
    return {"ok": True, "auto_fallback": req.enabled}


@router.post("/auto-start-ollama")
async def set_auto_start_ollama(req: AutoFallbackRequest) -> dict[str, Any]:
    """切到 ollama / 启动时是否自动拉起本机 ollama 进程。"""
    store.set_runtime(auto_start_ollama=req.enabled)
    return {"ok": True, "auto_start_ollama": req.enabled}


@router.get("/parser-api")
async def get_parser_api() -> dict[str, Any]:
    """第三方解析 API 配置（密钥只回打码值）。"""
    from app.core.config import mask_secret

    cfg = get_config().parser
    return {
        "enabled": cfg.enabled,
        "url_template": cfg.url_template,
        "auth_header": cfg.auth_header,
        "auth_value_masked": mask_secret(cfg.auth_value),
        "auth_value_set": bool(cfg.auth_value),
        "timeout": cfg.timeout,
        "ready": bool(cfg.enabled and cfg.url_template.strip()),
    }


@router.post("/parser-api")
async def set_parser_api(req: ParserApiRequest) -> dict[str, Any]:
    """保存第三方解析配置。

    密钥写 secrets.json，其余写 config.toml —— 和 ark key 同一套规矩，
    避免把密钥混进可能被分享/提交的配置文件。
    """
    from app.core.config import active_config_path, save_secret, set_toml_value

    toml_path = active_config_path()

    # 只允许 http(s)：URL 模板会被直接请求，别让 file:// 之类进来
    if req.url_template.strip():
        low = req.url_template.strip().lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            raise HTTPException(status_code=400, detail="接口地址必须是 http(s) 开头")

    set_toml_value(toml_path, "parser", "enabled", "true" if req.enabled else "false")
    set_toml_value(toml_path, "parser", "url_template", req.url_template.strip())
    set_toml_value(toml_path, "parser", "auth_header", req.auth_header.strip())
    set_toml_value(toml_path, "parser", "timeout", str(max(5, min(req.timeout, 120))))

    if req.clear_auth:
        save_secret("parser_api_auth", "")
    elif req.auth_value.strip():
        save_secret("parser_api_auth", req.auth_value.strip())

    store.reload()
    from app.core.config import mask_secret

    cfg = get_config().parser
    return {
        "ok": True,
        "enabled": cfg.enabled,
        "url_template": cfg.url_template,
        "auth_header": cfg.auth_header,
        "auth_value_masked": mask_secret(cfg.auth_value),
        "auth_value_set": bool(cfg.auth_value),
        "timeout": cfg.timeout,
        "ready": bool(cfg.enabled and cfg.url_template.strip()),
    }


@router.post("/parser-api/test")
async def test_parser_api(req: ParserTestRequest) -> dict[str, Any]:
    """用一条真实链接试解析，不写库。"""
    from app.services import parser_api

    target = req.url.strip()
    if not target:
        raise HTTPException(status_code=400, detail="请填一条要测试的链接")
    if not parser_api.get_parser_config().url_template.strip():
        raise HTTPException(status_code=400, detail="先填接口地址再测试")
    if not parser_api.get_parser_config().enabled:
        raise HTTPException(status_code=400, detail="先打开开关再测试")
    return parser_api.probe(target)


@router.post("/lan")
async def set_lan(req: LanRequest) -> dict[str, Any]:
    """LAN 开关（开启时强制 token）。"""
    import secrets

    cfg = get_config()
    if req.allow_lan and not cfg.web.token:
        token = req.token or secrets.token_urlsafe(32)
        store.set_runtime(token=token)
    store.set_runtime(allow_lan=req.allow_lan)
    return {
        "ok": True,
        "allow_lan": req.allow_lan,
        "token": cfg.web.token if req.allow_lan else None,
        "warning": "开启后需重启服务生效" if req.allow_lan else None,
    }


@router.post("/vault")
async def set_vault(req: VaultRequest) -> dict[str, Any]:
    """切换 vault 根目录。"""
    store.set_runtime(vault_path=req.path)
    return {"ok": True, "vault": req.path}


@router.post("/reindex")
async def reindex() -> dict[str, Any]:
    """重建索引（全库 md → notes/links/FTS）。"""
    from app.services.index_service import get_index_service

    return get_index_service().reindex_all()


@router.post("/scan")
async def scan() -> dict[str, Any]:
    """扫描存量 vault。"""
    from app.services.index_service import get_index_service

    return get_index_service().scan_vault().to_dict()


@router.post("/embeddings/rebuild")
async def rebuild_embeddings() -> dict[str, Any]:
    """全库向量重建。"""
    from app.services.embedding_service import get_embedding_service

    return get_embedding_service().rebuild_all()


@router.get("/prompt")
async def get_prompt() -> dict[str, Any]:
    """读取 prompt 模板（可编辑）。"""
    from app.providers.ai_service import load_prompt_template

    return {"template": load_prompt_template()}


@router.put("/prompt")
async def put_prompt(req: dict[str, str]) -> dict[str, Any]:
    """保存 prompt 模板。"""
    from app.providers.ai_service import save_prompt_template

    template = req.get("template", "")
    if not template.strip():
        raise HTTPException(status_code=400, detail="模板不能为空")
    path = save_prompt_template(template)
    return {"ok": True, "path": path.as_posix()}


@router.post("/shutdown")
async def shutdown() -> dict[str, Any]:
    """关闭服务。"""
    import os
    import threading

    def _stop() -> None:
        import time

        time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=_stop, daemon=True).start()
    return {"ok": True, "message": "服务即将关闭"}


# ---- 开机自启 / 电源 ---------------------------------------------------

@router.get("/autostart")
async def autostart_status() -> dict[str, Any]:
    """开机自启状态 + 电源策略建议。"""
    from app.services.autostart_service import autostart_status, power_suggestion

    return {
        "autostart": autostart_status(),
        "power": power_suggestion(),
    }


@router.post("/autostart/enable")
async def autostart_enable() -> dict[str, Any]:
    """注册开机自启（任务计划程序）。"""
    from app.services.autostart_service import enable_autostart

    result = enable_autostart()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "注册失败"))
    return result


@router.post("/autostart/disable")
async def autostart_disable() -> dict[str, Any]:
    """注销开机自启。"""
    from app.services.autostart_service import disable_autostart

    result = disable_autostart()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "注销失败"))
    return result


@router.post("/power/never-sleep")
async def power_never_sleep() -> dict[str, Any]:
    """设置接通电源时永不睡眠（需管理员权限）。"""
    from app.services.autostart_service import set_power_never_sleep

    result = set_power_never_sleep()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "设置失败"))
    return result


@router.get("/free-port")
async def free_port(start: int = 7688) -> dict[str, Any]:
    """探测可用端口（被占自动顺延）。"""
    from app.services.autostart_service import find_free_port

    port = find_free_port(start)
    return {"port": port, "shifted": port != start}
