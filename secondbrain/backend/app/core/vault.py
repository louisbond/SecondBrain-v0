"""Vault 读写层：Markdown 是唯一真相源。

对齐 v0.5.0 §4.1–§4.4：
- A 层 `01_Inbox/`：原文全文 + 字幕 + 截图 + 来源
- B 层 `02_Processed/`：AI 摘要 + 个人手稿 + 受控标签 + 相关链接
- C 层 `03_Notes/`：创作笔记（单一类型，form 字段区分）

Frontmatter 采用手写 YAML 子集（避免引入 pyyaml 依赖），支持：
    key: value
    key: [a, b, c]
    key:
      - a
      - b
    key: |
      多行文本
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.tokenizer import normalize_tag

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)


# --------------------------------------------------------------------------
# Frontmatter 解析 / 序列化
# --------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 md 文件 → (frontmatter dict, 正文)。"""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    body = text[m.end() :]
    data: dict[str, Any] = {}
    current_key: str | None = None
    for line in raw.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        # 列表项
        if line.lstrip().startswith("- ") and current_key:
            value = line.lstrip()[2:].strip().strip('"').strip("'")
            existing = data.get(current_key)
            if not isinstance(existing, list):
                existing = []
            existing.append(value)
            data[current_key] = existing
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "":
            data[key] = []
            current_key = key
            continue
        current_key = key
        if value.startswith("["):
            inner = value.strip("[]").strip()
            if not inner:
                data[key] = []
            else:
                data[key] = [
                    v.strip().strip('"').strip("'") for v in inner.split(",") if v.strip()
                ]
        elif value in ("true", "false"):
            data[key] = value == "true"
        elif value.startswith("|") or value.startswith(">"):
            data[key] = ""
        else:
            data[key] = value.strip('"').strip("'")
    return data, body


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if "\n" in text:
        return ""
    if text == "" or any(ch in text for ch in ":#[]{}\",'") or text.strip() != text:
        escaped = text.replace('"', '\\"')
        return f'"{escaped}"'
    return text


def dump_frontmatter(data: dict[str, Any]) -> str:
    """frontmatter dict → YAML 文本（含 --- 包裹）。"""
    lines = ["---"]
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"  - {_yaml_scalar(item)}")
        elif isinstance(value, str) and "\n" in value:
            lines.append(f"{key}: |")
            for ln in value.splitlines():
                lines.append(f"  {ln}")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def render_markdown(frontmatter: dict[str, Any], body: str) -> str:
    """组装完整 md 文件内容。"""
    return f"{dump_frontmatter(frontmatter)}\n{body.lstrip(chr(10))}"


# --------------------------------------------------------------------------
# 笔记读写
# --------------------------------------------------------------------------

@dataclass
class Note:
    """一条 B/C 层笔记（内存表示）。"""

    path: Path
    level: str                     # processed / note
    title: str = ""
    form: str = ""
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def rel_path(self) -> str:
        return self.path.as_posix()

    @property
    def summary(self) -> str:
        return str(self.frontmatter.get("ai_summary") or "")

    @property
    def manuscript(self) -> str:
        return str(self.frontmatter.get("personal_summary") or "")

    @property
    def tags(self) -> list[str]:
        return list(self.frontmatter.get("tags") or [])

    @property
    def partitions(self) -> list[str]:
        return list(self.frontmatter.get("partitions") or [])

    @property
    def digested(self) -> bool:
        return bool(self.frontmatter.get("digested"))

    @property
    def personal_summary_empty(self) -> bool:
        return not self.manuscript.strip()


def read_note(path: Path) -> Note | None:
    """读取 md 笔记，解析 frontmatter。"""
    path = Path(path)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    fm, body = parse_frontmatter(text)
    level = str(fm.get("type") or "")
    if level == "processed":
        level = "processed"
    elif level == "note":
        level = "note"
    else:
        level = "note"
    return Note(
        path=path,
        level=level,
        title=str(fm.get("title") or path.stem),
        form=str(fm.get("form") or ""),
        frontmatter=fm,
        body=body,
    )


