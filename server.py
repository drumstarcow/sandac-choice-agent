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
from urllib.parse import parse_qs, unquote, urlparse

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
    {"key": "cuisine", "label": "菜系=按分布选一类"},
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
        self.choices = {}            # meal / where / spice / cuisine / dish
        # 哪几层是用户主动选「随便」的。前三层全随便 → 菜系层解锁隐藏款「重口猎奇」
        self.rnd = {"meal": False, "where": False, "spice": False}
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
        counts = w.get("counts") or {}
        return {
            "scene": scene,
            "title": w.get("title", ""),
            "ctx": w.get("ctx", ""),
            "step": self.step,
            "options": [{"key": o, "prob": probs.get(o, 0.0), "count": counts.get(o)} for o in opts],
            "items": w.get("items", []),
            "skip": w.get("skip"),
            "note": w.get("note", ""),
            "rejected": [k for s, k in self.rejected if s == scene],
            "budget": self.budget,
            "budgetOptions": BUDGET_OPTIONS,
            "choices": dict(self.choices),
            "mealLabel": self.meal_label,
        }

    def all_random(self):
        """前三层是不是都交给了命运。隐藏款的解锁条件。"""
        return all(self.rnd.get(k) for k in ("meal", "where", "spice"))

    def reset(self):
        self.history = []
        self.engine = WheelEngine()
        self.wheel = None
        self.meal_type = ""
        self.meal_label = ""
        self.choices = {}
        self.rnd = {"meal": False, "where": False, "spice": False}
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
        "cuisine": SESS.choices.get("cuisine", ""),
        "unlock_low": SESS.all_random(),
    }, layer=layer or scene, source=source)
    if not payload or payload.get("error"):
        return None
    SESS.wheel = payload
    SESS.step = payload.get("scene", scene)
    return payload


# ------------------------- 动作 -------------------------
def _go_cuisine(preference):
    """进菜系层；若只剩一类（或者根本没有分组），别再让用户白点一下，直接进菜品。"""
    _wheel("cuisine", preference=preference, layer="cuisine")
    opts = (SESS.wheel or {}).get("options") or []
    if not opts:
        _wheel("dish", preference=preference, layer="dish")
        return {"wheel": SESS.wheel_view()}
    if len(opts) == 1:
        log(f"[SKIP] 菜系层只剩「{opts[0]}」，自动进入菜品层")
        return act_accept(opts[0])
    counts = (SESS.wheel or {}).get("counts") or {}
    real = [o for o in opts if o != tools.CUISINE_LOW]
    total = sum(v for k, v in counts.items() if k != tools.CUISINE_LOW)
    if total <= 6:
        # 池子就这么几个东西，再分菜系纯属折腾，直接摆菜品
        log(f"[SKIP] 菜系分组太小（{total} 个候选分 {len(real)} 类），直接进入菜品层")
        _wheel("dish", preference=preference, layer="dish")
        return {"wheel": SESS.wheel_view()}
    return {"wheel": SESS.wheel_view()}


def act_build_meal():
    # 从第一层重新起盘 = 开新的一轮：上一轮残留的场景/口味/菜系选择必须清掉，
    # 否则面包屑会串台（点「开始转盘」却显示上次的场景和菜系）。
    SESS.choices, SESS.rnd = {}, {"meal": False, "where": False, "spice": False}
    SESS.meal_type = SESS.meal_label = ""
    SESS.card = SESS.four = SESS.final = None
    SESS.rejected = []
    SESS.engine = WheelEngine()      # 新的一轮：上一轮的衰减/清零记录一并作废
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
    note = f"「{key}」已剔除，权重清零，概率已重排。"
    w["options"] = opts
    if scene == "dish":
        w["items"] = [i for i in w.get("items", []) if i.get("name") != key]
    if scene != "dish" and len(opts) == 1:
        note += f" 只剩「{opts[0]}」了，这层没得选，直接定就行。"
    return {"wheel": SESS.wheel_view(), "note": note}


