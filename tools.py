"""杉达选择助手·今天吃啥大转盘 —— 业务工具层（路径B：普通功能 + 自然语言驱动）

实现“帮助大学生做选择”所需的若干可被调用的业务操作：
- build_wheel：根据决策层级（where/spice/dish）从真实数据生成转盘配置
- finalize_decision：把最终决定写成产出物（Markdown 文件）
- share_with_roommate：把决定模拟发给室友（危险操作，需确认）

数据位于 data/food_data.json（当前为示例数据，替换为真实校园数据即可作为真实数据源）。
危险操作（发给室友）由 Agent 层做确认拦截。
"""
import json
import math
import datetime
from pathlib import Path
import profile as profile_mod
import history as history_mod

DATA_PATH = Path(__file__).parent / "data" / "food_data.json"
CAMPUS_PATH = Path(__file__).parent / "data" / "campus.json"
SHOP_PATH = Path(__file__).parent / "data" / "shops.json"
OUTPUT_DIR = Path(__file__).parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)


def _load():
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


# ------------------------- 转盘工具 -------------------------
def build_wheel(scene, preference="", budget=None):
    """根据决策层级生成一个转盘配置（真实选项来自数据文件）。

    参数:
      scene: 决策层级
        - "where" 第一层：今天在哪吃（食堂/外卖/外面吃）
        - "spice" 第二层：口味（辣/不辣/随便）
        - "dish"  第三层：具体菜品（按 preference 口味过滤；preference 为空或“随便”则全量）
      preference: 上层已确定的上下文（如口味“辣”/“不辣”/“随便”，或为空）
      budget: 金额上限（元），仅对 dish 层级生效；None/“不限” 表示不设上限
    返回: 一个 JSON 字符串，含 {"__wheel__": true, "title", "scene", "options", "ctx"}
    """
    data = _load()
    if scene == "where":
        options = data.get("where_options", [])
        title = "📍 今天在哪吃？"
        ctx = ""
    elif scene == "spice":
        options = ["辣", "不辣", "随便"]
        title = "🌶️ 今天想吃点啥口味？"
        ctx = preference
    elif scene == "dish":
        dishes = data.get("dishes", [])
        spice = (preference or "").strip()
        if spice in ("辣",):
            pool = [d for d in dishes if "辣" in d.get("tags", [])]
        elif spice in ("不辣",):
            pool = [d for d in dishes if "不辣" in d.get("tags", [])]
        else:  # 随便 / 空 -> 全量
            pool = list(dishes)
        # 预算上限过滤（转盘上的“金额上限”）
        cap = None
        if budget not in (None, "", "不限", "None"):
            try:
                cap = float(budget)
                pool = [d for d in pool if d.get("price", 0) <= cap]
            except Exception:
                cap = None
        # 去重保序，按价格升序方便看
        seen = set()
        ordered = []
        for d in sorted(pool, key=lambda x: x.get("price", 0)):
            if d["name"] not in seen:
                seen.add(d["name"])
                ordered.append(d)
        options = [d["name"] for d in ordered]
        title = "🍽️ 转到哪道菜？（不喜欢就拒绝，剔掉重转）"
        if cap is not None:
            title += f"  ｜ 预算≤￥{cap:.0f}"
        ctx = spice
    else:
        return json.dumps({"error": f"未知层级：{scene}"})

    return json.dumps({
        "__wheel__": True,
        "title": title,
        "scene": scene,
        "options": options,
        "ctx": ctx,
    }, ensure_ascii=False)


def get_dish_card(name):
    """查询某道菜的“美食名片”信息（特写描述 + 价格 + 位置 + 标签），用于勾起食欲。"""
    data = _load()
    for d in data.get("dishes", []):
        if d["name"] == name:
            return d
    return None


# ------------------------- 产出物 / 危险操作 -------------------------
def finalize_decision(where, spice, dish, note=""):
    """把最终决定写成产出物（Markdown 文件），返回摘要 + 文件路径。"""
    d = get_dish_card(dish) or {}
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUTPUT_DIR / f"今日决定_{ts}.md"
    spice_txt = spice if spice and spice != "随便" else "不挑口味（随便）"
    body = [
        "# 🍽️ 今日决定 · 杉达选择助手",
        "",
        f"- **在哪吃**：{where}",
        f"- **口味**：{spice_txt}",
        f"- **最终菜品**：{dish}",
        "",
        "## 美食名片",
        f"- 菜品：**{d.get('name', dish)}**  ￥{d.get('price', '?')}",
        f"- 位置：{d.get('canteen', '（未指定）')} / {d.get('window', '（未指定）')}",
        f"- 标签：{', '.join(d.get('tags', []))}",
        f"- 介绍：{d.get('desc', '（暂无介绍）')}",
        "",
        f"> 备注：{note or '（无）'}",
        "",
        "_由「杉达选择助手·今天吃啥大转盘」生成，数据为示例，以实际供应为准。_",
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
        "# 📍 今日就近决定 · 杉达选择助手",
        "",
        f"- **店家**：{name}",
        f"- **地址**：{address}",
        f"- **距校区**：{distance_km} km",
        f"- **人均预估**：￥{avg_price}",
        f"- **评分**：⭐ {rating}",
        f"- **招牌**：{signature or '（暂无）'}",
        "",
        f"> 备注：{note or '（无）'}",
        "",
        "_由「杉达选择助手·就近推荐」生成，商户与距离为示例数据，以实际为准。_",
    ]
    out.write_text("\n".join(body), encoding="utf-8")
    history_mod.log_decision({
        "type": "recommend", "where": "外面吃", "shop_name": name,
        "campus": profile_mod.load_profile().get("campus"),
        "budget": profile_mod.load_profile().get("budget_cap"),
        "price": avg_price, "rating": rating, "distance": distance_km,
        "tags": [],
    })
    return f"已保存就近决定到 {out}：去【{name}】（{address}，距校区{distance_km}km，人均￥{avg_price}，⭐{rating}）吃{signature}。"


