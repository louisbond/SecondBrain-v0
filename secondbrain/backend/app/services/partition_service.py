"""分区树服务。

分区是**树**（最多 3 层）：

- `parent_id` 自引用，`level` 是层级（1~3），`path` 是物化路径（"成长树/思维逻辑"）
- 选中父分区 = 包含其整棵子树（查询时向上展开）
- 笔记只记叶子分区；改名/移动父节点时子树路径级联更新
- 冷启动：vault 的目录树天然就是分区树（成长树/思维逻辑）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.db import get_db

MAX_LEVEL = 3


class PartitionService:
    """分区树的增删改查与层级运算。"""

    # -- 内部：笔记侧联动 -------------------------------------------------
    def _sync_notes_partitions(
        self, old_prefix: str, new_prefix: str | None
    ) -> int:
        """分区改名/删除后，同步所有笔记 md 里的 `partitions` 字段。

        · 改名：old_prefix 及其子树下的路径改成 new_prefix 对应路径
        · 删除（new_prefix=None）：把 old_prefix 及子树下的分区名从笔记里移除

        只改文件 frontmatter，不重建索引 —— 索引表里的 note_partitions 是按
        partition_id 关联的，改名不影响；删除时调用方会先清关联表。
        返回被改写的笔记数。
        """
        from app.core import vault as vault_mod

        rows = get_db().query("SELECT note_path FROM notes WHERE level='note'")
        changed = 0
        for r in rows:
            path = Path(str(r["note_path"]))
            if not path.exists():
                continue
            note = vault_mod.read_note(path)
            if note is None:
                continue
            parts = list(note.frontmatter.get("partitions") or [])
            if not parts:
                continue

            new_parts: list[str] = []
            hit = False
            for x in parts:
                s = str(x)
                if s == old_prefix or s.startswith(old_prefix + "/"):
                    hit = True
                    if new_prefix is not None:
                        new_parts.append(new_prefix + s[len(old_prefix) :])
                    # 删除时：不再放回（等于移除）
                else:
                    new_parts.append(s)

            if hit:
                fm = dict(note.frontmatter)
                fm["partitions"] = new_parts
                vault_mod.write_note(path, fm, note.body)
                changed += 1
        return changed

    # -- 查询 -------------------------------------------------------------
    def list_all(self) -> list[dict[str, Any]]:
        db = get_db()
        rows = db.query(
            "SELECT p.id, p.name, p.parent_id, p.level, p.path, "
            "(SELECT COUNT(*) FROM note_partitions np WHERE np.partition_id = p.id) AS count "
            "FROM partitions p ORDER BY p.level ASC, p.path ASC"
        )
        return [dict(r) for r in rows]

    def get(self, partition_id: int) -> dict[str, Any] | None:
        db = get_db()
        row = db.query_one(
            "SELECT id, name, parent_id, level, path FROM partitions WHERE id = ?",
            (partition_id,),
        )
        return dict(row) if row else None

    def get_by_path(self, path: str) -> dict[str, Any] | None:
        db = get_db()
        row = db.query_one(
            "SELECT id, name, parent_id, level, path FROM partitions WHERE path = ?",
            (path,),
        )
        return dict(row) if row else None

    def tree(self) -> list[dict[str, Any]]:
        """返回嵌套树（children 递归）。"""
        rows = self.list_all()
        nodes: dict[int, dict[str, Any]] = {
            int(r["id"]): {
                "id": r["id"],
                "name": r["name"],
                "path": r["path"],
                "level": r["level"],
                "parent_id": r["parent_id"],
                "count": r.get("count", 0),
                "children": [],
            }
            for r in rows
        }
        roots: list[dict[str, Any]] = []
        for row in rows:
            node = nodes[int(row["id"])]
            parent_id = row["parent_id"]
            if parent_id and int(parent_id) in nodes:
                nodes[int(parent_id)]["children"].append(node)
            else:
                roots.append(node)
        return roots

    # -- 层级运算 ---------------------------------------------------------
    def ancestors(self, partition_id: int) -> list[int]:
        """从根到自身（含自身）的 id 链。"""
        chain: list[int] = []
        current = self.get(partition_id)
        seen: set[int] = set()
        while current and current["parent_id"]:
            pid = int(current["parent_id"])
            if pid in seen:
                break
            seen.add(pid)
            chain.append(pid)
            current = self.get(pid)
        chain.append(partition_id)
        return list(reversed(chain))

    def subtree_ids(self, partition_id: int) -> list[int]:
        """自身 + 全部后代 id。"""
        rows = self.list_all()
        children: dict[int | None, list[int]] = {}
        for r in rows:
            children.setdefault(r["parent_id"], []).append(int(r["id"]))
        out: list[int] = []
        stack = [int(partition_id)]
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(children.get(cur, []))
        return out

    def descendant_paths(self, partition_id: int) -> list[str]:
        ids = set(self.subtree_ids(partition_id))
        return [r["path"] for r in self.list_all() if int(r["id"]) in ids]

    # -- 写入 -------------------------------------------------------------
    def create(
        self, name: str, parent_id: int | None = None
    ) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("分区名不能为空")
        if "/" in name:
            raise ValueError("分区名不能含 /")

        parent: dict[str, Any] | None = None
        if parent_id:
            parent = self.get(int(parent_id))
            if parent is None:
                raise ValueError("父分区不存在")
        level = (int(parent["level"]) + 1) if parent else 1
        if level > MAX_LEVEL:
            raise ValueError(f"分区最多 {MAX_LEVEL} 层")
        path = f"{parent['path']}/{name}" if parent else name

        if self.get_by_path(path) is not None:
            raise ValueError(f"分区「{path}」已存在")

        db = get_db()

        def _run(conn) -> None:
            conn.execute(
                "INSERT INTO partitions(name, parent_id, level, path) VALUES(?,?,?,?)",
                (name, int(parent_id) if parent_id else None, level, path),
            )

        db.write(_run)
        created = self.get_by_path(path)
        assert created is not None
        return created

    def update(
        self,
        partition_id: int,
        name: str | None = None,
        parent_id: int | None = None,
    ) -> dict[str, Any]:
        current = self.get(partition_id)
        if current is None:
            raise KeyError("分区不存在")

        new_name = (name if name is not None else current["name"]).strip()
        if not new_name:
            raise ValueError("分区名不能为空")
        if "/" in new_name:
            raise ValueError("分区名不能含 /")

        new_parent_id = current["parent_id"] if parent_id is None else parent_id
        if new_parent_id and int(new_parent_id) == int(partition_id):
            raise ValueError("分区不能作为自己的父级")
        # 不能挂到自己的后代下（会成环）
        if new_parent_id and int(new_parent_id) in set(self.subtree_ids(partition_id)):
            raise ValueError("不能把分区移动到它自己的子分区下")

        parent = self.get(int(new_parent_id)) if new_parent_id else None
        if new_parent_id and parent is None:
            raise ValueError("父分区不存在")
        new_level = (int(parent["level"]) + 1) if parent else 1
        if new_level > MAX_LEVEL:
            raise ValueError(f"分区最多 {MAX_LEVEL} 层")
        new_path = f"{parent['path']}/{new_name}" if parent else new_name

        if new_path != current["path"] and self.get_by_path(new_path) is not None:
            raise ValueError(f"分区「{new_path}」已存在")

        db = get_db()
        _subtree = self.subtree_ids(partition_id)
        old_prefix = str(current["path"])

        def _run(conn) -> None:
            conn.execute(
                "UPDATE partitions SET name=?, parent_id=?, level=?, path=? WHERE id=?",
                (
                    new_name,
                    int(new_parent_id) if new_parent_id else None,
                    new_level,
                    new_path,
                    partition_id,
                ),
            )
            # 子树路径级联
            for row in conn.execute(
                "SELECT id, path, level FROM partitions WHERE id != ?", (partition_id,)
            ).fetchall():
                p_path = str(row["path"])
                if p_path.startswith(old_prefix + "/"):
                    suffix = p_path[len(old_prefix) :]
                    conn.execute(
                        "UPDATE partitions SET path=?, level=? WHERE id=?",
                        (new_path + suffix, new_level + (int(row["level"]) - int(current["level"])), row["id"]),
                    )

        db.write(_run)
        # 联动：所有笔记 md 里的 partitions 跟着改名（含子树路径）
        synced = 0
        if new_path != old_prefix:
            synced = self._sync_notes_partitions(old_prefix, new_path)
        updated = self.get(partition_id)
        assert updated is not None
        if synced:
            updated["synced_notes"] = synced
        return updated

    def delete(self, partition_id: int) -> dict[str, Any]:
        """删除分区；子分区上移到父级（不丢内容）。"""
        current = self.get(partition_id)
        if current is None:
            raise KeyError("分区不存在")

        db = get_db()
        children = [
            int(r["id"])
            for r in db.query(
                "SELECT id FROM partitions WHERE parent_id = ?", (partition_id,)
            )
        ]
        parent_id = current["parent_id"]
        parent = self.get(int(parent_id)) if parent_id else None
        new_level = (int(parent["level"]) + 1) if parent else 1
        new_path = f"{parent['path']}/{current['name']}" if parent else current["name"]

        def _run(conn) -> None:
            for cid in children:
                conn.execute(
                    "UPDATE partitions SET parent_id=?, level=?, path=? WHERE id=?",
                    (
                        int(parent_id) if parent_id else None,
                        new_level,
                        f"{new_path}/{conn.execute('SELECT name FROM partitions WHERE id=?', (cid,)).fetchone()[0]}",
                        cid,
                    ),
                )
            conn.execute("DELETE FROM note_partitions WHERE partition_id = ?", (partition_id,))
            conn.execute("DELETE FROM partitions WHERE id = ?", (partition_id,))

        db.write(_run)
        # 联动：把已删分区（含子树）从笔记 md 的 partitions 里摘掉，
        # 否则下次索引重建会按旧名字把分区「复活」。
        removed = self._sync_notes_partitions(str(current["path"]), None)
        return {"ok": True, "moved_up": len(children), "cleaned_notes": removed}

    def ensure(self, path: str) -> dict[str, Any]:
        """按路径确保分区存在（逐级创建）。"""
        path = (path or "").strip().strip("/")
        if not path:
            raise ValueError("路径不能为空")
        current: dict[str, Any] | None = None
        for part in path.split("/"):
            parent_id = int(current["id"]) if current else None
            existing = self.get_by_path(f"{current['path']}/{part}" if current else part)
            if existing is not None:
                current = existing
                continue
            current = self.create(part, parent_id)
        assert current is not None
        return current

    def ensure_by_name(
        self, name: str, parent_id: int | None = None
    ) -> dict[str, Any]:
        """按 (父, 名) 确保分区存在（分区名可含 / 表示路径）。"""
        raw = (name or "").strip()
        if not raw:
            raise ValueError("分区名不能为空")
        if "/" in raw:
            return self.ensure(raw)
        parent = self.get(int(parent_id)) if parent_id else None
        path = f"{parent['path']}/{raw}" if parent else raw
        existing = self.get_by_path(path)
        if existing is not None:
            return existing
        return self.create(raw, parent_id)

    # -- 冷启动：从 vault 目录树导入 ---------------------------------------
    # 非分类目录：不该成为分区（附件、图片、媒体……）
    NON_CATEGORY_DIRS = {
        "附件",
        "附件们",
        "assets",
        "images",
        "image",
        "media",
        "files",
        "files_",
        "pictures",
        "photos",
        "static",
        "dist",
        "public",
        "downloads",
        "download",
        "temp",
        "tmp",
    }

    def seed_from_vault_dirs(self, vault_root: Path, max_level: int = MAX_LEVEL) -> int:
        """把 vault 的顶层目录树导成分区树（成长树/思维逻辑 → 父子分区）。"""
        from app.services.index_service import TOOL_DIRS

        created = 0

        def _walk(directory: Path, parent: dict[str, Any] | None, level: int) -> None:
            nonlocal created
            if level > max_level:
                return
            try:
                children = sorted(
                    p for p in directory.iterdir() if p.is_dir()
                )
            except OSError:
                return
            for child in children:
                if (
                    child.name in TOOL_DIRS
                    or child.name.startswith(".")
                    or child.name.lower() in self.NON_CATEGORY_DIRS
                ):
                    continue
                name = child.name
                path = f"{parent['path']}/{name}" if parent else name
                existing = self.get_by_path(path)
                if existing is None:
                    try:
                        existing = self.create(name, int(parent["id"]) if parent else None)
                        created += 1
                    except ValueError:
                        existing = self.get_by_path(path)
                if existing is not None:
                    _walk(child, existing, level + 1)

        _walk(Path(vault_root), None, 1)
        return created


_service: PartitionService | None = None


def get_partition_service() -> PartitionService:
    global _service
    if _service is None:
        _service = PartitionService()
    return _service
