"""选择历史与近期画像模块（路径B 智能体·从真实行为中学习）

每次用户完成一次“选择”（大转盘定菜 / 就近推荐选定店家）都记一条到 data/history.json；
再把最近 N 条（默认 10）聚合出“近期画像”：最常去的校区/就餐场景/口味、最爱的菜或店、
平均预算与花销、高频标签。这是智能体“记得你”的隐性记忆，与手动设置的 profile 互为补充。
用户可在界面查看历史、删除不想要的单条。
"""
import json
import uuid
import datetime
from pathlib import Path
from collections import Counter

HISTORY_PATH = Path(__file__).parent / "data" / "history.json"


def load_history():
    if HISTORY_PATH.exists():
        try:
            return json.load(open(HISTORY_PATH, encoding="utf-8"))
        except Exception:
            pass
    return []


def save_history(hist):
    HISTORY_PATH.parent.mkdir(exist_ok=True)
    json.dump(hist, open(HISTORY_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def log_decision(entry):
    """记录一次完成的选择。entry 可含：type/where/spice/dish/shop_name/campus/budget/price/rating/distance/tags。"""
    hist = load_history()
    rec = {
        "id": uuid.uuid4().hex[:8],
        "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        **{k: v for k, v in entry.items() if v not in (None, "")},
    }
    hist.append(rec)
    save_history(hist)
    return rec


def delete_history(rid):
    hist = [h for h in load_history() if h.get("id") != rid]
    save_history(hist)
    return len(hist)


def summarize_recent_profile(limit=10):
    """把最近 limit 条选择聚合为“近期画像”。返回 dict。"""
    hist = load_history()[-limit:]
    if not hist:
        return {"count": 0, "summary": "暂无选择历史。"}
    campuses = Counter(h.get("campus") for h in hist if h.get("campus"))
    wheres = Counter(h.get("where") for h in hist if h.get("where"))
    spices = Counter(h.get("spice") for h in hist if h.get("spice"))
    favorites = Counter((h.get("dish") or h.get("shop_name")) for h in hist if (h.get("dish") or h.get("shop_name")))
    budgets = [float(h["budget"]) for h in hist if h.get("budget") not in (None, "")]
    prices = [float(h["price"]) for h in hist if h.get("price") not in (None, "")]
    ratings = [float(h["rating"]) for h in hist if h.get("rating") not in (None, "")]
    tags = Counter()
    for h in hist:
        for t in (h.get("tags") or []):
            tags[t] += 1
    return {
        "count": len(hist),
        "campus": campuses.most_common(1)[0][0] if campuses else None,
        "where": wheres.most_common(1)[0][0] if wheres else None,
        "spice": spices.most_common(1)[0][0] if spices else None,
        "favorite": favorites.most_common(1)[0][0] if favorites else None,
        "avg_budget": round(sum(budgets) / len(budgets), 1) if budgets else None,
        "avg_price": round(sum(prices) / len(prices), 1) if prices else None,
        "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "top_tags": [t for t, _ in tags.most_common(3)],
    }


if __name__ == "__main__":
    log_decision({"type": "wheel", "where": "食堂", "spice": "不辣", "dish": "番茄炒蛋", "campus": "金海校区", "budget": 20, "price": 8, "tags": ["不辣", "素"]})
    log_decision({"type": "recommend", "where": "外面吃", "shop_name": "蜜雪冰城(金海路)", "campus": "金海校区", "price": 8, "rating": 4.6, "distance": 0.05, "tags": ["不辣", "甜"]})
    print(json.dumps(summarize_recent_profile(), ensure_ascii=False, indent=2))
