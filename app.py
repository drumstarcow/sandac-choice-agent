"""杉达选择助手·今天吃啥大转盘 —— Streamlit 交互界面（路径B 演示用）

特性：
- 自然语言对话（LLM 理解意图 + 解说），展示“工具调用过程”面板（看得见工具被调用）
- 交互式“决策大转盘”：SVG 转盘 + 转一下 / 接受 / 拒绝；拒绝后剔除选项、重排（概率均分）
- 决策树：在哪吃 → 口味(辣/不辣/随便) → 菜品；确定前弹出“美食名片”勾起食欲
- 危险操作（发给室友）需用户点击确认，演示权限控制
- 多轮对话 + 保存会话到本地
"""
import os
import json
import random
import datetime
import streamlit as st
from agent import ChoiceAgent
import tools

st.set_page_config(page_title="杉达选择助手·今天吃啥大转盘", page_icon="🎡", layout="wide")
st.title("🎡 杉达选择助手 · 今天吃啥大转盘")
st.caption("路径B·校园决策智能体 ｜ 自然语言 → 工具调用 → 中文反馈 ｜ 模型：本地 Ollama（无需联网Key）")

PALETTE = ["#FF6B6B", "#FFD166", "#06D6A0", "#118AB2", "#9B5DE5",
           "#F15BB5", "#00BBF9", "#FEE440", "#FB5607", "#8338EC",
           "#3A86FF", "#FFBE0B"]

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
    st.session_state.cur_wheel = None      # 当前转盘配置 dict
if "spin_index" not in st.session_state:
    st.session_state.spin_index = 0
if "choices" not in st.session_state:
    st.session_state.choices = {}          # {where, spice, dish}
if "dish_options" not in st.session_state:
    st.session_state.dish_options = []     # 第三层菜品池（用于拒绝后剔除重转）
if "card" not in st.session_state:
    st.session_state.card = None           # 美食名片（dict 或 None）
if "finalized" not in st.session_state:
    st.session_state.finalized = None      # 最终产出物信息


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
        # 标签
        mid = (a0 + a1) / 2
        lx, ly = cx + (r * 0.62) * math.cos(mid), cy + (r * 0.62) * math.sin(mid)
        svg.append(f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="15" fill="#fff" text-anchor="middle" dominant-baseline="middle" font-weight="bold">{opt}</text>')
    # 指针（顶部）
    svg.append(f'<polygon points="{cx},25 {cx-12},5 {cx+12},5" fill="#e63946"/>')
    svg.append(f'<circle cx="{cx}" cy="{cy}" r="26" fill="#fff" stroke="#222" stroke-width="2"/>')
    if 0 <= highlight < n:
        svg.append(f'<text x="{cx}" cy="{cy+5}" font-size="14" fill="#222" text-anchor="middle" font-weight="bold">选!</text>')
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
    col1, col2 = st.columns([1, 1])
    with col1:
        st.markdown(wheel_svg(opts, st.session_state.spin_index), unsafe_allow_html=True)
    with col2:
        st.write("**操作：**")
        if st.button("🎯 转一下", type="primary", key="spin"):
            st.session_state.spin_index = random.randrange(len(opts))
            st.rerun()
        picked = opts[st.session_state.spin_index] if opts else ""
        st.info(f"当前指向：**{picked}**")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ 接受", key="accept"):
                handle_accept(w, picked)
        with c2:
            if st.button("❌ 拒绝 / 换一个", key="reject"):
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
        nw = json.loads(tools.build_wheel("dish", picked))
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
        # 只剩一个，强制接受它（无需再转）
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
            # 从菜品池剔除当前，回到第三层转盘
            opts = list(st.session_state.dish_options)
            if d["name"] in opts:
                opts.remove(d["name"])
            st.session_state.dish_options = opts
            st.session_state.card = None
            if opts:
                st.session_state.cur_wheel = {"__wheel__": True, "title": "🍽️ 转到哪道菜？（不喜欢就拒绝，剔掉重转）",
                                             "scene": "dish", "options": opts, "ctx": st.session_state.choices.get("spice", "")}
            else:
                st.session_state.cur_wheel = json.loads(tools.build_wheel("dish", st.session_state.choices.get("spice", "")))
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


# ------------------------- 展示历史消息 -------------------------
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        if m.get("steps"):
            render_steps(m["steps"])
        st.write(m["content"])

# 转盘 / 名片区域
if st.session_state.cur_wheel:
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
            st.download_button("⬇️ 下载今日决定", f, file_name=os.path.basename(st.session_state.finalized["path"]))
    if st.button("📨 把决定发给室友", key="share"):
        msg = f"今天决定在【{st.session_state.choices.get('where')}】吃【{st.session_state.choices.get('spice')}】的【{st.session_state.choices.get('dish')}】，一起吗？"
        r = st.session_state.agent.chat(f"把这段内容发给室友：{msg}", st.session_state.history)
        if r["status"] == "confirm":
            st.session_state.pending = r
            st.rerun()
        else:
            st.session_state.messages.append({"role": "assistant", "content": r["content"], "steps": r.get("steps", [])})
            st.session_state.history.append({"role": "assistant", "content": r["content"]})
            st.rerun()

# 输入
if prompt := st.chat_input("例如：不知道今天吃啥，帮我选 / 我选食堂 / 我选不辣 / 就吃这个"):
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
        else:
            render_steps(r.get("steps", []))
            st.write(r["content"])
            st.session_state.messages.append({"role": "assistant", "content": r["content"], "steps": r.get("steps", [])})
            st.session_state.history.append({"role": "assistant", "content": r["content"]})

# 侧边栏
with st.sidebar:
    st.header("操作")
    if st.button("💾 保存本轮会话"):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = "sessions"
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"session_{ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st.session_state.history, f, ensure_ascii=False, indent=2)
        st.success(f"已保存到 {path}")
    if st.button("🔄 重新开始"):
        for k in ("messages", "history", "pending", "cur_wheel", "spin_index", "choices", "dish_options", "card", "finalized"):
            if k in ("messages", "history"):
                st.session_state[k] = []
            else:
                st.session_state[k] = None if k != "spin_index" else 0
        st.session_state.choices = {}
        st.session_state.dish_options = []
        st.session_state.spin_index = 0
        st.rerun()
    st.divider()
    st.caption("提示：数据源位于 data/food_data.json（示例），"
               "替换为杉达食堂真实菜品后即真实数据源。模型走本地 Ollama，无需联网 Key。")
