"""吃饱饱助手 —— 终端演示脚本（路径B 备用演示入口）

适用于：教师在课堂上要求“打开终端看执行过程”时，或没有浏览器环境的快速演示。
功能菜单：
  1) 大转盘（在哪吃→口味→菜品，可拒绝剔除重转，支持金额上限）
  2) 就近综合推荐（以校区为圆心，距离+性价比+评分排序）
  3) 设置我的画像（校区/预算/口味/忌口）
  4) 查看选择历史与近期画像
  5) 退出
危险操作（发给室友）会先请求确认。
"""
import json
import random
import tools
import profile as profile_mod
import history as history_mod

NEXT_SCENE = {"where": "spice", "spice": "dish", "dish": None}


def show_wheel(w):
    opts = list(w["options"])
    print(f"\n🎡 {w['title']}")
    for i, o in enumerate(opts):
        print(f"  {i+1}. {o}")
    while True:
        raw = input("编号/名称（r 随机转 / -名称 拒绝剔除）：").strip()
        if raw.lower() == "r":
            idx = random.randrange(len(opts))
            print(f"  🎯 指向：{opts[idx]}")
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(opts):
            return opts[int(raw) - 1]
        if raw in opts:
            return raw
        if raw.startswith("-") and raw[1:] in opts:
            opts.remove(raw[1:])
            if not opts:
                print("全部拒绝，重开本层。")
                return show_wheel(json.loads(tools.build_wheel(w["scene"], w.get("ctx", ""), w.get("budget"))))
            print("已剔除，剩余：", opts)
            continue
        print("无效，重试。")


def do_wheel():
    budget = input("转盘金额上限（回车=不限，或输入数字）：").strip()
    budget = budget if budget else "不限"
    choices, dish_options = {}, []
    w = json.loads(tools.build_wheel("where"))
    scene, choice = show_wheel(w); choices[scene] = choice
    while NEXT_SCENE.get(scene):
        nxt = NEXT_SCENE[scene]
        w = json.loads(tools.build_wheel(nxt, choice, budget))
        if nxt == "dish":
            dish_options = list(w["options"])
        scene, choice = show_wheel(w); choices[scene] = choice
    card = tools.get_dish_card(choices["dish"])
    print(f"\n📇 美食名片：{card['name']} ￥{card['price']} ｜ {card.get('canteen')}/{card.get('window')} ｜ {','.join(card.get('tags',[]))}")
    print(f"  {card.get('desc')}")
    while input("就吃这个？(y 确定 / n 换一个)：").strip().lower() == "n":
        if choices["dish"] in dish_options:
            dish_options.remove(choices["dish"])
        if dish_options:
            w = {"__wheel__": True, "title": "🍽️ 转到哪道菜？", "scene": "dish", "options": dish_options, "ctx": choices.get("spice"), "budget": budget}
        else:
            w = json.loads(tools.build_wheel("dish", choices.get("spice", ""), budget)); dish_options = list(w["options"])
        choices["dish"] = show_wheel(w)
        card = tools.get_dish_card(choices["dish"])
        print(f"📇 {card['name']} ￥{card['price']} ｜ {card.get('desc')}")
    print("\n🔧", tools.finalize_decision(choices.get("where", ""), choices.get("spice", ""), choices.get("dish", "")))


def do_recommend():
    prof = profile_mod.load_profile()
    print(f"当前画像：校区={prof['campus']} 预算={prof['budget_cap']} 忌口={prof['dislikes']}")
    crit = input("排序侧重（value 性价比/distance 就近/rating 好评，回车=value）：").strip() or "value"
    rec = json.loads(tools.recommend_nearby_food(criteria=crit, top_n=6))
    if rec.get("count", 0) == 0:
        print("（无符合条件，放宽条件再试）"); return
    print(f"\n📍 {rec['campus']} 就近推荐（{rec['center']}）共 {rec['count']} 家：")
    for i, it in enumerate(rec["items"], 1):
        print(f"  {i}.{'🏆' if i==1 else ' '} {it['name']} ｜ {it['distance_km']}km ｜ ￥{it['avg_price']} ｜ ⭐{it['rating']} ｜ 综合{it['score']} ｜ {it.get('signature')}")
    pick = input("选哪家编号生成决定（回车跳过）：").strip()
    if pick.isdigit() and 1 <= int(pick) <= len(rec["items"]):
        it = rec["items"][int(pick) - 1]
        print("\n🔧", tools.finalize_recommend(it["name"], it.get("address",""), it["distance_km"], it["avg_price"], it["rating"], it.get("signature","")))


def do_profile():
    prof = profile_mod.load_profile()
    print("当前画像：", json.dumps(prof, ensure_ascii=False))
    print("可用字段：campus / budget_cap / taste_prefs / dislikes / dietary")
    f = input("要设置的字段：").strip()
    v = input("值（预算数字或'不限'；多值用逗号）：").strip()
    print(tools.set_profile(f, v))


def do_history():
    hist = history_mod.load_history()
    if not hist:
        print("（暂无历史）"); return
    print(f"\n📜 选择历史（共 {len(hist)} 条，最近在上）：")
    for h in reversed(hist[-15:]):
        label = h.get("dish") or h.get("shop_name") or "选择"
        print(f"  {h.get('ts')} 【{h.get('where') or '外面吃'}】{label} ￥{h.get('price','?')}")
    print("\n🧠 近期画像：", json.dumps(history_mod.summarize_recent_profile(10), ensure_ascii=False, indent=2))


def main():
    print("=" * 56)
    print("🍱 吃饱饱助手（终端演示）｜ 记忆画像 + 校区定位 + 就近推荐")
    print("=" * 56)
    while True:
        print("\n菜单：1)大转盘  2)就近推荐  3)设置画像  4)历史/近期画像  5)退出")
        c = input("选择：").strip()
        if c == "1":
            do_wheel()
        elif c == "2":
            do_recommend()
        elif c == "3":
            do_profile()
        elif c == "4":
            do_history()
        elif c == "5":
            print("再见～"); break
        else:
            print("无效。")


if __name__ == "__main__":
    main()
