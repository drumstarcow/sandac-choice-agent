"""吃饱饱助手 —— Streamlit 交互界面（路径B 演示用）

特性：
- 用户记忆画像（侧边栏）：校区定位(圆心) / 金额上限 / 口味偏好 / 忌口 / 饮食限制，跨轮持久化
- 自然语言对话（LLM 理解意图 + 解说），展示“工具调用过程”面板
- 交互式“决策大转盘”：SVG 转盘 + 转一下 / 接受 / 拒绝；转盘支持金额上限过滤
- 就近综合推荐：以校区为圆心，按 距离+性价比+评分 排序，展示店名/位置/距离/预估花销/评分/综合分
- 决策树：在哪吃 → 口味(辣/不辣/随便) → 菜品；确定前弹“美食名片”
- 危险操作（发给室友）需用户确认
- 多轮对话 + 保存会话
"""
import os
import json
import random
import datetime
import streamlit as st
from agent import ChoiceAgent
import tools
import profile as profile_mod
import history as history_mod

st.set_page_config(page_title="吃饱饱助手", page_icon="🍱", layout="wide")
st.title("🍱 吃饱饱助手")
st.caption("校园决策智能体 ｜ 自然语言 → 工具调用 → 中文反馈 ｜ 记忆画像 + 校区定位 + 就近性价比推荐 ｜ 模型：本地 Ollama（无需联网Key）")

PALETTE = ["#FF6B6B", "#FFD166", "#06D6A0", "#118AB2", "#9B5DE5",
           "#F15BB5", "#00BBF9", "#FEE440", "#FB5607", "#8338EC",
           "#3A86FF", "#FFBE0B"]

COMMON_TAGS = ["辣", "不辣", "微辣", "清真", "荤", "素", "汤", "甜", "早餐", "米饭", "面食"]


# ------------------------- 智能体开场引导（二选一） -------------------------
def onboard_have_idea():
    """用户：已经有想法了 → 引导他说出想吃什么，后续由 agent 生成性价比方案。"""
    st.session_state.messages.append({"role": "user", "content": "我已经有想法了"})
    st.session_state.history.append({"role": "user", "content": "我已经有想法了"})
    reply = "好呀！你想吃啥？告诉我菜名或口味（比如「想吃火锅」「来点不辣的」），我帮你把附近性价比最高的几套方案列出来～"
    st.session_state.messages.append({"role": "assistant", "content": reply})
    st.session_state.history.append({"role": "assistant", "content": reply})
    st.session_state.onboarded = True
    st.session_state.cur_wheel = None
    st.session_state.card = None
    st.session_state.recommend_result = None
    st.rerun()


def onboard_no_idea():
    """用户：还没想好 → “把选择权交给命运”，弹出第一层大转盘。"""
    st.session_state.messages.append({"role": "assistant", "content": "既然没想法，那不如把选择权交给命运吧！🎰 来转大转盘～"})
    st.session_state.history.append({"role": "assistant", "content": "既然没想法，那不如把选择权交给命运吧！🎰"})
    st.session_state.cur_wheel = json.loads(tools.build_wheel("where"))
    st.session_state.spin_index = 0
    st.session_state.onboarded = True
    st.session_state.card = None
    st.session_state.recommend_result = None
    st.rerun()


# ------------------------- “有想法”分支兜底（保证必有方案） -------------------------
import re as _re


def _looks_like_have_idea(text):
    """粗略判断用户是否表达了明确就餐目标（而非“没想法/随便”）。"""
    t = text.strip()
    if any(k in t for k in ["没想法", "不知道", "随便", "帮我选", "你定", "没想好", "不知道吃啥"]):
        return False
    return any(k in t for k in ["想吃", "想来", "来份", "来个", "来点", "就要", "点个", "点一份",
                                "整点", "搞点", "我想", "来碗", "来杯", "喝杯", "来只", "吃个", "吃份"])


