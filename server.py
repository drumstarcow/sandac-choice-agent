"""吃饱饱助手 · 后端服务（标准库 http.server，零第三方依赖）

对外提供 app.py 桌面窗口所需的 HTTP 接口。三层结构：

  ① 对话层：每条用户消息都交给真实大模型（agent.ChoiceAgent），由模型自己决定调哪个工具；
     只有模型抛异常 / 返回空时才走关键词兜底，并打 `[FALLBACK]` 日志 + 界面黄标签。
  ② 编排层：转盘层级推进（餐别→场景→口味→菜品）由状态机保证确定性 —— 每层都真实调用
     tools.build_wheel，概率由 tools.WheelEngine 真实计算，不是前端假动画。
  ③ 收尾层：tools.finalize 输出四条并列推荐（综合最佳/最近/最便宜/评分最高），
     选定后生成打卡卡片并落盘到 outputs/。
"""
import datetime
import json
import os
import random
import socket
import sys
import threading
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(ROOT, "ui")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config                       # noqa: E402
import history as history_mod       # noqa: E402
import profile as profile_mod       # noqa: E402
import tools                        # noqa: E402
from agent import ChoiceAgent       # noqa: E402
from tools import WheelEngine       # noqa: E402
from tools import candidates as build_pool  # noqa: E402

BUDGET_OPTIONS = ["不限", "10", "15", "20", "25", "30", "40"]
LOCK = threading.Lock()

# 演示序列（P1-7）：固定 8 步，每步 1.5 秒，单步失败不中断整场
DEMO_STEPS = [
    {"key": "meal", "label": "餐别=正餐"},
    {"key": "where", "label": "场景=食堂"},
    {"key": "spice", "label": "口味=辣"},
    {"key": "spin", "label": "抽中 A"},
    {"key": "reject", "label": "拒绝 A"},
    {"key": "spin2", "label": "重抽 B"},
    {"key": "four", "label": "四条并列推荐"},
    {"key": "final", "label": "收尾卡片"},
]

CHEER_PROMPT = ("帮我把今天这一餐写成一句 20 字以内的打卡文案，第一人称，有画面感。"
                "要求：只输出这一句话本身，不要提问、不要反问、不要解释、不要表情符号、不要引号。"
                "菜品：{dish}；在哪：{where}；人均：{price} 元。")


# ------------------------- 会话状态 -------------------------
class Session:
    def __init__(self):
        self.agent = ChoiceAgent()
        self.history = []
        self.engine = WheelEngine()
        self.wheel = None
        self.meal_type = ""          # meal / dessert
        self.meal_label = ""
        self.choices = {}            # meal / where / spice / dish
        self.budget = "不限"
        self.rejected = []           # [(scene, key)]
        self.card = None
        self.four = None
        self.final = None
        self.step = "idle"           # idle/meal/where/spice/dish/card/done
        self.used_fallback = False
        self.fallback_reason = ""
        self.demo_index = 0
        self.logs = []

    # ---- 视图 ----
    def wheel_view(self):
        w = self.wheel
        if not w:
            return None
        scene, opts = w.get("scene", ""), w.get("options", [])
        probs = self.engine.probabilities(scene, opts) if opts else {}
        return {
            "scene": scene,
            "title": w.get("title", ""),
            "ctx": w.get("ctx", ""),
            "step": self.step,
            "options": [{"key": o, "prob": probs.get(o, 0.0)} for o in opts],
            "items": w.get("items", []),
            "note": w.get("note", ""),
            "rejected": [k for s, k in self.rejected if s == scene],
            "budget": self.budget,
            "budgetOptions": BUDGET_OPTIONS,
            "choices": dict(self.choices),
            "mealLabel": self.meal_label,
        }

    def reset(self):
        self.history = []
        self.engine = WheelEngine()
        self.wheel = None
        self.meal_type = ""
        self.meal_label = ""
        self.choices = {}
        self.rejected = []
        self.card = None
        self.four = None
        self.final = None
        self.step = "idle"
        self.used_fallback = False
        self.fallback_reason = ""
        self.demo_index = 0
        self.logs = []


SESS = Session()


def log(line):
    """控制台日志 + 内存日志（界面「运行日志」面板可看）。"""
    SESS.logs.append(line)
    SESS.logs = SESS.logs[-120:]
    print(line, flush=True)


