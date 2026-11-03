"""创作模板服务（原创区「空模板」）。

跟 `template_service`（解析模板）是两件事，别混：

| | 用途 | 谁用 | 形状 |
|---|---|---|---|
| 解析模板 | 告诉 AI **按什么结构**把原文整理成解析结果 | 工作区 AI | 一段给 AI 的提示词 |
| 创作模板 | 空手写笔记时的**骨架**（标题/小标题/打卡项） | 原创区编辑器 | 一段 Markdown 正文 |

用户："原创区的模板可在管理区添加" —— 之前这 6 个模板硬编码在
`OriginalView.vue` 里，想加一个得改代码重新打包。现在落库，
管理区可增删改，原创区直接读。

约定（沿用 v35/v36 的判断）：
  · 模板正文**不写 YAML frontmatter** —— `notes_service.create` 会自己生成，
    正文再写一段 `---` 会变成双重 frontmatter，Obsidian 打不开。
  · 占位符 `{{date}}` / `{{time}}` / `{{title}}` 由前端替换。
  · `suggest_tags` 存逗号分隔（SQLite 没有数组），出参转 list。
"""

from __future__ import annotations

from typing import Any

from app.core.db import get_db

# 内置模板。source='builtin'，可编辑可删除；
# 删掉的记在 deleted_note_templates 里，不会被重新播回来。
DEFAULT_NOTE_TEMPLATES: list[dict[str, str]] = [
    {
        "slug": 'distill',
        "name": '知识提炼',
        "description": 'B层标准格式：一句话总结 + 关键词 + 摘要 + ≥600字详细内容',
        "body": '# {{title}}\n\n- 分类：\n- 原始链接：\n- 作者：\n\n**关键词**：\n\n**摘要**：\n\n## 详细内容\n\n\n## 逻辑树\n\n- 主论点一\n  - 支撑论据\n- 主论点二\n\n## 双向链接\n- [[ ]]\n',
        "suggest_tags": '提炼',
        "suggest_partition": '',
    },
    {
        "slug": 'aggregate',
        "name": '知识聚合',
        "description": 'C层：按专题整合多篇提炼笔记，用它自己的话写',
        "body": '# {{title}}\n\n- 专题：\n- 更新：{{date}}\n\n## 这个专题在讲什么\n\n\n## 相关提炼笔记\n- [[ ]]\n\n## 我的整合\n\n\n## 结论 / 可复用的东西\n\n',
        "suggest_tags": '聚合',
        "suggest_partition": '',
    },
    {
        "slug": 'daily',
        "name": '日记',
        "description": 'Daily Note：当日要做的 + 记录 + 随想',
        "body": '# {{date}}\n\n## 今天要做的\n- [ ] \n\n## 记录\n\n\n## 随想\n\n\n## 今天关联到\n- [[ ]]\n',
        "suggest_tags": '日记',
        "suggest_partition": '',
    },
    {
        "slug": 'weekly',
        "name": '周回顾',
        "description": '一周复盘：做成什么、卡在哪、下周重点',
        "body": '# {{date}} 周回顾\n\n## 本周做成的事\n\n\n## 卡住的地方\n\n\n## 学到的东西\n\n\n## 下周最重要的一件事\n- [ ] \n\n## 本周笔记\n- [[ ]]\n',
        "suggest_tags": '复盘',
        "suggest_partition": '',
    },
    {
        "slug": 'template-note',
        "name": '规则模板',
        "description": 'D层：流程规范 / 提示词模板 / 操作手册',
        "body": '# {{title}}\n\n- 适用场景：\n- 更新：{{date}}\n\n## 规则 / 步骤\n\n1. \n2. \n\n## 注意事项\n\n',
        "suggest_tags": '模板',
        "suggest_partition": '',
    },
    {
        "slug": 'cheatsheet',
        "name": '基本概念',
        "description": 'Obsidian 语法速查（参考 ChaoNaoKu/基本概念.md 的常用部分）',
        "body": '# {{title}}\n\n> 速查表：左边是写法，右边是效果。点「源码」看原文照抄。\n\n## 笔记属性\n\n放在**文件开头**，三个减号包起来（本库由系统自动生成，手写会重复，别加）。\n可用字段：`aliases`（别名，可多个）、`tags`（标签）、`cssclasses`（样式）、\n以及任意自定义字段（文本 / 时间 / 数字 / 列表）。\n\n## 标题\n\n`# 一级` → 一级（本行上面那个就是）\n\n`## 二级`、`### 三级` …… 最多六级。**井号后面必须跟一个空格**。\n\n## 文本\n\n| 写法 | 效果 |\n|---|---|\n| `*斜体*` | *斜体* |\n| `**加粗**` | **加粗** |\n| `***又粗又斜***` | ***又粗又斜*** |\n| `==标黄==` | ==标黄== |\n| `~~划掉~~` | ~~划掉~~ |\n| `[[本名]]` | [[本名]]（双链） |\n| `[别名](本名)` | [别名](本名) |\n\n## 格式\n\n- 无序列表：`- 空格`\n  - 缩进两个空格就是下一级\n1. 有序列表：`1. 空格`\n2. 自动接号\n- [ ] 打卡列表：`- [ ] 空格`\n- [x] 打完勾：`- [x]`\n\n`---` 单独一行 = 分隔线：\n\n---\n',
        "suggest_tags": '',
        "suggest_partition": '',
    },
]