def _extract_keyword(text):
    """从“想吃火锅”这类句子里抽取核心目标词（火锅）。"""
    t = text.strip()
    for p in ["我想吃", "想吃", "我想来", "想来", "来份", "来个", "来点", "就要", "点个", "点一份",
              "整点", "搞点", "来碗", "来杯", "喝杯", "来只", "吃个", "吃份", "我想"]:
        if t.startswith(p):
            t = t[len(p):]
            break
    t = _re.sub(r"^(一份|一个|份|个|些|吃|来|点|喝)", "", t)
    t = t.strip(" 吧呀呢～。.，,！!？?的")
    return t

# ------------------------- session_state 初始化 -------------------------
if "agent" not in st.session_state:
    st.session_state.agent = ChoiceAgent()
if "messages" not in st.session_state:
    st.session_state.messages = []
if "history" not in st.session_state:
    st.session_state.history = []
if "pending" not in st.session_state:
    st.session_state.pending = None
if "cur_wheel" not in st.session_state:
    st.session_state.cur_wheel = None
if "spin_index" not in st.session_state:
    st.session_state.spin_index = 0
if "choices" not in st.session_state:
    st.session_state.choices = {}
if "dish_options" not in st.session_state:
    st.session_state.dish_options = []
if "card" not in st.session_state:
    st.session_state.card = None
if "finalized" not in st.session_state:
    st.session_state.finalized = None
if "recommend_result" not in st.session_state:
    st.session_state.recommend_result = None
if "wheel_budget" not in st.session_state:
    st.session_state.wheel_budget = "不限"
if "onboarded" not in st.session_state:
    st.session_state.onboarded = False
# 智能体主动开场：进入即问“今天想好吃啥了不？”并引导二选一
if not st.session_state.messages:
    st.session_state.messages.append({
        "role": "assistant",
        "content": "今天想好吃啥了不？😋 选一个：① 我已经有想法了 ② 还没想好～",
    })


