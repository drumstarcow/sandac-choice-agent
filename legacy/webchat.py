"""吃饱饱助手 · 网页版智能体对话（零第三方依赖）

内核仍然是「智能体」，不是菜单页：
- 每条消息都交给本地大模型，由模型自己决定调用哪个工具（真实 tool_calls，日志在工具卡片里可见）
- 转盘概率由 tools.WheelEngine 在后端真实计算：抽中衰减 0.6、拒绝清零、归一化
- 转盘层级推进（在哪吃 → 口味 → 菜品）由后端状态机保证确定性，前端只负责好看地展示

因为只用标准库，起服务不需要 pip install 任何东西：
    python webchat.py            # 启动并自动打开浏览器
    python webchat.py 8780       # 指定端口
"""
import ast
import json
import os
import re
import sys
import random
import threading
import webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tools                      # noqa: E402
from tools import WheelEngine     # noqa: E402
from agent import ChoiceAgent     # noqa: E402
import history as history_mod     # noqa: E402
import profile as profile_mod     # noqa: E402

BUDGET_OPTIONS = ["不限", "10", "15", "20", "25", "30", "40"]
PORT = int(os.getenv("CHIBAOBAO_PORT", "8770"))

# 同一时间只处理一个请求（本地单用户演示，避免模型并发互相拖慢）
LOCK = threading.Lock()


# ------------------------- 兜底意图判断（小模型不稳时由编排层接管） -------------------------

def _looks_like_no_idea(text):
    t = text.strip()
    return any(k in t for k in ["没想法", "不知道", "随便", "帮我选", "你定", "没想好",
                                "不知道吃啥", "帮我决定", "你帮我", "交给命运", "转一下", "转盘"])


def _looks_like_have_idea(text):
    t = text.strip()
    if _looks_like_no_idea(t):
        return False
    return any(k in t for k in ["想吃", "想来", "来份", "来个", "来点", "就要", "点个", "点一份",
                                "整点", "搞点", "我想", "来碗", "来杯", "喝杯", "来只", "吃个", "吃份"])


def _extract_keyword(text):
    t = text.strip()
    for p in ["我想吃", "想吃", "我想来", "想来", "来份", "来个", "来点", "就要", "点个", "点一份",
              "整点", "搞点", "来碗", "来杯", "喝杯", "来只", "吃个", "吃份", "我想"]:
        if t.startswith(p):
            t = t[len(p):]
            break
    t = re.sub(r"^(一份|一个|份|个|些|吃|来|点|喝)", "", t)
    return t.strip(" 吧呀呢～。.，,！!？?的")


# ------------------------- 会话状态机 -------------------------

class Session:
    """一次网页会话的全部状态（单用户演示，进程内保存）。"""

    def __init__(self):
        self.agent = ChoiceAgent()
        self.history = []        # 给 LLM 的上下文
        self.engine = WheelEngine()
        self.wheel = None        # 当前转盘 {"scene","title","options","ctx"}
        self.choices = {}        # where / spice / dish
        self.budget = "不限"
        self.rejected = set()    # (scene, key)
        self.card = None
        self.finalized = False

    # ---- 对外视图 ----
    def wheel_view(self):
        w = self.wheel
        if not w:
            return None
        scene, opts = w["scene"], w.get("options", [])
        probs = self.engine.probabilities(scene, opts) if opts else {}
        return {
            "scene": scene,
            "title": w.get("title", ""),
            "ctx": w.get("ctx", ""),
            "options": [{"key": o, "prob": probs.get(o, 0.0)} for o in opts],
            "budget": self.budget,
            "budgetOptions": BUDGET_OPTIONS,
            "choices": dict(self.choices),
        }

    def reset(self):
        self.history = []
        self.engine = WheelEngine()
        self.wheel = None
        self.choices = {}
        self.rejected = set()
        self.card = None
        self.finalized = False


SESS = Session()


# ------------------------- 业务动作 -------------------------

def act_build_where():
    SESS.wheel = json.loads(tools.build_wheel("where"))
    return SESS.wheel_view()


def act_spin():
    """按真实权重抽一次。抽中后该项权重 ×0.6，下次更难被抽到。"""
    w = SESS.wheel
    if not w or not w.get("options"):
        return None
    scene = w["scene"]
    opts = list(w["options"])
    probs = SESS.engine.probabilities(scene, opts)
    r = random.random()
    acc = 0.0
    picked = opts[-1]
    for o in opts:
        acc += probs.get(o, 0.0)
        if r <= acc:
            picked = o
            break
    SESS.engine.decay(scene, picked)          # 抽中衰减
    view = SESS.wheel_view()
    view["picked"] = picked
    view["pickedIndex"] = opts.index(picked)
    return view


def act_reject(key):
    w = SESS.wheel
    if not w:
        return {"wheel": None}
    scene = w["scene"]
    opts = [o for o in w.get("options", []) if o != key]
    SESS.engine.reject(scene, key)            # 权重清零，本会话不再复活
    SESS.rejected.add((scene, key))
    if not opts:
        SESS.wheel = None
        return {"wheel": None, "note": "所有选项都被你拒了，换个说法我再给你来一轮。"}
    w["options"] = opts
    return {"wheel": SESS.wheel_view(), "note": f"「{key}」已剔除，权重清零，概率已重排。"}


