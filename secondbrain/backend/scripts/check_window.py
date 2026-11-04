"""内嵌窗口自检：验证 pywebview 在本机能否创建窗口。"""

import http.server
import socketserver
import threading
import time

HTML = (
    "<html><body style='font:20px system-ui;padding:40px'>"
    "SecondBrain 内嵌窗口自检 OK"
    "</body></html>"
).encode()


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(HTML)))
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, *a):
        pass


srv = socketserver.TCPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
print("测试服务端口:", port)

import webview  # noqa: E402

print("pywebview 版本:", getattr(webview, "__version__", "?"))
try:
    win = webview.create_window(
        "SecondBrain 自检", f"http://127.0.0.1:{port}", width=700, height=420
    )

    def close_later():
        time.sleep(6)
        try:
            win.destroy()
        except Exception:
            pass

    threading.Thread(target=close_later, daemon=True).start()
    webview.start()
    print("✓ 内嵌窗口成功创建并关闭")
except Exception as e:  # noqa: BLE001
    print("✗ 内嵌窗口失败:", type(e).__name__, str(e)[:300])
finally:
    srv.shutdown()
