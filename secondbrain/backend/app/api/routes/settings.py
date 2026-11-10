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
    from app.core.network import lan_ip
    from app.services.asr_service import get_asr_service
    from app.services.embedding_service import get_embedding_service

    ip = lan_ip()
    base_url = f"http://{ip}:{cfg.web.port}" if ip else ""
    # 安全整改（2026-11-07）：只在真正开启 LAN 时才生成带 token 的分享链接。
    # 关闭 LAN 时这个链接毫无用处，却会把 token 明文暴露给任何能读到本接口的一方
    # （原实现配合 allow_origins=["*"]，等于任意网页都能拿走 token 接管服务）。
    share_url = (
        f"{base_url}/?token={cfg.web.token}"
        if base_url and cfg.web.token and cfg.web.allow_lan
        else base_url
    )
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
            # 手机访问必须用局域网 IP；127.0.0.1 在手机上指向手机自己。
            "lan_ip": ip,
            "lan_url": base_url,
            # 带 token 的完整链接：在手机上打开一次，前端会把 token 存进 localStorage
            # 安全整改（2026-11-07）：不再回传 token 明文。
            # 前端只用 share_url（复制手机链接），不读 token 字段；
            # token 只在「设置 → 开启 LAN」那一次的响应里返回给用户。
            "share_url": share_url,
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


class OllamaSettingsRequest(BaseModel):
    model: str = ""
    # 新：按链保存（kind + provider + model + think）
    kind: str = ""
    provider: str = ""
    think: bool | None = None
    model_extract: str | None = None
    model_create: str | None = None
    think_extract: bool | None = None
    think_create: bool | None = None


@router.get("/ollama")
async def get_ollama_settings() -> dict[str, Any]:
    """本地模型设置：当前值 + 已装模型列表（采集区 / 加工区就地控件用）。"""
    import httpx

    cfg = get_config()
    o = cfg.ai.ollama
    root = o.base_url.replace("/v1", "").rstrip("/")
    models: list[str] = []
    err = ""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{root}/api/tags")
        # 去掉 :latest 这类 tag，只留短名 —— 配置里写的也是短名
        models = sorted(
            {
                m.get("name", "").split(":")[0]
                for m in resp.json().get("models", [])
                if m.get("name")
            }
        )
    except Exception as exc:  # noqa: BLE001 - ollama 没起也要能打开设置页
        err = str(exc)[:120]
    return {
        "ok": not err,
        "error": err,
        "models": models,
        "model": o.model,
        "model_extract": o.model_extract,
        "model_create": o.model_create,
        "think_extract": o.think_extract,
        "think_create": o.think_create,
    }


@router.post("/ollama")
async def save_ollama_settings(req: OllamaSettingsRequest) -> dict[str, Any]:
    """保存本地模型选择与思考开关（写 config.toml，store.reload 热生效，无需重启）。"""
    from app.core.config import active_config_path, set_toml_value

    toml_path = active_config_path()
    if req.model:
        set_toml_value(toml_path, "ai.ollama", "model", req.model.strip())
    if req.model_extract is not None:
        set_toml_value(
            toml_path, "ai.ollama", "model_extract", req.model_extract.strip()
        )
    if req.model_create is not None:
        set_toml_value(toml_path, "ai.ollama", "model_create", req.model_create.strip())
    if req.think_extract is not None:
        set_toml_value(
            toml_path,
            "ai.ollama",
            "think_extract",
            "true" if req.think_extract else "false",
            raw=True,
        )
    if req.think_create is not None:
        set_toml_value(
            toml_path,
            "ai.ollama",
            "think_create",
            "true" if req.think_create else "false",
            raw=True,
        )
    store.reload()
    o = get_config().ai.ollama
    return {
        "ok": True,
        "model": o.model,
        "model_extract": o.model_extract,
        "model_create": o.model_create,
        "think_extract": o.think_extract,
        "think_create": o.think_create,
    }


# OpenAI 兼容链的候选模型（预设 + 用户当前值；界面允许手输任意型号）
OPENAI_MODEL_PRESETS = [    "doubao-seed-2-1-pro-260915",
    "doubao-1-5-thinking-pro-250415",
    "doubao-1-5-pro-32b-250115",
    "doubao-1-5-lite-32k-250115",
    "deepseek-chat",
    "deepseek-reasoner",
]