def act_accept(key):
    """接受当前层结果，推进到下一层；菜品层则产出美食名片 + 四条推荐。"""
    w = SESS.wheel
    if not w:
        return {"wheel": None}
    scene = w["scene"]

    if scene == "meal":
        if key == "随便":
            SESS.rnd["meal"] = True
            key = random.choice(["正餐", "甜品饮料"])
            log(f"[MEAL] 选“随便”→ 各 50% roll 出 {key}")
        SESS.meal_type = "dessert" if key == "甜品饮料" else "meal"
        SESS.meal_label = key
        SESS.choices["meal"] = key
        _wheel("where", layer="where")
        return {"wheel": SESS.wheel_view(), "accepted": key}

    if scene == "where":
        SESS.choices["where"] = key
        if key == "随便":
            SESS.rnd["where"] = True
        SESS.choices.pop("cuisine", None)      # 换了场景，菜系要重新算
        if SESS.meal_type == "dessert":
            # 奶茶咖啡没有辣不辣，口味这层直接跳过
            SESS.choices["spice"] = "随便"
            SESS.rnd["spice"] = True       # 没有这一层，等于也交给了命运
            log("[SKIP] 甜品饮料不需要口味层，直接进菜系")
            return _go_cuisine("随便")
        # 盘外的「哪都行」= 不限场景；口味层的「上一层」提示就别写成「随便」了
        _wheel("spice", preference="" if key == "随便" else key, layer="spice")
        return {"wheel": SESS.wheel_view(), "accepted": key}

    if scene == "spice":
        SESS.choices["spice"] = key
        if key == "随便":
            SESS.rnd["spice"] = True
        return _go_cuisine(key)

    if scene == "cuisine":
        if key not in (w.get("options") or []):
            log(f"[GUARD] 菜系层没有「{key}」这个选项，重开菜系层")
            return _go_cuisine(SESS.choices.get("spice", ""))
        SESS.choices["cuisine"] = key
        _wheel("dish", preference=SESS.choices.get("spice", ""), layer="dish")
        if not SESS.wheel or not SESS.wheel.get("options"):
            return {"wheel": None, "note": "这一类没筛出菜，我放宽条件重来。"}
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
    """四条并列推荐：用当前筛选条件下的真实候选池计算。

    已经定下某道菜时，池子收窄成「这道菜的所有店」—— 用户真正要决定的是去哪家买，
    四条就变成「综合最佳 / 最近 / 最便宜 / 评分最高」各指一家。只有一家在卖时仍按整桌菜比。
    """
    pool = build_pool(meal_type=SESS.meal_type, spice=SESS.choices.get("spice", ""),
                      budget=None if SESS.budget in ("不限", "", None) else SESS.budget,
                      where=SESS.choices.get("where", ""),
                      cuisine=SESS.choices.get("cuisine", ""))
    name = SESS.choices.get("dish")
    if name:
        # 判定用 tools.dish_key：格子上合并的是「麻辣烫」，数据里可能叫「麻辣烫（自选）」，
        # 这里要是还按字符串全等收窄，就会出现「格子说 3 家都有、点进去只比 2 家」。
        k = tools.dish_key(name)
        same = [c for c in pool if tools.dish_key(c.get("name")) == k]
        shops = {tools.shop_key(c.get("shop")) for c in same}
        if len(shops) >= 2:
            pool = same
            log(f"[FOUR] 「{name}」{len(shops)} 家都有，改比店铺")
    payload, _ = _call("finalize", {"candidates_list": pool, "picked_id": picked_id}, layer="four")
    if not payload:
        payload = json.loads(tools.finalize(pool, picked_id=picked_id))
    SESS.four = payload
    log(f"[FOUR] pool={payload.get('pool_size')} groups={len(payload.get('groups', []))}")
    return {"four": payload}


LAYERS = ["meal", "where", "spice", "cuisine", "dish"]
LAYER_NAMES = {"meal": "餐别", "where": "场景", "spice": "口味", "cuisine": "菜系", "dish": "菜品"}


