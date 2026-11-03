"""查询服务：全文搜索（C>B 权重）、标签筛选、反链、语义推荐。

对齐 v0.5.0 §11：
| 全文搜索 | FTS5 jieba+bigram，**默认只搜 B/C 层且 C 层权重 > B 层**，A 层开关临时纳入 |
| 标签筛选 | 词表侧栏 + AND 组合筛选 |
| 反向链接 | links 表查询 + 阅读页展示 |
| 语义推荐 | bge-m3 余弦 Top-N |
| 图谱 | **降级**：反链视图 |

性能目标：搜索 P95 <200ms@5000 条；反链查询 <50ms。
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from app.core.db import get_db
from app.core import fts as fts_mod
from app.core.tokenizer import build_match_query, jieba_error, search_mode

# B/C 层权重（C 层 > B 层）
LEVEL_WEIGHT = {"note": 2.0, "processed": 1.0}


class SearchService:
    """查询服务。"""

    def __init__(self) -> None:
        self.db = get_db()

    # -- 全文搜索 ---------------------------------------------------------
    def search(
        self,
        query: str,
        levels: list[str] | None = None,
        tags: list[str] | None = None,
        partitions: list[str] | None = None,
        include_inbox: bool = False,
        include_archived: bool = False,
        limit: int = 30,
        offset: int = 0,
    ) -> dict[str, Any]:
        """全文搜索。

        - 默认只搜 B/C 层（processed / note），C 层权重高于 B 层；
        - include_inbox=True 时临时纳入 A 层（权重最低）；
        - include_archived=True 时纳入归档笔记（默认不出，§10）；
        - tags 为 AND 组合筛选。
        """
        started = time.time()
        query = (query or "").strip()
        if not query:
            return {
                "results": [],
                "total": 0,
                "elapsed_ms": 0,
                "search_mode": search_mode(),
                "warning": jieba_error(),
            }

        levels = levels or ["processed", "note"]
        if include_inbox:
            levels = list(levels) + ["inbox"]

        match = build_match_query(query)
        if not match:
            return {
                "results": [],
                "total": 0,
                "elapsed_ms": 0,
                "search_mode": search_mode(),
                "warning": jieba_error(),
            }

        placeholders = ",".join("?" for _ in levels)

        # 归档笔记默认不进搜索（§10：搜索默认不出，开关可纳入）
        archived_clause = "" if include_archived else " AND n.archived = 0"

        # 标签 AND 筛选：每个标签一个 EXISTS 子句
        tag_clause = ""
        for tag in tags or []:
            tag_clause += (
                " AND EXISTS (SELECT 1 FROM note_tags nt JOIN tags t ON t.id = nt.tag_id "
                "WHERE nt.note_id = n.id AND t.name = ?)"
            )

        partition_clause = ""
        partition_params: list[str] = []
        for part in partitions or []:
            # 分区是树：选父分区 = 含其整棵子树
            partition_clause += (
                " AND EXISTS (SELECT 1 FROM note_partitions np "
                "JOIN partitions p ON p.id = np.partition_id "
                "WHERE np.note_id = n.id AND (p.name = ? OR p.path LIKE ?))"
            )
            partition_params.append(part)
            partition_params.append(f"{part}/%")

        sql = f"""
            SELECT f.note_path, n.title AS title, f.level, f.snippet_src,
                   bm25(notes_fts, ?, ?, ?, ?, ?, ?, ?, ?) AS score,
                   n.id AS note_id, n.digested, n.archived, n.created_at
            FROM notes_fts f
            JOIN notes n ON n.note_path = f.note_path
            WHERE notes_fts MATCH ?
              AND f.level IN ({placeholders})
              {archived_clause}
              {tag_clause}
              {partition_clause}
            ORDER BY
              CASE f.level WHEN 'note' THEN 2 ELSE 1 END DESC,
              score ASC
            LIMIT ? OFFSET ?
        """
        params = [*fts_mod.BM25_WEIGHTS, match, *levels]
        params.extend([t for t in (tags or [])])
        params.extend(partition_params)
        params.extend([limit, offset])

        try:
            rows = self.db.query(sql, params)
        except sqlite3.OperationalError as exc:
            return {
                "results": [],
                "total": 0,
                "elapsed_ms": int((time.time() - started) * 1000),
                "search_mode": search_mode(),
                "error": f"FTS 查询失败：{exc}",
            }

        total_sql = f"""
            SELECT COUNT(*)
            FROM notes_fts f
            JOIN notes n ON n.note_path = f.note_path
            WHERE notes_fts MATCH ? AND f.level IN ({placeholders})
              {archived_clause} {tag_clause} {partition_clause}
        """
        total_params = [match, *levels]
        total_params.extend([t for t in (tags or [])])
        total_params.extend(partition_params)
        total = self.db.scalar(total_sql, total_params) or 0

        results = []
        for row in rows:
            results.append(
                {
                    "note_path": row["note_path"],
                    "title": row["title"] or "（无标题）",                    "level": row["level"],
                    "level_label": "创作笔记" if row["level"] == "note" else "提炼笔记",
                    "snippet": fts_mod.make_snippet(str(row["snippet_src"] or ""), query),
                    "score": round(row["score"] or 0.0, 3),
                    "note_id": row["note_id"],
                    "digested": bool(row["digested"]),
                    "archived": bool(row["archived"]),
                    "created_at": row["created_at"] or "",
                }
            )

        return {
            "results": results,
            "total": total,
            "elapsed_ms": int((time.time() - started) * 1000),
            "search_mode": search_mode(),
            "warning": jieba_error(),
        }

    # -- 标签筛选 ---------------------------------------------------------
    def list_tags(self) -> list[dict[str, Any]]:
        """受控词表（标签 + 分区）。"""
        tags = self.db.query(
            "SELECT t.id, t.name, t.source, COUNT(nt.note_id) AS count "
            "FROM tags t LEFT JOIN note_tags nt ON nt.tag_id = t.id "
            "GROUP BY t.id ORDER BY count DESC, t.name"
        )
        partitions = self.db.query(
            "SELECT p.id, p.name, COUNT(np.note_id) AS count "
            "FROM partitions p LEFT JOIN note_partitions np ON np.partition_id = p.id "
            "GROUP BY p.id ORDER BY count DESC, p.name"
        )
        return {
            "tags": [dict(r) for r in tags],
            "partitions": [dict(r) for r in partitions],
        }

    # -- 反链 -------------------------------------------------------------
    def backlinks(self, note_path: str) -> list[dict[str, Any]]:
        """谁引用了这条笔记（反链）。"""
        rows = self.db.query(
            "SELECT n.note_path, n.title, n.level, l.kind, s.title AS src_title, "
            "s.level AS src_level "
            "FROM links l "
            "JOIN notes n ON n.id = l.dst_note_id "
            "JOIN notes s ON s.id = l.src_note_id "
            "WHERE n.note_path = ?",
            (note_path,),
        )
        return [dict(r) for r in rows]

    def outlinks(self, note_path: str) -> list[dict[str, Any]]:
        """这条笔记引用了谁（出链）。"""
        rows = self.db.query(
            "SELECT n.note_path, n.title, n.level, l.kind FROM links l "
            "JOIN notes n ON n.id = l.dst_note_id "
            "JOIN notes s ON s.id = l.src_note_id "
            "WHERE s.note_path = ?",
            (note_path,),
        )
        return [dict(r) for r in rows]

    def link_graph(self, note_path: str) -> dict[str, Any]:
        """反链视图数据（echarts 树图用，非力导向图谱）。"""
        backlinks = self.backlinks(note_path)
        outlinks = self.outlinks(note_path)
        return {
            "center": {"note_path": note_path},
            "backlinks": [
                {
                    "path": b["note_path"],
                    "title": b["title"],
                    "kind": b["kind"],
                    "label": "被引用" if b["kind"] == "citation" else "确认关联",
                }
                for b in backlinks
            ],
            "outlinks": [
                {
                    "path": o["note_path"],
                    "title": o["title"],
                    "kind": o["kind"],
                    "label": "引用" if o["kind"] == "citation" else "确认关联",
                }
                for o in outlinks
            ],
        }

    # -- 找相似 -----------------------------------------------------------
    def similar(self, note_path: str, top_k: int = 10) -> dict[str, Any]:
        """语义推荐：找相似笔记。"""
        try:
            from app.services.embedding_service import get_embedding_service

            svc = get_embedding_service()
            if not svc.available():
                return {
                    "results": [],
                    "available": False,
                    "message": "embedding 服务不可用，相关召回降级为关键词匹配",
                    "fallback": self._keyword_similar(note_path, top_k),
                }
            results = svc.similar_notes(note_path, top_k=top_k)
            return {"results": results, "available": True}
        except Exception as exc:  # noqa: BLE001
            return {
                "results": [],
                "available": False,
                "message": str(exc),
                "fallback": self._keyword_similar(note_path, top_k),
            }

    def _keyword_similar(self, note_path: str, top_k: int) -> list[dict[str, Any]]:
        """embedding 不可用时的降级：关键词 + 标签匹配。"""
        row = self.db.query_one(
            "SELECT title FROM notes WHERE note_path = ?", (note_path,)
        )
        if row is None:
            return []
        title = str(row["title"] or "")
        tags = [
            str(r["name"])
            for r in self.db.query(
                "SELECT t.name FROM tags t JOIN note_tags nt ON nt.tag_id = t.id "
                "JOIN notes n ON n.id = nt.note_id WHERE n.note_path = ?",
                (note_path,),
            )
        ]
        if not tags:
            return []
        placeholders = ",".join("?" for _ in tags)
        rows = self.db.query(
            f"SELECT n.note_path, n.title, COUNT(*) AS c FROM note_tags nt "
            f"JOIN tags t ON t.id = nt.tag_id JOIN notes n ON n.id = nt.note_id "
            f"WHERE t.name IN ({placeholders}) AND n.note_path != ? "
            f"GROUP BY n.note_path ORDER BY c DESC LIMIT ?",
            [*tags, note_path, top_k],
        )
        return [
            {
                "note_path": r["note_path"],
                "title": r["title"],
                "score": float(r["c"]),
                "reason": f"共同标签：{', '.join(tags)}",
            }
            for r in rows
        ]


_search: SearchService | None = None


def get_search_service() -> SearchService:
    global _search
    if _search is None:
        _search = SearchService()
    return _search