# ------------------------- 用户记忆画像 -------------------------
def get_profile():
    """读取当前用户画像（校区/预算/口味/忌口等），用于个性化推荐与预过滤。"""
    return json.dumps({"__profile__": True, "profile": profile_mod.load_profile()}, ensure_ascii=False)


def set_profile(field, value):
    """更新用户画像某一字段（校区/预算/口味偏好/忌口/饮食限制/称呼）。"""
    return profile_mod.update_profile(field, value)


# ------------------------- 就近综合推荐（定位 + 距离 + 性价比 + 评分） -------------------------
def _haversine(lat1, lng1, lat2, lng2):
    """两点间直线距离（km），真实球面计算。"""
    R = 6371.080
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def recommend_nearby_food(campus=None, taste=None, budget=None, criteria="value", top_n=5):
    """以校区为圆心，综合「距离 + 性价比 + 评分」给用户推荐就近美食。

    参数:
      campus  : 校区名（如“金海校区”），缺省用画像里的校区
      taste   : 口味偏好标签（如“不辣/辣/清真”），缺省用画像 taste_prefs+dietary
      budget  : 金额上限（元），缺省用画像 budget_cap；“不限”表示不限制
      criteria: 排序侧重 —— value(性价比综合,默认) / distance(就近优先) / rating(好评优先)
      top_n   : 返回前几名
    返回: 含 __recommend__ 标记的 JSON，items 按综合分降序，每项含距离/价格/评分/综合分/评分拆解。
    """
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
        shops_doc = json.load(open(SHOP_PATH, encoding="utf-8"))
        shops = shops_doc.get("shops", [])
    except Exception as e:
        return json.dumps({"error": f"读取商铺数据失败：{e}"}, ensure_ascii=False)

    # 预算：显式 budget 优先，否则画像
    if budget in (None, "", "不限", "None"):
        budget = prof.get("budget_cap")
    # 口味/饮食限制：显式 taste 优先，否则画像
    if taste:
        taste_list = [t.strip() for t in str(taste).replace("，", ",").split(",") if t.strip()]
    else:
        taste_list = list(prof.get("taste_prefs", [])) + list(prof.get("dietary", []))
    dislikes = list(prof.get("dislikes", []))

    items = []
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
        items.append(item)

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
        cost = 1 - (i["avg_price"] - minp) / (maxp - minp) if maxp > minp else 1.0   # 越便宜越高
        rat = (i["rating"] - minr) / (maxr - minr) if maxr > minr else 1.0           # 评分越高越高
        dsc = 1 - (i["distance_km"] - mind) / (maxd - mind) if maxd > mind else 1.0   # 越近越高
        if criteria == "distance":
            i["score"] = round(0.70 * dsc + 0.20 * rat + 0.10 * cost, 3)
        elif criteria == "rating":
            i["score"] = round(0.60 * rat + 0.25 * dsc + 0.15 * cost, 3)
        else:  # value：性价比综合
            i["score"] = round(0.35 * dsc + 0.35 * cost + 0.30 * rat, 3)
        i["score_breakdown"] = {"距离分": round(dsc, 2), "性价比分": round(cost, 2), "评分分": round(rat, 2)}

    items.sort(key=lambda x: x["score"], reverse=True)
    items = items[:top_n]
    return json.dumps({
        "__recommend__": True, "campus": campus, "center": c["address"],
        "criteria": criteria, "budget": budget, "count": len(items), "items": items,
    }, ensure_ascii=False)


# ------------------------- 近期画像（从历史选择中学习） -------------------------
def get_recent_profile():
    """聚合最近 10 次真实选择，生成“近期画像”（最常去校区/场景/口味、最爱菜或店、平均预算与花销）。智能体据此给出更贴合的推荐。"""
    return json.dumps({"__recent_profile__": True, "recent": history_mod.summarize_recent_profile(10)}, ensure_ascii=False)


