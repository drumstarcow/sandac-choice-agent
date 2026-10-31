"""吃饱饱助手 —— 业务工具层（真实数据 + 真实概率 + 四条并列推荐）

模块职责：
- candidate 池：把「食堂菜品」与「周边店铺招牌」统一成同一种候选结构
  {id name shop price distance rating tags meal_type desc img}
- WheelEngine：真实权重概率（抽中衰减 0.6 / 拒绝清零 / 归一化），**算法保持不变**
- build_wheel：按层级造盘 —— meal(餐别) → where(场景) → spice(口味) → dish(菜品)
- search_dish：点名直达（用户说了具体菜名，跳过转盘）
- finalize：收尾四条并列推荐（综合最佳 / 最近 / 最便宜 / 评分最高，跨条去重）
- plate_svg：本地生成 4:3 菜品图（离线可用）
- 产出物与危险操作：finalize_decision / finalize_recommend / share_with_roommate
"""
import datetime
import json
import math
import re
from pathlib import Path

import history as history_mod
import profile as profile_mod

DATA_PATH = Path(__file__).parent / "data" / "food_data.json"
CAMPUS_PATH = Path(__file__).parent / "data" / "campus.json"
SHOP_PATH = Path(__file__).parent / "data" / "shops.json"
OUTPUT_DIR = Path(__file__).parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

MEAL_LABELS = {"meal": "正餐", "dessert": "甜品饮料"}
WALK_KM_PER_MIN = 0.08          # 步行速度 ≈ 80 m/min，用于“走 N 分钟”的理由文案


# ------------------------- 数据读取 -------------------------
def _load():
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_shops():
    with open(SHOP_PATH, encoding="utf-8") as f:
        return json.load(f)


def _campus_center(campus=None):
    try:
        cp = json.load(open(CAMPUS_PATH, encoding="utf-8"))
        name = campus or profile_mod.load_profile().get("campus") or cp.get("default", "金海校区")
        c = cp["campuses"].get(name) or cp["campuses"][cp["default"]]
        return c
    except Exception:
        return {"lat": 31.2708, "lng": 121.6695, "address": "上海市浦东新区金海路2727号"}


def _haversine(lat1, lng1, lat2, lng2):
    """两点间直线距离（km），真实球面计算。"""
    R = 6371.080
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ------------------------- 转盘权重概率引擎（算法未改） -------------------------
class WheelEngine:
    """真实权重概率模型（对应《转盘设计建议》第二节）。

    - 抽中后衰减：w *= 0.6，避免连着抽同一个
    - 被拒绝：本次会话内 w = 0，永不复活（直到新会话/重置）
    - 归一化：每次抽取前 p_i = w_i / Σw
    - 空态：全部归零时由调用方走兜底（不空转）
    权重存于内存（每会话一份），驱动大转盘“真的会变”的概率，
    转盘每个扇区都会显示当前概率%，拒绝后重排屏幕上可见。
    """

    DECAY = 0.6  # 抽中后衰减系数

    def __init__(self):
        self.weights = {}  # layer -> {option: weight}

    def _ensure(self, layer, options):
        w = self.weights.setdefault(layer, {})
        opts = list(options)
        # 删除已不在候选集里的（被拒绝剔除的）项
        for k in list(w):
            if k not in opts:
                del w[k]
        # 新出现的项默认权重 1.0；保留存活项的已有权重（含衰减历史）
        for o in opts:
            w.setdefault(o, 1.0)
        return w

    def probabilities(self, layer, options):
        """返回 {option: 概率}（已归一化）。"""
        w = self._ensure(layer, options)
        total = sum(w.values())
        if total <= 0:
            return {o: 0.0 for o in options}
        return {o: round(w[o] / total, 4) for o in options}

    def decay(self, layer, item):
        """抽中后衰减（避免连抽同一个）。"""
        w = self.weights.get(layer)
        if w and item in w:
            w[item] *= self.DECAY

    def reject(self, layer, item):
        """拒绝：本会话内该选项权重清零，永不复活。"""
        w = self.weights.get(layer)
        if w and item in w:
            w[item] = 0.0

    def reset_layer(self, layer):
        self.weights.pop(layer, None)

    def reset(self):
        self.weights = {}


# ------------------------- 候选池 -------------------------
def _norm_meal_type(v):
    """把各种写法归一化到 meal / dessert。"""
    s = str(v or "").strip().lower()
    if s in ("meal", "正餐", "饭", "吃饭", "主食"):
        return "meal"
    if s in ("dessert", "甜品", "甜品饮料", "饮料", "喝的", "drink"):
        return "dessert"
    return ""