async def _models_for(name: str, cfg: Any) -> tuple[list[str], str]:
    """列某个 provider 可选的模型。

    ollama → 读本机 /api/tags（装了什么列什么）；
    openai_compat → 内置预设 + 当前配置里用过的型号（允许手输任意型号）。
    """
    seg = cfg.ai.ollama if name == "ollama" else cfg.ai.openai_compat
    if name == "ollama":
        root = (seg.base_url or "").replace("/v1", "").rstrip("/")
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{root}/api/tags")
            return (
                sorted(
                    {
                        str(m.get("name", "")).split(":")[0]
                        for m in resp.json().get("models", [])
                        if m.get("name")
                    }
                ),
                "",
            )
        except Exception as exc:  # noqa: BLE001
            return [], str(exc)[:120]

    models = list(OPENAI_MODEL_PRESETS)
    for m in (seg.model, seg.model_extract, seg.model_create):
        m = (m or "").strip()
        if m and m not in models:
            models.insert(0, m)
    return models, ""


def _chain_provider(cfg: Any, kind: str) -> str:
    """某条链实际用哪个 provider（空 = 跟随全局）。"""
    name = (
        (cfg.ai.provider_create if kind == "create" else cfg.ai.provider_extract) or ""
    ).strip()
    return name or (get_router().current_name or cfg.ai.provider or "ollama")


@router.get("/ai-model")
async def get_ai_model_settings() -> dict[str, Any]:
    """就地模型控件的数据源：**按链路分别返回** provider / 模型 / 思考开关。

    两条链可以各用各的 provider（提取走本机 ollama 免费求快、创作走 API
    求质量），所以 models 也必须按该链自己的 provider 去列 —— 否则会出现
    「本地链却只看到 API 型号」这种对不上的情况。
    """
    cfg = get_config()
    chains: dict[str, Any] = {}
    err = ""
    for kind in ("extract", "create"):
        name = _chain_provider(cfg, kind)
        models, e = await _models_for(name, cfg)
        err = err or e
        seg = cfg.ai.ollama if name == "ollama" else cfg.ai.openai_compat
        model = (seg.model_create if kind == "create" else seg.model_extract) or seg.model
        chains[kind] = {
            "provider": name,
            "provider_label": "本地" if name == "ollama" else "API",
            "models": models,
            "model": model,
            "think": bool(seg.think_create if kind == "create" else seg.think_extract),
            "think_supported": name == "ollama",
            "override": bool(
                (cfg.ai.provider_create if kind == "create" else cfg.ai.provider_extract)
                or ""
            ),
            "error": e,
        }

    # 两个 provider 各自的可选型号（前端在控件里就地切「本地 / API」时要现拿）
    available: dict[str, Any] = {}
    for pname in ("ollama", "openai_compat"):
        plist, perr = await _models_for(pname, cfg)
        pseg = cfg.ai.ollama if pname == "ollama" else cfg.ai.openai_compat
        available[pname] = {
            "label": "本地" if pname == "ollama" else "API",
            "models": plist,
            "error": perr,
            "default_model": pseg.model,
        }

    return {
        "ok": not err,
        "error": err,
        "provider": get_router().current_name or cfg.ai.provider,
        "chains": chains,
        "providers": available,
    }


@router.post("/ai-model")
async def save_ai_model_settings(req: OllamaSettingsRequest) -> dict[str, Any]:
    """保存某条链的 provider / 模型 / 思考开关（写 config.toml，热生效）。

    新写法（前端就地控件用）：{kind, provider, model, think}
    老写法（双字段一次性保存）：{model_extract, model_create, think_extract, ...}
    """
    from app.core.config import active_config_path, set_toml_value

    toml_path = active_config_path()
    kind = (req.kind or "").strip()
    global_name = get_router().current_name or get_config().ai.provider or "ollama"

    if kind in ("extract", "create"):
        provider = (req.provider or "").strip()
        # 空字符串 = 跟随全局链路（写空值，而不是删键）
        set_toml_value(toml_path, "ai", f"provider_{kind}", provider)
        section = (
            "ai.ollama" if (provider or global_name) == "ollama" else "ai.openai_compat"
        )
        set_toml_value(toml_path, section, f"model_{kind}", (req.model or "").strip())
        if req.think is not None:
            set_toml_value(
                toml_path,
                section,
                f"think_{kind}",
                "true" if req.think else "false",
                raw=True,
            )
    else:
        section = "ai.ollama" if global_name == "ollama" else "ai.openai_compat"
        if req.model_extract is not None:
            set_toml_value(toml_path, section, "model_extract", req.model_extract.strip())
        if req.model_create is not None:
            set_toml_value(toml_path, section, "model_create", req.model_create.strip())
        if req.think_extract is not None:
            set_toml_value(
                toml_path,
                section,
                "think_extract",
                "true" if req.think_extract else "false",
                raw=True,
            )
        if req.think_create is not None:
            set_toml_value(
                toml_path,
                section,
                "think_create",
                "true" if req.think_create else "false",
                raw=True,
            )

    store.reload()
    return await get_ai_model_settings()


