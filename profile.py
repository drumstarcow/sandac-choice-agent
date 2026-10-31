"""用户记忆画像模块（路径B 智能体·记忆能力）

把用户在对话中透露的稳定偏好持久化到 data/profile.json，跨轮/跨会话保留：
- campus      : 定位到的校区（如“金海校区”），作为就近推荐的圆心
- budget_cap  : 金额上限（元），None/“不限” 表示不设上限
- taste_prefs : 口味偏好标签（如 ["不辣"]）
- dislikes    : 忌口标签（如 ["辣"]）
- dietary     : 饮食限制（如 ["清真"]）
- name        : 用户称呼（可选）

这是“智能体”区别于“一次性工具”的标志性能力：它记得你，下次直接按你的偏好预过滤。
"""
import json
from pathlib import Path

PROFILE_PATH = Path(__file__).parent / "data" / "profile.json"

DEFAULT_PROFILE = {
    "campus": "金海校区",
    "budget_cap": None,
    "taste_prefs": [],
    "dislikes": [],
    "dietary": [],
    "name": "",
}

VALID_FIELDS = {"campus", "budget_cap", "taste_prefs", "dislikes", "dietary", "name"}


def load_profile():
    if PROFILE_PATH.exists():
        try:
            with open(PROFILE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            base = dict(DEFAULT_PROFILE)
            base.update({k: v for k, v in data.items() if k in VALID_FIELDS})
            return base
        except Exception:
            pass
    return dict(DEFAULT_PROFILE)


def save_profile(p):
    PROFILE_PATH.parent.mkdir(exist_ok=True)
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)


def update_profile(field, value):
    """更新画像字段，返回 JSON 字符串（含最新 profile）。"""
    if field not in VALID_FIELDS:
        return json.dumps({"error": f"未知字段：{field}，可选：{sorted(VALID_FIELDS)}"}, ensure_ascii=False)
    p = load_profile()
    if field == "budget_cap":
        if value in (None, "", "不限", "None", "none"):
            p["budget_cap"] = None
        else:
            try:
                p["budget_cap"] = float(value)
            except Exception:
                return json.dumps({"error": "budget_cap 需为数字（元）或“不限”"}, ensure_ascii=False)
    elif field in ("taste_prefs", "dislikes", "dietary"):
        if isinstance(value, str):
            value = [v.strip() for v in str(value).replace("，", ",").split(",") if v.strip()]
        p[field] = list(value)
    else:
        p[field] = value
    save_profile(p)
    return json.dumps({"ok": True, "profile": p}, ensure_ascii=False)


if __name__ == "__main__":
    print("当前画像：", json.dumps(load_profile(), ensure_ascii=False, indent=2))
    print(update_profile("campus", "金海校区"))
    print(update_profile("budget_cap", "20"))
    print(update_profile("dislikes", "辣"))
