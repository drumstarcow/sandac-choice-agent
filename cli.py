"""杉达选择助手·今天吃啥大转盘 —— 终端演示脚本（路径B 备用演示入口）

适用于：教师在课堂上要求“打开终端看执行过程”时，或没有浏览器环境的快速演示。
危险操作（发给室友）在这里会先请求确认，确认后才执行。
决策树：在哪吃 → 口味(辣/不辣/随便) → 菜品（可拒绝剔除重转）→ 美食名片 → 确定。
"""
import json
import tools
from agent import ChoiceAgent

NEXT_SCENE = {"where": "spice", "spice": "dish", "dish": None}


def show_wheel(w, agent, history):
    """在终端展示一个转盘并让用户选择；返回 (scene, choice)。"""
    opts = w["options"]
    print(f"\n🎡 {w['title']}")
    for i, o in enumerate(opts):
        print(f"  {i+1}. {o}")
    while True:
        raw = input("请输入编号或名称（输入 r 随机转一下）：").strip()
        if raw.lower() == "r":
            import random
            idx = random.randrange(len(opts))
            print(f"  🎯 转盘指向：{opts[idx]}")
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(opts):
            return w["scene"], opts[int(raw) - 1]
        if raw in opts:
            return w["scene"], raw
        # 拒绝式：用户输入 x- 表示拒绝某选项
        if raw.startswith("-") and raw[1:] in opts:
            opts.remove(raw[1:])
            if not opts:
                print("全部被拒绝，重新开始该层。")
                return show_wheel(json.loads(tools.build_wheel(w["scene"], w.get("ctx", ""))), agent, history)
            print("已剔除，剩余：", opts)
            continue
        print("输入无效，请重试。")


def main():
    a = ChoiceAgent()
    hist = []
    print("=" * 56)
    print("🎡 杉达选择助手 · 今天吃啥大转盘（终端演示，危险操作需确认）")
    print("输入需求回车执行；输入 exit 退出。")
    print("=" * 56)
    choices = {}
    dish_options = []

    # 第一轮：让 LLM 理解意图并给出第一层转盘
    first = input("\n你：").strip()
    if first.lower() in ("exit", "quit", "退出"):
        return
    r = a.chat(first, hist)
    if r["status"] == "confirm":
        print("[需确认]", r["tool"], r["args"])
        r = a.resume_after_approve(r, first, hist)
    wheel = r.get("wheel") or (json.loads(tools.build_wheel("where")) if r["status"] != "wheel" else None)
    if not wheel:
        print("助手：", r.get("content"))
        return
    # 后续层级：状态机确定性推进（保证演示稳定）
    scene, choice = show_wheel(wheel, a, hist)
    choices[scene] = choice
    while NEXT_SCENE.get(scene):
        nxt = NEXT_SCENE[scene]
        w = json.loads(tools.build_wheel(nxt, choice))
        if nxt == "dish":
            dish_options = list(w["options"])
        scene, choice = show_wheel(w, a, hist)
        choices[scene] = choice

    # 美食名片
    card = tools.get_dish_card(choices["dish"])
    print("\n📇 美食名片")
    print(f"  {card['name']} ￥{card['price']} ｜ {card.get('canteen')}/{card.get('window')} ｜ {','.join(card.get('tags',[]))}")
    print(f"  {card.get('desc')}")
    ok = input("就吃这个吗？(y 确定 / n 换一个)：").strip().lower()
    while ok == "n":
        if choices["dish"] in dish_options:
            dish_options.remove(choices["dish"])
        if dish_options:
            w = {"__wheel__": True, "title": "🍽️ 转到哪道菜？", "scene": "dish", "options": dish_options, "ctx": choices.get("spice")}
        else:
            w = json.loads(tools.build_wheel("dish", choices.get("spice", "")))
            dish_options = list(w["options"])
        scene, choice = show_wheel(w, a, hist)
        choices["dish"] = choice
        card = tools.get_dish_card(choices["dish"])
        print(f"\n📇 美食名片：{card['name']} ￥{card['price']} ｜ {card.get('desc')}")
        ok = input("就吃这个吗？(y/n)：").strip().lower()

    # 确定 → 产出物
    res = tools.finalize_decision(choices.get("where", ""), choices.get("spice", ""), choices.get("dish", ""))
    print("\n🔧 调用 finalize_decision →", res)

    # 可选：发给室友（危险，确认）
    if input("\n把决定发给室友？(y/n)：").strip().lower() == "y":
        msg = f"今天决定在【{choices.get('where')}】吃【{choices.get('spice')}】的【{choices.get('dish')}】，一起吗？"
        rr = a.chat(f"把这段内容发给室友：{msg}", hist)
        if rr["status"] == "confirm":
            if input("确认发送？(y/n)：").strip().lower() == "y":
                rr = a.resume_after_approve(rr, msg, hist)
        print("结果：", rr.get("content"))


if __name__ == "__main__":
    main()
