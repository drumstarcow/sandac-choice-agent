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


def _port_in_use(port):
    """能不能连上？能连上说明已经有进程在听（不管它是不是我）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _free_port(start=8770, tries=40):
    """找一个「连不上 + 能独占绑定」的端口。

    只靠 bind 试是不行的：Windows 的 SO_REUSEADDR 会把已占用端口也算成功，
    结果新服务以为绑定成功、实际收不到请求，浏览器打开就是 404。
    所以先探测连接，再用 SO_EXCLUSIVEADDRUSE 真绑一次确认。
    """
    for p in range(start, start + tries):
        if _port_in_use(p):
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return start


def _serving_my_app(port):
    """起完服务后自检：这个地址返回的到底是不是我自己的页面。"""
    import urllib.request
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"http://127.0.0.1:{port}/",
                                     headers={"Cache-Control": "no-store"})
        with opener.open(req, timeout=4) as r:
            head = r.read(4000).decode("utf-8", "ignore")
        return "吃饱饱" in head
    except Exception:
        return False


def start_server(prefer=8770, tries=6):
    """绑端口 → 起线程 → 自检页面。被旧进程抢端口时自动换下一个。"""
    port = prefer
    for _ in range(tries):
        port = _free_port(port)
        if port != prefer:
            print(f"  ⚠  端口 {prefer} 已被其它程序占用（多半是上次没关干净的窗口），改用 {port}")
            prefer = port          # 只提示一次
        try:
            srv = server.serve(port)
        except OSError:
            port += 1
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.35)
        if _serving_my_app(port):
            return srv, port
        srv.shutdown()
        srv.server_close()
        print(f"  ⚠  端口 {port} 上返回的不是本程序页面（可能残留在旧进程），换一个")
        port += 1
    raise RuntimeError("起不来：连续多个端口都被占用，请关掉残留的 Python 窗口再试")


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
    prefer = next((int(a) for a in args if a.isdigit()), None) \
        or int(os.getenv("CHIBAOBAO_PORT", "8770"))

    print(BANNER)
    print("  吃饱饱助手 · 校园吃饭决策智能体（真模型 / 真工具调用）")
    print("  " + "-" * 58)
    config.announce()                       # ← 必须打印：模型：deepseek-chat（云）

    srv, port = start_server(prefer)        # 绑独占端口 + 自检页面确实是自己
    url = f"http://127.0.0.1:{port}/"
    print(f"  地址：{url}")
    print("  关闭本窗口即停止服务（决定已落盘 outputs/，不会丢）")
    print("  " + "-" * 58, flush=True)

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
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:                 # 双击启动时别让人只看到一屏 traceback
        print()
        print("  启动失败：", exc)
        print("  常见原因：端口被残留进程占用 / 端口被安全软件拦截。")
        print("  处理办法：关掉其它残留的 Python 黑窗口，再双击一次。")
        print()
        try:
            input("  按回车键退出 …")
        except Exception:
            pass
        sys.exit(1)
