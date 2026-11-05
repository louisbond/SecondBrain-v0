"""前端结构约束（v41）：别再长回 900 行的巨石组件。

拆分前 `ManageView.vue` 952 行、`ProcessView.vue` 851 行：
6 个页签的状态 / 模板 / 样式全挤在一个 setup 里，改一处要在几百行里找。
v41 拆成主壳（94 / 72 行）+ 子组件 + composable，最大 .vue 314 行。

这里用**棘轮**锁住成果：
  · 新文件 ≤ 500 行；
  · 两个历史超限文件不在本轮范围，但锁住当前规模**不许再长**，
    并明确标成下一轮候选 —— 不是放过，是不让技术债继续扩大。
"""

from __future__ import annotations

from pathlib import Path

FRONTEND_SRC = Path(__file__).resolve().parent.parent.parent / "frontend" / "src"
MAX_LINES = 500

# 下一轮候选：值是允许的最大行数（当前 714 / 577，留一点余量）
RATCHET: dict[str, int] = {
    "views/WorkbenchView.vue": 750,
    "views/SettingsView.vue": 620,
}


def _source_files() -> list[tuple[str, int]]:
    out = []
    for path in sorted(FRONTEND_SRC.rglob("*")):
        if path.suffix not in (".vue", ".js"):
            continue
        rel = path.relative_to(FRONTEND_SRC).as_posix()
        out.append((rel, len(path.read_text(encoding="utf-8").splitlines())))
    return out


def test_no_oversized_source_file():
    offenders = []
    for rel, n in _source_files():
        limit = RATCHET.get(rel, MAX_LINES)
        if n > limit:
            offenders.append(f"{rel}: {n} 行 > 上限 {limit}")
    assert not offenders, "超大文件（应拆成子组件 / composable）：\n  " + "\n  ".join(offenders)


def test_ratchet_entries_still_exist():
    """棘轮名单里的文件若已被拆掉，就把它从名单删除，别留死条目。"""
    files = {rel for rel, _ in _source_files()}
    missing = sorted(set(RATCHET) - files)
    assert not missing, f"棘轮名单里有已不存在的文件（拆完了就删条目）：{missing}"


def test_manage_and_process_are_thin_shells():
    """本轮拆出来的两个主壳：只做编排，不得再堆业务逻辑。"""
    for rel, limit in (("views/ManageView.vue", 160), ("views/ProcessView.vue", 160)):
        n = len((FRONTEND_SRC / rel).read_text(encoding="utf-8").splitlines())
        assert n <= limit, f"{rel} 又长到 {n} 行（上限 {limit}）——状态与模板应放进子组件"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
