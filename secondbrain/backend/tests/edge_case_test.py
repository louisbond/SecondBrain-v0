"""边界场景自测（打运行中的服务）。

覆盖常规 happy path 之外、容易出问题的地方：
1. 用户在 Obsidian 里手改 md → 重建索引后能搜到（设计 §4.1）
2. 标签合并 → 受影响 md 的 frontmatter 被改写（§7）
3. 归档 → 引用同步改写，链接不失效（§10）
4. 文件名安全：路径穿越 / 非法字符 / 超长名（§4.1）
5. 并发提交 → 去重不漏、不重复
6. 截图上传 → 落到 assets/ 并返回 md 引用（§5.2）
7. 状态机不变量：非法迁移被拒、重复提交不产生重复条目
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE = os.environ.get("SB_BASE", "http://127.0.0.1:7688")

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def call(method: str, path: str, body: dict | None = None, raw: bytes | None = None,
         content_type: str | None = None):
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    elif body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return resp.status, _maybe_json(text)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return exc.code, _maybe_json(text)
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def enc(path: str) -> str:
    """路径段 URL 编码（中文路径必须编码）。"""
    from urllib.parse import quote

    return quote(str(path), safe="/")


def _maybe_json(text: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append((name, detail))
        print(f"  ✗ {name}  {detail}")


def section(t: str) -> None:
    print(f"\n── {t} ──")


def main() -> int:
    print(f"边界场景自测 @ {BASE}\n" + "=" * 60)

    # ---- 1. 手改 md → 重建索引 ----------------------------------------
    section("1. 用户手改 md → 重建索引后可见")
    _, settings = call("GET", "/api/settings")
    vault_path = Path(settings["vault"]["path"]) if isinstance(settings, dict) else None
    check("vault 路径可读", vault_path is not None, str(vault_path))

    if vault_path:
        # 模拟用户用 Obsidian 手动加了一篇带独特词的老笔记
        marker = "手写笔记标记词玄猫"
        hand_written = vault_path / "手写测试笔记.md"
        hand_written.write_text(
            "---\n"
            "title: 手写测试笔记\n"
            "type: note\n"
            "form: fragment\n"
            "tags: [手写标签]\n"
            "---\n\n"
            f"这是一篇用户手写的笔记，包含独特标记词：{marker}。\n",
            encoding="utf-8",
        )
        check("手写文件已创建", hand_written.exists(), str(hand_written))

        # 不重建索引时搜不到（索引是缓存）
        _, before = call("POST", "/api/search", {"query": "玄猫"})
        check(
            "重建前搜不到（索引未更新）",
            isinstance(before, dict) and before.get("total", 0) == 0,
            f"total={before.get('total') if isinstance(before, dict) else before}",
        )

        _, reidx = call("POST", "/api/settings/reindex")
        check("重建索引成功", isinstance(reidx, dict) and reidx.get("ok"), str(reidx)[:100])

        _, after = call("POST", "/api/search", {"query": "玄猫"})
        check(
            "重建后能搜到手写笔记",
            isinstance(after, dict) and after.get("total", 0) >= 1,
            f"total={after.get('total') if isinstance(after, dict) else after}",
        )
        if isinstance(after, dict) and after.get("results"):
            titles = [r["title"] for r in after["results"]]
            check("命中的是手写笔记", any("手写" in t for t in titles), str(titles[:3]))

        # 清理
        hand_written.unlink(missing_ok=True)
        call("POST", "/api/settings/reindex")

    # ---- 2. 标签合并改写 frontmatter ----------------------------------
    section("2. 标签合并 → 改写受影响 md")
    _, note = call(
        "POST",
        "/api/notes",
        {
            "title": "标签合并自测笔记",
            "body": "正文内容，用于验证标签合并会改写 frontmatter。",
            "tags": ["待合并旧标签", "保留标签"],
        },
    )
    note_path = note.get("note_path") if isinstance(note, dict) else None
    check("笔记已创建", bool(note_path), str(note)[:100])

    if note_path:
        call("POST", "/api/tags", {"name": "待合并旧标签"})
        call("POST", "/api/tags", {"name": "保留标签"})

        _, merged = call("POST", "/api/tags/merge", {"src": "待合并旧标签", "dst": "保留标签"})
        check("合并接口返回 ok", isinstance(merged, dict) and merged.get("ok"), str(merged)[:120])
        if isinstance(merged, dict):
            check("合并影响数 ≥ 1", merged.get("affected", 0) >= 1, str(merged.get("affected")))

        content = Path(note_path).read_text(encoding="utf-8")
        check("旧标签已从 frontmatter 移除", "待合并旧标签" not in content, content[:200])
        check("目标标签已写入", "保留标签" in content, content[:200])

        # 索引同步更新
        _, found = call("POST", "/api/search", {"query": "标签合并自测笔记"})
        check("合并后仍可搜到", isinstance(found, dict) and found.get("total", 0) >= 1,
              f"total={found.get('total') if isinstance(found, dict) else found}")

    # ---- 3. 归档改写引用 ----------------------------------------------
    section("3. 归档 → 引用同步改写")
    _, target = call(
        "POST",
        "/api/notes",
        {"title": "被归档的目标笔记", "body": "目标正文。", "tags": ["归档测试"]},
    )
    target_path = target.get("note_path") if isinstance(target, dict) else None
    target_stem = Path(target_path).stem if target_path else ""
    check("目标笔记已创建", bool(target_path), str(target)[:100])

    if target_path:
        _, src = call(
            "POST",
            "/api/notes",
            {
                "title": "引用了目标的笔记",
                "body": f"这里引用了 [[{target_stem}]] 作为依据。",
                "tags": ["归档测试"],
            },
        )
        src_path = src.get("note_path") if isinstance(src, dict) else None
        check("引用方笔记已创建", bool(src_path), str(src)[:100])

        if src_path:
            # 确认链接边已建立
            _, detail = call("GET", f"/api/notes/{enc(src_path)}")
            outlinks = detail.get("outlinks", []) if isinstance(detail, dict) else []
            check("链接边已建立", len(outlinks) >= 1, str(outlinks)[:120])

            _, arch = call("POST", f"/api/notes/{enc(target_path)}/archive")
            check("归档成功", isinstance(arch, dict) and arch.get("ok"), str(arch)[:120])
            new_path = arch.get("note_path") if isinstance(arch, dict) else None
            check("已移入 99_Archive", bool(new_path) and "99_Archive" in new_path, str(new_path))

            # 引用方内容里的 [[链接]] 应被改写
            src_content = Path(src_path).read_text(encoding="utf-8")
            check(
                "引用方 frontmatter 的引用已改写",
                target_stem not in src_content or new_path.split("/")[-1][:-3] in src_content,
                src_content[:250],
            )

            # 归档后默认搜不到（可开关纳入）
            # 注意：查询词会走 bigram 展开，"笔记"这类常见二元组会命中很多文档，
            # 所以不能用 total 判定，要看归档文件本身是否还在结果里。
            _, hits = call("POST", "/api/search", {"query": "被归档的目标笔记"})
            paths = [h["note_path"] for h in hits.get("results", [])] if isinstance(hits, dict) else []
            check(
                "归档笔记默认不出现在搜索结果",
                new_path not in paths,
                f"结果 {len(paths)} 条，含归档文件={new_path in paths}",
            )

            _, hits2 = call(
                "POST",
                "/api/search",
                {"query": "被归档的目标笔记", "include_archived": True},
            )
            paths2 = [h["note_path"] for h in hits2.get("results", [])] if isinstance(hits2, dict) else []
            check(
                "开关打开后归档笔记可见",
                new_path in paths2,
                f"结果 {len(paths2)} 条，含归档文件={new_path in paths2}",
            )

    # ---- 4. 文件名安全 ------------------------------------------------
    section("4. 文件名安全（路径穿越 / 非法字符）")
    dangerous = [
        "../../../etc/passwd",
        "a/b\\c",
        "con:aux",
        "x" * 300,
        "<script>alert(1)</script>",
    ]
    ok_all = True
    for name in dangerous:
        _, res = call(
            "POST",
            "/api/notes",
            {"title": name, "body": "文件名安全测试。", "tags": []},
        )
        if not isinstance(res, dict) or not res.get("note_path"):
            ok_all = False
            continue
        p = Path(res["note_path"])
        # 必须真实落在 vault 内（这才是安全的判据）
        try:
            p.resolve().relative_to(vault_path.resolve())
        except ValueError:
            ok_all = False
            print(f"      路径逃逸：{name} -> {p}")
            continue
        # 文件名里不应残留点号串
        if ".." in p.name:
            ok_all = False
            print(f"      文件名含点号串：{name} -> {p.name}")
    check("危险标题均被安全落盘", ok_all)

    # 清理这些测试笔记
    if vault_path:
        for sub in ("03_Notes", "99_Archive"):
            d = vault_path / sub
            if d.exists():
                for f in d.glob("*.md"):
                    try:
                        if "安全测试" in f.read_text(encoding="utf-8"):
                            f.unlink()
                    except OSError:
                        pass
        call("POST", "/api/settings/reindex")

    # ---- 5. 并发提交去重 -----------------------------------------------
    section("5. 并发提交 → 去重不漏不重")
    url = f"https://www.bilibili.com/video/BV1Race{int(time.time()) % 1000000:06d}"
    import threading

    results: list[Any] = []

    def _submit():
        results.append(call("POST", "/api/collect/submit",
                            {"entries": [{"url": url, "platform": "bilibili"}]}))

    threads = [threading.Thread(target=_submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    accepted = sum(len(r[1].get("accepted", [])) for r in results if isinstance(r[1], dict))
    dupes = sum(len(r[1].get("duplicates", [])) for r in results if isinstance(r[1], dict))
    check("并发下只入队 1 条", accepted == 1, f"accepted={accepted}")
    check("其余 7 次被判重复", dupes == 7, f"duplicates={dupes}")

    _, lst = call("GET", "/api/items?limit=300")
    if isinstance(lst, dict):
        urls = [i.get("source_url") for i in lst.get("items", []) if i.get("source_url")]
        check("库内 source_url 无重复", len(urls) == len(set(urls)),
              f"{len(urls)} 条中 {len(urls) - len(set(urls))} 个重复")

    # ---- 6. 截图上传 ---------------------------------------------------
    section("6. 截图上传")
    # 最小合法 PNG
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6360000002000100ffff0300000600"
        "0557bfabd40000000049454e44ae426082"
    )
    boundary = "----sbtest"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="shot.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode() + png + f"\r\n--{boundary}--\r\n".encode()

    status, res = call("POST", "/api/upload", raw=body,
                       content_type=f"multipart/form-data; boundary={boundary}")
    check("上传返回 200", status == 200, f"HTTP {status} {str(res)[:100]}")
    if isinstance(res, dict):
        check("返回 md 引用格式", str(res.get("embed", "")).startswith("![["), str(res.get("embed")))
        check("落到 assets/", "assets" in str(res.get("relative", "")), str(res.get("relative")))
        if vault_path and res.get("path"):
            check("文件确实存在", Path(res["path"]).exists(), str(res["path"]))

    # 非法类型应被拒
    body2 = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="evil.exe"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + b"MZ..." + f"\r\n--{boundary}--\r\n".encode()
    status2, res2 = call("POST", "/api/upload", raw=body2,
                         content_type=f"multipart/form-data; boundary={boundary}")
    check("非法类型被拒（400）", status2 == 400, f"HTTP {status2}")

    # ---- 7. 状态机不变量 ----------------------------------------------
    section("7. 状态机与幂等")
    _, items = call("GET", "/api/items?limit=300")
    if isinstance(items, dict):
        lst = items.get("items", [])
        legal = {
            "queued", "extracting", "extracted", "extract_failed",
            "ai_processing", "ai_failed", "pending_review", "reviewed",
        }
        bad = [i["status"] for i in lst if i.get("status") not in legal]
        check("状态全部合法", not bad, str(set(bad)))
        reviewed = [i for i in lst if i.get("status") == "reviewed"]
        # 已入库 → 必须能在 B 层（processed）搜到
        missing = []
        for i in reviewed:
            title = (i.get("title") or "").strip()
            if not title:
                continue
            _, hit = call(
                "POST",
                "/api/search",
                {"query": title, "levels": ["processed"], "include_archived": True},
            )
            if not (isinstance(hit, dict) and hit.get("total", 0) >= 1):
                missing.append(title)
        check(
            "已入库条目都能在 B 层搜到",
            not missing,
            f"{len(reviewed)} 条 reviewed，缺 {len(missing)}：{missing[:2]}",
        )

    # 幂等：重复 reindex 不改变笔记数
    _, r1 = call("POST", "/api/settings/reindex")
    _, r2 = call("POST", "/api/settings/reindex")
    if isinstance(r1, dict) and isinstance(r2, dict):
        check("重建幂等", r1.get("indexed") == r2.get("indexed"),
              f"{r1.get('indexed')} vs {r2.get('indexed')}")

    # ---- 汇总 ---------------------------------------------------------
    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("\n失败明细：")
        for name, detail in FAILED:
            print(f"  · {name} — {detail}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    # 隔离实例：绝不碰用户的真实 vault
    from harness import IsolatedServer

    with IsolatedServer() as _srv:
        os.environ["SB_BASE"] = _srv.base
        globals()["BASE"] = _srv.base
        print(f"[隔离实例] {_srv.base}  vault={_srv.vault}")
        sys.exit(main())