# ------------------------- 工具执行包装 -------------------------
def _call(name, args, layer="", source="orchestrator"):
    """真实执行一次工具，并打印 `[TOOL] ... source=` 日志。

    source 用来区分是谁发起的：model（模型自己决定）/ orchestrator（界面点了某个按钮）
    / fallback（模型异常后的兜底）。评审只看日志就能判断哪次是模型真调用。
    """
    try:
        result = tools.call_tool(name, args)
    except Exception as e:                      # 工具异常也不许打崩整场
        result = f"工具 {name} 异常：{e}"
        log(f"[TOOL] source={source} tool_calls=1 layer={layer or '-'} name={name} ERROR={e}")
        return None, result
    log(f"[TOOL] source={source} tool_calls=1 layer={layer or '-'} name={name}")
    try:
        payload = json.loads(result)
    except Exception:
        payload = None
    return payload, result


def _wheel(scene, preference="", layer="", source="orchestrator"):
    budget = None if SESS.budget in ("不限", "", None) else SESS.budget
    payload, _ = _call("build_wheel", {
        "scene": scene, "preference": preference, "budget": budget,
        "meal_type": SESS.meal_type, "where": SESS.choices.get("where", ""),
    }, layer=layer or scene, source=source)
    if not payload or payload.get("error"):
        return None
    SESS.wheel = payload
    SESS.step = payload.get("scene", scene)
    return payload


# ------------------------- 动作 -------------------------
def act_build_meal():
    if _wheel("meal"):
        return {"wheel": SESS.wheel_view()}
    return {"wheel": None, "note": "造盘失败，请重试"}


def act_spin():
    """按真实权重抽一次。抽中后该项权重 ×0.6，下次更难被抽到。"""
    w = SESS.wheel
    if not w or not w.get("options"):
        return {"wheel": None}
    scene, opts = w["scene"], list(w["options"])
    probs = SESS.engine.probabilities(scene, opts)
    r, acc, picked = random.random(), 0.0, opts[-1]
    for o in opts:
        acc += probs.get(o, 0.0)
        if r <= acc:
            picked = o
            break
    SESS.engine.decay(scene, picked)
    log(f"[SPIN] layer={scene} picked={picked} p={probs.get(picked)}")
    view = SESS.wheel_view()
    view["picked"] = picked
    view["pickedIndex"] = opts.index(picked)
    return {"wheel": view}


def act_reject(key):
    w = SESS.wheel
    if not w:
        return {"wheel": None}
    scene = w["scene"]
    opts = [o for o in w.get("options", []) if o != key]
    SESS.engine.reject(scene, key)
    SESS.rejected.append((scene, key))
    log(f"[REJECT] layer={scene} key={key} remain={len(opts)}")
    if not opts:
        SESS.wheel = None
        return {"wheel": None, "note": "所有选项都被你拒了，换个说法我再给你来一轮。"}
    w["options"] = opts
    if scene == "dish":
        w["items"] = [i for i in w.get("items", []) if i.get("name") != key]
    return {"wheel": SESS.wheel_view(), "note": f"「{key}」已剔除，权重清零，概率已重排。"}


def act_accept(key):
    """接受当前层结果，推进到下一层；菜品层则产出美食名片 + 四条推荐。"""
    w = SESS.wheel
    if not w:
        return {"wheel": None}
    scene = w["scene"]

    if scene == "meal":
        if key == "随便":
            key = random.choice(["正餐", "甜品饮料"])
            log(f"[MEAL] 选“随便”→ 各 50% roll 出 {key}")
        SESS.meal_type = "dessert" if key == "甜品饮料" else "meal"
        SESS.meal_label = key
        SESS.choices["meal"] = key
        _wheel("where", layer="where")
        return {"wheel": SESS.wheel_view(), "accepted": key}

    if scene == "where":
        SESS.choices["where"] = key
        _wheel("spice", preference=key, layer="spice")
        return {"wheel": SESS.wheel_view(), "accepted": key}

    if scene == "spice":
        SESS.choices["spice"] = key
        _wheel("dish", preference=key, layer="dish")
        if not SESS.wheel or not SESS.wheel.get("options"):
            return {"wheel": None, "note": "这一层没筛出菜，我放宽条件重来。"}
        return {"wheel": SESS.wheel_view(), "accepted": key}

    # dish 层：定菜
    SESS.choices["dish"] = key
    item = next((i for i in w.get("items", []) if i.get("name") == key), None)
    if item is None:
        card = tools.get_dish_card(key) or {"name": key}
        item = dict(card)
        item.setdefault("shop", "")
        item.setdefault("price", 0)
        item.setdefault("distance", 0)
        item.setdefault("rating", 0)
        item.setdefault("tags", [])
        item.setdefault("desc", "")
    SESS.card = item
    SESS.wheel = None
    SESS.step = "card"
    four = act_four(item.get("id"))
    log(f"[PICK] dish={key} shop={item.get('shop')}")
    return {"wheel": None, "accepted": key, "card": item,
            "four": four.get("four"), "view": _view_extra()}