def act_accept(key):
    """接受当前层的结果，推进到下一层；菜品层则产出美食名片。"""
    w = SESS.wheel
    if not w:
        return {"wheel": None}
    scene = w["scene"]
    if scene == "where":
        SESS.choices["where"] = key
        SESS.wheel = json.loads(tools.build_wheel("spice", key))
        return {"wheel": SESS.wheel_view(), "accepted": key}
    if scene == "spice":
        SESS.choices["spice"] = key
        budget = None if SESS.budget in ("不限", "", None) else SESS.budget
        SESS.wheel = json.loads(tools.build_wheel("dish", key, budget))
        return {"wheel": SESS.wheel_view(), "accepted": key}
    # dish
    SESS.choices["dish"] = key
    SESS.wheel = None
    SESS.card = tools.get_dish_card(key) or {"name": key}
    return {"wheel": None, "accepted": key, "card": SESS.card}


def act_budget(value):
    SESS.budget = value
    w = SESS.wheel
    if not w or w["scene"] != "dish":
        return {"wheel": SESS.wheel_view() if w else None}
    budget = None if value in ("不限", "", None) else value
    spice = SESS.choices.get("spice", "")
    SESS.wheel = json.loads(tools.build_wheel("dish", spice, budget))
    SESS.rejected = {r for r in SESS.rejected if r[0] != "dish"}
    if not SESS.wheel.get("options"):
        return {"wheel": None, "note": f"这个预算（{value} 元）下没找到菜，我把范围放宽一点。"}
    return {"wheel": SESS.wheel_view(), "note": f"金额上限已设为 {value}，菜品重新筛过。"}


def act_finalize():
    d = SESS.card or {}
    name = SESS.choices.get("dish") or d.get("name", "")
    where = SESS.choices.get("where", "")
    spice = SESS.choices.get("spice", "")
    res = tools.finalize_decision(where, spice, name)
    prof = profile_mod.load_profile()
    history_mod.log_decision({
        "type": "wheel", "where": where, "spice": spice, "dish": name,
        "campus": prof.get("campus"),
        "budget": SESS.budget if SESS.budget not in ("不限", "", None) else None,
        "price": d.get("price"), "tags": d.get("tags", []),
    })
    SESS.finalized = True
    return {"content": res, "card": d}


def act_pick_shop(payload):
    name = payload.get("name", "")
    res = tools.finalize_recommend(
        name=name, address=payload.get("address", ""),
        distance_km=payload.get("distance_km", 0), avg_price=payload.get("avg_price", 0),
        rating=payload.get("rating", 0), signature=payload.get("signature", ""),
    )
    prof = profile_mod.load_profile()
    history_mod.log_decision({
        "type": "recommend", "shop_name": name, "campus": prof.get("campus"),
        "price": payload.get("avg_price"), "rating": payload.get("rating"),
        "distance": payload.get("distance_km"), "tags": payload.get("tags", []),
    })
    SESS.finalized = True
    return {"content": res}


def parse_steps(steps):
    """把 agent.py 的步骤文本解析成结构化工具调用，前端渲染成卡片。

    原始格式：'🔧 调用 build_wheel({\'scene\': \'where\'})' 后跟 '↳ {json...}'
    """
    calls, notes = [], []
    cur = None
    for s in steps or []:
        if not isinstance(s, str):
            continue
        t = s.strip()
        if t.startswith("🔧 调用 "):
            body = t[len("🔧 调用 "):]
            name, args = body, {}
            i = body.find("(")
            if i > 0 and body.endswith(")"):
                name = body[:i]
                try:
                    args = ast.literal_eval(body[i + 1:-1])
                except Exception:
                    args = {"_raw": body[i + 1:-1]}
            cur = {"name": name, "args": args, "result": ""}
            calls.append(cur)
        elif t.startswith("↳") and cur is not None:
            cur["result"] = t[1:].strip()[:800]
            cur = None
        elif t.startswith("（模型这次没主动接上"):
            notes.append(t)
        elif t.startswith("✅ 用户已确认"):
            calls.append({"name": "confirmed", "args": {}, "result": t})
    return {"calls": calls, "notes": notes}


def assistant_msg(content, steps):
    return {"role": "assistant", "content": content,
            "tools": parse_steps(steps), "steps": steps}


def act_again():
    """名片上点「不要这个」：把这菜剔出候选，重新弹菜品转盘。"""
    spice = SESS.choices.get("spice", "")
    budget = None if SESS.budget in ("不限", "", None) else SESS.budget
    name = SESS.choices.get("dish")
    w = json.loads(tools.build_wheel("dish", spice, budget))
    if name and name in w.get("options", []):
        w["options"].remove(name)
        SESS.engine.reject("dish", name)
    SESS.card = None
    SESS.choices.pop("dish", None)
    if not w.get("options"):
        SESS.wheel = None
        return {"wheel": None}
    SESS.wheel = w
    return {"wheel": SESS.wheel_view()}