def _norm_scene(scene):
    """把模型可能传歪的 scene 归一化到 meal / where / spice / dish。"""
    s = str(scene or "").strip().lower()
    if s in ("meal", "where", "spice", "dish"):
        return s
    if any(k in s for k in ("餐别", "正餐", "甜品", "吃什么类型", "meal")):
        return "meal"
    if any(k in s for k in ("口味", "辣", "spice", "味道")):
        return "spice"
    if any(k in s for k in ("菜", "dish", "吃什么", "吃啥", "具体")):
        return "dish"
    if any(k in s for k in ("哪", "地点", "食堂", "外卖", "外面", "附近", "场景", "where", "餐厅")):
        return "where"
    return "where"


def candidates(meal_type="", spice="", budget=None, where="", keyword=""):
    """统一候选池（食堂菜品 + 周边店铺招牌）。

    过滤顺序：餐别 → 场景 → 口味 → 预算 → 关键词。
    口味“随便”/空 表示该层不参与过滤。
    """
    meal_type = _norm_meal_type(meal_type)
    data = _load()
    center = _campus_center()
    out = []

    for d in data.get("dishes", []):
        out.append({
            "id": d.get("id"), "name": d.get("name"), "shop": d.get("shop", "食堂"),
            "price": d.get("price", 0), "distance": d.get("distance", 0.2),
            "rating": d.get("rating", 4.0), "tags": d.get("tags", []),
            "meal_type": d.get("meal_type", "meal"), "desc": d.get("desc", ""),
            "img": d.get("img", ""), "where": "食堂", "source": "canteen",
        })

    for s in _load_shops().get("shops", []):
        dist = _haversine(center["lat"], center["lng"], s["lat"], s["lng"])
        out.append({
            "id": s.get("id"), "name": s.get("dish") or s.get("signature") or s.get("name"),
            "shop": s.get("name"), "price": s.get("avg_price", 0),
            "distance": round(dist, 2), "rating": s.get("rating", 4.0),
            "tags": s.get("tags", []), "meal_type": s.get("meal_type", "meal"),
            "desc": s.get("desc", ""), "img": s.get("img", ""),
            "where": "外面吃", "source": "shop",
            "address": s.get("address", ""), "category": s.get("category", ""),
        })

    # 餐别
    if meal_type in ("meal", "dessert"):
        out = [c for c in out if c["meal_type"] == meal_type]
    # 场景
    if where == "食堂":
        out = [c for c in out if c["where"] == "食堂"]
    elif where in ("外面吃", "外卖"):   # 口头上说「点外卖」也落在周边店铺这一档
        out = [c for c in out if c["where"] == "外面吃"]
    # 口味
    if spice and spice != "随便":
        if spice == "辣":
            out = [c for c in out if any(t in ("辣", "微辣") for t in c["tags"])]
        elif spice == "不辣":
            out = [c for c in out if "不辣" in c["tags"]]
        else:
            out = [c for c in out if spice in c["tags"]]
    # 预算
    if budget not in (None, "", "不限", "None", "none"):
        try:
            cap = float(budget)
            out = [c for c in out if float(c["price"]) <= cap]
        except Exception:
            pass
    # 关键词（点名直达）
    kw = str(keyword or "").strip()
    if kw:
        hit = [c for c in out if kw in (c["name"] + c["shop"] + " ".join(c["tags"])
                                        + c.get("category", "") + c.get("desc", ""))]
        out = hit or out

    for c in out:
        c["walk_min"] = max(1, round(float(c["distance"]) / WALK_KM_PER_MIN))
    return out


