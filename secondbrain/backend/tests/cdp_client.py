"""极简 CDP 客户端（Chrome DevTools Protocol over WebSocket）。

用途：驱动真实浏览器点击页面控件，读回 DOM 状态与控制台错误。
**不依赖外部库**（只��� asyncio + websockets），方便在任何机器上跑。

为什么不用 headless 截图判断布局？——截图对 <select> 渲染有失真，
必须用 CDP 量 computed style 与 scrollHeight（项目里踩过两次"截图骗人"）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

import websockets

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_edge() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    raise FileNotFoundError("找不到本机 Edge（headless 渲染用）")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Browser:
    """一个 headless Edge 实例 + 一张页面。"""

    def __init__(self, width: int = 1440, height: int = 900) -> None:
        self.width = width
        self.height = height
        self.port = free_port()
        self.proc: subprocess.Popen | None = None
        self.profile = tempfile.mkdtemp(prefix="sb_cdp_")
        self.ws: websockets.WebSocketClientProtocol | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.dialogs: list[str] = []

    # ---------------- 生命周期 ----------------
    async def __aenter__(self) -> "Browser":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        edge = find_edge()
        self.proc = subprocess.Popen(
            [
                edge,
                "--headless=new",
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile}",
                f"--window-size={self.width},{self.height}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--hide-scrollbars",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-features=Translate,MediaRouter",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ws_url = await self._wait_ws()
        self.ws = await websockets.connect(ws_url, max_size=32 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read_loop())
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        await self.send("Log.enable")

    async def _wait_ws(self, timeout: float = 30.0) -> str:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/list", timeout=2
                ) as r:
                    targets = json.loads(r.read().decode())
                for t in targets:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.3)
        raise RuntimeError(f"Edge CDP 未就绪: {last}")

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)

    # ---------------- CDP 底层 ----------------
    async def _read_loop(self) -> None:
        assert self.ws is not None
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                else:
                    await self._on_event(msg)
        except Exception:  # noqa: BLE001
            pass

    async def _on_event(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "Runtime.consoleAPICalled":
            if params.get("type") in ("error", "warning"):
                text = " ".join(
                    str(a.get("value", a.get("description", "")))
                    for a in params.get("args", [])
                )
                self.console_errors.append(f"[{params.get('type')}] {text[:400]}")
        elif method == "Runtime.exceptionThrown":
            d = params.get("exceptionDetails", {})
            text = d.get("exception", {}).get("description") or d.get("text", "")
            self.page_errors.append(str(text)[:400])
        elif method == "Log.entryAdded":
            e = params.get("entry", {})
            if e.get("level") == "error":
                self.console_errors.append(f"[log] {str(e.get('text'))[:400]}")
        elif method == "Page.javascriptDialogOpening":
            # confirm()/prompt() 会阻塞页面，不处理的话后续 CDP 命令全部超时。
            # 一律**取消**——审计不该替用户点掉任何确认框。
            self.dialogs.append(params.get("message", ""))
            # 注意：不能在这里 await send()——响应是由本读循环派发的，
            # await 会自锁（读循环卡在事件处理里，收不到自己的响应）。
            # 另起任务处理。
            asyncio.create_task(self._dismiss_dialog())

    async def _dismiss_dialog(self) -> None:
        try:
            await self.send("Page.handleJavaScriptDialog", {"accept": False})
        except Exception:  # noqa: BLE001
            pass

    async def send(self, method: str, params: dict | None = None) -> dict:
        assert self.ws is not None
        self._id += 1
        mid = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        msg = await asyncio.wait_for(fut, timeout=60)
        if "error" in msg:
            raise RuntimeError(f"{method} 失败: {msg['error']}")
        return msg.get("result", {})

    # ---------------- 高层 API ----------------
    async def goto(self, url: str, settle: float = 1.2) -> None:
        await self.send("Page.navigate", {"url": url})
        await asyncio.sleep(settle)

    async def eval(self, expression: str, await_promise: bool = False) -> object:
        res = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if res.get("exceptionDetails"):
            d = res["exceptionDetails"]
            desc = d.get("exception", {}).get("description") or d.get("text", "")
            raise RuntimeError(f"JS 异常: {desc}")
        return res.get("result", {}).get("value")

    async def screenshot(self, path: str) -> str:
        """整页截图存到 path（PNG）。返回写入的路径。

        用途：**视觉验收**。布局类改动（面积、留白、对齐）光看 DOM 尺寸
        判断不了好不好看，必须真的看一眼。
        """
        import base64
        from pathlib import Path as _P

        res = await self.send(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": False},
        )
        data = res.get("data") or ""
        out = _P(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(data))
        return str(out)

    async def wait_for(self, expression: str, timeout: float = 12.0, interval: float = 0.25) -> bool:
        """轮询直到表达式为真。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if await self.eval(expression):
                    return True
            except RuntimeError:
                pass
            await asyncio.sleep(interval)
        return False

    async def click_text(self, selector: str, text: str) -> bool:
        """点第一个文本包含 text 的元素（用于 .tab / .btn / .check-chip）。"""
        js = (
            f"(() => {{const els=[...document.querySelectorAll({json.dumps(selector)})];"
            f"const el=els.find(e=>(e.innerText||e.textContent||'').trim().includes({json.dumps(text)}));"
            f"if(!el) return false; el.click(); return true;}})()"
        )
        return bool(await self.eval(js))

    async def click_all(self, selector: str, limit: int = 40) -> int:
        js = (
            f"(() => {{const els=[...document.querySelectorAll({json.dumps(selector)})].slice(0,{limit});"
            f"els.forEach(e=>e.click()); return els.length;}})()"
        )
        return int(await self.eval(js) or 0)

    async def measure(self) -> dict:
        """量内容区高度与视口（判断「每页内容高度 ≤ 视口」）。"""
        js = """
        (() => {
          const c = document.querySelector('.content');
          const first = c && c.firstElementChild;
          return {
            vh: window.innerHeight,
            vw: window.innerWidth,
            contentH: c ? c.scrollHeight : -1,
            contentW: c ? c.clientWidth : -1,
            firstW: first ? first.getBoundingClientRect().width : -1,
            bodyH: document.body.scrollHeight,
            docH: document.documentElement.scrollHeight
          };
        })()
        """
        return await self.eval(js)  # type: ignore[return-value]

    async def toasts(self) -> list[str]:
        js = "[...document.querySelectorAll('.toast')].map(t=>t.innerText.trim())"
        return list(await self.eval(js) or [])  # type: ignore[arg-type]
