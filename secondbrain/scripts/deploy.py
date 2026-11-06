#!/usr/bin/env python
"""把打包产物部署到安装目录，**先清掉旧版本残留**。

为什么需要它：`robocopy /E` 这种覆盖式部署只会「加上新文件、覆盖同名文件」，
不会删除新版本里已经不存在的旧文件。PyInstaller 的产物里有一堆版本绑定的
二进制扩展（`.pyd`），残留就会被 Python 优先导入：

    OSError/崩溃算好的，更常见的是**静默卡死**——
    例如旧版 httptools 的 HTTP parser 被 uvicorn 认出来启用，
    但和新的 Python 层 ABI 不匹配，表现为 TCP 能连上、请求永远不返回，
    桌面窗口加载 URL 一直等 → 用户看到的就是「打开是黑的」。

所以正确姿势是：把程序文件整体挪走再放新的，用户数据（config/ data/）原地不动。

用法：
    python scripts/deploy.py --src dist-release/SecondBrain --dst D:/Apps/SecondBrain
    python scripts/deploy.py --src ... --dst ... --dry-run

安全约束：
- 目标目录必须已有 SecondBrain.exe（防止手滑指向别处）；
- 只移动程序文件，config/ data/ 及任何 *.bak / _old* / _stale* 一律不碰；
- 旧文件移到 <dst>/_old-<时间戳>/ 而不是删除，确认没问题后可手动清。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

# 部署时必须保留的用户数据目录（相对安装根）
KEEP_DIRS = {"config", "data", "vault"}
# 这些前缀/后缀的条目一律不碰
KEEP_PREFIXES = ("_old", "_stale")
KEEP_SUFFIXES = (".bak",)


def is_kept(name: str) -> bool:
    if name in KEEP_DIRS:
        return True
    if name.startswith(KEEP_PREFIXES):
        return True
    return name.endswith(KEEP_SUFFIXES)


def check_src(src: Path) -> list[str]:
    problems: list[str] = []
    if not (src / "SecondBrain.exe").exists():
        problems.append(f"{src} 里没有 SecondBrain.exe")
    if not (src / "_internal").is_dir():
        problems.append(f"{src} 里没有 _internal/")
    pyd = list((src / "_internal").rglob("*.pyd")) if (src / "_internal").is_dir() else []
    if not pyd:
        problems.append("产物里没有 .pyd，看起来不是 PyInstaller onedir 输出")
    return problems


def check_dst(dst: Path) -> list[str]:
    if not dst.is_dir():
        return [f"目标目录不存在：{dst}"]
    if not (dst / "SecondBrain.exe").exists():
        return [
            f"{dst} 里没有 SecondBrain.exe —— 为了安全不往陌生目录写，"
            "请先确认路径，或手动把旧版本放进去"
        ]
    return []


def plan(dst: Path) -> tuple[list[Path], list[Path]]:
    """返回 (要挪走的旧程序文件, 用户数据)。"""
    move: list[Path] = []
    keep: list[Path] = []
    for entry in sorted(dst.iterdir()):
        if is_kept(entry.name):
            keep.append(entry)
        else:
            move.append(entry)
    return move, keep


def main() -> int:
    ap = argparse.ArgumentParser(description="部署 SecondBrain 打包产物（先清残留）")
    ap.add_argument("--src", required=True, help="打包产物目录（含 SecondBrain.exe）")
    ap.add_argument("--dst", required=True, help="安装目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不动文件")
    args = ap.parse_args()

    src = Path(args.src).expanduser().resolve()
    dst = Path(args.dst).expanduser().resolve()

    problems = check_src(src) + check_dst(dst)
    if problems:
        for p in problems:
            print(f"[x] {p}", file=sys.stderr)
        return 2

    move, keep = plan(dst)
    print(f"源：{src}")
    print(f"目标：{dst}")
    print(f"要挪走的旧程序条目：{len(move)}")
    for entry in move:
        print(f"    - {entry.name}")
    print(f"原地保留（用户数据）：{[e.name for e in keep] or '（无）'}")

    src_pyd = len(list((src / "_internal").rglob("*.pyd")))
    dst_pyd = len(list((dst / "_internal").rglob("*.pyd"))) if (dst / "_internal").is_dir() else 0
    print(f".pyd 数量：目标 {dst_pyd} → 源 {src_pyd}")
    if dst_pyd > src_pyd:
        print(
            f"    ! 目标多出 {dst_pyd - src_pyd} 个二进制扩展，"
            "这正是会导致「黑屏/卡死」的残留"
        )

    if args.dry_run:
        print("\n--dry-run，未改动任何文件")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = dst / f"_old-{stamp}"
    backup.mkdir(parents=True, exist_ok=False)
    print(f"\n1/3 移走旧程序文件 → {backup.name}/")
    for entry in move:
        shutil.move(str(entry), str(backup / entry.name))

    print("2/3 复制新版本 …")
    shutil.copytree(src, dst, dirs_exist_ok=True)

    print("3/3 校验 …")
    new_pyd = len(list((dst / "_internal").rglob("*.pyd"))) if (dst / "_internal").is_dir() else 0
    ok = True
    if not (dst / "SecondBrain.exe").exists():
        print("[x] 部署后没有 SecondBrain.exe", file=sys.stderr)
        ok = False
    if new_pyd != src_pyd:
        print(f"[x] .pyd 数量不一致：{new_pyd} != {src_pyd}", file=sys.stderr)
        ok = False
    if not (dst / "_internal" / "frontend" / "dist" / "index.html").exists():
        # 前端缺失时 API 能用但窗口是白的，值得单独报
        print("[!] 没找到 _internal/frontend/dist/index.html，窗口可能空白", file=sys.stderr)

    if ok:
        print(f"完成。旧文件在 {backup}，确认新版本正常后可删除。")
        return 0
    print("部署有问题，旧文件仍在备份目录，可整体移回。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