# ------------------------- 转盘 / 名片渲染 -------------------------
def wheel_svg(options, highlight=-1):
    n = len(options)
    if n == 0:
        return ""
    import math
    cx, cy, r = 200, 200, 170
    svg = [f'<svg viewBox="0 0 400 400" width="360" height="360" xmlns="http://www.w3.org/2000/svg">']
    svg.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#f8f9fa" stroke="#dee2e6" stroke-width="2"/>')
    angles = [2 * math.pi * i / n for i in range(n)]
    for i, opt in enumerate(options):
        a0 = angles[i] - math.pi / 2
        a1 = angles[(i + 1) % n] - math.pi / 2
        x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
        x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        large = 1 if (a1 - a0) > math.pi else 0
        color = PALETTE[i % len(PALETTE)] if i != highlight else "#222"
        svg.append(f'<path d="M{cx},{cy} L{x0:.1f},{y0:.1f} A{r},{r} 0 {large} 1 {x1:.1f},{y1:.1f} Z" fill="{color}" stroke="#fff" stroke-width="2"/>')
        mid = (a0 + a1) / 2
        lx, ly = cx + (r * 0.62) * math.cos(mid), cy + (r * 0.62) * math.sin(mid)
        svg.append(f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="15" fill="#fff" text-anchor="middle" dominant-baseline="middle" font-weight="bold">{opt}</text>')
    svg.append(f'<polygon points="{cx},20 {cx-13},2 {cx+13},2" fill="#e63946"/>')
    # 中心“转一下”按钮（视觉锚点，符合转盘中心放转动按钮的习惯）
    svg.append(f'<circle cx="{cx}" cy="{cy}" r="38" fill="#e63946" stroke="#fff" stroke-width="3"/>')
    svg.append(f'<circle cx="{cx}" cy="{cy}" r="38" fill="none" stroke="rgba(255,255,255,.5)" stroke-width="6"/>')
    svg.append(f'<text x="{cx}" y="{cy-2}" font-size="17" fill="#fff" text-anchor="middle" font-weight="bold">转一下</text>')
    svg.append(f'<text x="{cx}" y="{cy+16}" font-size="10" fill="#fff" text-anchor="middle">点这里</text>')
    svg.append('</svg>')
    return "".join(svg)


def render_steps(steps):
    if not steps:
        return
    with st.expander("🔧 工具调用过程", expanded=False):
        for s in steps:
            st.write(s)


def render_wheel():
    w = st.session_state.cur_wheel
    if not w:
        return
    opts = w["options"]
    st.subheader(w["title"])
    # 💰 金额上限：贯穿整个大转盘流程（菜品层实时过滤，前两层仅记录并传递到菜品层生效）
    budget_opts = ["不限", "10", "15", "20", "25", "30", "40"]
    cur_b = st.session_state.wheel_budget
    nb = st.selectbox("💰 金额上限（用于菜品筛选）", budget_opts,
                      index=budget_opts.index(cur_b) if cur_b in budget_opts else 0,
                      key="budget_sel")
    if nb != st.session_state.wheel_budget:
        st.session_state.wheel_budget = nb
        if w["scene"] == "dish":
            nw = json.loads(tools.build_wheel("dish", st.session_state.choices.get("spice", ""), nb))
            st.session_state.cur_wheel = nw
            st.session_state.dish_options = list(nw["options"])
            st.session_state.spin_index = 0
        st.rerun()
    # 转盘居中展示，中心是醒目的“转一下”按钮
    st.markdown(f'<div style="display:flex;justify-content:center">{wheel_svg(opts, st.session_state.spin_index)}</div>',
                unsafe_allow_html=True)
    picked = opts[st.session_state.spin_index] if opts else ""
    st.info(f"🎯 当前指向：**{picked}**")
    # “转一下”按钮放在转盘正下方居中（与中心视觉按钮呼应）
    _, center_col, _ = st.columns([1, 1.4, 1])
    with center_col:
        if st.button("🎯 转一下", type="primary", key="spin", use_container_width=True):
            st.session_state.spin_index = random.randrange(len(opts))
            st.rerun()
    c1, c2 = st.columns(2)
    with c1:
        if st.button("✅ 接受", key="accept", use_container_width=True):
            handle_accept(w, picked)
    with c2:
        if st.button("❌ 拒绝 / 换一个", key="reject", use_container_width=True):
            handle_reject(w, picked)


def handle_accept(w, picked):
    scene = w["scene"]
    if scene == "where":
        st.session_state.choices["where"] = picked
        nw = json.loads(tools.build_wheel("spice", picked))
        st.session_state.cur_wheel = nw
        st.session_state.spin_index = 0
        st.rerun()
    elif scene == "spice":
        st.session_state.choices["spice"] = picked
        nw = json.loads(tools.build_wheel("dish", picked, st.session_state.wheel_budget))
        st.session_state.cur_wheel = nw
        st.session_state.dish_options = list(nw["options"])
        st.session_state.spin_index = 0
        st.rerun()
    elif scene == "dish":
        st.session_state.choices["dish"] = picked
        st.session_state.card = tools.get_dish_card(picked)
        st.session_state.cur_wheel = None
        st.rerun()


def handle_reject(w, picked):
    scene = w["scene"]
    opts = list(w["options"])
    if picked in opts:
        opts.remove(picked)
    if scene == "dish":
        st.session_state.dish_options = opts
    if len(opts) <= 1:
        if opts:
            handle_accept({"scene": scene, "options": opts}, opts[0])
            return
        else:
            st.session_state.cur_wheel = None
            st.warning("所有选项都被拒绝了，请重新描述需求。")
            st.rerun()
    nw = dict(w)
    nw["options"] = opts
    if scene == "dish":
        nw["title"] = "🍽️ 转到哪道菜？（不喜欢就拒绝，剔掉重转）"
    st.session_state.cur_wheel = nw
    st.session_state.spin_index = 0
    st.rerun()


def render_card():
    d = st.session_state.card
    if not d:
        return
    st.subheader("📇 美食名片 · 确定前先勾起食欲")
    emoji = "🍜" if "面" in d["name"] else ("🍚" if "饭" in d["name"] else ("🥘" if "辣" not in d.get("tags", []) else "🌶️"))
    if "甜" in d.get("tags", []):
        emoji = "🍰"
    if "汤" in d.get("tags", []):
        emoji = "🍲"
    card_html = f"""
    <div style="max-width:520px;border:1px solid #eee;border-radius:16px;overflow:hidden;box-shadow:0 6px 20px rgba(0,0,0,.08)">
      <div style="height:160px;background:linear-gradient(135deg,#ff9a9e,#fad0c4);display:flex;align-items:center;justify-content:center;font-size:90px">{emoji}</div>
      <div style="padding:18px 20px">
        <div style="font-size:24px;font-weight:bold;color:#222">{d['name']} <span style="color:#e63946">￥{d['price']}</span></div>
        <div style="color:#666;margin:6px 0">📍 {d.get('canteen','')} / {d.get('window','')}</div>
        <div style="margin:8px 0">{' '.join(f'<span style="background:#06D6A0;color:#fff;border-radius:10px;padding:2px 8px;font-size:12px;margin-right:4px">{t}</span>' for t in d.get('tags',[]))}</div>
        <div style="color:#444;line-height:1.6">{d.get('desc','')}</div>
      </div>
    </div>"""
    st.markdown(card_html, unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    with c1:
        if st.button("😋 就吃这个，确定", type="primary", key="confirm_card"):
            do_finalize()
    with c2:
        if st.button("🔁 换一个", key="change_card"):
            opts = list(st.session_state.dish_options)
            if d["name"] in opts:
                opts.remove(d["name"])
            st.session_state.dish_options = opts
            st.session_state.card = None
            if opts:
                st.session_state.cur_wheel = {"__wheel__": True, "title": "🍽️ 转到哪道菜？（不喜欢就拒绝，剔掉重转）",
                                             "scene": "dish", "options": opts, "ctx": st.session_state.choices.get("spice", "")}
            else:
                st.session_state.cur_wheel = json.loads(tools.build_wheel("dish", st.session_state.choices.get("spice", ""), st.session_state.wheel_budget))
                st.session_state.dish_options = list(st.session_state.cur_wheel["options"])
            st.session_state.spin_index = 0
            st.rerun()


def do_finalize():
    ch = st.session_state.choices
    res = tools.finalize_decision(ch.get("where", ""), ch.get("spice", ""), ch.get("dish", ""))
    steps = [f"🔧 调用 finalize_decision({ch})", f"↳ {res}"]
    path = ""
    if "保存到 " in res:
        path = res.split("保存到 ", 1)[1].split("：", 1)[0].strip()
    st.session_state.finalized = {"text": res, "path": path}
    st.session_state.messages.append({"role": "assistant", "content": res, "steps": steps})
    st.session_state.history.append({"role": "assistant", "content": res})
    st.session_state.card = None
    st.rerun()


# ------------------------- 就近综合推荐 渲染 -------------------------
def run_recommend(criteria="value"):
    prof = profile_mod.load_profile()
    raw = tools.recommend_nearby_food(
        campus=prof.get("campus"),
        taste="",  # 用画像里的偏好/忌口
        budget=prof.get("budget_cap"),
        criteria=criteria,
        top_n=6,
    )
    rec = json.loads(raw)
    st.session_state.recommend_result = rec
    st.session_state.cur_wheel = None
    st.session_state.card = None
    st.rerun()


def render_recommend():
    rec = st.session_state.recommend_result
    if not rec:
        return
    if rec.get("note"):
        st.info(rec["note"])
    if rec.get("count", 0) == 0:
        return
    crit_label = {"value": "性价比综合", "distance": "就近优先", "rating": "好评优先"}.get(rec.get("criteria"), "")
    st.subheader(f"📍 就近推荐 · {rec.get('campus','')}（圆心：{rec.get('center','')}）")
    st.caption(f"排序侧重：**{crit_label}** ｜ 预算：{'不限' if rec.get('budget') in (None,'不限') else '￥'+str(rec.get('budget'))} ｜ 共 {rec.get('count')} 家")
    for idx, it in enumerate(rec["items"]):
        rank = idx + 1
        ribbon = "🏆 最佳性价比" if rank == 1 else f"#{rank}"
        bd = it.get("score_breakdown", {})
        bars = (f"距离分 {bd.get('距离分',0)} ｜ 性价比分 {bd.get('性价比分',0)} ｜ 评分分 {bd.get('评分分',0)}")
        col1, col2 = st.columns([3, 1])
        with col1:
            st.markdown(f"**{ribbon}　{it['name']}**　`{it.get('category','')}`")
            st.markdown(f"📌 {it.get('address','')}　🚶 **{it['distance_km']} km**　💰 人均 **￥{it['avg_price']}**　⭐ {it['rating']}")
            st.markdown(f"🍽️ 招牌：{it.get('signature','')}　｜　{' '.join(it.get('tags',[]))}")
            st.markdown(f"🔢 综合分 **{it['score']}**　（{bars}）")
        with col2:
            if st.button(f"📌 选这家", key=f"pick_{idx}"):
                res = tools.finalize_recommend(it["name"], it.get("address",""), it["distance_km"], it["avg_price"], it["rating"], it.get("signature",""))
                path = res.split("保存到 ",1)[1].split("：",1)[0].strip() if "保存到 " in res else ""
                st.session_state.finalized = {"text": res, "path": path}
                st.session_state.recommend_result = None
                st.rerun()


# ------------------------- 右侧：记忆画像面板 -------------------------
with st.sidebar:
    st.header("🧠 我的记忆画像")
    prof = profile_mod.load_profile()
    campuses = ["金海校区", "嘉善校区", "长寿路校区"]
    campus = st.selectbox("📍 定位校区（圆心）", campuses, index=campuses.index(prof["campus"]) if prof["campus"] in campuses else 0)
    if campus != prof["campus"]:
        profile_mod.update_profile("campus", campus)
        prof = profile_mod.load_profile()
        st.rerun()
    budget_opts = ["不限", "10", "15", "20", "25", "30", "40"]
    cur_b = "不限" if prof["budget_cap"] in (None, "", "不限") else str(int(prof["budget_cap"]))
    budget = st.selectbox("💰 金额上限", budget_opts, index=budget_opts.index(cur_b) if cur_b in budget_opts else 0)
    if budget != cur_b:
        profile_mod.update_profile("budget_cap", budget)
        st.rerun()
    taste = st.multiselect("😋 口味偏好", COMMON_TAGS, default=prof["taste_prefs"])
    if taste != prof["taste_prefs"]:
        profile_mod.update_profile("taste_prefs", ",".join(taste))
        st.rerun()
    dislikes = st.multiselect("🚫 忌口", COMMON_TAGS, default=prof["dislikes"])
    if dislikes != prof["dislikes"]:
        profile_mod.update_profile("dislikes", ",".join(dislikes))
        st.rerun()
    dietary = st.multiselect("🕌 饮食限制", ["清真"], default=prof["dietary"])
    if dietary != prof["dietary"]:
        profile_mod.update_profile("dietary", ",".join(dietary))
        st.rerun()
    st.caption("这些偏好会被智能体记住，下次推荐/转盘自动套用（本地保存，不进仓库）。")
    st.divider()

    # 选择历史 + 近期画像（从真实行为中学习）
    st.header("📜 选择历史")
    hist = history_mod.load_history()
    if not hist:
        st.caption("还没有选择记录，完成一次决定后会出现在这里。")
    else:
        rp = history_mod.summarize_recent_profile(10)
        st.markdown("**🧠 近期画像（最近 10 次）**")
        st.caption(f"最常去：{rp.get('campus') or '—'} ｜ 场景：{rp.get('where') or '—'} ｜ 口味：{rp.get('spice') or '—'} ｜ 最爱：{rp.get('favorite') or '—'}")
        st.caption(f"均预算：{'—' if rp.get('avg_budget') is None else '￥'+str(rp['avg_budget'])} ｜ 均花销：{'—' if rp.get('avg_price') is None else '￥'+str(rp['avg_price'])} ｜ 标签：{', '.join(rp.get('top_tags', [])) or '—'}")
        st.caption(f"共 {len(hist)} 条记录（不想要可删除）")
        for h in reversed(hist[-20:]):
            label = h.get("dish") or h.get("shop_name") or "选择"
            where = h.get("where") or ("外面吃" if h.get("type") == "recommend" else "—")
            rating = f" ⭐{h['rating']}" if h.get("rating") else ""
            cA, cB = st.columns([5, 1])
            with cA:
                st.markdown(f"• `{h.get('ts','')}` 【{where}】{label} ￥{h.get('price','?')}{rating}")
            with cB:
                if st.button("🗑", key=f"del_{h['id']}", help="删除这条记录"):
                    history_mod.delete_history(h["id"])
                    st.rerun()
    st.divider()

    st.header("操作")
    if st.button("💾 保存本轮会话"):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs("sessions", exist_ok=True)
        path = os.path.join("sessions", f"session_{ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st.session_state.history, f, ensure_ascii=False, indent=2)
        st.success(f"已保存到 {path}")
    if st.button("🔄 重新开始"):
        for k in ("messages", "history", "pending", "cur_wheel", "spin_index", "choices", "dish_options", "card", "finalized", "recommend_result"):
            if k in ("messages", "history"):
                st.session_state[k] = []
            elif k == "spin_index":
                st.session_state[k] = 0
            elif k == "choices":
                st.session_state[k] = {}
            elif k == "dish_options":
                st.session_state[k] = []
            else:
                st.session_state[k] = None
        st.session_state.wheel_budget = "不限"
        st.rerun()
    st.divider()
    st.caption("数据源：data/food_data.json（菜品示例）、data/shops.json + data/campus.json（周边商户示例）。"
               "替换为杉达真实数据即真实数据源。模型走本地 Ollama，无需联网 Key。")


# ------------------------- 主区：就近推荐入口（已进入对话后显示） -------------------------
if st.session_state.onboarded:
    st.markdown("### 🎯 不想转大转盘？直接要方案")
    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        if st.button("🏆 最佳性价比方案", type="primary", key="rec_value"):
            run_recommend("value")
    with c2:
        if st.button("🚶 就近优先", key="rec_dist"):
            run_recommend("distance")
    with c3:
        if st.button("⭐ 好评优先", key="rec_rate"):
            run_recommend("rating")
    st.caption("以你定位的校区为圆心，按 距离 + 性价比 + 评分 实时加权排序，显示店名/位置/距离/预估花销/评分。点「选这家」可生成决定文件。")

# ------------------------- 展示历史消息 -------------------------
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        if m.get("steps"):
            render_steps(m["steps"])
        st.write(m["content"])

# ------------------------- 开场引导：二选一（仅首次） -------------------------
if not st.session_state.onboarded:
    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        if st.button("✅ 我已经有想法了", type="primary", key="ob_have"):
            onboard_have_idea()
    with c2:
        if st.button("🎡 还没想好", key="ob_none"):
            onboard_no_idea()

# 就近推荐区
if st.session_state.recommend_result:
    render_recommend()
    if st.button("↩️ 返回对话 / 大转盘", key="back_rec"):
        st.session_state.recommend_result = None
        st.rerun()
# 转盘 / 名片区域
elif st.session_state.cur_wheel:
    render_wheel()
elif st.session_state.card:
    render_card()

# 危险操作确认卡片
if st.session_state.pending:
    p = st.session_state.pending
    st.warning(f"⚠️ 检测到危险操作：**{p['tool']}**，参数：{json.dumps(p['args'], ensure_ascii=False)}")
    st.write("这是对外发送动作，需你确认后才执行：")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("✅ 确认发送", type="primary", key="confirm_send"):
            r = st.session_state.agent.resume_after_approve(p, "", st.session_state.history)
            st.session_state.messages.append({"role": "assistant", "content": r["content"], "steps": r["steps"]})
            st.session_state.history.append({"role": "assistant", "content": r["content"]})
            st.session_state.pending = None
            st.rerun()
    with c2:
        if st.button("❌ 取消", key="cancel_send"):
            st.session_state.messages.append({"role": "assistant", "content": "已取消发送（危险操作未执行）。"})
            st.session_state.history.append({"role": "assistant", "content": "已取消发送。"})
            st.session_state.pending = None
            st.rerun()

# 最终产出物 + 发给室友
if st.session_state.finalized:
    st.success(st.session_state.finalized["text"])
    if st.session_state.finalized.get("path") and os.path.exists(st.session_state.finalized["path"]):
        with open(st.session_state.finalized["path"], "rb") as f:
            st.download_button("⬇️ 下载决定", f, file_name=os.path.basename(st.session_state.finalized["path"]))
    if st.button("📨 把决定发给室友", key="share"):
        ch = st.session_state.choices
        msg = f"今天决定在【{ch.get('where')}】吃【{ch.get('spice')}】的【{ch.get('dish')}】，一起吗？"
        r = st.session_state.agent.chat(f"把这段内容发给室友：{msg}", st.session_state.history)
        if r["status"] == "confirm":
            st.session_state.pending = r
            st.rerun()
        else:
            st.session_state.messages.append({"role": "assistant", "content": r["content"], "steps": r.get("steps", [])})
            st.session_state.history.append({"role": "assistant", "content": r["content"]})
            st.rerun()

# 输入
if prompt := st.chat_input("例如：不知道今天吃啥，帮我选 / 我选食堂 / 我选不辣 / 就吃这个 / 我在金海校区，预算20，附近吃啥"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.session_state.history.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.write(prompt)

    with st.chat_message("assistant"):
        with st.spinner("智能体思考中…"):
            r = st.session_state.agent.chat(prompt, st.session_state.history)
        if r["status"] == "confirm":
            render_steps(r.get("steps", []))
            st.session_state.pending = r
            st.session_state.messages.append({"role": "assistant", "content": "（等待你确认危险操作）", "steps": r.get("steps", [])})
            st.rerun()
        elif r["status"] == "wheel":
            render_steps(r.get("steps", []))
            st.session_state.cur_wheel = r["wheel"]
            st.session_state.spin_index = 0
            if r.get("content"):
                st.write(r["content"])
            st.session_state.messages.append({"role": "assistant", "content": r.get("content") or "来转一下大转盘吧！", "steps": r.get("steps", [])})
            st.session_state.history.append({"role": "assistant", "content": r.get("content") or ""})
            st.rerun()
        elif r["status"] == "recommend":
            render_steps(r.get("steps", []))
            st.session_state.recommend_result = r["recommend"]
            st.session_state.cur_wheel = None
            st.session_state.card = None
            if r.get("content"):
                st.write(r["content"])
            st.session_state.messages.append({"role": "assistant", "content": r.get("content") or "已为你就近推荐：", "steps": r.get("steps", [])})
            st.session_state.history.append({"role": "assistant", "content": r.get("content") or ""})
            st.rerun()
        else:
            # 兜底：用户有明确就餐目标，但模型未触发推荐工具时，确定性生成方案，
            # 保证“有想法”分支一定能给出性价比方案（不依赖小模型是否稳定发 tool call）
            if _looks_like_have_idea(prompt) and not st.session_state.cur_wheel \
                    and not st.session_state.recommend_result and not st.session_state.finalized:
                kw = _extract_keyword(prompt)
                rec = json.loads(tools.recommend_nearby_food(keyword=kw, criteria="value"))
                if rec.get("count", 0) > 0:
                    tip = "好嘞～按你的想法，附近性价比最高的几套方案 👇"
                    render_steps([f"🔧 调用 recommend_nearby_food(keyword={kw!r})"])
                    st.session_state.recommend_result = rec
                    st.session_state.cur_wheel = None
                    st.session_state.card = None
                    st.session_state.messages.append({"role": "assistant", "content": tip, "steps": [f"🔧 调用 recommend_nearby_food(keyword={kw!r})"]})
                    st.session_state.history.append({"role": "assistant", "content": tip})
                    st.rerun()
            render_steps(r.get("steps", []))
            st.write(r["content"])
            st.session_state.messages.append({"role": "assistant", "content": r["content"], "steps": r.get("steps", [])})
            st.session_state.history.append({"role": "assistant", "content": r["content"]})
