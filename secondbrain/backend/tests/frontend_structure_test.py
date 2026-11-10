"""前端结构约束（v41）：别再长回 900 行的巨石组件。

v41 拆掉 `ManageView.vue`(952) / `ProcessView.vue`(851)，
v42 拆掉剩下两个 `WorkbenchView.vue`(714) / `SettingsView.vue`(577)。

现在 **src 下没有超过 500 行的文件**，棘轮已收紧到统一上限：
不再有例外名单，任何文件长过 500 行就失败。四个页面主壳另锁 ≤160 行，
防止"拆完又慢慢把逻辑搬回视图"。

历史做法（v41）曾对两个超限文件用例外名单锁住当前规模不许再长 ——
本轮拆完后名单清空，那条经验保留在注释里：不是放过，是不让技术债扩大。
"""

from __future__ import annotations

from pathlib import Path

FRONTEND_SRC = Path(__file__).resolve().parent.parent.parent / "frontend" / "src"
MAX_LINES = 500

# 例外名单：v42 拆完最后两个超限文件后已清空。
# 以后若确实需要临时放宽，在这里登记并写明**什么时候拆**。
RATCHET: dict[str, int] = {}


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


def test_page_shells_are_thin():
    """四个页面主壳：只做编排，不得再堆业务逻辑。"""
    for rel, limit in (
        ("views/ManageView.vue", 160),
        ("views/ProcessView.vue", 160),
        ("views/WorkbenchView.vue", 160),
        ("views/SettingsView.vue", 160),
    ):
        n = len((FRONTEND_SRC / rel).read_text(encoding="utf-8").splitlines())
        assert n <= limit, f"{rel} 又长到 {n} 行（上限 {limit}）——状态与模板应放进子组件"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
