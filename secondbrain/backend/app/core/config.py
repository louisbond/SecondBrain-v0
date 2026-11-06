"""SecondBrain 配置加载。

配置来源优先级（后者覆盖前者）：
1. 内置默认值（本模块 DEFAULTS）
2. config.toml（项目 config/ 或用户目录 ~/.secondbrain/config.toml）
3. secrets.json（0600，存放 token / cookie 等敏感项）
4. 环境变量 SECOND_BRAIN_*（用于打包发行版覆盖）

设计要点：
- md 文件是唯一真相源，SQLite 只是可重建的索引，因此本模块只管"路径与开关"，
  不缓存任何知识内容。
- 所有路径统一解析为绝对路径，Windows 路径不做 POSIX 化，避免反斜杠转义问题。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "SecondBrain"

# **全应用唯一版本源**（v39）。
#
# 以前版本号散在三处硬编码（FastAPI app、/api/health、/diagnose），
# 而这里的 APP_VERSION 根本没人用 —— 于是侧边栏显示的版本号
# 和真实进度永远对不上（都写到 v38 了还写着 0.5.0）。
# 现在：改这一行，后端接口 / 前端显示 / 打包元数据一起变。
APP_VERSION = "0.7.3"


def _detect_project_root() -> Path:
    """项目根目录。

    开发态：backend/app/core/config.py -> backend/app/core -> backend/app -> backend -> <root>
    打包态（PyInstaller）：exe 同级目录 —— 配置、数据、vault 引用都落在那里，
    不能指向 _MEIPASS（那是每次启动都会解包的临时目录）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


PROJECT_ROOT = _detect_project_root()


def _bundled_config_template() -> Path | None:
    """包内配置模板（首次运行时复制到 exe 同级，用户可自行修改）。"""
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    roots.append(PROJECT_ROOT)
    for root in roots:
        candidate = root / "config" / "config.example.toml"
        if candidate.exists():
            return candidate
    return None


def _resolve_bin_dir(toml_data: dict[str, Any]) -> Path:
    """内嵌二进制目录（yt-dlp / ffmpeg）。

    打包态：优先用包内 `_MEIPASS/bin`（随 exe 走），
    用户在 config.toml 里显式写了 storage.bin_dir 时以用户为准。
    """
    explicit = _get(toml_data, "storage.bin_dir", "")
    if explicit:
        p = Path(str(explicit)).expanduser()
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        packaged = Path(meipass) / "bin"
        if packaged.is_dir():
            return packaged
    return PROJECT_ROOT / "bin"


def _resolve_prompts_dir(toml_data: dict[str, Any]) -> Path:
    """提示词模板目录（与 bin 同理：打包态在 _MEIPASS 内）。"""
    explicit = _get(toml_data, "storage.prompts_dir", "")
    if explicit:
        p = Path(str(explicit)).expanduser()
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        packaged = Path(meipass) / "prompts"
        if packaged.is_dir():
            return packaged
    return PROJECT_ROOT / "prompts"


