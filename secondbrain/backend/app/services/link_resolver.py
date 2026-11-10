"""链接解析器：一次性索引 vault，之后 O(1) 查表。

**为什么需要它**：`vault_mod.link_target_to_relpath()` 每次调用都会
`rglob("*.md")` 遍历整个 vault（还可能逐个读 frontmatter）。
逐条解析 5000 篇笔记的引用就是 O(n²)——实测重建索引从 18s 变成 >900s。

这里改为：先扫一遍 vault 建立
    {文件名 stem} → 路径
    {去掉日期前缀的 stem} → 路径
    {frontmatter title} → 路径
三张映射表，之后查表即可。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core import vault as vault_mod


class LinkResolver:
    """vault 内 [[链接]] 目标的解析器。"""

    def __init__(self, vault_path: Path) -> None:
        self.vault_path = Path(vault_path)
        self._by_stem: dict[str, str] = {}
        self._by_title: dict[str, str] = {}
        self._by_path: dict[str, str] = {}
        self._built = False

    # -- 建索引 -----------------------------------------------------------
    def build(self) -> LinkResolver:
        """扫描 vault 建立映射表（幂等）。"""
        if self._built:
            return self
        try:
            files = list(self.vault_path.rglob("*.md"))
        except OSError:
            files = []

        candidates: list[Path] = []
        for path in files:
            stem = path.stem
            # 文件名精确匹配优先
            self._by_stem.setdefault(stem, path.as_posix())
            # 去掉日期前缀（2026-11-01-标题 → 标题）
            if "-" in stem:
                short = stem.rsplit("-", 1)[-1]
                if short:
                    self._by_stem.setdefault(short, path.as_posix())
            candidates.append(path)

        # frontmatter title 匹配（最宽容）
        for path in candidates:
            try:
                fm, _ = vault_mod.parse_frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            title = str(fm.get("title") or "").strip()
            if title:
                self._by_title.setdefault(title, path.as_posix())
                # 也允许用标题的"去日期前缀"形式
                if "-" in title:
                    self._by_title.setdefault(title.rsplit("-", 1)[-1].strip(), path.as_posix())

        self._built = True
        return self

    # -- 解析 -------------------------------------------------------------
    def resolve(self, raw: str) -> str:
        """把 [[目标]] 解析成 vault 内绝对路径（posix）。找不到返回空串。"""
        target = raw.strip()
        if target.startswith("[[") and target.endswith("]]"):
            target = target[2:-2]
        if not target:
            return ""
        target = target.replace("\\", "/")
        self.build()

        # 1) 直接是 vault 内路径
        if target.endswith(".md"):
            candidate = self.vault_path / target
            if candidate.exists():
                return candidate.as_posix()
            hit = self._by_path.get(target)
            if hit:
                return hit

        stem = Path(target).stem

        # 2) 文件名精确匹配
        hit = self._by_stem.get(stem)
        if hit:
            return hit

        # 3) frontmatter title 匹配
        hit = self._by_title.get(stem)
        if hit:
            return hit

        # 4) 去日期前缀后匹配
        if "-" in stem:
            short = stem.rsplit("-", 1)[-1]
            hit = self._by_stem.get(short) or self._by_title.get(short)
            if hit:
                return hit

        return ""

    def stats(self) -> dict[str, Any]:
        return {
            "stems": len(self._by_stem),
            "titles": len(self._by_title),
        }