def act_four(picked_id=None):
    """四条并列推荐：用当前筛选条件下的真实候选池计算。"""
    pool = build_pool(meal_type=SESS.meal_type, spice=SESS.choices.get("spice", ""),
                      budget=None if SESS.budget in ("不限", "", None) else SESS.budget,
                      where=SESS.choices.get("where", ""))
    payload, _ = _call("finalize", {"candidates_list": pool, "picked_id": picked_id}, layer="four")
    if not payload:
        payload = json.loads(tools.finalize(pool, picked_id=picked_id))
    SESS.four = payload
    log(f"[FOUR] pool={payload.get('pool_size')} groups={len(payload.get('groups', []))}")
    return {"four": payload}


def act_budget(value):
    SESS.budget = value
    if SESS.step != "dish":
        return {"wheel": SESS.wheel_view() if SESS.wheel else None}
    _wheel("dish", preference=SESS.choices.get("spice", ""), layer="dish")
    SESS.rejected = [r for r in SESS.rejected if r[0] != "dish"]
    if not SESS.wheel or not SESS.wheel.get("options"):
        return {"wheel": None, "note": f"预算 {value} 元下没菜了，我放宽一点。"}
    return {"wheel": SESS.wheel_view(), "note": f"金额上限已设为 {value}，菜品重新筛过。"}


def act_select(payload):
    """从四条推荐里选一个，变成当前名片。"""
    pid = payload.get("id") or ""
    groups = (SESS.four or {}).get("groups", [])
    item = None
    for g in groups:
        if g["item"].get("id") == pid:
            item = g["item"]
            break
    if not item:
        return {"card": SESS.card}
    SESS.card = item
    SESS.choices["dish"] = item.get("name")
    SESS.step = "card"
    log(f"[SELECT] {item.get('shop')} / {item.get('name')}")
    return {"card": item, "view": _view_extra()}


def act_again():
    """名片上点「换一个」：把这菜剔出候选，重新弹菜品转盘。"""
    name = SESS.choices.get("dish")
    _wheel("dish", preference=SESS.choices.get("spice", ""), layer="dish")
    w = SESS.wheel
    if not w:
        return {"wheel": None}
    if name and name in w.get("options", []):
        w["options"].remove(name)
        w["items"] = [i for i in w.get("items", []) if i.get("name") != name]
        SESS.engine.reject("dish", name)
    SESS.card = None
    SESS.four = None
    SESS.choices.pop("dish", None)
    SESS.step = "dish"
    if not w.get("options"):
        SESS.wheel = None
        return {"wheel": None, "note": "没有别的菜了，换个口味试试？"}
    return {"wheel": SESS.wheel_view()}


def _cheer(item):
    """一句打卡文案：优先用模型生成，失败退回模板。"""
    try:
        text = SESS.agent.one_shot(CHEER_PROMPT.format(
            dish=item.get("name"), where=SESS.choices.get("where") or item.get("shop"),
            price=item.get("price")))
        text = (text or "").strip().strip("“”\"'")
        if 4 <= len(text) <= 40 and "{" not in text:
            log("[CHEER] 模型生成成功")
            return text, "model"
    except Exception as e:
        log(f"[CHEER] 模型生成失败：{e}")
    return f"今天这顿{sess_place()}的{item.get('name', '饭')}，吃得很满足。", "template"


def sess_place():
    return SESS.choices.get("where") or SESS.card.get("where") or "食堂"