def write_note(path: Path, frontmatter: dict[str, Any], body: str) -> Path:
    """写入 md 笔记（父目录自动创建）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(frontmatter, body), encoding="utf-8")
    return path


def sanitize_filename(name: str, max_len: int = 60) -> str:
    """文件名安全化：去掉非法字符，限制长度，折叠点号串。

    非法字符（含 `/` `\\` `:` `*` `?` `"` `<` `>` `|`）会被直接去掉，
    因此 `../../etc/passwd` 会退化成 `......etcpasswd`——路径穿越被阻断，
    但残留的点号串既难看也可能被某些工具误判为相对路径，这里一并折叠。
    """
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "", name or "").strip()
    name = re.sub(r"\s+", " ", name)
    # 折叠连续的点（保留单个点，如 "v1.2"）
    name = re.sub(r"\.{2,}", ".", name)
    name = name.strip(". ")
    if not name:
        name = "untitled"
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name


def unique_path(directory: Path, stem: str, suffix: str = ".md") -> Path:
    """在目录内生成不冲突的文件名（追加 -2 / -3 …）。"""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    index = 2
    while True:
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def now_iso() -> str:
    """本地时间 ISO 格式（无时区标记，与文档示例一致）。"""
    return datetime.now().replace(microsecond=0).isoformat()


def slugify_tag(name: str) -> str:
    return normalize_tag(name)


# --------------------------------------------------------------------------
# 链接解析
# --------------------------------------------------------------------------

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


def parse_wikilinks(text: str) -> list[str]:
    """从文本中提取 [[...]] 链接目标（不含 #anchor 与 |alias）。"""
    if not text:
        return []
    out: list[str] = []
    for raw in WIKILINK_RE.findall(text):
        target = raw.strip()
        if target:
            out.append(target)
    return out


def link_target_to_relpath(target: str, vault_path: Path, level_hint: str = "") -> str:
    """把 [[目标]] 解析成 vault 内相对路径（posix）。

    解析顺序（越靠前越精确）：
    1. 目标本身是 vault 内的相对路径 → 直接用；
    2. 按**文件名精确匹配**（用户写 [[03_Notes/xxx.md]]）；
    3. 按**文件名后缀匹配**（用户写 [[标题]]，文件叫 2026-11-01-标题.md）；
    4. 按 **frontmatter title 匹配**（最宽容，也最符合"用标题引用"的直觉）；
    5. 兜底：按层目录拼路径。

    第 3、4 步是必需的——工具生成的 B/C 层文件名带日期前缀
    （`2026-11-01-什么是第一性原理.md`），而用户手写引用时几乎总是写标题。
    """
    target = target.strip().replace("\\", "/")
    if target.endswith(".md"):
        candidate = vault_path / target
        if candidate.exists():
            return candidate.as_posix()

    stem = Path(target).stem

    # 2) 文件名精确匹配
    direct = vault_path / f"{stem}.md"
    if direct.exists():
        return direct.as_posix()

    # 3) 文件名后缀匹配（去掉日期前缀，如 2026-11-01-标题.md）
    #    必须以分隔符开头才匹配，避免 "笔记" 命中 "手写测试笔记" 这类误伤
    try:
        suffix_matches = [
            p
            for p in vault_path.rglob("*.md")
            if p.stem.endswith(f"-{stem}") or p.stem.endswith(f"_{stem}")
        ]
    except OSError:
        suffix_matches = []
    if suffix_matches:
        return _pick_best(suffix_matches, vault_path)

    # 4) frontmatter title 匹配
    try:
        title_matches = []
        for path in vault_path.rglob("*.md"):
            if path.stem == stem:
                title_matches.append(path)
                continue
            try:
                fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            if str(fm.get("title") or "").strip() == stem:
                title_matches.append(path)
    except OSError:
        title_matches = []
    if title_matches:
        return _pick_best(title_matches, vault_path)

    # 5) 兜底：按层目录拼路径
    if level_hint:
        return f"{level_hint}/{stem}.md"
    return f"{stem}.md"


def _pick_best(matches: list[Path], vault_path: Path) -> str:
    """多个候选时优先 B/C 层（排除 A 层与归档）。"""

    def _score(p: Path) -> tuple[int, str]:
        s = 0
        try:
            parts = p.relative_to(vault_path).parts
        except ValueError:
            parts = ()
        if parts and parts[0] in ("02_Processed", "03_Notes"):
            s -= 2
        if parts and parts[0] == "99_Archive":
            s += 5
        if parts and parts[0] == "01_Inbox":
            s += 3
        return (s, p.as_posix())

    return sorted(matches, key=_score)[0].as_posix()