# ------------------------- 注册表 / Schema / 危险标记 -------------------------
TOOL_REGISTRY = {
    "build_wheel": build_wheel,
    "finalize_decision": finalize_decision,
    "share_with_roommate": share_with_roommate,
    "get_profile": get_profile,
    "set_profile": set_profile,
    "recommend_nearby_food": recommend_nearby_food,
    "finalize_recommend": finalize_recommend,
    "get_recent_profile": get_recent_profile,
}

# 危险操作：涉及对外发送，必须经用户确认才能执行
DANGEROUS_TOOLS = {"share_with_roommate"}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "build_wheel",
            "description": "生成一个“决策大转盘”的配置。参数 scene 为决策层级：where=第一层(食堂/外卖/外面吃)；spice=第二层(辣/不辣/随便)；dish=第三层(具体菜品，按 preference 口味过滤，preference 为“辣/不辣/随便”，空或“随便”表示不过滤全量)。preference 为上层已确定的上下文。返回的转盘选项来自真实数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "description": "决策层级：where / spice / dish"},
                    "preference": {"type": "string", "description": "上层上下文，如口味“辣/不辣/随便”，可为空"}
                },
                "required": ["scene"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "finalize_decision",
            "description": "把最终决定写成产出物（Markdown 文件）。参数 where 在哪吃、spice 口味、dish 最终菜品、note 备注（可选）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "where": {"type": "string", "description": "在哪吃：食堂/外卖/外面吃"},
                    "spice": {"type": "string", "description": "口味：辣/不辣/随便"},
                    "dish": {"type": "string", "description": "最终确定的菜品名"},
                    "note": {"type": "string", "description": "备注，可选"}
                },
                "required": ["where", "spice", "dish"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "share_with_roommate",
            "description": "【危险操作·需用户确认】把一段内容模拟发送给室友（演示用，不真实外发）。参数 content 发送内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "发送给室友的内容"}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_profile",
            "description": "读取当前用户画像：校区(定位圆心)、金额上限、口味偏好、忌口、饮食限制等。用于个性化推荐与预过滤，回答前应先读取。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_profile",
            "description": "更新用户画像某一字段（跨轮记住用户偏好）。字段 field 取值：campus(校区名，如“金海校区”)/ budget_cap(金额上限元或“不限”)/ taste_prefs(口味偏好，逗号分隔如“不辣”)/ dislikes(忌口)/ dietary(饮食限制如“清真”)/ name(称呼)。value 为对应值。",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "description": "要更新的字段名：campus/budget_cap/taste_prefs/dislikes/dietary/name"},
                    "value": {"type": "string", "description": "字段值"}
                },
                "required": ["field", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_nearby_food",
            "description": "以用户所在校区为圆心，综合「距离+性价比+评分」推荐就近美食。当用户问“附近吃啥/推荐个性价比高的/外面吃去哪/便宜好吃的”时调用。参数：campus(校区，缺省用画像)；taste(口味偏好如“不辣”，缺省用画像)；budget(金额上限元，缺省用画像，“不限”不限制)；criteria(排序侧重：value性价比综合/默认, distance就近优先, rating好评优先)；top_n(返回数量默认5)。返回每项含店名/地址/距离km/均价/评分/综合分。",
            "parameters": {
                "type": "object",
                "properties": {
                    "campus": {"type": "string", "description": "校区名，如“金海校区”，可空"},
                    "taste": {"type": "string", "description": "口味偏好，逗号分隔，可空"},
                    "budget": {"type": "string", "description": "金额上限（元）或“不限”，可空"},
                    "criteria": {"type": "string", "description": "排序侧重：value / distance / rating，默认 value"},
                    "top_n": {"type": "integer", "description": "返回前几名，默认5"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "finalize_recommend",
            "description": "把“就近决定”写成产出物（Markdown 文件）。参数：name 店名、address 地址、distance_km 距离、avg_price 人均、rating 评分、signature 招牌(可选)、note 备注(可选)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "店名"},
                    "address": {"type": "string", "description": "地址"},
                    "distance_km": {"type": "number", "description": "距校区公里数"},
                    "avg_price": {"type": "number", "description": "人均预估"},
                    "rating": {"type": "number", "description": "评分"},
                    "signature": {"type": "string", "description": "招牌菜，可选"},
                    "note": {"type": "string", "description": "备注，可选"}
                },
                "required": ["name", "address", "distance_km", "avg_price", "rating"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_profile",
            "description": "聚合用户最近 10 次真实选择，生成“近期画像”：最常去的校区/就餐场景/口味、最爱菜品或店、平均预算与花销、高频标签。用于对用户习惯做个性化推荐。返回含 __recent_profile__ 标记的 JSON。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
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
    import pprint
    print("== where ==")
    pprint.pprint(json.loads(build_wheel("where")))
    print("== spice ==")
    pprint.pprint(json.loads(build_wheel("spice", "食堂")))
    print("== dish(辣) ==")
    pprint.pprint(json.loads(build_wheel("dish", "辣")))
    print("== dish(随便) ==")
    pprint.pprint(json.loads(build_wheel("dish", "随便")))