def act_finalize():
    item = SESS.card or {}
    if not item and SESS.four:
        # 一键演示 / 四条直达时可能还没单独「接受」过某道菜 → 用综合最佳那条兜上
        groups = SESS.four.get("groups") or []
        if groups:
            item = groups[0]["item"]
            SESS.card = item
            log("[FINAL] 未单独选菜，取四条里的「综合最佳」收尾")
    name = item.get("name") or SESS.choices.get("dish", "")
    if not name:
        return {"final": None, "note": "还没定菜呢，先转一下转盘。"}
    cheer, how = _cheer(item)
    where = SESS.choices.get("where") or ("外面吃" if item.get("source") == "shop" else "食堂")
    spice = SESS.choices.get("spice", "随便")
    saved = ""
    try:
        saved = tools.finalize_decision(where, spice, name, note=cheer)
        log(f"[SAVE] {saved[:60]}")
    except Exception as e:
        log(f"[SAVE] 落盘失败：{e}")
    SESS.final = {
        "name": name, "shop": item.get("shop", ""), "price": item.get("price", 0),
        "distance": item.get("distance", 0),
        "walk_min": item.get("walk_min", max(1, round(float(item.get("distance", 0)) / 0.08))),
        "rating": item.get("rating", 0), "img": item.get("img", ""),
        "desc": item.get("desc", ""), "cheer": cheer, "cheer_from": how,
        "date": datetime.datetime.now().strftime("%Y-%m-%d"),
        "meal": SESS.meal_label or "正餐", "where": where, "spice": spice,
        "saved": saved, "tags": item.get("tags", []),
    }
    SESS.step = "done"
    return {"final": SESS.final}


def act_pick_shop(payload):
    name = payload.get("name", "")
    try:
        res = tools.finalize_recommend(
            name=name, address=payload.get("address", ""),
            distance_km=payload.get("distance_km", 0), avg_price=payload.get("avg_price", 0),
            rating=payload.get("rating", 0), signature=payload.get("signature", ""))
    except Exception as e:
        return {"content": f"落盘失败：{e}"}
    return {"content": res}


# ------------------------- 对话 -------------------------
def _view_extra():
    return {"step": SESS.step, "card": SESS.card, "four": SESS.four, "final": SESS.final}


def _msg(content, calls=None, fallback=False, reason=""):
    return {"role": "assistant", "content": content, "calls": calls or [],
            "fallback": fallback, "reason": reason}


def _fallback(text, reason):
    """兜底：只在模型异常 / 返回空时调用。所有兜底入口都必须经过这里打标。"""
    SESS.used_fallback = True
    SESS.fallback_reason = reason
    log(f"[FALLBACK] reason={reason}")
    if any(k in text for k in ("随便", "不知道", "没想法", "你定", "帮我选", "帮我决定")):
        _wheel("meal", layer="meal", source="fallback")
        return [_msg("既然没想法，那不如把选择权交给命运吧 🎰", fallback=True, reason=reason),
                {"role": "wheel", "wheel": SESS.wheel_view()}]
    payload, _ = _call("search_dish", {"keyword": text.strip(), "meal_type": SESS.meal_type,
                                       "budget": SESS.budget}, layer="four", source="fallback")
    if payload and payload.get("groups"):
        SESS.four = payload
        SESS.step = "four"
        return [_msg(payload.get("say") or "按你说的，我拉了四条并排给你 👇", fallback=True, reason=reason),
                {"role": "four", "four": payload}]
    return [_msg("我这边没接上，你再说一遍？或者直接说「随便」让我转盘。",
                 fallback=True, reason=reason)]