# ------------------------- 转盘工具 -------------------------
def build_wheel(scene, preference="", budget=None, meal_type="", where=""):
    """根据决策层级生成一个转盘配置（真实选项来自数据文件）。

    scene:
      - "meal"  第 0 层：餐别（正餐 / 甜品饮料，另有盘外「你替我定」）
      - "where" 第 1 层：在哪吃（食堂 / 外面吃）
      - "spice" 第 2 层：口味（辣 / 不辣，另有盘外「你替我定」）
      - "dish"  第 3 层：具体菜品（按 餐别+口味+预算 过滤，带 items 供格子渲染）
    preference: 上层已确定的上下文（口味 辣/不辣/随便）
    budget: 金额上限（元），仅 dish 层生效
    meal_type: meal / dessert，第 0 层结果，用于过滤菜品池
    """
    scene = _norm_scene(scene)
    data = _load()
    mt = _norm_meal_type(meal_type)

    if scene == "meal":
        # 「随便」不再是扇区：它和真实选项混在一张盘上，一旦拒掉一个，
        # 剩下的「随便」就等于另一个选项，转盘没有悬念，逻辑上是冗余的。
        # 改成盘外的一个「你替我定」按钮，扇区只放互斥的真实选项。
        options = ["正餐", "甜品饮料"]
        title = "🍽️ 这顿想吃什么类型的？"
        ctx = ""
        items = []
        skip = {"key": "随便", "label": "都行，你替我定"}
    elif scene == "where":
        options = data.get("where_options", ["食堂", "外面吃"])
        title = "📍 今天在哪吃？"
        ctx = MEAL_LABELS.get(mt, "")
        items = []
        skip = None
    elif scene == "spice":
        options = ["辣", "不辣"]
        title = "🌶️ 今天想吃点啥口味？"
        ctx = where
        items = []
        skip = {"key": "随便", "label": "都行，你替我定"}
    elif scene == "dish":
        pool = candidates(meal_type=mt, spice=preference, budget=budget, where=where)
        note = ""
        if not pool:
            # 该组合筛空了（例如甜品选了“辣”）→ 放宽口味，不让转盘空转
            pool = candidates(meal_type=mt, spice="", budget=budget, where=where)
            note = "这个组合没筛出菜，已放宽口味限制"
        if not pool:
            pool = candidates(meal_type=mt, budget=budget)
            note = "条件太严，已放宽到同餐别全部菜品"
        seen, ordered = set(), []
        for d in sorted(pool, key=lambda x: (x.get("price", 0), -x.get("rating", 0))):
            if d["id"] not in seen:
                seen.add(d["id"])
                ordered.append(d)
        options = [d["name"] for d in ordered]
        items = ordered
        skip = None
        title = "🍽️ 转到哪道菜？（不喜欢就拒绝，剔掉重转）"
        if budget not in (None, "", "不限"):
            title += f"  ｜ 预算≤￥{float(budget):.0f}"
        ctx = preference
    else:
        return json.dumps({"error": f"未知层级：{scene}"}, ensure_ascii=False)

    payload = {
        "__wheel__": True, "title": title, "scene": scene,
        "options": options, "ctx": ctx, "meal_type": mt,
        "where": where, "items": items, "skip": skip,
    }
    if scene == "dish":
        payload["note"] = note
    return json.dumps(payload, ensure_ascii=False)


def get_dish_card(name):
    """查询某道菜的“美食名片”（也就把店铺招牌当成一道菜查）。"""
    for d in _load().get("dishes", []):
        if d["name"] == name or d.get("id") == name:
            return d
    for s in _load_shops().get("shops", []):
        if name in (s.get("dish"), s.get("signature"), s.get("name")):
            out = dict(s)
            out["name"] = s.get("dish") or s.get("signature")
            out["shop"] = s.get("name")
            out["price"] = s.get("avg_price")
            out["desc"] = s.get("desc", "")
            return out
    return None


_PLATE_FAMILIES = [
    (("辣", "微辣"), [("#F26B3A", "#FFB37A"), ("#E4572E", "#F6A15C"), ("#D9503F", "#F58C6B")]),
    (("甜",), [("#D96A8C", "#F7C6D4"), ("#C25E86", "#F3B8CE"), ("#E08498", "#FBD7DE")]),
    (("素",), [("#5F9E63", "#C8E0AE"), ("#6E9B57", "#D2E3B0"), ("#4E8B6A", "#BFE0C2")]),
    ((), [("#C97B3C", "#F4D8A8"), ("#B06A3C", "#EFC99A"), ("#A8813F", "#EBD3A0")]),
]


def plate_svg(dish_id):
    """本地生成 4:3 菜品占位图（SVG，纯本地，断网也能显示）。"""
    d = get_dish_card(dish_id) or {}
    name = d.get("name") or "这道菜"
    tags = d.get("tags", [])
    emoji = _dish_emoji(name)
    fam = _PLATE_FAMILIES[-1][1]
    for keys, palette in _PLATE_FAMILIES:
        if keys and any(k in tags for k in keys):
            fam = palette
            break
    h = sum(ord(ch) for ch in str(dish_id or name)) % len(fam)
    c1, c2 = fam[h]
    nm = _esc(name[:12])
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300" viewBox="0 0 400 300">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient></defs>
<rect width="400" height="300" fill="url(#g)"/>
<circle cx="330" cy="56" r="74" fill="#ffffff" opacity="0.10"/>
<circle cx="58" cy="252" r="92" fill="#ffffff" opacity="0.08"/>
<circle cx="200" cy="140" r="86" fill="#ffffff" opacity="0.16"/>
<text x="200" y="142" font-size="104" text-anchor="middle" dominant-baseline="middle">{emoji}</text>
<text x="200" y="246" font-size="30" font-family="PingFang SC,Microsoft YaHei,sans-serif"
 text-anchor="middle" fill="#ffffff" font-weight="600">{nm}</text>