def _default_user_dir() -> Path:
    override = os.environ.get("SECOND_BRAIN_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".secondbrain"


def _candidate_toml_paths() -> list[Path]:
    """配置文件候选（按优先级）：env > exe 同级 > 用户目录。"""
    candidates = [
        PROJECT_ROOT / "config" / "config.toml",
        _default_user_dir() / "config.toml",
    ]
    env_path = os.environ.get("SECOND_BRAIN_CONFIG")
    if env_path:
        candidates.insert(0, Path(env_path).expanduser())
    return candidates


def _candidate_secret_paths() -> list[Path]:
    return [
        PROJECT_ROOT / "config" / "secrets.json",
        _default_user_dir() / "secrets.json",
    ]


def active_config_path() -> Path:
    """当前生效（或首次运行应当创建）的 config.toml。

    兼容策略：已有部署的配置在 exe 同级，继续优先读它，升级不丢配置；
    全新安装（可能装在 Program Files，不可写）一律落用户目录。
    """
    env_path = os.environ.get("SECOND_BRAIN_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    for p in _candidate_toml_paths():
        if p.exists():
            return p
    return _default_user_dir() / "config.toml"


def active_secret_path() -> Path:
    """密钥文件同理；写回必须用这个路径，否则会出现「保存了却读不到」。

    指定了 `SECOND_BRAIN_CONFIG` 时，密钥跟随配置文件放在同一目录 ——
    配置读 A、密钥写 B 是最隐蔽的一类 bug：保存返回成功，重启后密钥没了。
    """
    env_path = os.environ.get("SECOND_BRAIN_CONFIG")
    if env_path:
        return Path(env_path).expanduser().parent / "secrets.json"
    for p in _candidate_secret_paths():
        if p.exists():
            return p
    return _default_user_dir() / "secrets.json"


def _default_data_dir() -> Path:
    """数据目录默认值：兼容老部署 + 新安装可移植。

    老部署的 config.toml 里 `data_dir = ""`，靠默认值把数据放在 exe 同级
    data/。若把默认值直接改成用户目录，升级后服务会连到一个新建的空库，
    表现为「笔记全不见了」。只要老库还在就继续用它。
    """
    legacy = PROJECT_ROOT / "data"
    if (legacy / "secondbrain.db").exists():
        return legacy
    return _default_user_dir() / "data"


def _default_vault_dir() -> Path:
    """同上：老部署若已有默认库，保留；全新安装才用用户目录。"""
    legacy = PROJECT_ROOT / "data" / "vault"
    if legacy.is_dir():
        return legacy
    return _default_user_dir() / "vault"


@dataclass
class VaultConfig:
    """Obsidian vault 目录配置。"""

    path: Path
    inbox_dir: str = "01_Inbox"
    processed_dir: str = "02_Processed"
    notes_dir: str = "03_Notes"
    assets_dir: str = "assets"
    archive_dir: str = "99_Archive"
    reports_dir: str = "_reports"

    def dir(self, key: str) -> Path:
        """按语义键取子目录绝对路径。"""
        mapping = {
            "inbox": self.inbox_dir,
            "processed": self.processed_dir,
            "notes": self.notes_dir,
            "assets": self.assets_dir,
            "archive": self.archive_dir,
            "reports": self.reports_dir,
        }
        return self.path / mapping[key]

    def ensure(self) -> None:
        """创建工具独占写入的目录树。"""
        for key in ("inbox", "processed", "notes", "assets", "archive", "reports"):
            self.dir(key).mkdir(parents=True, exist_ok=True)


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 7688
    allow_lan: bool = False
    token: str | None = None


@dataclass
class OllamaProviderConfig:
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "qwen2.5:7b-instruct"
    timeout: int = 300
    # OpenAI 兼容端点用（火山方舟等）；Ollama 本地不需要
    api_key: str = ""


@dataclass
class EmbeddingConfig:
    model: str = "bge-m3"
    dim: int = 1024
    batch_size: int = 8
    timeout: int = 60
    # 本地模型目录：设置后优先从该目录加载（离线可用）
    model_dir: Path | None = None


@dataclass
class AIConfig:
    # v34：workbuddy 链路已移除（CLI 读不到桌面端认证，永远 auth_required）。
    # 现在主链默认 openai_compat，备链 ollama。
    provider: str = "openai_compat"
    auto_fallback: bool = True
    fallback_threshold: int = 3
    # 切到 ollama / 启动时发现本机没在监听，就顺手把 ollama 拉起来（仅回环地址）
    auto_start_ollama: bool = True
    ollama: OllamaProviderConfig = field(default_factory=OllamaProviderConfig)
    openai_compat: OllamaProviderConfig = field(
        default_factory=lambda: OllamaProviderConfig(
            base_url="", model="", timeout=180
        )
    )
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)


@dataclass
class ASRConfig:
    engine: str = "faster-whisper"
    model_size: str = "small"
    compute: str = "int8"
    concurrency: int = 1
    cache_dir: Path = field(
        default_factory=lambda: _default_user_dir() / "data" / "cache" / "audio"
    )
    # 本地模型目录：设置后优先从该目录加载（离线可用）。
    # 留空则从 HuggingFace Hub 下载（首次运行自动缓存）。
    model_dir: Path | None = None


@dataclass
class CollectConfig:
    min_chars: int = 200
    web_timeout: int = 20
    batch_limit: int = 100
    cookie_file: Path | None = None


@dataclass
class ParserApiConfig:
    """第三方解析 API（可选兜底）。

    平台不给匿名请求返回正文时（抖音/快手/小红书），可以接一个聚合解析服务。
    **默认关闭**：开启意味着把链接发给第三方，必须由用户显式同意。

    `url_template` 支持 `{url}` 占位符，如
    `https://api.example.com/parse?url={url}&key=xxx`；
    不含占位符时自动拼成 `...?url=<encode>`。
    需要放到 header 里的鉴权用 auth_header / auth_value。
    """

    enabled: bool = False
    url_template: str = ""
    auth_header: str = ""
    auth_value: str = ""
    timeout: int = 30


@dataclass
class DuplicateConfig:
    vector_threshold: float = 0.85


@dataclass
class NotifyConfig:
    windows_toast: bool = True


@dataclass
class MaintainConfig:
    weekly_report: bool = True
    weekly_report_weekday: int = 6  # 周日（周一=0）
    weekly_report_hour: int = 21
    recap_sample: str = "weighted_unreferenced"


@dataclass
class AppConfig:
    vault: VaultConfig
    web: WebConfig
    ai: AIConfig
    asr: ASRConfig
    collect: CollectConfig
    parser: ParserApiConfig
    duplicate: DuplicateConfig
    notify: NotifyConfig
    maintain: MaintainConfig
    data_dir: Path
    db_path: Path
    cache_dir: Path
    bin_dir: Path
    prompts_dir: Path
    user_dir: Path


# --------------------------------------------------------------------------
# TOML 读取（Python 3.11+ 自带 tomllib）
# --------------------------------------------------------------------------

try:  # pragma: no cover - Python < 3.11 兜底
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover
    _toml = None  # type: ignore[assignment]


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_bytes()
    if _toml is not None:
        try:
            return _toml.loads(raw.decode("utf-8"))
        except Exception:
            return {}
    # 极简回退解析：仅支持 key = value / [section] 结构
    data: dict[str, Any] = {}
    section: dict[str, Any] = data
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = data.setdefault(line[1:-1].strip(), {})
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            val = val.strip()
            if val.startswith(("'", '"')) and val.endswith(("'", '"')) and len(val) >= 2:
                section[key.strip()] = val[1:-1]
            elif val in ("true", "false"):
                section[key.strip()] = val == "true"
            elif val.isdigit():
                section[key.strip()] = int(val)
            else:
                section[key.strip()] = val
    return data


def _get(data: dict[str, Any], path: str, default: Any) -> Any:
    """按 "a.b" 取值。

    **空字符串视为未配置**——配置文件里写 `key = ""` 的语义是"用默认值"，
    而不是"把路径设成空"（否则 sqlite 会报 unable to open database file）。
    """
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    if cur is None:
        return default
    if isinstance(cur, str) and not cur.strip():
        return default
    return cur


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    if isinstance(value, int):
        return bool(value)
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        if isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return list(default)


def set_toml_value(path: Path, section: str, key: str, value: str) -> bool:
    """就地更新 config.toml 里的某个键，保留注释与排版。

    只做最小改动：找到 `[section]` 段里的 `key = ...` 就替换，
    段内没有就插在段头之后；整个 section 都没有就追加到文件末尾。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False

    lines = text.splitlines()
    header = f"[{section}]"
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break

    new_line = f'{key} = "{value}"'
    if start is None:
        suffix = "" if not lines else "\n"
        path.write_text(
            text.rstrip() + suffix + f"\n[{section}]\n{new_line}\n", encoding="utf-8"
        )
        return True

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].strip().startswith("["):
            end = j
            break

    for k in range(start + 1, end):
        stripped = lines[k].strip()
        if stripped.startswith(f"{key}") and "=" in stripped:
            lines[k] = new_line
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True

    lines.insert(start + 1, new_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def save_secret(name: str, value: str) -> Path:
    """把密钥写入 secrets.json（与 config.toml 分开，避免误提交）。"""
    target = active_secret_path()
    data: dict[str, Any] = {}
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[name] = value
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def mask_secret(value: str) -> str:
    """脱敏显示：只保留末 4 位。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{'*' * 8}{value[-4:]}"


class ConfigStore:
    """配置加载器（线程安全，支持 reload）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._config: AppConfig | None = None

    # -- 路径定位 ---------------------------------------------------------
    def _candidate_toml_paths(self) -> list[Path]:
        return _candidate_toml_paths()

    def _candidate_secret_paths(self) -> list[Path]:
        return _candidate_secret_paths()

    def _seed_config(self) -> None:
        """首次运行：把包内模板播种到用户可写目录。

        以前写 exe 同级 config/，装到 Program Files 会直接失败。
        只有所有候选路径都不存在时才会调用，因此不会覆盖已有部署。
        """
        template = _bundled_config_template()
        if template is None:
            return
        target = active_config_path()
        if target.exists():
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                template.read_text(encoding="utf-8"), encoding="utf-8"
            )
            print(f"[SecondBrain] 已生成配置文件：{target}")
        except OSError as exc:
            print(f"[SecondBrain] 配置文件生成失败（将使用默认配置）：{exc}")

    def _load_raw(self) -> tuple[dict[str, Any], dict[str, Any]]:
        toml_data: dict[str, Any] = {}
        for path in self._candidate_toml_paths():
            if path.exists():
                toml_data = _read_toml(path)
                break
        else:
            # 没有任何配置文件（典型场景：exe 首次运行）→ 用包内模板播种一份
            self._seed_config()
            for path in self._candidate_toml_paths():
                if path.exists():
                    toml_data = _read_toml(path)
                    break

        secrets: dict[str, Any] = {}
        # 读与写必须同源：active_secret_path 会跟随 SECOND_BRAIN_CONFIG，
        # 找不到再退回候选列表（兼容把密钥放别处的老部署）。
        for path in [active_secret_path(), *self._candidate_secret_paths()]:
            if path.exists():
                try:
                    secrets = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    secrets = {}
                break
        return toml_data, secrets

    # -- 构建 -------------------------------------------------------------
    def _build(self) -> AppConfig:
        toml_data, secrets = self._load_raw()

        user_dir = _default_user_dir()
        user_dir.mkdir(parents=True, exist_ok=True)

        vault_path = Path(
            _get(toml_data, "vault.path", str(_default_vault_dir()))
        ).expanduser()
        env_vault = os.environ.get("SECOND_BRAIN_VAULT")
        if env_vault:
            vault_path = Path(env_vault).expanduser()
        if not vault_path.is_absolute():
            vault_path = (PROJECT_ROOT / vault_path).resolve()

        vault = VaultConfig(
            path=vault_path,
            inbox_dir=_get(toml_data, "vault.inbox_dir", "01_Inbox"),
            processed_dir=_get(toml_data, "vault.processed_dir", "02_Processed"),
            notes_dir=_get(toml_data, "vault.notes_dir", "03_Notes"),
            assets_dir=_get(toml_data, "vault.assets_dir", "assets"),
            archive_dir=_get(toml_data, "vault.archive_dir", "99_Archive"),
            reports_dir=_get(toml_data, "vault.reports_dir", "_reports"),
        )

        data_dir = Path(
            _get(toml_data, "storage.data_dir", str(_default_data_dir()))
        ).expanduser()
        if not data_dir.is_absolute():
            data_dir = (PROJECT_ROOT / data_dir).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)

        db_path = Path(
            _get(toml_data, "storage.db_path", str(data_dir / "secondbrain.db"))
        )
        # 环境变量优先（脚本/工具可以指向别的库）
        env_db = os.environ.get("SECOND_BRAIN_DB")
        if env_db:
            db_path = Path(env_db).expanduser().expanduser()
        if not db_path.is_absolute():
            db_path = (PROJECT_ROOT / db_path).resolve()

        allow_lan = _as_bool(_get(toml_data, "web.allow_lan", False), False)
        token = secrets.get("web_token") or _get(toml_data, "web.token", None)

        web = WebConfig(
            host=_get(toml_data, "web.host", "127.0.0.1"),
            port=_as_int(_get(toml_data, "web.port", 7688), 7688),
            allow_lan=allow_lan,
            token=str(token) if token else None,
        )

        # v34：workbuddy 字段已移除。旧 config.toml 里的 [ai.workbuddy] 段
        # 会被**静默忽略**（这里不读它，也不报错）——用户升级不用手改配置。
        ai = AIConfig(
            provider=_get(toml_data, "ai.provider", "openai_compat"),
            auto_fallback=_as_bool(_get(toml_data, "ai.auto_fallback", True), True),
            auto_start_ollama=_as_bool(
                _get(toml_data, "ai.auto_start_ollama", True), True
            ),
            fallback_threshold=_as_int(
                _get(toml_data, "ai.fallback_threshold", 3), 3
            ),
            ollama=OllamaProviderConfig(
                base_url=_get(
                    toml_data, "ai.ollama.base_url", "http://127.0.0.1:11434/v1"
                ),
                model=_get(toml_data, "ai.ollama.model", "qwen2.5:7b-instruct"),
                timeout=_as_int(_get(toml_data, "ai.ollama.timeout", 300), 300),
            ),
            openai_compat=OllamaProviderConfig(
                base_url=_get(toml_data, "ai.openai_compat.base_url", ""),
                model=_get(toml_data, "ai.openai_compat.model", ""),
                timeout=_as_int(_get(toml_data, "ai.openai_compat.timeout", 180), 180),
                # 密钥优先从 secrets.json 取（不落在 config.toml 里）
                api_key=str(
                    secrets.get("ark_api_key")
                    or secrets.get("openai_api_key")
                    or _get(toml_data, "ai.openai_compat.api_key", "")
                    or ""
                ),
            ),
            embedding=EmbeddingConfig(
                model=_get(toml_data, "ai.embedding.model", "bge-m3"),
                dim=_as_int(_get(toml_data, "ai.embedding.dim", 1024), 1024),
                batch_size=_as_int(_get(toml_data, "ai.embedding.batch_size", 8), 8),
                model_dir=(
                    Path(_get(toml_data, "ai.embedding.model_dir", ""))
                    if _get(toml_data, "ai.embedding.model_dir", "")
                    else None
                ),
            ),
        )

        asr = ASRConfig(
            engine=_get(toml_data, "asr.engine", "faster-whisper"),
            model_size=_get(toml_data, "asr.model_size", "small"),
            compute=_get(toml_data, "asr.compute", "int8"),
            concurrency=_as_int(_get(toml_data, "asr.concurrency", 1), 1),
            cache_dir=Path(
                _get(
                    toml_data,
                    "asr.cache_dir",
                    str(data_dir / "cache" / "audio"),
                )
            ),
            model_dir=(
                Path(_get(toml_data, "asr.model_dir", ""))
                if _get(toml_data, "asr.model_dir", "")
                else None
            ),
        )

        cookie_file = _get(toml_data, "collect.cookie_file", None)
        collect = CollectConfig(
            min_chars=_as_int(_get(toml_data, "collect.min_chars", 200), 200),
            web_timeout=_as_int(_get(toml_data, "collect.web_timeout", 20), 20),
            batch_limit=_as_int(_get(toml_data, "collect.batch_limit", 100), 100),
            cookie_file=Path(cookie_file) if cookie_file else None,
        )

        parser = ParserApiConfig(
            enabled=_as_bool(_get(toml_data, "parser.enabled", False), False),
            url_template=str(_get(toml_data, "parser.url_template", "") or "").strip(),
            auth_header=str(_get(toml_data, "parser.auth_header", "") or "").strip(),
            auth_value=str(secrets.get("parser_api_auth") or ""),
            timeout=_as_int(_get(toml_data, "parser.timeout", 30), 30),
        )

        duplicate = DuplicateConfig(
            vector_threshold=_as_float(
                _get(toml_data, "duplicate.vector_threshold", 0.85), 0.85
            )
        )
        notify = NotifyConfig(
            windows_toast=_as_bool(
                _get(toml_data, "notify.windows_toast", True), True
            )
        )
        maintain = MaintainConfig(
            weekly_report=_as_bool(
                _get(toml_data, "maintain.weekly_report", True), True
            ),
            weekly_report_weekday=_as_int(
                _get(toml_data, "maintain.weekly_report_weekday", 6), 6
            ),
            weekly_report_hour=_as_int(
                _get(toml_data, "maintain.weekly_report_hour", 21), 21
            ),
            recap_sample=_get(
                toml_data, "maintain.recap_sample", "weighted_unreferenced"
            ),
        )

        # 环境变量覆盖（打包发行版用）
        env_provider = os.environ.get("SECOND_BRAIN_AI_PROVIDER")
        if env_provider:
            ai.provider = env_provider
        env_port = os.environ.get("SECOND_BRAIN_PORT")
        if env_port:
            web.port = _as_int(env_port, web.port)
        env_allow_lan = os.environ.get("SECOND_BRAIN_ALLOW_LAN")
        if env_allow_lan is not None:
            web.allow_lan = _as_bool(env_allow_lan, web.allow_lan)

        # 模型下载走 HuggingFace Hub；国内网络可设 HF_ENDPOINT 指向镜像。
        # 放在这里设置，保证无论从哪入口启动都生效。
        hf_endpoint = os.environ.get("HF_ENDPOINT") or _get(
            toml_data, "model.hf_endpoint", ""
        )
        if hf_endpoint and not os.environ.get("HF_ENDPOINT"):
            os.environ["HF_ENDPOINT"] = hf_endpoint
        if hf_endpoint:
            os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

        cfg = AppConfig(
            vault=vault,
            web=web,
            ai=ai,
            asr=asr,
            collect=collect,
            parser=parser,
            duplicate=duplicate,
            notify=notify,
            maintain=maintain,
            data_dir=data_dir,
            db_path=db_path,
            cache_dir=data_dir / "cache",
            bin_dir=_resolve_bin_dir(toml_data),
            prompts_dir=_resolve_prompts_dir(toml_data),
            user_dir=user_dir,
        )
        return cfg

    # -- 公共接口 ---------------------------------------------------------
    def get(self) -> AppConfig:
        with self._lock:
            if self._config is None:
                self._config = self._build()
            return self._config

    def reload(self) -> AppConfig:
        with self._lock:
            self._config = self._build()
            return self._config

    def set_runtime(self, **kwargs: Any) -> AppConfig:
        """运行时修改配置（设置页用），不落盘到 config.toml（避免破坏用户文件）。"""
        with self._lock:
            cfg = self.get()
            if "provider" in kwargs:
                cfg.ai.provider = str(kwargs["provider"])
            if "allow_lan" in kwargs:
                cfg.web.allow_lan = bool(kwargs["allow_lan"])
            if "auto_fallback" in kwargs:
                cfg.ai.auto_fallback = bool(kwargs["auto_fallback"])
            if "auto_start_ollama" in kwargs:
                cfg.ai.auto_start_ollama = bool(kwargs["auto_start_ollama"])
            if "vault_path" in kwargs:
                cfg.vault.path = Path(str(kwargs["vault_path"]))
                cfg.vault.ensure()
            if "token" in kwargs and kwargs["token"]:
                cfg.web.token = str(kwargs["token"])
            return cfg


store = ConfigStore()


def get_config() -> AppConfig:
    return store.get()
