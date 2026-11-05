"""前端 ↔ 后端 契约测试。

**为什么需要这个文件**：
v30 的控件审计发现「采集区点入库永远失败」，根因是
前端 `collectApi.submit({entries: [...]})` 与 client.js 里的
`submit(entries, forceSimilar)` 签名不一致 → 请求体被包成
`{entries: {entries: [...]}}` → 后端 422「Input should be a valid list」。

同类 bug 的特征：
- 前端 HTTP 200/422，页面看起来"点了没反应"；
- 原有 5 个测试套件**全绿**（它们只打后端，不碰前端 payload）。

所以这里做一件它们都不做的事：
**静态解析前端源码里构造的 payload 字段名，与后端 Pydantic 模型的字段比对。**

字段对不上 → 直接失败，不等到运行时才 422。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
FRONTEND_SRC = BACKEND_DIR.parent / "frontend" / "src"
sys.path.insert(0, str(BACKEND_DIR))


def _read(rel: str) -> str:
    path = FRONTEND_SRC / rel
    if not path.exists():
        raise AssertionError(f"前端源码不存在：{path}")
    return path.read_text(encoding="utf-8")


def _read_containing(call: str) -> tuple[str, str]:
    """在整个前端源码里找包含某个**调用**的文件，返回 (相对路径, 源码)。

    v41 的教训：以前用例把路径写死（`views/ProcessView.vue`），
    把核准逻辑抽到 composable 之后，明明 payload 一个字没改，
    测试却报 `ValueError: substring not found`。
    **契约测试关心的是 payload 字段，不该关心代码放在哪个文件。**
    这里改成按调用点定位，以后再搬文件也不会假失败。
    """
    for path in sorted(FRONTEND_SRC.rglob("*")):
        if path.suffix not in (".vue", ".js", ".ts"):
            continue
        text = path.read_text(encoding="utf-8")
        if call in text:
            return str(path.relative_to(FRONTEND_SRC)), text
    raise AssertionError(f"前端源码里找不到调用 {call}")


def _model_fields(model_name: str) -> set[str]:
    """取后端 Pydantic 模型的字段名集合。"""
    from app.api.routes import collect, notes, review, workbench

    for mod in (collect, notes, review, workbench):
        model = getattr(mod, model_name, None)
        if model is not None:
            return set(model.model_fields.keys())
    raise AssertionError(f"找不到后端模型 {model_name}")


def _payload_keys_after(src: str, call: str) -> set[str]:
    """取 `xxxApi.yyy({ ... })` 调用里对象字面量的顶层 key。

    要同时认得两种写法：
      { title: x, body: y }        显式 key
      { title: x, body }           ES6 简写（key = 变量名）
    """
    idx = src.index(call)
    # 从调用处找到配平的 { }
    start = src.index("{", idx)
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                block = src[start : i + 1]
                break
    else:
        raise AssertionError(f"{call} 的对象字面量没配平")

    # 顶层 key：depth == 1 时，形如 `key:` 或 `key` 后跟 `,` / `}`
    keys: set[str] = set()
    d = 0
    token = re.compile(
        r"[{}]|\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*\.\.\.\s*\]"  # 展开：[...x]
        r"|([A-Za-z_][A-Za-z0-9_]*)\s*:"                          # 显式 key
        r"|([A-Za-z_][A-Za-z0-9_]*)\s*(?=[,}])"                  # 简写 key
    )
    for m in token.finditer(block):
        tok = m.group(0)
        if tok == "{":
            d += 1
            continue
        if tok == "}":
            d -= 1
            continue
        if d != 1:
            continue
        if m.group(1):      # [...spread]
            keys.add(m.group(1))
        elif m.group(2):    # key:
            keys.add(m.group(2))
        elif m.group(3):    # 简写 key
            keys.add(m.group(3))
    return keys


class TestFrontendContract:
    """前端 payload 字段必须与后端请求模型一致。"""

    def test_collect_submit_payload_matches_model(self):
        """采集区入库：entries 必须是**数组**（不是被包了双层）。"""
        backend_fields = _model_fields("SubmitRequest")
        assert "entries" in backend_fields

        _, src = _read_containing("collectApi.submit(")
        keys = _payload_keys_after(src, "collectApi.submit(")
        assert keys, "没解析到 collectApi.submit 的 payload 字段"

        # 请求体顶层必须只有后端认识的字段
        extra = keys - backend_fields
        assert not extra, (
            f"采集区入库 payload 有后端不认识的字段 {extra}；"
            f"后端 SubmitRequest 只接受 {sorted(backend_fields)}"
        )
        assert "entries" in keys, f"采集区入库 payload 缺 entries：{keys}"

    def test_client_submit_signature_matches_caller(self):
        """client.js 的 submit 签名必须与调用方传参形态一致。

        回归背景：老签名 `submit(entries, forceSimilar)`，
        调用方传了 `{entries: [...]}` → 双层包裹 → 422。
        """
        src = _read("api/client.js")
        m = re.search(
            r"submit:\s*\(([^)]*)\)\s*=>\s*api\.post\(\s*'/api/collect/submit'\s*,\s*([^)]+)\)",
            src,
        )
        assert m, "client.js 里找不到 collectApi.submit 的定义"

        params, body_expr = m.group(1).strip(), m.group(2).strip()

        # body 必须直接用参数（透传），不能是 {entries: <param>} 这种再包装
        assert params in body_expr, (
            f"submit 的请求体 {body_expr!r} 没直接透传参数 {params!r}；"
            "再包一层会让后端收到 {entries: {entries: [...]}} → 422"
        )

        _, caller = _read_containing("collectApi.submit(")
        keys = _payload_keys_after(caller, "collectApi.submit(")
        # 调用方传的应该是「entries 数组本身」或「整个请求体」，
        # 两种都不该出现"submit 里再包一层"
        assert "entries" in keys or keys == set(), keys

    def test_note_create_payload_matches_model(self):
        """原创区保存：正文字段名必须与 CreateNoteRequest 一致。

        回归背景：前端传 `content`，后端模型是 `body`
        → 正文被静默丢弃，存出来的 md 只有 frontmatter。
        """
        backend_fields = _model_fields("CreateNoteRequest")
        assert "body" in backend_fields

        _, src = _read_containing("notesApi.create(")
        keys = _payload_keys_after(src, "notesApi.create(")
        assert "title" in keys, keys
        assert "body" in keys, (
            f"原创区保存 payload 缺 body 字段：{keys}；"
            f"后端 CreateNoteRequest 接受 {sorted(backend_fields)}"
        )
        assert "content" not in keys, (
            "前端传了 content，后端 CreateNoteRequest 不认（正文会静默丢失）"
        )

    def test_review_confirm_payload_matches_model(self):
        """加工区确认入库：payload 字段必须与 ConfirmRequest 一致。"""
        backend_fields = _model_fields("ConfirmRequest")
        _, src = _read_containing("reviewApi.confirm(")
        keys = _payload_keys_after(src, "reviewApi.confirm(")
        extra = keys - backend_fields
        assert not extra, f"确认入库 payload 有后端不认识的字段 {extra}"
        assert "title" in keys and "tags" in keys, keys

    def test_fill_manuscript_payload_matches_model(self):
        """补写手稿：note_path 走请求体（路径里会被贪婪路由抢先命中）。"""
        backend_fields = _model_fields("ManuscriptIn")
        assert {"note_path", "manuscript"} <= backend_fields

        src = _read("api/client.js")
        m = re.search(r"fillManuscript:\s*\(([^)]*)\)\s*=>", src)
        assert m, "client.js 里找不到 fillManuscript"
        # 参数名是 path + manuscript，而后端模型要 note_path
        assert "note_path" in src, "client.js 没传 note_path"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
