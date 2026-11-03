"""AI 契约层：prompt 模板 + JSON 解析 + 重试。

对齐 v0.5.0 §6.2：
- Prompt 模板按内容类型自适应（short_video / article / long_course），
  模板存 `prompts/*.md` 可编辑；
- 期望 JSON：
  {"title_suggestion","summary","summary_type","tags","new_tags",
   "partitions","keywords","confidence"}
- 解析失败（JSON 破损/字段缺失）→ 自动重试 2 次（重试时附上错误说明）→ 仍失败落 ai_failed；
- new_tags 不自动入词表——核准页展示为"待定标签"，用户勾选确认才写入；
- LLM 输出中的任何字符串都不直接转换为库内笔记链接；keywords 仅作 embedding 辅助提示。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import get_config
from app.providers.ai_providers import ProviderError, get_router

# 内容类型 → 摘要风格
SUMMARY_TYPE_BY_CONTENT: dict[str, str] = {
    "short_video": "bullets",
    "article": "structured",
    "long_course": "structured",
}

DEFAULT_PROMPT_TEMPLATE = """你是知识库助手。基于给定内容产出 JSON。

**只输出一个 JSON 对象，不要输出任何解释文字，不要用 markdown 代码块包裹。**

严格使用下面这组字段（类型必须一致）：

{{
  "title_suggestion": "不超过 30 字的中文标题",
  "summary": "摘要正文。bullets 风格：3-5 条要点，每条一行并以 '- ' 开头；structured 风格：按「核心观点：…」「论据：…」「我的启发：…」分段",
  "summary_type": "bullets 或 structured 之一",
  "tags": ["从给定标签词表中选的标签，最多 5 个；没有合适的就给空数组"],
  "new_tags": [{{"name": "词表外的新标签", "reason": "为什么词表里没有它"}}],
  "partitions": ["从给定分区词表中选的分区；没有就给空数组"],
  "new_partitions": [{{"name": "词表外的新分区", "reason": "为什么词表里没有它"}}],
  "keywords": ["3-6 个关键词"],
  "confidence": 0.85
}}

注意：
- summary 必须是**字符串**，不要嵌套对象或数组；
- tags / partitions 只能从下面给出的词表里选；
- 确实是词表外的新标签才放进 new_tags，并在 reason 里说明；
- 分区同理：词表里没有合适的分区时，放进 new_partitions（用户可在核准页勾选确认）。

内容类型：{content_type}
适用风格：{style_hint}

可用分区词表：{partitions_list}
可用标签词表：{tags_list}

