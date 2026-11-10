"""中文分词与 FTS5 双列载荷。

对齐 v0.5.0 §3.2 / §4.5：
- FTS5 `unicode61` 对中文只按 Unicode 字符切分，"第一性原理" 会被当成一个整词，
  搜 "原理" 命中不了。因此写入时额外构造 `fts_payload(...)`：
    jieba 精确词 + bigram_cjk 全量二元组
  两份结果 OR 起来 → 两字词 / 子串均可命中。
- 查询侧使用同样的对称策略（`build_match_query`）。
- jieba 缺失时自动降级为纯 bigram，并在响应里显式带上 `search_mode=bigram` 告警。
"""

from __future__ import annotations

import re
import threading

_JIEBA_LOCK = threading.Lock()
_JIEBA_STATE: dict[str, object] = {"available": False, "module": None, "error": None}

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_WORD_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+|[A-Za-z0-9]+")
# FTS5 查询需要转义的字符
_FTS_SPECIAL = re.compile(r'[":()\*\^\-+]')


def _init_jieba() -> None:
    """延迟加载 jieba；失败则永久降级 bigram。"""
    with _JIEBA_LOCK:
        if _JIEBA_STATE["available"]:
            return
        try:
            import jieba  # type: ignore

            # jieba 首次加载会打印日志，静音之
            jieba.setLogLevel(60)
            _JIEBA_STATE["module"] = jieba
            _JIEBA_STATE["available"] = True
        except Exception as exc:  # noqa: BLE001
            _JIEBA_STATE["available"] = False
            _JIEBA_STATE["error"] = str(exc)


def jieba_available() -> bool:
    _init_jieba()
    return bool(_JIEBA_STATE["available"])


def search_mode() -> str:
    """当前分词模式：jieba_bigram 或 bigram。"""
    return "jieba_bigram" if jieba_available() else "bigram"


def jieba_error() -> str | None:
    _init_jieba()
    err = _JIEBA_STATE.get("error")
    return str(err) if err else None


def bigrams(text: str) -> list[str]:
    """CJK 连续片段的二元组切分。"""
    out: list[str] = []
    for run in _WORD_RE.findall(text):
        if _CJK_RE.match(run):
            if len(run) == 1:
                out.append(run)
            else:
                out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return out


def tokenize(text: str) -> list[str]:
    """分词：jieba 词 + bigram（去重保序）。"""
    if not text:
        return []
    text = text.strip()
    if not text:
        return []

    seen: set[str] = set()
    tokens: list[str] = []

    def _push(tok: str) -> None:
        tok = tok.strip()
        if tok and tok not in seen:
            seen.add(tok)
            tokens.append(tok)

    if jieba_available():
        module = _JIEBA_STATE["module"]
        assert module is not None
        for word in module.cut(text):  # type: ignore[attr-defined]
            if _CJK_RE.search(word) or re.match(r"^[A-Za-z0-9]+$", word):
                _push(word)
    # bigram 始终附加（保证子串命中），与 jieba 结果取并集
    for bg in bigrams(text):
        _push(bg)
    return tokens


def build_match_query(text: str, mode: str | None = None) -> str:
    """构造 FTS5 MATCH 表达式。

    对每个有效 token 生成 `"token"*` 形式——前缀匹配保证输入 "第一" 能命中 "第一性原理"，
    同时双引号包住避免被当作 FTS5 运算符。
    """
    text = (text or "").strip()
    if not text:
        return ""
    mode = mode or search_mode()
    tokens = tokenize(text) if mode == "jieba_bigram" else bigrams(text)
    if not tokens:
        tokens = [text]
    parts: list[str] = []
    for tok in tokens:
        cleaned = _FTS_SPECIAL.sub(" ", tok).strip()
        if not cleaned:
            continue
        parts.append(f'"{cleaned}"*')
    if not parts:
        cleaned = _FTS_SPECIAL.sub(" ", text).strip()
        if not cleaned:
            return ""
        parts.append(f'"{cleaned}"*')
    return " OR ".join(parts)


def fts_payload(text: str) -> str:
    """写入 FTS5 的载荷文本：token 以空格连接。

    注意：FTS5 unicode61 分词器会把空格作为 token 边界，因此用空格连接的 token
    会被切成独立词条，查询侧 `token*` 前缀匹配即可命中。
    """
    if not text:
        return ""
    return " ".join(tokenize(text))


def build_fts_payload(fields: dict[str, str]) -> str:
    """把多个字段合并为一个 FTS 载荷（标题权重更高 → 重复一次）。"""
    chunks: list[str] = []
    for key, weight in (("title", 3), ("tags", 2), ("summary", 2), ("manuscript", 1), ("body", 1)):
        value = fields.get(key)
        if not value:
            continue
        payload = fts_payload(value)
        if payload:
            chunks.extend([payload] * weight)
    return " ".join(chunks)


def normalize_tag(name: str) -> str:
    """标签归一化：去空白、全角转半角、大小写统一（仅 ASCII 字母）。"""
    if not name:
        return ""
    value = name.strip()
    # 全角字符转半角
    value = "".join(
        chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch for ch in value
    )
    value = value.replace("　", " ")
    value = re.sub(r"\s+", " ", value).strip()
    # ASCII 统一小写（中文无影响）
    value = re.sub(r"[A-Z]", lambda m: m.group(0).lower(), value)
    return value
