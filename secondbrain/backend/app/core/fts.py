"""FTS 写入与检索的共享实现。

对齐 v0.5.0 §4.5 / §11：
- 写入双列：jieba + bigram_cjk 分词结果写入 FTS5；
- 每个字段写入**自己那一列**（title/summary/manuscript/body/tags），
  排序权重交给 bm25 的列权重表达，而不是靠重复 payload——
  重复写法会让索引体积翻 3–4 倍，5000 条重建就逼近 60s 上限；
- 查询排序权重：level='note'(C层) > level='processed'(B层)；
- 摘要片段由 snippet_src（UNINDEXED 原文）生成，在 Python 侧高亮。
"""

from __future__ import annotations

import re
from typing import Any

from app.core.tokenizer import build_match_query, fts_payload, search_mode

# bm25 列权重：顺序必须与表列顺序一致
# title, summary, manuscript, body, tags, level(UNINDEXED), note_path(UNINDEXED), snippet_src(UNINDEXED)
BM25_WEIGHTS = (10.0, 4.0, 3.0, 1.0, 6.0, 0.0, 0.0, 0.0)

# 片段高亮标记
HL_OPEN = "«"
HL_CLOSE = "»"


def build_columns(
    title: str = "",
    summary: str = "",
    manuscript: str = "",
    body: str = "",
    tags: str = "",
) -> dict[str, str]:
    """把字段转成 FTS 各列的载荷。

    每列只放自己的 token，排序权重由 BM25_WEIGHTS 表达。
    """
    return {
        "title": fts_payload(title),
        "summary": fts_payload(summary),
        "manuscript": fts_payload(manuscript),
        "body": fts_payload(body),
        "tags": fts_payload(tags),
    }


def build_snippet_src(
    title: str = "",
    summary: str = "",
    manuscript: str = "",
    body: str = "",
    tags: str = "",
) -> str:
    """生成用于摘要片段的原文（未分词，UNINDEXED 列）。"""
    parts: list[str] = []
    if title:
        parts.append(title)
    if summary:
        parts.append(summary)
    if manuscript:
        parts.append(manuscript)
    if body:
        parts.append(body)
    if tags:
        parts.append(tags)
    return "\n".join(parts)


def make_snippet(src: str, query: str, width: int = 140) -> str:
    """从原文生成带高亮标记的摘要片段。"""
    if not src:
        return ""
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    lower = src.lower()

    # 找第一个命中位置
    pos = -1
    hit_len = 0
    for term in terms:
        found = lower.find(term.lower())
        if found >= 0 and (pos < 0 or found < pos):
            pos = found
            hit_len = len(term)
    if pos < 0:
        return src[:width].replace("\n", " ")

    start = max(0, pos - width // 3)
    end = min(len(src), start + width)
    fragment = src[start:end].replace("\n", " ")

    for term in sorted(terms, key=len, reverse=True):
        if not term:
            continue
        fragment = re.sub(
            re.escape(term),
            f"{HL_OPEN}{term}{HL_CLOSE}",
            fragment,
            flags=re.IGNORECASE,
        )
    return ("…" if start > 0 else "") + fragment + ("…" if end < len(src) else "")


def match_expression(query: str) -> str:
    """构造 FTS5 MATCH 表达式（与写入对称）。"""
    return build_match_query(query)


def level_weight(level: str) -> float:
    """C 层权重高于 B 层（§11）。"""
    return 2.0 if level == "note" else 1.0


def search_mode_name() -> str:
    return search_mode()