@router.get("/parser-api/presets")
async def parser_api_presets() -> dict[str, Any]:
    """可选的第三方解析服务清单（静态目录，不联网、不代表已连通）。

    前端用它铺一个下拉列表：选中即填模板，省得用户自己去翻文档找接口地址。
    只收 GET + query 传链接的服务 —— 本模块的调用方式就是 GET。
    """
    from app.services import parser_api

    return {"presets": parser_api.preset_list()}


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
    """用一条真实链接试解析，不写库。

    开关没打开也允许测 —— 先验证服务通不通，再决定要不要开。
    """
    from app.services import parser_api

    target = req.url.strip()
    if not target:
        raise HTTPException(status_code=400, detail="请填一条要测试的链接")
    if not parser_api.get_parser_config().url_template.strip():
        raise HTTPException(status_code=400, detail="先选一个服务或填接口地址再测试")
    return parser_api.probe(target)


@router.post("/lan")
async def set_lan(req: LanRequest) -> dict[str, Any]:
    """LAN 开关（开启时强制 token）。

    v0.7.5 之前只改运行时：点完看着生效，重启就回到「只监听 127.0.0.1」，
    token 也丢了 —— 之前复制到手机上的链接重启后必然 401。现在两样都落盘：
    `allow_lan` 写 config.toml，token 写 secrets.json（不进可分享的配置文件）。
    """
    import secrets

    from app.core.config import active_config_path, save_secret, set_toml_value

    cfg = get_config()
    token = (req.token or cfg.web.token or "").strip()
    if req.allow_lan and not token:
        token = secrets.token_urlsafe(32)

    save_secret("web_token", token if req.allow_lan else "")
    set_toml_value(
        active_config_path(), "web", "allow_lan", "true" if req.allow_lan else "false"
    )
    store.reload()
    cfg = get_config()
    return {
        "ok": True,
        "allow_lan": cfg.web.allow_lan,
        "token": cfg.web.token if req.allow_lan else None,
        "warning": (
            "已写入配置：监听地址将改为 0.0.0.0，需重启服务生效（手机才能连上）"
            if req.allow_lan
            else None
        ),
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


# ---------------------------------------------------------------------------
# 提示词模板（提取链）
# ---------------------------------------------------------------------------

DEFAULT_COLLECT_TEMPLATE = """你是采集助手。从给定网页/视频信息里提取结构化原文。

要求：
1. 保留原文的标题、正文字幕/简介、作者、发布时间；
2. 不要改写、不要总结，只做清洗（去掉广告、导航、无关推荐）；
3. 输出纯文本，段落之间空一行。

【来源信息】
{content}
"""

PROMPT_PLACEHOLDERS = [    "{content}",
    "{content_type}",
    "{style_hint}",
    "{partitions_list}",
    "{tags_list}",
]


class PromptRequest(BaseModel):
    text: str = ""
    # 提示词归属：extract（面向接口）/ collect（采集）
    scope: str = "extract"


@router.get("/prompts")
async def get_prompts() -> dict[str, Any]:
    """提取链提示词：当前生效内容 + 内置默认 + 是否被自定义。

    创作链（模板化生成）的提示词在「管理 → 解析模板」里改，不在这里。
    """
    from app.providers.ai_service import DEFAULT_PROMPT_TEMPLATE, load_prompt_template

    cfg = get_config()

    def _read(name: str, default: str) -> dict[str, Any]:
        p = cfg.prompts_dir / f"{name}.txt"
        custom = False
        try:
            if p.exists():
                txt = p.read_text(encoding="utf-8").strip()
                if txt:
                    return {
                        "text": txt,
                        "default": default,
                        "custom": True,
                        "path": str(p),
                        "placeholders": PROMPT_PLACEHOLDERS,
                    }
        except OSError:
            custom = False
        return {
            "text": default,
            "default": default,
            "custom": custom,
            "path": str(p),
            "placeholders": PROMPT_PLACEHOLDERS,
        }

    return {
        "extract": _read("extract", DEFAULT_PROMPT_TEMPLATE),
        "collect": _read("collect", DEFAULT_COLLECT_TEMPLATE),
    }


@router.post("/prompts")
async def save_prompts(req: PromptRequest) -> dict[str, Any]:
    """保存提取链提示词到 prompts/extract.txt。下一次调用即生效（每次现读）。"""
    cfg = get_config()
    scope = (req.scope or "extract").strip()
    if scope not in ("extract", "collect"):
        raise HTTPException(status_code=400, detail=f"未知的提示词范围：{scope}")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="提示词不能为空")
    if scope == "extract" and "{content}" not in text:
        raise HTTPException(
            status_code=400,
            detail="提示词必须包含 {content} 占位符，否则模型看不到原文",
        )
    try:
        cfg.prompts_dir.mkdir(parents=True, exist_ok=True)
        # 按 scope 写各自的文件 —— 原来硬编码 extract.txt，
        # 导致「采集模板」的内容被写进提取链的提示词（污染 AI 提取），
        # 而真正的 collect.txt 从没被创建过（保存后刷新就回退）。
        # 与下面的 reset_prompts 保持一致。
        path = cfg.prompts_dir / f"{scope}.txt"
        path.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入失败：{exc}") from exc
    return {"ok": True, "path": str(path), "custom": True}