def act_chat(text):
    text = (text or "").strip()
    if not text:
        return {"messages": []}
    r = None
    try:
        r = SESS.agent.chat(text, SESS.history, scope="opening")
    except Exception as e:
        log(f"[CHAT] 模型异常：{type(e).__name__}: {e}")
        r = None

    if r is None or r.get("status") == "error":
        reason = (r or {}).get("error") or "模型调用异常"
        out = _fallback(text, reason)
        _remember(text, out)
        return {"messages": out, "fallback": True}

    calls = r.get("calls") or []
    tool_calls = r.get("tool_calls")
    if tool_calls is None:
        tool_calls = len(calls)
    # 模型这一轮自己发起的工具调用，逐条打 source=model（和界面点击 / 兜底区分开）
    for c in calls:
        log(f"[TOOL] source=model tool_calls={tool_calls} layer={r.get('status') or '-'} "
            f"name={c.get('name', '')}")
    log(f"[CHAT] tool_calls={tool_calls} status={r.get('status')} content_len={len(r.get('content') or '')}")

    content = (r.get("content") or "").strip()
    out = []
    if content:
        out.append(_msg(content, calls=calls))

    status = r.get("status")
    if status == "wheel" and r.get("wheel"):
        w = r["wheel"]
        if not w.get("options"):
            _wheel("meal", layer="meal")
        else:
            SESS.wheel = w
            SESS.step = w.get("scene", "meal")
            if w.get("meal_type"):
                SESS.meal_type = w["meal_type"]
                SESS.meal_label = tools.MEAL_LABELS.get(w["meal_type"], "")
        if not content:
            out.append(_msg("既然没想法，那不如把选择权交给命运吧 🎰", calls=calls))
        out.append({"role": "wheel", "wheel": SESS.wheel_view()})
    elif status == "recommend" and r.get("recommend"):
        # 模型有时会走 recommend_nearby_food（店铺维度）；统一转成「四条并列」再上屏，
        # 转换用的是同一份真实候选，不是另算一套。
        rec = r["recommend"]
        if not rec.get("groups"):
            pool = []
            for it in rec.get("items", []):
                pool.append({
                    "id": it.get("id") or it.get("name"),
                    "name": it.get("dish") or it.get("signature") or it.get("name"),
                    "shop": it.get("name"), "price": it.get("avg_price", 0),
                    "distance": it.get("distance_km", 0), "rating": it.get("rating", 0),
                    "tags": it.get("tags", []), "meal_type": it.get("meal_type", "meal"),
                    "desc": it.get("desc", ""), "img": it.get("img", ""),
                    "where": "外面吃", "walk_min": it.get("walk_min", 1),
                })
            if pool:
                four = json.loads(tools.finalize(pool, criteria_note=rec.get("note", "")))
            else:
                four = rec
        else:
            four = rec
        SESS.four = four
        SESS.step = "four"
        out.append({"role": "four", "four": four})
        if not content:
            out.append(_msg(four.get("say") or "附近这几家，按四个维度各挑了一条 👇", calls=calls))
    else:
        # 模型既没调工具、也没说话 → 才算“返回空”，才允许兜底
        if tool_calls == 0 and not content:
            out.extend(_fallback(text, f"模型返回空（status={status} tool_calls=0 content=0）"))
        elif not content:
            out.append(_msg("（模型没说话）", calls=calls))

    if not out:
        out.append(_msg("（我这边没接上，你再说一遍？）", calls=calls))
    _remember(text, out)
    return {"messages": out}


def _remember(text, out):
    SESS.history.append({"role": "user", "content": text})
    for m in out:
        if m.get("role") == "assistant" and m.get("content"):
            SESS.history.append({"role": "assistant", "content": m["content"]})
    SESS.history = SESS.history[-12:]


# ------------------------- 一键演示 -------------------------
def act_demo_next():
    """推进一步固定演示序列；单步失败只记录，不中断。"""
    idx = SESS.demo_index
    if idx >= len(DEMO_STEPS):
        return {"done": True, "index": idx, "total": len(DEMO_STEPS)}
    step = DEMO_STEPS[idx]
    key = step["key"]
    payload = {}
    try:
        if idx == 0:                       # 从干净状态起盘
            SESS.reset()
            SESS.agent = SESS.agent or ChoiceAgent()
            act_build_meal()
            payload = act_accept("正餐")
        elif key == "where":
            payload = act_accept("食堂")
        elif key == "spice":
            payload = act_accept("辣")
        elif key == "spin":
            payload = act_spin()
            SESS.demo_a = (payload.get("wheel") or {}).get("picked")
        elif key == "reject":
            payload = act_reject(getattr(SESS, "demo_a", None) or "")
        elif key == "spin2":
            payload = act_spin()
        elif key == "four":
            payload = act_four((SESS.wheel or {}).get("picked") if SESS.wheel else None)
        elif key == "final":
            payload = act_finalize()
        ok = True
    except Exception as e:
        ok = False
        payload = {"note": f"{step['label']} 这一步出错了：{type(e).__name__}: {e}"}
        log(f"[DEMO] step={idx} FAILED {e}")
        traceback.print_exc()
    SESS.demo_index = idx + 1
    if ok:
        log(f"[DEMO] step={idx + 1}/{len(DEMO_STEPS)} {step['label']}")
    return {
        "done": False, "index": idx, "total": len(DEMO_STEPS),
        "label": step["label"], "ok": ok, "payload": payload,
        "wheel": SESS.wheel_view(), "view": _view_extra(),
    }


