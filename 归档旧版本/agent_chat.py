"""吃饱饱助手 · 终端智能体对话（真实 LLM + 真实 Tool Calling）

这是「智能体本体」的演示入口，不是网页：
- 模型自己决定调哪个工具、传什么参数（tool_calls 由 Ollama 真实返回）
- 终端只负责展示调用过程 + 承载交互（转盘层选择 / 拒绝剔除 / 偏好确认）
- 转盘概率由 tools.WheelEngine 真实计算，拒绝后概率重排可见
- 分阶段工具作用域（TOOL_SCOPES）：只给该阶段需要的工具，小模型也能选对

用法：
  python agent_chat.py                      # 交互式
  python agent_chat.py --demo               # 自动演示
  python agent_chat.py --model qwen2.5:3b-instruct --demo
"""
import os
import sys
import json
import time
import random

# --model 必须在 import agent 之前设置（agent 在导入时读取 OLLAMA_MODEL）
_argv = sys.argv[1:]
if "--model" in _argv:
    try:
        os.environ["OLLAMA_MODEL"] = _argv[_argv.index("--model") + 1]
    except IndexError:
        pass
DEMO = "--demo" in _argv

from agent import ChoiceAgent, MODEL          # noqa: E402
import tools                                   # noqa: E402

OPENING = ("今天想好吃啥了不？😋\n"
           "  · 有想法 → 直接说「想吃火锅 / 来份牛肉面」\n"
           "  · 没想法 → 说「随便」或「不知道」，我把选择权交给命运 🎰")

DEMO_TURNS = [
    "我在金海校区，今天想吃火锅",
    "算了没想法，你帮我选吧",
]
_demo_i = 0


# ----------------------------- 输出辅助 -----------------------------
def hr(ch="─", n=68):
    print(ch * n)


def say(who, text):
    print(f"\n{who}：{text}")


def trace(steps):
    """打印模型真实发起的工具调用链条。"""
    if not steps:
        return
    print("\n  ┌─ 工具调用过程 " + "─" * 48)
    for s in steps:
        s2 = s if len(s) <= 320 else s[:320] + " …"
        print("  │ " + s2.replace("\n", " "))
    print("  └" + "─" * 62)


def ask(prompt, kind="chat"):
    global _demo_i
    if DEMO:
        if kind == "chat":
            ans = DEMO_TURNS[_demo_i] if _demo_i < len(DEMO_TURNS) else ""
            _demo_i += 1
        elif kind == "wheel":
            ans = random.choice(["1", "1", "r"])
        elif kind == "card":
            ans = "y"
        else:
            ans = ""
        print(f"{prompt}{ans}")
        time.sleep(0.15)
        return ans
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


# ----------------------------- 转盘层 -----------------------------
def show_wheel(w, engine):
    opts = list(w["options"])
    scene = w.get("scene", "?")
    probs = engine.probabilities(scene, opts)
    hr()
    print(f"🎡 {w['title']}   共 {len(opts)} 项")
    hr()
    for i, o in enumerate(opts, 1):
        bar = "█" * max(0, int(round(probs.get(o, 0) * 26)))
        print(f"  {i:>2}. {o:<12} {probs.get(o, 0) * 100:5.1f}%  {bar}")
    hr()
    print("  编号=选它 ｜ r=随机转（按权重） ｜ -名称=拒绝剔除（概率会重排）")
    return opts, scene


def spin(engine, scene, opts):
    probs = engine.probabilities(scene, opts)
    r, acc, pick = random.random(), 0.0, opts[-1]
    for o in opts:
        acc += probs.get(o, 0)
        if r <= acc:
            pick = o
            break
    return pick


def run_wheel(w, engine):
    """跑完某一层转盘，返回用户接受（或被拒绝后剩下的）选项。"""
    while True:
        opts, scene = show_wheel(w, engine)
        if not opts:
            print("  （本层没有可用选项了）")
            return ""
        raw = ask("你的选择：", kind="wheel")
        if raw.lower() == "r":
            pick = spin(engine, scene, opts)
            engine.decay(scene, pick)          # 抽中衰减
            print(f"\n  🎯 转盘指向：{pick}   （该项权重 ×0.6，下次更难被抽到）")
            yn = ask("  接受吗？(y=接受 / 其他=再转)：", kind="card")
            if yn.lower() in ("y", "是", ""):
                return pick
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(opts):
            return opts[int(raw) - 1]
        if raw in opts:
            return raw
        if raw.startswith("-") and raw[1:] in opts:
            bad = raw[1:]
            engine.reject(scene, bad)          # 拒绝清零
            w = dict(w)
            w["options"] = [o for o in opts if o != bad]
            print(f"\n  ❌ 已拒绝「{bad}」——权重清零，本会话不会再推给你")
            if len(w["options"]) <= 1:
                print("  （只剩一项，直接定为它）")
                return w["options"][0] if w["options"] else ""
            continue
        print("  看不懂，再试一次。")


def show_card(name):
    card = tools.get_dish_card(name)
    hr()
    print(f"📇 美食名片 · {card.get('name')}   ￥{card.get('price')}")
    print(f"   📍 {card.get('canteen', '')} {card.get('window', '')}")
    print(f"   🏷️ {' / '.join(card.get('tags', []))}")
    print(f"   {card.get('desc', '')}")
    hr()