def act_chat(text):
    """一轮真实对话：模型自己决定调哪个工具。模型没接上时由编排层兜底。"""
    s = SESS
    r = s.agent.chat(text, s.history, scope="opening")
    out = []

    content = (r.get("content") or "").strip()
    steps = r.get("steps", [])
    if content:
        out.append(assistant_msg(content, steps))

    status = r.get("status")

    if status == "wheel" and r.get("wheel"):
        s.wheel = r["wheel"]
        # 模型给的 scene 可能不规范，或该层没筛出候选 → 兜底回调第一层
        if not s.wheel.get("options"):
            s.wheel = json.loads(tools.build_wheel("where"))
            steps = list(steps) + ["（这一层没筛出候选，已回退到第一层：今天在哪吃）"]
        if not content:
            out.append(assistant_msg("既然没想法，那不如把选择权交给命运吧！🎰", steps))
        out.append({"role": "wheel", "wheel": s.wheel_view()})

    elif status == "recommend" and r.get("recommend"):
        if not content:
            out.append(assistant_msg("好嘞～按你的想法，附近性价比最高的几套方案 👇", steps))
        out.append({"role": "recommend", "recommend": r["recommend"],
                    "budgetOptions": BUDGET_OPTIONS})

    else:
        # 模型没发工具调用 → 编排层兜底（工具仍然真实执行，界面会标明）
        no_tool = not any(isinstance(x, str) and "🔧" in x for x in steps)
        if no_tool and not s.wheel and not s.finalized:
            if _looks_like_no_idea(text):
                s.wheel = json.loads(tools.build_wheel("where"))
                if not content:
                    out.append(assistant_msg(
                        "既然没想法，那不如把选择权交给命运吧！🎰",
                        ["🔧 调用 build_wheel({'scene': 'where'})",
                         "（模型这次没主动接上，由编排层发起；工具调用与结果仍是真实的）"]))
                out.append({"role": "wheel", "wheel": s.wheel_view()})
            elif _looks_like_have_idea(text):
                kw = _extract_keyword(text)
                rec = json.loads(tools.recommend_nearby_food(keyword=kw, criteria="value"))
                if rec.get("count", 0) > 0:
                    if not content:
                        out.append(assistant_msg(
                            "好嘞～按你的想法，附近性价比最高的几套方案 👇",
                            [f"🔧 调用 recommend_nearby_food(keyword={kw!r})",
                             "（模型这次没主动接上，由编排层发起；工具调用与结果仍是真实的）"]))
                    out.append({"role": "recommend", "recommend": rec,
                                "budgetOptions": BUDGET_OPTIONS})

    s.history.append({"role": "user", "content": text})
    if content:
        s.history.append({"role": "assistant", "content": content})
    s.history = s.history[-12:]

    if not out:
        out.append(assistant_msg(content or "（我这边没接上，你再说一遍？）", steps))
    return out


# ------------------------- HTTP 服务 -------------------------

def state_payload():
    prof = profile_mod.load_profile()
    try:
        recent = history_mod.summarize_recent_profile(10)
    except Exception:
        recent = {"count": 0}
    return {
        "profile": prof,
        "recent": recent,
        "history": history_mod.load_history()[-8:],
        "wheel": SESS.wheel_view(),
        "card": SESS.card,
        "finalized": SESS.finalized,
        "budget": SESS.budget,
        "budgetOptions": BUDGET_OPTIONS,
        "model": getattr(SESS.agent, "model", ""),
    }


ROUTES = {
    "/api/chat":      lambda p: {"messages": act_chat((p.get("text") or "").strip())},
    "/api/spin":      lambda p: {"wheel": act_spin()},
    "/api/reject":    lambda p: act_reject(p.get("key", "")),
    "/api/accept":    lambda p: act_accept(p.get("key", "")),
    "/api/budget":    lambda p: act_budget(p.get("value", "不限")),
    "/api/finalize":  lambda p: act_finalize(),
    "/api/again":     lambda p: act_again(),
    "/api/pick":      lambda p: act_pick_shop(p),
    "/api/reset":     lambda p: (SESS.reset(), {"ok": True})[1],
    "/api/start-wheel": lambda p: {"wheel": act_build_where()},
}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    def log_message(self, fmt, *args):
        pass  # 安静一点

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()
        if p == "/api/state":
            try:
                with LOCK:
                    return self._json(state_payload())
            except Exception as e:   # 绝不让异常把连接掐断（前端会只看到 Failed to fetch）
                import traceback
                traceback.print_exc()
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        return super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        fn = ROUTES.get(p)
        if not fn:
            return self._json({"error": "unknown api"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        try:
            with LOCK:
                return self._json(fn(payload))
        except Exception as e:  # 不让一个异常打死服务
            import traceback
            traceback.print_exc()
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def main():
    port = PORT
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 58)
    print("  吃饱饱助手 · 网页版智能体对话")
    print(f"  地址：{url}")
    print("  关闭这个窗口即可停止服务")
    print("=" * 58)
    try:
        if not os.getenv("CHIBAOBAO_NO_BROWSER"):
            webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