@router.post("/prompts/reset")
async def reset_prompts(scope: str = "extract") -> dict[str, Any]:
    """删除自定义提示词，恢复内置默认。"""
    cfg = get_config()
    path = cfg.prompts_dir / f"{scope}.txt"
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"删除失败：{exc}") from exc
    return {"ok": True, "custom": False}


class ProbeRequest(BaseModel):
    url: str = ""


@router.post("/adapters/probe")
async def probe_adapter(req: ProbeRequest) -> dict[str, Any]:
    """用一条真实链接实测适配器能力（只抓取，不写库）。

    平台适配能力卡片里的 support_level 是各 adapter 的**声明值**，
    不代表此刻真的能抓。这个端点跑一次真实抓取，返回：
    是否成功 / 平台 / 解析器 / 标题 / 正文字数 / 是否有字幕。
    """
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="请粘贴一条分享链接")

    from app.adapters import get_adapter
    from app.core.parser import detect_platform
    from app.workers.pipeline import _read_cookie

    cfg = get_config()
    platform = detect_platform(url)
    adapter = get_adapter(
        platform, cookie=_read_cookie(platform), timeout=cfg.collect.web_timeout
    )
    if adapter is None:
        return {
            "ok": False,
            "platform": platform,
            "error": f"没有 {platform} 的适配器",
        }
    try:
        r = adapter.extract(url)
        text = str(r.text or "")
        media = [str(m) for m in (r.extra or {}).get("media") or [] if m]
        return {
            "ok": bool(r.ok),
            "platform": platform,
            "extractor": r.extractor,
            "title": r.title or "",
            "chars": len(text),
            "asr": bool(r.asr),
            "preview": text[:120],
            "error": r.error or "",
            "cookie_used": bool(_read_cookie(platform)),
            # **抓到的是不是完整正文**：只有一句文案（抖音 SSR 的 desc）
            # 时 complete=False，用户一眼就知道"这条只能拿到标题/简介"。
            "complete": bool(r.complete),
            # **有没有视频直链**：有的话程序会下载转写口播台词，
            # 用户就知道"配了 Cookie 到底有没有用"。
            "media_count": len(media),
            "media": media[:3],
        }
    except Exception as exc:  # noqa: BLE001 - 网络/解析异常都算"本次不可用"
        return {
            "ok": False,
            "platform": platform,
            "error": str(exc)[:200],
            "cookie_used": bool(_read_cookie(platform)),
        }