</svg>"""


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_EMOJI_RULES = [
    (("拉面", "小面", "面", "粉"), "🍜"),
    (("饭", "抓饭", "盖浇"), "🍚"),
    (("火锅", "香锅", "麻辣烫", "烫", "锅"), "🍲"),
    (("奶茶", "拿铁", "咖啡", "柠檬水", "豆浆", "饮"), "🧋"),
    (("双皮奶", "绿豆沙", "杨枝甘露", "圆子", "蛋挞", "甜品"), "🍮"),
    (("包", "饺子", "蒸饺"), "🥟"),
    (("蛋",), "🥚"),
    (("粥",), "🥣"),
    (("汤",), "🍲"),
    (("鸡排", "鸡", "炸鸡", "汉堡"), "🍗"),
    (("沙拉", "轻食"), "🥗"),
    (("串", "烤"), "🍢"),
    (("肉",), "🥩"),
]


def _dish_emoji(name):
    for keys, e in _EMOJI_RULES:
        if any(k in str(name) for k in keys):
            return e
    return "🍱"


# ------------------------- 四条并列推荐（收尾） -------------------------
def _norm(v, lo, hi, invert=False):
    if hi <= lo:
        return 1.0
    x = (v - lo) / (hi - lo)
    return 1 - x if invert else x


def finalize(candidates_list, picked_id=None, criteria_note=""):
    """同一收尾函数：输出四条并列推荐（综合最佳 / 最近 / 最便宜 / 评分最高）。

    score = 0.5*norm(评分) + 0.3*norm(1/价格) + 0.2*norm(1/距离)
    跨条去重：同一家店同一道菜只出现一次，重复就顺延下一位。
    """
    cands = [dict(c) for c in (candidates_list or [])]
    if not cands:
        return json.dumps({"__four__": True, "groups": [], "pool_size": 0,
                           "note": "这个条件下没找到能吃的，放宽一点再试。"}, ensure_ascii=False)

    prices = [float(c["price"]) for c in cands]
    ratings = [float(c["rating"]) for c in cands]
    dists = [float(c["distance"]) for c in cands]
    minp, maxp = min(prices), max(prices)
    minr, maxr = min(ratings), max(ratings)
    mind, maxd = min(dists), max(dists)

    for c in cands:
        nr = _norm(float(c["rating"]), minr, maxr)
        npt = _norm(1.0 / max(float(c["price"]), 0.5),
                    1.0 / max(maxp, 0.5), 1.0 / max(minp, 0.5))
        nd = _norm(float(c["distance"]), mind, maxd, invert=True)
        c["score"] = round(0.5 * nr + 0.3 * npt + 0.2 * nd, 3)

    def key(c):
        return (c.get("shop", ""), c.get("name", ""))

    specs = [
        ("best", "综合最佳", "🏆", sorted(cands, key=lambda x: -x["score"])),
        ("near", "最近", "🚶", sorted(cands, key=lambda x: (x["distance"], -x["score"]))),
        ("cheap", "最便宜", "💰", sorted(cands, key=lambda x: (x["price"], -x["score"]))),
        ("top", "评分最高", "⭐", sorted(cands, key=lambda x: (-x["rating"], -x["score"]))),
    ]

    used = set()
    groups = []
    for gkey, label, emoji, ordered in specs:
        pick = None
        for c in ordered:
            if key(c) not in used:
                pick = c
                break
        if pick is None:            # 候选池不够四个不同选项：宁可不给，也不拿重复的凑数
            continue
        used.add(key(pick))
        groups.append({"key": gkey, "label": label, "emoji": emoji,
                       "item": pick, "reason": _reason(gkey, pick, cands),
                       "picked": bool(picked_id) and pick.get("id") == picked_id})

    note = criteria_note.strip().rstrip("：:，,；; ")
    if len(groups) >= len(specs):
        note = f"{note}，按四个维度各挑一条" if note else "按四个维度各挑一条"
    else:
        tail = "不重复的都在这儿了，没拿重复的凑数"
        note = f"{note}，{tail}" if note else tail

    return json.dumps({
        "__four__": True, "groups": groups, "pool_size": len(cands),
        "picked_id": picked_id, "note": note,
        "avg_price": round(sum(prices) / len(prices), 1),
    }, ensure_ascii=False)


def _reason(gkey, c, cands):
    """一句话理由，例如「走 3 分钟就到，评分 4.6」。"""
    walk, price, rating = c.get("walk_min", 1), c.get("price"), c.get("rating")
    shop = c.get("shop", "")
    if gkey == "best":
        return f"{shop}这家，走 {walk} 分钟、人均 ¥{price:g}、评分 {rating}，三项都均衡"
    if gkey == "near":
        return f"走 {walk} 分钟就到，评分 {rating}，饿的时候别犹豫"
    if gkey == "cheap":
        avg = sum(float(x["price"]) for x in cands) / max(len(cands), 1)
        save = avg - float(price)
        tail = f"，比这一层平均省 ¥{save:.0f}" if save > 0.5 else "，这层里最便宜"
        return f"只要 ¥{price:g}{tail}"
    if gkey == "top":
        return f"评分 {rating}，这层里口碑最好的一家"
    return ""


# ------------------------- 点名直达 -------------------------
_LEAD_WORDS = ("我想吃点", "我想吃", "我要吃", "我想来", "我想喝", "想吃点", "想吃", "要吃点",
               "吃个", "来一份", "来份", "来一个", "来个", "帮我找", "帮我点", "帮我",
               "有没有", "有没", "附近有", "附近", "今天", "中午", "晚上", "早上",
               "不要", "不想", "就要吃", "就要", "我")
_TAIL_CHARS = "吗吧呢啊呀哦嘛的了先啦哟呗"

# 关键词一点都没沾上时，按「类别线索」找替代（只用于替代，不冒充命中）
_HINT_TABLE = [
    (("火锅",), ("锅",)),
    (("麻辣烫", "麻辣拌", "麻辣"), ("麻辣", "辣")),
    (("鸡",), ("鸡",)),
    (("牛",), ("牛",)),
    (("鱼",), ("鱼",)),
    (("粥",), ("粥",)),
    (("面", "粉", "米线"), ("面", "粉", "米线")),
    (("饭", "盖浇", "盖饭"), ("饭",)),
    (("饺", "包", "馄饨"), ("饺", "包")),
    (("烤", "串", "烧烤"), ("烤", "串")),
    (("甜", "奶", "奶茶", "咖啡"), ("奶", "甜", "咖啡")),
    (("沙拉", "轻食"), ("沙拉", "轻食")),
    (("汤",), ("汤",)),
]


def clean_keyword(kw):
    """把「不要，我想吃黄焖鸡米饭吧」收拾成「黄焖鸡米饭」。"""
    s = re.sub(r"[\s，。！？、,.!?~～…]+", "", str(kw or ""))
    leads = sorted(_LEAD_WORDS, key=len, reverse=True)
    for _ in range(4):                       # 「我 中午 想吃 XXX」可能叠好几层
        for w in leads:
            if s.startswith(w) and len(s) > len(w):
                s = s[len(w):]
                break
        else:
            break
    while len(s) > 2 and s[-1] in _TAIL_CHARS:
        s = s[:-1]
    return s.strip()


def match_score(c, kw):
    """相关性打分：完全同名 > 菜名包含 > 用户说得更长 > 店名 > 标签描述 > 双字覆盖。"""
    if not kw:
        return 0
    name = c.get("name") or ""
    shop = c.get("shop") or ""
    if kw == name:
        return 100
    if kw in name:
        return 90
    if name and name in kw:
        return 85
    if kw in shop:
        return 70
    meta = shop + (c.get("category") or "") + "".join(c.get("tags") or []) + (c.get("desc") or "")
    if kw in meta:
        return 50
    if len(kw) >= 2:
        grams = [kw[i:i + 2] for i in range(len(kw) - 1)]
        hit = sum(1 for g in grams if g in name)
        if hit / len(grams) >= 0.5:
            return 30 + int(20 * hit / len(grams))
    return 0


def similar_hints(kw):
    """按类别找替代线索；没有线索返回空列表。"""
    outs = []
    for keys, targets in _HINT_TABLE:
        if any(k in kw for k in keys):
            outs.extend(targets)
    return list(dict.fromkeys(outs))


def search_dish(keyword="", meal_type="", budget=None, where="", taste=""):
    """点名直达：用户说了具体菜名/店名 → 跳过转盘，直接给并列方案。

    命中就给命中项；没收录就**如实说没有**，再给同类替代并标明是替代，
    绝不把整个候选池冒充成「最接近的方案」。
    """
    kw = clean_keyword(keyword)
    pool_all = candidates(meal_type=meal_type, budget=budget, where=where)

    best = max((match_score(c, kw) for c in pool_all), default=0) if kw else 0
    near = [c for c in pool_all if match_score(c, kw) >= 30] if kw else []
    near.sort(key=lambda c: -match_score(c, kw))
    exact = best >= 70                 # 菜名命中，或店名命中（如"火锅"→凑凑火锅）才算真找到
    matched = bool(kw) and exact
    hint_words = [] if matched or near else similar_hints(kw)

    if not kw:                          # 没给关键词：正常按条件给四条
        hits = list(pool_all)
        hit_count = 0
        note = ""
        say = "附近这几家，按四个维度各挑一条 👇"
    elif matched:
        hits = near
        hit_count = len(hits)
        note = f"「{kw}」相关共 {hit_count} 条"
        say = f"「{kw}」找到了，按四个维度各挑一条 👇"
    elif near:                          # 沾边但没这道菜：说清楚是「最接近」
        hits = near
        hit_count = 0
        note = f"菜单里没有「{kw}」，最接近的是"
        say = f"菜单里没有「{kw}」，最接近的是这几样 👇"
    elif hint_words:                    # 按类别找同类
        hits = [c for c in pool_all if any(h in (c.get("name") or "") for h in hint_words)]
        hits.sort(key=lambda c: -(c.get("rating") or 0))
        hit_count = 0
        note = f"菜单里没有「{kw}」，这是同类的替代"
        say = f"菜单里没有「{kw}」，我不瞎编——按同类给你找了这几样 👇"
    else:                               # 一点线索都没有：给评分靠前的，如实标注
        hits = list(pool_all)
        hits.sort(key=lambda c: -(c.get("rating") or 0))
        hit_count = 0
        note = f"菜单里没有「{kw}」，这是今天评分靠前的"
        say = f"菜单里没有「{kw}」，这几样是今天评分靠前的 👇"

    data = json.loads(finalize(hits, criteria_note=note))
    data["keyword"] = kw
    data["matched"] = matched
    data["hit_count"] = hit_count
    data["replaced"] = bool(kw) and not matched
    data["hint_words"] = hint_words
    data["say"] = say
    return json.dumps(data, ensure_ascii=False)


# ------------------------- 产出物 / 危险操作 -------------------------
def finalize_decision(where, spice, dish, note=""):
    """把最终决定写成产出物（Markdown 文件），返回摘要 + 文件路径。"""
    d = get_dish_card(dish) or {}
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUTPUT_DIR / f"今日决定_{ts}.md"
    spice_txt = spice if spice and spice != "随便" else "不挑口味（随便）"
    body = [
        "# 🍽️ 今日决定 · 吃饱饱助手", "",
        f"- **在哪吃**：{where}",
        f"- **口味**：{spice_txt}",
        f"- **最终菜品**：{dish}", "",
        "## 美食名片",
        f"- 菜品：**{d.get('name', dish)}**  ￥{d.get('price', '?')}",
        f"- 位置：{d.get('shop') or d.get('canteen', '（未指定）')}",
        f"- 标签：{', '.join(d.get('tags', []))}",
        f"- 介绍：{d.get('desc', '（暂无介绍）')}", "",
        f"> 备注：{note or '（无）'}", "",
        "_由「吃饱饱助手」生成，数据为示例，以实际供应为准。_",
    ]
    out.write_text("\n".join(body), encoding="utf-8")
    history_mod.log_decision({
        "type": "wheel", "where": where, "spice": spice_txt, "dish": dish,
        "campus": profile_mod.load_profile().get("campus"),
        "budget": profile_mod.load_profile().get("budget_cap"),
        "price": d.get("price"), "tags": d.get("tags", []),
    })
    return f"已生成今日决定并保存到 {out}：在【{where}】吃【{spice_txt}】的【{dish}】（￥{d.get('price','?')}）。"


def share_with_roommate(content):
    """【危险操作】模拟把一段内容发给室友（不真实外发，仅演示）。"""
    return (f"【模拟发送·未真实外发】已把以下内容发给室友：\n“{content}”\n"
            f"（这是课堂演示用的模拟发送，不会真实对外发出任何消息。）")


def finalize_recommend(name, address, distance_km, avg_price, rating, signature="", note=""):
    """把“就近决定”写成产出物（Markdown 文件），返回摘要 + 文件路径。"""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUTPUT_DIR / f"就近决定_{ts}.md"
    body = [
        "# 📍 今日就近决定 · 吃饱饱助手", "",
        f"- **店家**：{name}",
        f"- **地址**：{address}",
        f"- **距校区**：{distance_km} km",
        f"- **人均预估**：￥{avg_price}",
        f"- **评分**：⭐ {rating}",
        f"- **招牌**：{signature or '（暂无）'}", "",
        f"> 备注：{note or '（无）'}", "",
        "_由「吃饱饱助手·就近推荐」生成，商户与距离为示例数据，以实际为准。_",
    ]
    out.write_text("\n".join(body), encoding="utf-8")
    history_mod.log_decision({
        "type": "recommend", "where": "外面吃", "shop_name": name,
        "campus": profile_mod.load_profile().get("campus"),
        "budget": profile_mod.load_profile().get("budget_cap"),
        "price": avg_price, "rating": rating, "distance": distance_km, "tags": [],
    })
    return f"已保存就近决定到 {out}：去【{name}】（{address}，距校区{distance_km}km，人均￥{avg_price}，⭐{rating}）吃{signature}。"


# ------------------------- 用户记忆画像 -------------------------
def get_profile():
    """读取当前用户画像（校区/预算/口味/忌口等），用于个性化推荐与预过滤。"""
    return json.dumps({"__profile__": True, "profile": profile_mod.load_profile()}, ensure_ascii=False)


def set_profile(field, value):
    """更新用户画像某一字段（校区/预算/口味偏好/忌口/饮食限制/称呼）。"""
    return profile_mod.update_profile(field, value)


def save_preference(key, value):
    """【需确认的危险写操作】记住用户的一项稳定偏好（口味/忌口/饮食限制/称呼）。"""
    return profile_mod.update_profile(key, value)


# ------------------------- 就近综合推荐（定位 + 距离 + 性价比 + 评分） -------------------------
def recommend_nearby_food(campus=None, taste=None, budget=None, criteria="value", top_n=5, keyword=None):
    """以校区为圆心，综合「距离 + 性价比 + 评分」给用户推荐就近美食。"""
    prof = profile_mod.load_profile()
    campus = campus or prof.get("campus") or "金海校区"
    try:
        cp = json.load(open(CAMPUS_PATH, encoding="utf-8"))
        c = cp["campuses"].get(campus)
    except Exception as e:
        return json.dumps({"error": f"读取校区数据失败：{e}"}, ensure_ascii=False)
    if not c:
        return json.dumps({"error": f"未知校区：{campus}，可选：{list(cp['campuses'].keys())}"}, ensure_ascii=False)
    try:
        shops = _load_shops().get("shops", [])
    except Exception as e:
        return json.dumps({"error": f"读取商铺数据失败：{e}"}, ensure_ascii=False)

    if budget in (None, "", "不限", "None"):
        budget = prof.get("budget_cap")
    if taste:
        taste_list = [t.strip() for t in str(taste).replace("，", ",").split(",") if t.strip()]
    else:
        taste_list = list(prof.get("taste_prefs", [])) + list(prof.get("dietary", []))
    dislikes = list(prof.get("dislikes", []))

    kw = (str(keyword).strip() if keyword else "")
    matched, all_filtered = [], []
    for s in shops:
        if budget is not None:
            try:
                if s.get("avg_price", 999) > float(budget):
                    continue
            except Exception:
                pass
        tags = s.get("tags", [])
        if any(d in tags for d in dislikes):
            continue
        if taste_list and not any(t in tags for t in taste_list):
            continue
        dist = _haversine(c["lat"], c["lng"], s["lat"], s["lng"])
        item = dict(s)
        item["distance_km"] = round(dist, 2)
        item["walk_min"] = max(1, round(dist / WALK_KM_PER_MIN))
        item["dish"] = s.get("dish") or s.get("signature")
        all_filtered.append(item)
        if kw:
            hay = " ".join([str(s.get("name", "")), str(s.get("signature", "")),
                            str(s.get("dish", "")), str(s.get("category", "")),
                            " ".join(s.get("tags", []))])
            if kw in hay:
                matched.append(item)
        else:
            matched.append(item)

    items = matched if (matched or not kw) else all_filtered
    none_match_note = ""
    if kw and not matched:
        none_match_note = f"附近没有严格匹配「{kw}」的店，以下是综合推荐。"

    if not items:
        return json.dumps({
            "__recommend__": True, "campus": campus, "center": c["address"],
            "criteria": criteria, "budget": budget, "count": 0, "items": [],
            "note": "没有符合当前预算/口味的店家，试试放宽条件。",
        }, ensure_ascii=False)

    prices = [i["avg_price"] for i in items]
    ratings = [i["rating"] for i in items]
    dists = [i["distance_km"] for i in items]
    minp, maxp = min(prices), max(prices)
    minr, maxr = min(ratings), max(ratings)
    mind, maxd = min(dists), max(dists)
    for i in items:
        cost = 1 - (i["avg_price"] - minp) / (maxp - minp) if maxp > minp else 1.0
        rat = (i["rating"] - minr) / (maxr - minr) if maxr > minr else 1.0
        dsc = 1 - (i["distance_km"] - mind) / (maxd - mind) if maxd > mind else 1.0
        if criteria == "distance":
            i["score"] = round(0.70 * dsc + 0.20 * rat + 0.10 * cost, 3)
        elif criteria == "rating":
            i["score"] = round(0.60 * rat + 0.25 * dsc + 0.15 * cost, 3)
        else:
            i["score"] = round(0.35 * dsc + 0.35 * cost + 0.30 * rat, 3)
        i["score_breakdown"] = {"距离分": round(dsc, 2), "性价比分": round(cost, 2), "评分分": round(rat, 2)}

    items.sort(key=lambda x: x["score"], reverse=True)
    items = items[:top_n]
    return json.dumps({
        "__recommend__": True, "campus": campus, "center": c["address"],
        "criteria": criteria, "budget": budget, "count": len(items), "items": items,
        "note": none_match_note or "",
    }, ensure_ascii=False)


# ------------------------- 近期画像（从历史选择中学习） -------------------------
def get_recent_profile():
    """聚合最近 10 次真实选择，生成“近期画像”。"""
    return json.dumps({"__recent_profile__": True, "recent": history_mod.summarize_recent_profile(10)},
                      ensure_ascii=False)


# ------------------------- 注册表 / Schema / 危险标记 -------------------------
TOOL_REGISTRY = {
    "build_wheel": build_wheel,
    "search_dish": search_dish,
    "finalize": finalize,
    "finalize_decision": finalize_decision,
    "finalize_recommend": finalize_recommend,
    "share_with_roommate": share_with_roommate,
    "get_profile": get_profile,
    "set_profile": set_profile,
    "save_preference": save_preference,
    "recommend_nearby_food": recommend_nearby_food,
    "get_recent_profile": get_recent_profile,
}

# 危险操作：涉及对外发送 / 写入长期记忆，必须经用户确认才能执行
DANGEROUS_TOOLS = {"share_with_roommate", "save_preference"}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "build_wheel",
            "description": "生成一个“决策大转盘”的配置。scene 为决策层级：meal=第0层(正餐/甜品饮料/随便)；where=第1层(食堂/外面吃)；spice=第2层(辣/不辣/随便)；dish=第3层(具体菜品，按 preference 口味过滤)。preference 为上层已确定的上下文。选项来自真实数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "description": "决策层级：meal / where / spice / dish"},
                    "preference": {"type": "string", "description": "上层上下文，如口味“辣/不辣/随便”，可为空"},
                },
                "required": ["scene"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_dish",
            "description": "点名直达：用户已经说出具体想吃的东西（如“想吃火锅”“来份牛肉面”）时调用，跳过转盘直接给四条并列方案（综合最佳/最近/最便宜/评分最高）。keyword 只填食物或店名关键词。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "食物/店名关键词，如“火锅”“牛肉面”"},
                    "meal_type": {"type": "string", "description": "meal=正餐 / dessert=甜品饮料，可空"},
                    "budget": {"type": "string", "description": "金额上限（元）或“不限”，可空"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize_decision",
            "description": "把最终决定写成产出物（Markdown 文件）。参数 where 在哪吃、spice 口味、dish 最终菜品、note 备注（可选）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "where": {"type": "string", "description": "在哪吃：食堂/外面吃"},
                    "spice": {"type": "string", "description": "口味：辣/不辣/随便"},
                    "dish": {"type": "string", "description": "最终确定的菜品名"},
                    "note": {"type": "string", "description": "备注，可选"},
                },
                "required": ["where", "spice", "dish"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "share_with_roommate",
            "description": "【危险操作·需用户确认】把一段内容模拟发送给室友（演示用，不真实外发）。",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string", "description": "发送给室友的内容"}},
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_profile",
            "description": "读取当前用户画像：校区(定位圆心)、金额上限、口味偏好、忌口、饮食限制等。回答前应先读取。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_profile",
            "description": "更新用户画像的“非敏感”字段。仅用于 campus(校区)/ budget_cap(金额上限元或“不限”)/ name(称呼)。口味/忌口/饮食限制请用 save_preference。",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "description": "字段名：campus/budget_cap/name"},
                    "value": {"type": "string", "description": "字段值"},
                },
                "required": ["field", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_preference",
            "description": "【需用户确认】记住一项稳定偏好（写操作，Agent 会先回显确认）。key：dislikes/taste_prefs/dietary/name。用户说“忘掉xx”时把对应值置空。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "画像字段：dislikes/taste_prefs/dietary/name"},
                    "value": {"type": "string", "description": "字段值"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_nearby_food",
            "description": "以校区为圆心，综合「距离+性价比+评分」推荐就近美食（用户问“附近吃啥/性价比高的/外面吃去哪”但没有点具体菜时用）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "campus": {"type": "string", "description": "校区名，如“金海校区”，可空"},
                    "taste": {"type": "string", "description": "口味偏好，逗号分隔，可空"},
                    "budget": {"type": "string", "description": "金额上限（元）或“不限”，可空"},
                    "criteria": {"type": "string", "description": "value / distance / rating，默认 value"},
                    "top_n": {"type": "integer", "description": "返回前几名，默认5"},
                    "keyword": {"type": "string", "description": "目标关键词，可空"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize_recommend",
            "description": "把“就近决定”写成产出物（Markdown 文件）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "店名"},
                    "address": {"type": "string", "description": "地址"},
                    "distance_km": {"type": "number", "description": "距校区公里数"},
                    "avg_price": {"type": "number", "description": "人均预估"},
                    "rating": {"type": "number", "description": "评分"},
                    "signature": {"type": "string", "description": "招牌菜，可选"},
                    "note": {"type": "string", "description": "备注，可选"},
                },
                "required": ["name", "address", "distance_km", "avg_price", "rating"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_profile",
            "description": "聚合用户最近 10 次真实选择，生成“近期画像”：最常去场景/口味、最爱菜品或店、平均花销。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def call_tool(name, args):
    fn = TOOL_REGISTRY.get(name)
    if not fn:
        return f"未知工具：{name}"
    try:
        return str(fn(**args))
    except TypeError as e:
        return f"工具 {name} 参数错误：{e}"
    except Exception as e:
        return f"工具 {name} 执行出错：{e}"


if __name__ == "__main__":
    print("== meal ==")
    print(build_wheel("meal"))
    print("== dish(正餐/辣/食堂) ==")
    print(build_wheel("dish", "辣", None, "meal", "食堂")[:400])
    print("== 四条 ==")
    print(search_dish("火锅")[:600])