内容：
{content}
"""

STYLE_HINTS = {
    "bullets": "短视频/图文 → summary_type=bullets（3-5 条要点 + 一句话总结）",
    "structured": "长文/课程 → summary_type=structured（核心观点 / 论据 / 我的启发 分区）",
}


# 解析结果的字数档位（用户可调）
MIN_CHARS_PRESETS: list[int] = [200, 500, 800, 1000, 1500, 2000, 3000, 5000]
DEFAULT_MIN_CHARS: int = 800


@dataclass
class AIResult:
    """一次 AI 调用的结构化结果。"""

    ok: bool
    title_suggestion: str = ""
    summary: str = ""
    summary_type: str = "bullets"
    tags: list[str] = field(default_factory=list)
    new_tags: list[dict[str, str]] = field(default_factory=list)
    new_partitions: list[dict[str, str]] = field(default_factory=list)
    partitions: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    confidence: float = 0.0
    error: str | None = None
    attempts: int = 0
    provider: str = ""
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "title_suggestion": self.title_suggestion,
            "summary": self.summary,
            "summary_type": self.summary_type,
            "tags": self.tags,
            "new_tags": self.new_tags,
            "new_partitions": self.new_partitions,
            "partitions": self.partitions,
            "keywords": self.keywords,
            "confidence": self.confidence,
            "error": self.error,
            "attempts": self.attempts,
            "provider": self.provider,
        }


def detect_content_type(text: str, title: str = "") -> str:
    """内容类型判定（影响摘要风格）。"""
    length = len(text or "")
    if length < 1200:
        return "short_video"
    if length < 8000:
        return "article"
    return "long_course"


def load_prompt_template() -> str:
    """从 prompts/ 目录读取模板（不存在则用内置默认）。"""
    cfg = get_config()
    path = cfg.prompts_dir / "extract.txt"
    try:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
    except OSError:
        pass
    return DEFAULT_PROMPT_TEMPLATE


def save_prompt_template(text: str) -> Path:
    """保存 prompt 模板（模板可编辑，§6.2）。"""
    cfg = get_config()
    cfg.prompts_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.prompts_dir / "extract.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _extract_json_block(raw: str) -> dict[str, Any] | None:
    """从模型输出中提取 JSON 对象。

    模型常在 JSON 前后带解释文字或 ```json 包裹，这里做多级提取。
    """
    if not raw:
        return None
    text = raw.strip()

    # 1) ```json ... ``` 包裹
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # 2) 直接解析
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # 3) 提取最外层大括号块
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    break
    return None


def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                if item.strip():
                    out.append(item.strip())
            elif isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    out.append(name)
        return out
    return []


def _coerce_new_tags(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name:
                out.append({"name": name, "reason": str(item.get("reason") or "")})
        elif isinstance(item, str) and item.strip():
            out.append({"name": item.strip(), "reason": ""})
    return out


REQUIRED_FIELDS = ("summary", "tags", "partitions")

# 模型偶尔把 summary 返回成嵌套对象（如 {"bullets": [...], "one_liner": "..."}），
# 甚至把要点直接放在顶层。这些都要规整成**文本**，否则 str(dict) 的 Python repr
# 会被写进 B 层 md（真相源）。
_SUMMARY_LIST_KEYS = ("bullets", "points", "items", "要点", "关键点")
_SUMMARY_LINE_KEYS = ("one_liner", "oneline", "one_line", "一句话总结", "一句话")


def _normalize_summary(value: Any) -> str:
    """把各种形状的 summary 规整成 markdown 文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        lines = [str(v).strip() for v in value if str(v).strip()]
        return "\n".join(
            ln if ln.startswith(("-", "*", "#")) else f"- {ln}" for ln in lines
        )
    if isinstance(value, dict):
        parts: list[str] = []
        for key in _SUMMARY_LIST_KEYS:
            items = value.get(key)
            if isinstance(items, list):
                parts.extend(f"- {str(it).strip()}" for it in items if str(it).strip())
                break
        for key in _SUMMARY_LINE_KEYS:
            line = value.get(key)
            if line:
                parts.append(f"一句话总结：{str(line).strip()}")
                break
        if not parts:
            # 兜底：把其它键值按 "键：值" 渲染
            for k, v in value.items():
                if isinstance(v, str) and v.strip():
                    parts.append(f"{k}：{v.strip()}")
                elif isinstance(v, list):
                    parts.extend(f"- {str(x).strip()}" for x in v if str(x).strip())
        return "\n".join(parts)
    return str(value).strip()


def _extract_summary(payload: dict[str, Any]) -> str:
    """从 payload 里取摘要文本（容忍顶层 bullets / one_liner 的写法）。"""
    if "summary" in payload:
        return _normalize_summary(payload.get("summary"))
    for key in _SUMMARY_LIST_KEYS + _SUMMARY_LINE_KEYS:
        if key in payload:
            return _normalize_summary({key: payload.get(key)})
    return ""


def parse_ai_payload(raw: str, expected_type: str) -> tuple[AIResult | None, str | None]:
    """解析模型输出。返回 (结果, 错误说明)。"""
    payload = _extract_json_block(raw)
    if payload is None:
        return None, "输出不是合法 JSON"

    # summary 可能被模型写成对象/数组，甚至直接给顶层 bullets/one_liner
    summary = _extract_summary(payload)
    if not summary:
        return None, "summary 字段为空"

    payload = dict(payload)
    payload["summary"] = summary
    missing = [f for f in REQUIRED_FIELDS if f not in payload]
    if missing:
        return None, f"缺少必需字段：{', '.join(missing)}"

    result = AIResult(
        ok=True,
        title_suggestion=str(payload.get("title_suggestion") or "").strip(),
        summary=summary,
        summary_type=str(payload.get("summary_type") or expected_type).strip(),
        tags=_coerce_list(payload.get("tags")),
        new_tags=_coerce_new_tags(payload.get("new_tags")),
        new_partitions=_coerce_new_tags(payload.get("new_partitions")),
        partitions=_coerce_list(payload.get("partitions")),
        keywords=_coerce_list(payload.get("keywords")),
        confidence=_coerce_confidence(payload.get("confidence")),
        raw=raw,
    )
    if result.summary_type not in ("bullets", "structured"):
        result.summary_type = expected_type
    return result, None


def _coerce_confidence(value: Any) -> float:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, conf))


class AIService:
    """AI 契约服务。"""

    def __init__(self) -> None:
        self.cfg = get_config()
        self.router = get_router()
        self._router_fallback_hook = False

    # -- 词表 -------------------------------------------------------------
    def _load_vocab(self) -> tuple[list[str], list[str]]:
        """读取受控词表（分区 + 标签），注入 prompt。

        分区给 **path**（"成长树/思维逻辑"）而不是 name——
        分区是树，name 在不同父级下可能重复，path 才唯一，
        且前端按 path 存（选父分区即含子树）。
        """
        from app.core.db import get_db

        db = get_db()
        partitions = [
            str(r["path"] or r["name"])
            for r in db.query(
                "SELECT name, path FROM partitions ORDER BY level, path"
            )
        ]
        tags = [str(r["name"]) for r in db.query("SELECT name FROM tags ORDER BY name")]
        return partitions, tags

    # -- 主流程 -----------------------------------------------------------
    def analyze(
        self,
        text: str,
        title: str = "",
        content_type: str | None = None,
        max_attempts: int = 3,
    ) -> AIResult:
        """执行 AI 契约调用（失败自动重试 2 次）。"""
        ctype = content_type or detect_content_type(text, title)
        expected_type = SUMMARY_TYPE_BY_CONTENT.get(ctype, "bullets")
        template = load_prompt_template()
        partitions, tags = self._load_vocab()

        last_error = ""
        raw = ""
        for attempt in range(1, max_attempts + 1):
            prompt = self._build_prompt(
                template, text, ctype, expected_type, partitions, tags, last_error
            )
            try:
                raw = self.router.complete(prompt)
            except ProviderError as exc:
                last_error = f"Provider 调用失败：{exc}"
                continue
            except Exception as exc:  # noqa: BLE001
                last_error = f"未知错误：{exc}"
                continue

            result, error = parse_ai_payload(raw, expected_type)
            if result is not None:
                result.attempts = attempt
                result.provider = self.router.current_name
                # 契约校验：tags/partitions 必须在受控词表内（AI 只能选词表内的值）
                self._enforce_vocab(result, partitions, tags)
                return result

            last_error = error or "解析失败"
            if attempt < max_attempts:
                # 重试时附上错误说明（§6.2）
                continue

        return AIResult(
            ok=False,
            error=f"AI 契约调用失败（{max_attempts} 次尝试）：{last_error}",
            attempts=max_attempts,
            provider=self.router.current_name,
            raw=raw,
        )

    def _build_prompt(
        self,
        template: str,
        text: str,
        content_type: str,
        expected_type: str,
        partitions: list[str],
        tags: list[str],
        error_hint: str,
    ) -> str:
        # 内容过长时截断（保留头部与尾部，中间摘要）
        body = text or ""
        max_chars = 12000
        if len(body) > max_chars:
            head = body[: int(max_chars * 0.7)]
            tail = body[-int(max_chars * 0.2) :]
            body = f"{head}\n\n……（中间内容已截断）……\n\n{tail}"

        prompt = template.format(
            content_type=content_type,
            style_hint=STYLE_HINTS.get(expected_type, STYLE_HINTS["bullets"]),
            partitions_list="、".join(partitions) if partitions else "（暂无，请全部放入 new_partitions）",
            tags_list="、".join(tags) if tags else "（暂无，请全部放入 new_tags）",
            content=body,
        )
        if error_hint:
            prompt += (
                f"\n\n【上一次输出解析失败：{error_hint}】"
                "\n请严格只输出一个合法 JSON 对象，不要包含任何解释文字或 markdown 代码块。"
            )
        return prompt

    # -- 模板化解析（工作区）------------------------------------------------
    def analyze_with_template(
        self,
        text: str,
        template_id: int | None = None,
        min_chars: int = DEFAULT_MIN_CHARS,
        max_attempts: int = 3,
        long_timeout: float = 300.0,
    ) -> dict[str, Any]:
        """按模板生成解析结果。

        - 未指定模板时先让 AI **自动匹配**最合适的模板；
        - 生成结果强制校验 **不少于 min_chars 字**，不足则带着"不足多少字"的提示
          **重新生成**（最多 max_attempts 次）。

        返回 dict：ok / template / content / chars / attempts / error / matched_by
        """
        from app.services.template_service import get_template_service

        text = (text or "").strip()
        if not text:
            return {
                "ok": False,
                "error": "没有可解析的内容",
                "attempts": 0,
                "chars": 0,
                "content": "",
                "template": None,
            }

        tpl_svc = get_template_service()
        templates = tpl_svc.list()
        if not templates:
            tpl_svc.seed_defaults()
            templates = tpl_svc.list()

        chosen: dict[str, Any] | None = None
        matched_by = "manual"
        if template_id:
            chosen = tpl_svc.get(int(template_id))
            if chosen is None:
                return {
                    "ok": False,
                    "error": f"模板不存在（id={template_id}）",
                    "attempts": 0,
                    "chars": 0,
                    "content": "",
                    "template": None,
                }
        else:
            # AI 自动匹配模板
            match = self._match_template(text, templates)
            if not match.get("ok"):
                return {
                    "ok": False,
                    "error": match.get("error") or "模板匹配失败",
                    "attempts": match.get("attempts", 0),
                    "chars": 0,
                    "content": "",
                    "template": None,
                }
            chosen = match["template"]
            matched_by = "ai"

        assert chosen is not None
        last_error = ""
        raw = ""
        for attempt in range(1, max_attempts + 1):
            prompt = self._build_template_prompt(
                chosen["body"],
                text,
                min_chars,
                last_error,
            )
            try:
                raw = self.router.complete(prompt, timeout=long_timeout)
            except ProviderError as exc:
                last_error = f"Provider 调用失败：{exc}"
                continue
            except Exception as exc:  # noqa: BLE001
                last_error = f"未知错误：{exc}"
                continue

            content = (raw or "").strip()
            if len(content) >= min_chars:
                return {
                    "ok": True,
                    "template": chosen,
                    "content": content,
                    "chars": len(content),
                    "attempts": attempt,
                    "matched_by": matched_by,
                    "error": None,
                }

            last_error = (
                f"上一次输出只有 {len(content)} 字，低于要求的 {min_chars} 字"
                f"（还差 {min_chars - len(content)} 字）"
            )

        return {
            "ok": False,
            "template": chosen,
            "content": (raw or "").strip(),
            "chars": len((raw or "").strip()),
            "attempts": max_attempts,
            "matched_by": matched_by,
            "error": (
                f"生成的内容始终不足 {min_chars} 字（{max_attempts} 次尝试）：{last_error}"
            ),
        }

    def _match_template(
        self, text: str, templates: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """让 AI 从模板清单里挑一个最合适的。"""
        catalog = "\n".join(
            f"{t['id']}. {t['name']}——{t.get('description') or ''}"
            for t in templates
        )
        body = text[:4000]
        prompt = (
            "下面是待解析内容的内容预览：\n"
            f"{body}\n\n"
            "下面是可选的解析模板：\n"
            f"{catalog}\n\n"
            "请只输出一个 JSON 对象：{\"template_id\": <模板编号>, \"reason\": \"一句话理由\"}。"
            "选最贴合这段内容结构的模板。"
        )
        last_error = ""
        for attempt in range(1, 3):
            try:
                raw = self.router.complete(prompt, timeout=120.0)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                continue
            payload = _extract_json_block(raw)
            if payload:
                try:
                    tid = int(payload.get("template_id"))
                except (TypeError, ValueError):
                    tid = -1
                match = next((t for t in templates if int(t["id"]) == tid), None)
                if match is not None:
                    return {
                        "ok": True,
                        "template": match,
                        "reason": str(payload.get("reason") or ""),
                        "attempts": attempt,
                    }
                last_error = f"模型选了不存在的模板 id={tid}"
            else:
                last_error = "模板匹配输出不是合法 JSON"
        return {"ok": False, "error": last_error, "attempts": 2}

    def _build_template_prompt(
        self,
        template_body: str,
        text: str,
        min_chars: int,
        error_hint: str,
    ) -> str:
        body = text or ""
        max_chars = 12000
        if len(body) > max_chars:
            head = body[: int(max_chars * 0.7)]
            tail = body[-int(max_chars * 0.2) :]
            body = f"{head}\n\n……（中间内容已截断）……\n\n{tail}"

        prompt = (
            "你是知识库助手。请**严格按照下面的模板结构**，把给出的内容整理成解析结果。\n\n"
            "【模板】\n"
            f"{template_body}\n\n"
            "【原始内容】\n"
            f"{body}\n\n"
            "【硬性要求】\n"
            f"1. 输出正文**不少于 {min_chars} 字**，要展开讲清楚，不能只写标题或空话；\n"
            "2. 只输出解析结果正文，不要输出前言、后记或「以下是解析结果」之类的话；\n"
            "3. 可以使用 markdown 小标题和列表，但不要包裹在代码块里。"
        )
        if error_hint:
            prompt += (
                f"\n\n【上一次输出不合格：{error_hint}】"
                f"请重新生成，确保正文不少于 {min_chars} 字，并把每一条都展开说明。"
            )
        return prompt

    @staticmethod
    def _enforce_vocab(result: AIResult, partitions: list[str], tags: list[str]) -> None:
        """词表外取值移入 new_tags / new_partitions（AI 只能从词表选）。"""
        if not partitions and result.partitions:
            # 分区词表为空 → AI 给的分区全是建议值，转入 new_partitions
            for name in result.partitions:
                result.new_partitions.append({"name": name, "reason": "分区词表为空"})
            result.partitions = []
        if partitions:
            allowed = {p.lower() for p in partitions}
            keep: list[str] = []
            for name in result.partitions:
                if name.lower() in allowed:
                    keep.append(name)
                else:
                    result.new_partitions.append({"name": name, "reason": "词表外分区"})
            result.partitions = keep

        if tags:
            allowed = {t.lower() for t in tags}
            keep = []
            for name in result.tags:
                if name.lower() in allowed:
                    keep.append(name)
                else:
                    result.new_tags.append({"name": name, "reason": "词表外标签"})
            result.tags = keep


_ai: AIService | None = None


def get_ai_service() -> AIService:
    global _ai
    if _ai is None:
        _ai = AIService()
    return _ai