def show_recommend(rec):
    items = rec.get("items", [])
    hr()
    print(f"📍 {rec.get('campus', '')} 就近推荐 · 共 {rec.get('count', len(items))} 家")
    hr()
    for i, it in enumerate(items, 1):
        tag = "🏆" if i == 1 else f"#{i}"
        print(f"  {tag:<3} {it['name']:<12} {it['distance_km']:>5}km  ￥{it['avg_price']:<4} "
              f"⭐{it['rating']:<4} 综合 {it['score']}  {it.get('signature', '')}")
    if rec.get("note"):
        print(f"\n  ℹ️  {rec['note']}")
    hr()


# ----------------------------- 主流程 -----------------------------
def main():
    hr("═")
    print("🍱 吃饱饱助手 · 终端智能体（真实 LLM + 真实工具调用）")
    print(f"   模型：{MODEL}   ｜   工具总数：{len(tools.TOOL_SCHEMAS)} 个（按阶段裁剪暴露）")
    if DEMO:
        print("   模式：自动演示 --demo")
    hr("═")

    agent = ChoiceAgent()
    engine = tools.WheelEngine()
    history = []
    say("吃饱饱助手", OPENING)
    history.append({"role": "assistant", "content": OPENING})

    while True:
        text = ask("\n你：", kind="chat")
        if not text:
            print("\n（结束）")
            break

        t0 = time.time()
        # 开场分流只喂「用户自己说过的话」，不喂前文那大段开场白/回复——
        # 0.5B 会被冗长的 assistant 文本带偏而忘记调工具。
        user_hist = [m for m in history if m.get("role") == "user"]
        r = agent.chat(text, user_hist[-4:], scope="opening")
        cost = time.time() - t0
        trace(r.get("steps", []))
        print(f"  ⏱  {cost:.1f}s")

        # 危险操作
        if r.get("status") == "confirm":
            print(f"\n⚠️ 对外动作：{r['tool']}({json.dumps(r['args'], ensure_ascii=False)})")
            if ask("  执行？(y/n)：", kind="card").lower() == "y":
                rr = agent.resume_after_approve(r, text, history)
                trace(rr.get("steps", []))
                say("吃饱饱助手", rr.get("content", ""))
                history.append({"role": "assistant", "content": rr.get("content", "")})
            else:
                print("  已取消")
            continue

        # 就近推荐
        if r.get("status") == "recommend":
            if r.get("content"):
                say("吃饱饱助手", r["content"])
            show_recommend(r["recommend"])
            history.append({"role": "assistant", "content": r.get("content", "")})
            continue

        # 转盘
        if r.get("status") == "wheel":
            w = r["wheel"]
            if r.get("content"):
                say("吃饱饱助手", r["content"])
            history.append({"role": "assistant", "content": r.get("content", "")})
            choices = {}

            while w:
                picked = run_wheel(w, engine)
                if not picked:
                    break
                print(f"\n  ✅ 本层定为：{picked}")
                history.append({"role": "user", "content": picked})
                scene = w.get("scene")
                choices[scene] = picked

                # 菜品层：出名片 → 定案
                if scene == "dish":
                    show_card(picked)
                    if ask("就吃这个？(y=确定 / 其他=换一个)：", kind="card").lower() in ("y", "是", ""):
                        ctx = (f"where={choices.get('where', '不限')}，"
                               f"spice={choices.get('spice', '不限')}，dish={picked}，定了")
                        r2 = agent.chat(ctx, [], scope="settle_dish")
                        trace(r2.get("steps", []))
                        say("吃饱饱助手", r2.get("content", ""))
                        history.append({"role": "assistant", "content": r2.get("content", "")})
                        w = None
                    else:
                        opts = [o for o in w["options"] if o != picked]
                        engine.reject("dish", picked)
                        w = dict(w, options=opts) if opts else json.loads(
                            tools.build_wheel("dish", w.get("ctx", ""), "不限"))
                    continue

                # 下一层：让模型自己调 build_wheel（真 agency）
                nxt = {"where": "spice", "spice": "dish"}.get(scene)
                if not nxt:
                    w = None
                    continue
                r2 = agent.chat(f"我选{picked}", history, scope="wheel")
                trace(r2.get("steps", []))
                if r2.get("status") == "wheel":
                    w = r2["wheel"]
                else:
                    # 小模型没接上：由编排层发起同一把工具的调用（工具仍真实执行、结果来自数据文件）
                    prof = json.loads(tools.get_profile()).get("profile", {})
                    args = {"scene": nxt, "preference": picked, "budget": prof.get("budget_cap", "不限")}
                    res = tools.call_tool("build_wheel", args)
                    trace([f"[工具] 调用 build_wheel({args})", f"↳ {res}"])
                    print("  （这一步由编排层发起：小模型未自主接续，工具调用与结果仍是真实的）")
                    w = json.loads(res)
            continue

        # 普通回复
        say("吃饱饱助手", r.get("content", ""))
        history.append({"role": "assistant", "content": r.get("content", "")})
        if DEMO and _demo_i >= len(DEMO_TURNS):
            print("\n（演示剧本已跑完）")
            break


if __name__ == "__main__":
    main()