def _tags_to_list(raw: str | None) -> list[str]:
    return [t.strip() for t in str(raw or "").split(",") if t.strip()]


def _row_to_dict(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["suggest_tags"] = _tags_to_list(d.get("suggest_tags"))
    return d


class NoteTemplateService:
    """创作模板的增删改查 + 首次播种。"""

    # ---------------- 播种 ----------------
    def ensure_seeded(self) -> None:
        """表空的时候把内置模板写进去（只做一次）。

        用 `deleted_note_templates` 记住用户删过的名字，
        避免"删了又回来"的循环。
        """
        db = get_db()
        row = db.query_one("SELECT COUNT(*) AS n FROM note_templates")
        if row and int(row["n"]) > 0:
            return

        gone = {
            r["name"]
            for r in db.query("SELECT name FROM deleted_note_templates")
        }
        for i, tpl in enumerate(DEFAULT_NOTE_TEMPLATES):
            if tpl["name"] in gone:
                continue
            db.execute(
                "INSERT INTO note_templates "
                "(name, description, body, suggest_tags, suggest_partition, "
                " sort_order, source) VALUES (?,?,?,?,?,?,?)",
                (
                    tpl["name"],
                    tpl["description"],
                    tpl["body"],
                    tpl["suggest_tags"],
                    tpl["suggest_partition"],
                    i * 10,
                    "builtin",
                ),
            )

    def restore_defaults(self) -> int:
        """把内置模板补回来（用户手滑删了想要回）。返回新增条数。"""
        db = get_db()
        have = {r["name"] for r in db.query("SELECT name FROM note_templates")}
        added = 0
        for i, tpl in enumerate(DEFAULT_NOTE_TEMPLATES):
            if tpl["name"] in have:
                continue
            db.execute(
                "INSERT INTO note_templates "
                "(name, description, body, suggest_tags, suggest_partition, "
                " sort_order, source) VALUES (?,?,?,?,?,?,?)",
                (
                    tpl["name"],
                    tpl["description"],
                    tpl["body"],
                    tpl["suggest_tags"],
                    tpl["suggest_partition"],
                    i * 10,
                    "builtin",
                ),
            )
            db.execute(
                "DELETE FROM deleted_note_templates WHERE name = ?", (tpl["name"],)
            )
            added += 1
        return added

    # ---------------- 读 ----------------
    def list(self) -> list[dict[str, Any]]:
        self.ensure_seeded()
        db = get_db()
        rows = db.query(
            "SELECT id, name, description, body, suggest_tags, "
            "suggest_partition, sort_order, source, created_at, updated_at "
            "FROM note_templates ORDER BY sort_order ASC, id ASC"
        )
        return [_row_to_dict(r) for r in rows]

    def get(self, template_id: int) -> dict[str, Any] | None:
        db = get_db()
        row = db.query_one(
            "SELECT id, name, description, body, suggest_tags, "
            "suggest_partition, sort_order, source, created_at, updated_at "
            "FROM note_templates WHERE id = ?",
            (template_id,),
        )
        return _row_to_dict(row) if row else None

    # ---------------- 写 ----------------
    def create(
        self,
        name: str,
        body: str,
        description: str = "",
        suggest_tags: list[str] | str | None = None,
        suggest_partition: str = "",
    ) -> dict[str, Any] | None:
        self.ensure_seeded()
        db = get_db()
        exists = db.query_one("SELECT id FROM note_templates WHERE name = ?", (name,))
        if exists:
            return None
        row = db.query_one("SELECT COALESCE(MAX(sort_order), 0) AS m FROM note_templates")
        order = int(row["m"]) + 10 if row else 10
        db.execute(
            "INSERT INTO note_templates "
            "(name, description, body, suggest_tags, suggest_partition, "
            " sort_order, source) VALUES (?,?,?,?,?,?,?)",
            (
                name,
                description,
                body,
                ",".join(_tags_to_list(suggest_tags if isinstance(suggest_tags, str) else ",".join(suggest_tags or []))),
                suggest_partition,
                order,
                "manual",
            ),
        )
        last = db.query_one(
            "SELECT id FROM note_templates WHERE name = ?", (name,)
        )
        return self.get(int(last["id"])) if last else None

    def update(self, template_id: int, **fields: Any) -> dict[str, Any] | None:
        """只更新传进来的字段（None = 不动）。"""
        db = get_db()
        if not db.query_one("SELECT id FROM note_templates WHERE id = ?", (template_id,)):
            return None

        sets: list[str] = []
        args: list[Any] = []
        if fields.get("name") is not None:
            sets.append("name = ?")
            args.append(str(fields["name"]).strip())
        if fields.get("description") is not None:
            sets.append("description = ?")
            args.append(str(fields["description"]))
        if fields.get("body") is not None:
            sets.append("body = ?")
            args.append(str(fields["body"]))
        if fields.get("suggest_tags") is not None:
            raw = fields["suggest_tags"]
            if isinstance(raw, str):
                val = ",".join(_tags_to_list(raw))
            else:
                val = ",".join(str(x).strip() for x in raw if str(x).strip())
            sets.append("suggest_tags = ?")
            args.append(val)
        if fields.get("suggest_partition") is not None:
            sets.append("suggest_partition = ?")
            args.append(str(fields["suggest_partition"]))
        if fields.get("sort_order") is not None:
            sets.append("sort_order = ?")
            args.append(int(fields["sort_order"]))

        if sets:
            sets.append("updated_at = CURRENT_TIMESTAMP")
            args.append(template_id)
            db.execute(
                f"UPDATE note_templates SET {', '.join(sets)} WHERE id = ?", tuple(args)
            )
        return self.get(template_id)

    def delete(self, template_id: int) -> bool:
        db = get_db()
        row = db.query_one(
            "SELECT name FROM note_templates WHERE id = ?", (template_id,)
        )
        if not row:
            return False
        db.execute("DELETE FROM note_templates WHERE id = ?", (template_id,))
        # 记一笔，免得下次 ensure_seeded 又把它播回来
        db.execute(
            "INSERT OR REPLACE INTO deleted_note_templates (name) VALUES (?)",
            (row["name"],),
        )
        return True


_service: NoteTemplateService | None = None


def get_note_template_service() -> NoteTemplateService:
    global _service
    if _service is None:
        _service = NoteTemplateService()
    return _service
