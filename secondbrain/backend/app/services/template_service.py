"""解析模板服务（工作区）。

模板 = "按什么结构把原文整理成解析结果"。工作区里 AI 会**自动匹配**最合适的模板，
再按模板生成解析结果；模板本身支持手动新增 / 编辑 / 删除。
"""

from __future__ import annotations

from typing import Any

from app.core.db import get_db
from app.core.vault import now_iso

# 首次使用时写入的默认模板（用户可改可删）
DEFAULT_TEMPLATES: list[dict[str, str]] = [
    {
        "name": "要点摘要",
        "description": "通用：提取核心要点 + 一句话总结",
        "body": (
            "请把下面这段内容整理成结构化解析结果，包含：\n"
            "1. 一句话结论（不超过 60 字）\n"
            "2. 核心要点（3-6 条，每条一句话，展开说明，不要只写标题）\n"
            "3. 关键细节（背景、数据、案例、原话）\n"
            "4. 可落地之处（读者能直接用的判断或做法）\n"
            "要求：正文不少于 1000 字，宁可展开也不要空洞。"
        ),
    },
    {
        "name": "结构化笔记",
        "description": "长文/课程：背景 / 观点 / 论据 / 启发",
        "body": (
            "请把下面这段内容整理成一份完整笔记，分四部分：\n"
            "## 背景\n（这段内容出现的语境、涉及的人物或事件）\n"
            "## 核心观点\n（作者/讲述者的主张，逐条展开）\n"
            "## 论据与细节\n（支撑观点的具体材料、数据、例子）\n"
            "## 我的启发\n（从中提炼的判断、疑问与行动）\n"
            "要求：正文不少于 1000 字，每部分都要有实质内容。"
        ),
    },
    {
        "name": "教学拆解",
        "description": "科普/教程：概念 / 原理 / 例子 / 误区",
        "body": (
            "请把下面这段内容整理成一份可教学的解析，包含：\n"
            "## 这是什么\n（概念定义，用通俗话解释一遍）\n"
            "## 原理\n（背后是怎么运作的，分步骤说明）\n"
            "## 例子\n（至少两个具体例子或场景）\n"
            "## 常见误区\n（容易搞错的地方，以及正确理解）\n"
            "要求：正文不少于 1000 字，面向零基础读者。"
        ),
    },
]


class TemplateService:
    """解析模板的增删改查。"""

    def list(self) -> list[dict[str, Any]]:
        db = get_db()
        rows = db.query(
            "SELECT id, name, description, body, source, created_at, updated_at "
            "FROM parse_templates ORDER BY id ASC"
        )
        return [dict(r) for r in rows]

    def get(self, template_id: int) -> dict[str, Any] | None:
        db = get_db()
        row = db.query_one(
            "SELECT id, name, description, body, source, created_at, updated_at "
            "FROM parse_templates WHERE id = ?",
            (template_id,),
        )
        return dict(row) if row else None

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        db = get_db()
        row = db.query_one(
            "SELECT id, name, description, body, source, created_at, updated_at "
            "FROM parse_templates WHERE name = ?",
            (name,),
        )
        return dict(row) if row else None

    def create(
        self,
        name: str,
        body: str,
        description: str = "",
        source: str = "manual",
    ) -> dict[str, Any]:
        name = (name or "").strip()
        body = (body or "").strip()
        if not name:
            raise ValueError("模板名不能为空")
        if not body:
            raise ValueError("模板内容不能为空")

        db = get_db()
        existing = self.get_by_name(name)
        if existing is not None:
            raise ValueError(f"模板「{name}」已存在")

        ts = now_iso()
        db.write(
            lambda conn: conn.execute(
                "INSERT INTO parse_templates(name, description, body, source, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (name, description.strip(), body, source, ts, ts),
            )
        )
        created = self.get_by_name(name)
        assert created is not None
        return created

    def update(
        self,
        template_id: int,
        name: str | None = None,
        body: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        current = self.get(template_id)
        if current is None:
            raise KeyError("模板不存在")
        new_name = (name if name is not None else current["name"]).strip()
        new_body = (body if body is not None else current["body"]).strip()
        new_desc = (description if description is not None else current["description"]).strip()
        if not new_name:
            raise ValueError("模板名不能为空")
        if not new_body:
            raise ValueError("模板内容不能为空")

        db = get_db()
        dup = self.get_by_name(new_name)
        if dup is not None and int(dup["id"]) != int(template_id):
            raise ValueError(f"模板「{new_name}」已存在")

        db.write(
            lambda conn: conn.execute(
                "UPDATE parse_templates SET name=?, description=?, body=?, updated_at=? "
                "WHERE id=?",
                (new_name, new_desc, new_body, now_iso(), template_id),
            )
        )
        updated = self.get(template_id)
        assert updated is not None
        return updated

    def delete(self, template_id: int) -> bool:
        db = get_db()
        existing = self.get(template_id)
        if existing is None:
            return False
        db.write(
            lambda conn: conn.execute(
                "DELETE FROM parse_templates WHERE id = ?", (template_id,)
            )
        )
        # 内置模板被用户删掉后不要复活
        if str(existing.get("source")) == "system":
            self._remember_deleted(str(existing.get("name") or ""))
        return True

    def seed_defaults(self, force: bool = False) -> int:
        """写入缺失的默认模板（同名已存在则跳过）。

        只补「从没存在过」的：用户主动删掉的内置模板不会被复活
        —— 用 deleted_templates 表记下来。
        """
        created = 0
        for tpl in DEFAULT_TEMPLATES:
            if self.get_by_name(tpl["name"]) is not None:
                continue
            if self._was_deleted(tpl["name"]):
                continue
            try:
                self.create(
                    tpl["name"],
                    tpl["body"],
                    tpl["description"],
                    source="system",
                )
                created += 1
            except ValueError:
                continue
        return created

    def _was_deleted(self, name: str) -> bool:
        """用户是否删过这个内置模板。"""
        try:
            row = self.db.query_one(
                "SELECT name FROM deleted_templates WHERE name = ?", (name,)
            )
            return row is not None
        except Exception:
            return False

    def _remember_deleted(self, name: str) -> None:
        try:
            self.db.write(
                lambda conn: conn.execute(
                    "INSERT OR IGNORE INTO deleted_templates(name, deleted_at) VALUES(?, ?)",
                    (name, now_iso()),
                )
            )
        except Exception:
            pass


_service: TemplateService | None = None


def get_template_service() -> TemplateService:
    global _service
    if _service is None:
        _service = TemplateService()
    return _service