def act_jump(scene="", key=None):
    """直接打开某一层转盘，不必从第一层一层层转下来。

    用户心里已经有数（「我就想去外面吃」「我就是想吃火锅」）时，前面的层级纯属浪费点击。
    做法：清掉这一层及其之后的选择，缺失的上层上下文用最中性的默认值补齐，
    然后照常真实调用 build_wheel —— 盘上的候选、概率、四条推荐全都还是同一套数据。
    """
    scene = tools._norm_scene(scene)
    if scene not in LAYERS:
        return {"wheel": SESS.wheel_view(), "note": "这层没认出来，还是看当前这张盘。"}

    # 回到这一层：本层及之后的选择全部作废，名片/四条一起收起来
    for k in LAYERS[LAYERS.index(scene):]:
        SESS.choices.pop(k, None)
        if k in SESS.rnd:
            SESS.rnd[k] = False
    SESS.card = None
    SESS.four = None
    SESS.final = None

    # 上层没走过的，用最中性的默认值补上（正餐 / 不限场景 / 不限口味）
    if scene != "meal" and not SESS.meal_type:
        SESS.meal_type, SESS.meal_label = "meal", "正餐"
        SESS.choices["meal"] = "正餐"
    if scene in ("spice", "cuisine", "dish"):
        SESS.choices.setdefault("where", "随便")
        SESS.choices.setdefault("spice", "随便")

    if scene == "meal":
        _wheel("meal", layer="meal")
    elif scene == "where":
        _wheel("where", layer="where")
    elif scene == "spice":
        if SESS.meal_type == "dessert":
            # 奶茶咖啡没有辣不辣，这一层本来就不存在
            _go_cuisine("随便")
        else:
            _wheel("spice", preference=SESS.choices.get("where", ""), layer="spice")
    elif scene == "cuisine":
        _go_cuisine(SESS.choices.get("spice", "随便"))
    else:
        _wheel("dish", preference=SESS.choices.get("spice", "随便"), layer="dish")

    if not SESS.wheel or not SESS.wheel.get("options"):
        return {"wheel": None, "note": f"「{LAYER_NAMES[scene]}」这层暂时没有可选项。"}

    # 直接点了某个具体选项（例如面包屑/口令里点名火锅）→ 当作已经选定，继续往下走
    if key and key in (SESS.wheel.get("options") or []):
        out = act_accept(key)
        out["note"] = f"直接定了「{key}」，往前走了。"
        return out

    real = LAYER_NAMES.get(SESS.wheel.get("scene"), LAYER_NAMES[scene])
    log(f"[JUMP] 直接打开「{real}」层")
    return {"wheel": SESS.wheel_view(), "note": f"已跳过前面几层，直接打开「{real}」转盘。"}


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
    # 场景选了「随便」的话，卡片上的「在哪吃」不能真写个「随便」，用这道菜实际的归属兜底
    where = SESS.choices.get("where") or ""
    if where in ("", "随便"):
        where = item.get("where") or ("外面吃" if item.get("source") == "shop" else "食堂")
    spice = SESS.choices.get("spice", "随便")
    saved = ""
    try:
        saved = tools.finalize_decision(where, spice, name, note=cheer)
        log(f"[SAVE] {saved[:60]}")
    except Exception as e:
        log(f"[SAVE] 落盘失败：{e}")
    SESS.final = {
        "id": item.get("id", ""),
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
            scene = w.get("scene", "meal")
            # 模型可能被用户一句「直接看菜系」直接叫到某一层，上层的上下文就缺了；
            # 补齐成「正餐 / 不限场景 / 不限口味」，面包屑和后续筛选用的是同一份状态。
            if scene != "meal" and not SESS.meal_type:
                SESS.meal_type, SESS.meal_label = "meal", "正餐"
                SESS.choices.setdefault("meal", "正餐")
            if scene in ("spice", "cuisine", "dish"):
                SESS.choices.setdefault("where", "随便")
            if scene in ("cuisine", "dish"):
                SESS.choices.setdefault("spice", "随便")
            SESS.wheel = w
            SESS.step = scene
            if w.get("meal_type"):
                SESS.meal_type = w["meal_type"]
                SESS.meal_label = tools.MEAL_LABELS.get(w["meal_type"], "")
        if not content:
            # 模型跳了层（不是从餐别起盘）就别再念「交给命运」那句，它说的是反话
            jumped = SESS.wheel and SESS.wheel.get("scene") not in ("meal", None)
            if jumped:
                out.append(_msg(f"行，跳过前面几层，直接看「{LAYER_NAMES.get(SESS.wheel['scene'], '这一层')}」。", calls=calls))
            else:
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
        elif key == "cuisine":
            opts = (SESS.wheel or {}).get("options") or []
            payload = act_accept(opts[0]) if opts else {}
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
        "choices": dict(SESS.choices),
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
    "/api/jump": lambda p: act_jump(p.get("scene", ""), p.get("key")),
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

    def _svg(self, dish_id, name=""):
        body = tools.plate_svg(dish_id, name=name).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=600")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/plate/"):
            # ?n= 是归一后的菜名：一道菜多家店时，图上写的名字要和名片标题一致
            nm = (parse_qs(urlparse(self.path).query).get("n") or [""])[0]
            return self._svg(unquote(path[len("/plate/"):]).rsplit(".", 1)[0], nm)
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
