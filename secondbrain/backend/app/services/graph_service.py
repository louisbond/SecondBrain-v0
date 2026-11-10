"""图谱数据服务：把库里的笔记、分区、标签、链接变成一张可渲染的图。

前端用自研的轻量 3D 力导向引擎渲染（零外部依赖）：
节点有类型（笔记 / 分区 / 标签），边有关系（链接 / 引用 / 归属 / 标签）。
"""

from __future__ import annotations

from typing import Any

from app.core.db import get_db

NODE_TYPES = {"note": "笔记", "partition": "分区", "tag": "标签"}


class GraphService:
    """图谱数据。"""

    def __init__(self) -> None:
        self.db = get_db()

    def build(
        self,
        limit: int = 400,
        include_tags: bool = True,
        include_partitions: bool = True,
        level: str = "all",
    ) -> dict[str, Any]:
        db = self.db

        # ---- 节点：笔记 ----
        level_filter = ""
        params: list[Any] = []
        if level in ("note", "processed"):
            level_filter = " AND level = ?"
            params.append(level)
        rows = db.query(
            "SELECT id, note_path, title, level, digested FROM notes "
            f"WHERE archived = 0{level_filter} ORDER BY id DESC LIMIT ?",
            [*params, limit],
        )

        nodes: list[dict[str, Any]] = []
        id_by_path: dict[str, int] = {}
        for r in rows:
            digested = bool(r["digested"])
            nodes.append(
                {
                    "id": f"n:{r['id']}",
                    "kind": "note",
                    "label": r["title"] or r["note_path"],
                    "path": r["note_path"],
                    "level": r["level"],
                    # 有个人手稿的笔记是库的核心资产 → 更大更醒目
                    "has_manuscript": digested,
                    "size": 3 if digested else 1,
                }
            )
            id_by_path[str(r["note_path"])] = int(r["id"])

        edges: list[dict[str, Any]] = []

        # ---- 节点：个人手稿（3D 树的主要元素之一）----
        # 每篇有手稿的笔记，把手稿本身作为节点挂出来——
        # 用户自己的思考是知识库的主体，不是附属品。
        mrows = db.query(
            "SELECT id, note_path, title FROM notes "
            "WHERE archived = 0 AND digested = 1 "
            "ORDER BY id DESC LIMIT ?",
            [limit],
        )
        manuscript_ids: set[int] = set()
        for r in mrows:
            nid = int(r["id"])
            manuscript_ids.add(nid)
            nodes.append(
                {
                    "id": f"m:{nid}",
                    "kind": "manuscript",
                    "label": "手稿",
                    "path": r["note_path"],
                    "note_title": r["title"] or r["note_path"],
                    "size": 2,
                }
            )
            edges.append(
                {"source": f"n:{nid}", "target": f"m:{nid}", "kind": "manuscript"}
            )

        # ---- 节点：分区 ----
        if include_partitions:
            prows = db.query(
                "SELECT p.id, p.name, p.path, p.parent_id, "
                "(SELECT COUNT(*) FROM note_partitions np WHERE np.partition_id = p.id) AS cnt "
                "FROM partitions p ORDER BY p.level, p.path"
            )
            for r in prows:
                nodes.append(
                    {
                        "id": f"p:{r['id']}",
                        "kind": "partition",
                        "label": r["name"],
                        "path": r["path"],
                        "size": 1 + min(int(r["cnt"] or 0), 8),
                    }
                )
            for r in prows:
                if r["parent_id"]:
                    edges.append(
                        {
                            "source": f"p:{r['parent_id']}",
                            "target": f"p:{r['id']}",
                            "kind": "hierarchy",
                        }
                    )

        # ---- 节点：标签 ----
        if include_tags:
            trows = db.query(
                "SELECT t.id, t.name, "
                "(SELECT COUNT(*) FROM note_tags nt WHERE nt.tag_id = t.id) AS cnt "
                "FROM tags t ORDER BY cnt DESC LIMIT 60"
            )
            for r in trows:
                nodes.append(
                    {
                        "id": f"t:{r['id']}",
                        "kind": "tag",
                        "label": r["name"],
                        "size": 1 + min(int(r["cnt"] or 0), 6),
                    }
                )

        # ---- 边：笔记 ↔ 分区（归属）----
        if include_partitions:
            arows = db.query(
                "SELECT np.note_id, np.partition_id FROM note_partitions np"
            )
            for r in arows:
                if f"n:{r['note_id']}" in {x["id"] for x in nodes}:
                    edges.append(
                        {
                            "source": f"n:{r['note_id']}",
                            "target": f"p:{r['partition_id']}",
                            "kind": "belongs",
                        }
                    )

        # ---- 边：笔记 ↔ 标签 ----
        if include_tags:
            tedges = db.query("SELECT note_id, tag_id FROM note_tags")
            for r in tedges:
                edges.append(
                    {
                        "source": f"n:{r['note_id']}",
                        "target": f"t:{r['tag_id']}",
                        "kind": "tagged",
                    }
                )

        # ---- 边：笔记 ↔ 笔记（确认链接 / 引用）----
        lrows = db.query(
            "SELECT src_note_id, dst_note_id, kind FROM links"
        )
        for r in lrows:
            edges.append(
                {
                    "source": f"n:{r['src_note_id']}",
                    "target": f"n:{r['dst_note_id']}",
                    "kind": str(r["kind"] or "link"),
                }
            )

        # 只保留两端都在节点集里的边
        valid_ids = {x["id"] for x in nodes}
        edges = [
            e
            for e in edges
            if e["source"] in valid_ids and e["target"] in valid_ids
        ]

        return {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "nodes": len(nodes),
                "edges": len(edges),
                "notes": sum(1 for n in nodes if n["kind"] == "note"),
                "manuscripts": sum(1 for n in nodes if n["kind"] == "manuscript"),
                "partitions": sum(1 for n in nodes if n["kind"] == "partition"),
                "tags": sum(1 for n in nodes if n["kind"] == "tag"),
            },
        }

    # -- 目录树（快速跳转用）------------------------------------------
    def vault_tree(self) -> list[dict[str, Any]]:
        """vault 目录树：目录 → 子目录 / md 文件。"""
        from pathlib import Path

        from app.core.config import get_config

        root = get_config().vault.path

        def _walk(path: Path) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            try:
                entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
            except OSError:
                return out
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    children = _walk(entry)
                    out.append(
                        {
                            "kind": "dir",
                            "name": entry.name,
                            "path": entry.relative_to(root).as_posix(),
                            "children": children,
                        }
                    )
                elif entry.suffix.lower() == ".md":
                    out.append(
                        {
                            "kind": "file",
                            "name": entry.stem,
                            "path": entry.relative_to(root).as_posix(),
                            "children": [],
                        }
                    )
            return out

        return _walk(Path(root))


_service: GraphService | None = None


def get_graph_service() -> GraphService:
    global _service
    if _service is None:
        _service = GraphService()
    return _service
