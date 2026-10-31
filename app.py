"""吃饱饱助手 —— 唯一演示入口（桌面窗口启动器）

    python app.py            # 起服务 + 开桌面窗口
    python app.py --no-win   # 只起服务（调试用，浏览器手动打开）
    python app.py 8780       # 指定端口

窗口优先级：pywebview（装了就用）→ Edge/Chrome 的 --app 无边框窗口 → 默认浏览器。
三者打开的都是同一份 ui/index.html，内核都是 server.py 里的真实智能体。
"""
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config        # noqa: E402
import server        # noqa: E402

BANNER = r"""
  ____  _   _  ___  ____    ____    ____    ____
 / ___|| | | |/ _ \| __ )  / __ \  / __ \  / ___|
 \___ \| |_| | | |  _ \ | |  | || |  | || |
  ___) |  _  | |_| | |_) || |__| || |__| || |___
 |____/|_| |_|\___/|____/  \____/  \____/  \____|
"""


def _free_port(start=8770, tries=40):
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def _edge_path():
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(p):
            return p
    return None


def open_window(url):
    """依次尝试 pywebview → Edge app 窗口 → 默认浏览器。返回用了哪种。"""
    try:
        import webview
        webview.create_window("吃饱饱助手 · 校园吃饭决策智能体", url,
                              width=1300, height=880, min_size=(960, 680))
        webview.start()
        return "pywebview"
    except Exception:
        pass

    edge = _edge_path()
    if edge:
        try:
            subprocess.Popen([edge, f"--app={url}", "--window-size=1300,880",
                              "--no-first-run", "--disable-features=Translate"])
            return "edge-app"
        except Exception:
            pass

    webbrowser.open(url)
    return "browser"


def main():
    args = list(sys.argv[1:])
    no_win = "--no-win" in args
    port = next((int(a) for a in args if a.isdigit()), None) \
        or _free_port(int(os.getenv("CHIBAOBAO_PORT", "8770")))

    print(BANNER)
    print("  吃饱饱助手 · 校园吃饭决策智能体（真模型 / 真工具调用）")
    print("  " + "-" * 58)
    config.announce()                       # ← 必须打印：模型：deepseek-chat（云）
    srv = server.serve(port)
    url = f"http://127.0.0.1:{port}/"
    print(f"  地址：{url}")
    print("  关闭本窗口即停止服务（决定已落盘 outputs/，不会丢）")
    print("  " + "-" * 58, flush=True)

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)

    if no_win or os.getenv("CHIBAOBAO_NO_WINDOW"):
        print("  --no-win：请手动用浏览器打开上面的地址", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        srv.shutdown()
        return

    shell = open_window(url)
    print(f"  窗口：{shell}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main()