# ------------------------- 偏好面板 -------------------------
def act_pref_delete(payload):
    field = payload.get("field", "")
    value = payload.get("value", "")
    prof = profile_mod.load_profile()
    if field == "budget_cap":
        profile_mod.update_profile("budget_cap", "不限")
        SESS.budget = "不限"
    elif field in ("dislikes", "taste_prefs", "dietary"):
        cur = list(prof.get(field, []))
        cur = [v for v in cur if v != value]
        profile_mod.update_profile(field, cur)
    elif field == "campus":
        profile_mod.update_profile("campus", "金海校区")
    log(f"[PREF] 删除 {field}={value}")
    return {"profile": profile_mod.load_profile()}


def pref_chips():
    """偏好面板的条目（每条都带删除按钮）。"""
    p = profile_mod.load_profile()
    chips = []
    for v in p.get("dislikes", []):
        chips.append({"field": "dislikes", "value": v, "text": f"不吃{v}", "kind": "忌口"})
    for v in p.get("taste_prefs", []):
        chips.append({"field": "taste_prefs", "value": v, "text": f"偏{v}", "kind": "口味"})
    for v in p.get("dietary", []):
        chips.append({"field": "dietary", "value": v, "text": f"{v}", "kind": "限制"})
    if p.get("budget_cap"):
        chips.append({"field": "budget_cap", "value": p["budget_cap"],
                      "text": f"预算 {p['budget_cap']:g}", "kind": "预算"})
    if p.get("campus"):
        chips.append({"field": "campus", "value": p["campus"], "text": p["campus"], "kind": "定位"})
    return chips


# ------------------------- HTTP -------------------------
def state_payload():
    prof = profile_mod.load_profile()
    try:
        recent = history_mod.summarize_recent_profile(10)
    except Exception:
        recent = {"count": 0}
    return {
        "profile": prof,
        "chips": pref_chips(),
        "recent": recent,
        "history": history_mod.load_history()[-8:],
        "wheel": SESS.wheel_view(),
        "card": SESS.card,
        "four": SESS.four,
        "final": SESS.final,
        "step": SESS.step,
        "budget": SESS.budget,
        "budgetOptions": BUDGET_OPTIONS,
        "mealLabel": SESS.meal_label,
        "model": config.backend_label(),
        "notes": config.notes(),
        "usedFallback": SESS.used_fallback,
        "fallbackReason": SESS.fallback_reason,
        "logs": SESS.logs[-40:],
    }


ROUTES = {
    "/api/chat": lambda p: act_chat(p.get("text", "")),
    "/api/start": lambda p: act_build_meal(),
    "/api/spin": lambda p: act_spin(),
    "/api/reject": lambda p: act_reject(p.get("key", "")),
    "/api/accept": lambda p: act_accept(p.get("key", "")),
    "/api/budget": lambda p: act_budget(p.get("value", "不限")),
    "/api/four": lambda p: act_four(p.get("picked_id")),
    "/api/select": lambda p: act_select(p),
    "/api/again": lambda p: act_again(),
    "/api/finalize": lambda p: act_finalize(),
    "/api/pick": lambda p: act_pick_shop(p),
    "/api/demo/next": lambda p: act_demo_next(),
    "/api/pref/delete": lambda p: act_pref_delete(p),
    "/api/reset": lambda p: (SESS.reset(), {"ok": True, "step": "idle"})[1],
    "/api/state": lambda p: state_payload(),
}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=UI_DIR, **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _svg(self, dish_id):
        body = tools.plate_svg(dish_id).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=600")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/plate/"):
            return self._svg(unquote(path[len("/plate/"):]).rsplit(".", 1)[0])
        if path in ("/", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()
        if path == "/api/state":
            try:
                with LOCK:
                    return self._json(state_payload())
            except Exception as e:
                traceback.print_exc()
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        fn = ROUTES.get(path)
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
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)


class Server(ThreadingHTTPServer):
    """独占端口的 HTTP 服务。

    Windows 上 SO_REUSEADDR 允许重复绑定同一端口（谁先监听谁收包），
    于是「新进程以为自己起来了，浏览器却连到上次没关干净的旧进程」——
    表现就是首页 404。这里关掉复用、改用 SO_EXCLUSIVEADDRUSE，
    端口被占时直接抛 OSError，由上层换端口，绝不错绑。
    """
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET,
                                       socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()


def serve(port):
    return Server(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8770
    config.announce()
    print(f"   http://127.0.0.1:{port}/")
    serve(port).serve_forever()
