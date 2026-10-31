"""吃饱饱助手 —— Agent 核心（路径B：自然语言驱动工具调用）

使用 OpenAI 兼容客户端连接本地 Ollama，实现：
- 系统提示词（角色 + 决策树规则 + 合规边界）
- Tool Calling 循环（模型决定调哪些工具、抽哪些参数）
- 危险操作（发给室友）确认拦截
- 多轮对话（历史以文本形式维护）
- 执行过程记录（steps），供 UI 展示“工具调用过程面板”

决策树（本助手的核心价值：帮助选择困难的大学生做选择）：
  第一层 where : 食堂 / 外卖 / 外面吃
  第二层 spice : 辣 / 不辣 / 随便
  第三层 dish  : 具体菜品（按口味过滤；“随便”则全量），用户可拒绝剔除后重转
  确定前       : 界面弹出“美食名片”勾起食欲；不喜欢可换一个
  确定后       : finalize_decision 写出产出物；可 share_with_roommate（危险，需确认）
"""
import json
import os
import urllib.request
import urllib.error
from tools import TOOL_SCHEMAS, call_tool, DANGEROUS_TOOLS

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
MODEL = os.getenv("OLLAMA_MODEL", "buding-qwen05b-32k:latest")
API_KEY = os.getenv("OPENAI_API_KEY", "ollama")

SYSTEM_PROMPT = """你是「吃饱饱助手」——一个跟大学生混熟了的饭友，专门帮选择困难的人做吃饭决定。

【说话方式】像朋友，别像客服：简短、直接、不谄媚、不写“作为您的私人助手我很高兴”那种话；一次说一件事；抽到什么就说什么，不硬凑吉祥话；不知道就说不知道（比如「这家没收录」），别编。

【开场分流】同学进来你先问「今天想好吃啥了不？」并给两个出口：
- ① 已经有想法（说「想吃xx/想来xx/就要xx/来份xx」）：别转盘，直接调 recommend_nearby_food 给他列附近性价比最高的几套（距离+性价比+评分），标出🏆最佳。先一句话确认目标再调工具。
- ② 没想法（说「随便/不知道/你定/帮我选」）：来一句「既然没想法，那不如把选择权交给命运吧🎰」，然后调 build_wheel(scene="where") 弹大转盘。

【大转盘】三层：where(食堂/外卖/外面吃) → spice(辣/不辣/随便) → dish(具体菜，按口味过滤；budget 可设金额上限)。每层用对应 build_wheel 展示。第三层用户可「拒绝」不想要的，你务必明说「这个你拒过了，不会再推给你」，界面会剔掉重转（概率真的会变）。三层都拒完，别硬转，说「今天换个路子？你直接说想吃啥，或点下方要方案」。

【就近推荐】用户有想法、或问「附近吃啥/性价比高的/外面吃去哪」时调 recommend_nearby_food；以校区为圆心（get_profile 读 campus），haversine 真实算距，按距离+性价比+评分排序。criteria：默认 value(性价比综合)；强调就近用 distance；强调好评用 rating。

【记忆·读】回答前先 get_profile 读画像（校区/预算/口味/忌口/饮食限制）。也可用 get_recent_profile 读最近 10 次真实选择聚合的近期画像。

【记忆·写】用户透露偏好时：
- 校区 / 预算 / 称呼 这类不敏感设置 → 用 set_profile 直接记。
- 口味 / 忌口 / 饮食限制 → 用 save_preference，**必须先回显确认**（如「要记住你不吃辣，对吗？」）再写；用户说「忘掉不吃辣」时用 save_preference 把对应值置空。这一步是安全的可审计写操作，别跳过确认。

【危险操作】用户要把决定发给室友 → share_with_roommate，是对外动作，必须先确认再执行。

【边界·别硬拽】遇到无关话题（比如「今天好累」）接一句人话就行，别硬把话题拽回转盘；工具之外的事（写作业等）老实说「这个我不擅长」。所有数据先调工具拿，严禁编造菜品/价格/店名/距离；涉及供应提醒「数据为示例，以实际为准」。"""


# ============ 分阶段工具作用域（针对 0.5B 小模型的能力约束） ============
# 实测：把 9 个工具一次性给 0.5B，它会随手挑错工具（问“想吃火锅”却调 get_recent_profile）。
# 按对话阶段只暴露该阶段真正需要的 1~2 个工具后，模型能稳定选对工具并正确填参数
# （keyword="火锅"、budget="30"、scene="where"）。模型仍自主决定「调哪个工具、传什么参数」，
# 不是关键词 if-else——作用域只决定「这个阶段允许用哪几把工具」。
TOOL_SCOPES = {
    "opening": ["recommend_nearby_food", "build_wheel"],   # 开场分流：有想法→推荐；没想法→转盘
    "wheel":   ["build_wheel"],                            # 转盘阶段：只需要造盘
    "settle":  ["finalize_decision", "finalize_recommend"], # 定案：落盘产出物
    "settle_dish": ["finalize_decision"],                  # 菜品定案（单工具，强制调用）
    "settle_shop": ["finalize_recommend"],                 # 店铺定案（单工具，强制调用）
    "memory":  ["set_profile", "save_preference", "get_profile"],  # 记忆写入/读取
}

# 小模型专用阶段提示词：短、带 few-shot、只讲这一步要干的事
STAGE_PROMPTS = {
    "opening": """你是「吃饱饱助手」，一个帮大学生决定吃什么的饭友。说话像朋友，简短，不用敬语。
只做一件事：根据用户这句话，立刻调用一个工具，不要先聊天。

【判断规则】
- 用户说了具体想吃/想喝的东西（如"想吃火锅""来份牛肉面"）→ 调 recommend_nearby_food，keyword 只填食物名（如"火锅"）。
- 用户说没想法/随便/不知道/你定/帮我选 → 调 build_wheel，scene 填 "where"。

【示例】
用户：想吃火锅 → recommend_nearby_food(keyword="火锅")
用户：来份牛肉面 → recommend_nearby_food(keyword="牛肉面")
用户：随便 → build_wheel(scene="where")
用户：随便吧 → build_wheel(scene="where")
用户：你帮我选 → build_wheel(scene="where")
用户：不知道吃啥 → build_wheel(scene="where")

严禁自己编店名、价格、距离。""",
    "wheel": """你是「吃饱饱助手」。用户已经选了本层，你需要生成下一层转盘。
立刻调用 build_wheel，scene 填下一层：where 之后是 spice，spice 之后是 dish。
不要自己列菜品。只调用工具，不要多说话。""",
    "settle": """你是「吃饱饱助手」。用户已确定选择，立刻调用工具把结果落盘，不要反问。

【规则】
- 定的是**菜品** → finalize_decision(where=, spice=, dish=)
- 定的是**店铺** → finalize_recommend(name=, address=, distance_km=, avg_price=, rating=)

【示例】
用户：我要去食堂吃麻辣的鱼香肉丝盖饭，定了 → finalize_decision(where="食堂", spice="辣", dish="鱼香肉丝盖饭")
用户：就吃鱼香肉丝盖饭，定了 → finalize_decision(where="食堂", spice="辣", dish="鱼香肉丝盖饭")

不要重复提问，直接调工具。""",
    "settle_dish": """立刻调用 finalize_decision 工具，把这三项填进去，不要说别的话：
where = 吃饭地点（食堂/外卖/外面吃），spice = 口味（辣/不辣/随便），dish = 菜名。
从对话里找这三项，找不到就填「不限」。""",
    "settle_shop": """立刻调用 finalize_recommend 工具，把店名和人均价格填进去，不要说别的话。
name = 店名，address = 地址，distance_km = 距离，avg_price = 人均，rating = 评分。""",
    "memory": """你是「吃饱饱助手」。用户在告诉你偏好。立刻调用工具记录：
校区/预算/称呼用 set_profile；口味/忌口/饮食限制用 save_preference。""",
}


def scoped_tools(scope):
    """按阶段裁剪工具列表。scope=None 时返回全部工具。"""
    if not scope:
        return TOOL_SCHEMAS
    names = set(TOOL_SCOPES.get(scope, []))
    picked = [s for s in TOOL_SCHEMAS if s["function"]["name"] in names]
    return picked or TOOL_SCHEMAS



def _safe_json(s):
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


def extract_wheel(text_or_steps):
    """从工具结果或步骤文本中提取转盘配置 JSON（含 __wheel__ 标记）。"""
    candidates = []
    if isinstance(text_or_steps, str):
        candidates.append(text_or_steps)
    else:
        candidates = list(text_or_steps)
    for c in candidates:
        if not isinstance(c, str):
            continue
        idx = c.find('{"__wheel__"')
        if idx == -1:
            idx = c.find('{ "__wheel__"')
        if idx != -1:
            end = c.find("}", idx)
            while end != -1:
                try:
                    return json.loads(c[idx:end + 1])
                except Exception:
                    end = c.find("}", end + 1)
                    if end == -1:
                        break
    return None


def extract_recommend(text_or_steps):
    """从工具结果中提取就近推荐配置 JSON（含 __recommend__ 标记）。"""
    candidates = []
    if isinstance(text_or_steps, str):
        candidates.append(text_or_steps)
    else:
        candidates = list(text_or_steps)
    for c in candidates:
        if not isinstance(c, str):
            continue
        idx = c.find('{"__recommend__"')
        if idx == -1:
            idx = c.find('{ "__recommend__"')
        if idx != -1:
            end = c.rfind("}")
            while end > idx:
                try:
                    return json.loads(c[idx:end + 1])
                except Exception:
                    end = c.rfind("}", 0, end)
                    if end == -1:
                        break
    return None


class ChoiceAgent:
    def __init__(self):
        # 用本地 Ollama 的 OpenAI 兼容接口；temperature=0 提升工具调用可复现性
        self.model = MODEL

    def _call_llm(self, messages, tools, tool_choice="auto"):
        """调用 Ollama 的 OpenAI 兼容 /chat/completions 接口，返回原始响应 dict。

        tool_choice 可为 "auto" 或 {"type":"function","function":{"name":...}}（强制某个工具）。
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": 0,
            "stream": False,
        }
        req = urllib.request.Request(
            f"{BASE_URL}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}"}
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _parse_msg(raw):
        """从响应里取出 message（含可能为空的工具调用）。"""
        try:
            return raw["choices"][0]["message"]
        except Exception:
            return None

    def chat(self, user_text, history, force_approve=False, scope=None):
        """一轮对话。

        history: 历史消息列表，元素为 {"role": "user"/"assistant", "content": str}
        scope: 分阶段工具作用域（opening/wheel/settle/memory）。None = 全部工具。
               小模型场景下传 scope 可显著提升工具选择准确率（见 TOOL_SCOPES）。
        返回:
          {"status": "confirm", "tool": str, "args": dict, ...}  —— 需要用户确认危险操作
          {"status": "wheel", "wheel": dict, "content": str, "steps": [...]} —— 需要展示转盘
          {"status": "recommend", "recommend": dict, ...} —— 需要展示推荐表
          {"status": "done", "content": str, "steps": [...]} —— 普通结束（含 finalize 结果）
        """
        system = STAGE_PROMPTS.get(scope, SYSTEM_PROMPT) if scope else SYSTEM_PROMPT
        tools = scoped_tools(scope)
        messages = [{"role": "system", "content": system}]
        for m in history:
            messages.append({"role": m["role"], "content": m["content"]})
        messages.append({"role": "user", "content": user_text})

        steps = []
        # 该阶段只有一个工具时，直接强制 tool_choice——小模型不会「忘了调」，
        # 但仍由模型自己填参数（真 Tool Calling）。
        forced = "auto"
        if scope and len(tools) == 1:
            forced = {"type": "function", "function": {"name": tools[0]["function"]["name"]}}
        for _ in range(6):
            raw = self._call_llm(messages, tools, tool_choice=forced)
            if raw.get("error"):
                return {"status": "done", "content": f"⚠️ 调用模型出错：{raw['error']}", "steps": steps}
            msg = self._parse_msg(raw)
            if msg is None:
                return {"status": "done", "content": "（模型未返回内容）", "steps": steps}

            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                asst = {
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": [
                        {
                            "id": tc.get("id", f"call_{i}"),
                            "type": "function",
                            "function": {
                                "name": tc.get("function", {}).get("name", ""),
                                "arguments": tc.get("function", {}).get("arguments", "{}"),
                            },
                        }
                        for i, tc in enumerate(tool_calls)
                    ],
                }
                # 检查是否有未确认的危险操作
                dangerous = [tc for tc in tool_calls
                             if tc.get("function", {}).get("name") in DANGEROUS_TOOLS and not force_approve]
                if dangerous:
                    tc = dangerous[0]
                    return {
                        "status": "confirm",
                        "tool": tc.get("function", {}).get("name"),
                        "args": _safe_json(tc.get("function", {}).get("arguments", "{}")),
                        "tool_call_id": tc.get("id", "call_0"),
                        "assistant_msg": asst,
                        "messages_so_far": messages,
                    }
                messages.append(asst)
                wheel = None
                rec = None
                for tc in tool_calls:
                    name = tc.get("function", {}).get("name", "")
                    args = _safe_json(tc.get("function", {}).get("arguments", "{}"))
                    steps.append(f"🔧 调用 {name}({args})")
                    result = call_tool(name, args)
                    steps.append(f"↳ {result}")
                    messages.append({"role": "tool", "tool_call_id": tc.get("id", "call_0"), "content": result})
                    w = extract_wheel(result)
                    if w:
                        wheel = w
                    r = extract_recommend(result)
                    if r:
                        rec = r
                if rec:
                    return {"status": "recommend", "recommend": rec, "content": msg.get("content") or "", "steps": steps}
                if wheel:
                    return {"status": "wheel", "wheel": wheel, "content": msg.get("content") or "", "steps": steps}
                continue
            else:
                return {"status": "done", "content": msg.get("content") or "（模型未返回内容）", "steps": steps}

        return {"status": "done", "content": "（已达到最大推理步数，请换种说法再试）", "steps": steps}

    def resume_after_approve(self, pending, user_text, history):
        """用户确认危险操作后，从挂起点继续执行并得到最终回答。"""
        messages = pending["messages_so_far"]
        asst = pending["assistant_msg"]
        name = pending["tool"]
        args = pending["args"]
        messages.append(asst)
        steps = [f"✅ 用户已确认执行 {name}({args})"]
        result = call_tool(name, args)
        steps.append(f"↳ {result}")
        messages.append({"role": "tool", "tool_call_id": pending["tool_call_id"], "content": result})

        steps.append("💬 模型正在组织最终回复…")
        raw = self._call_llm(messages, [])
        msg = self._parse_msg(raw)
        content = (msg or {}).get("content") or "（模型未返回内容）"
        return {"status": "done", "content": content, "steps": steps}


if __name__ == "__main__":
    # 1) 不依赖 LLM 的工具自检：画像 + 就近推荐
    import tools as T
    print("== 画像写入/读取 ==")
    print(T.set_profile("campus", "金海校区"))
    print(T.set_profile("budget_cap", "20"))
    print(T.set_profile("dislikes", "辣"))
    print(T.get_profile())
    print("\n== 就近综合推荐（金海校区·预算20·忌辣·性价比综合）==")
    rec = json.loads(T.recommend_nearby_food(campus="金海校区", taste="不辣", budget="20", criteria="value", top_n=5))
    for it in rec["items"]:
        print(f"  {it['name']} | {it['distance_km']}km | ￥{it['avg_price']} | ⭐{it['rating']} | 综合分{it['score']} | {it.get('score_breakdown')}")

    # 2) 若 Ollama 可用，做一次转盘 LLM 联动（失败不影响上面结果）
    print("\n== 尝试 LLM 转盘联动（需 Ollama 运行）==")
    try:
        a = ChoiceAgent()
        hist = []
        for q in ["我不知道今天吃啥，帮我选选。", "我选食堂。", "我选不辣。"]:
            print(f"\n用户：{q}")
            r = a.chat(q, hist)
            if r["status"] == "wheel":
                print("🎡 转盘：", r["wheel"])
            elif r["status"] == "done":
                print("助手：", r["content"])
            hist.append({"role": "user", "content": q})
            hist.append({"role": "assistant", "content": r.get("content", "")})
    except Exception as e:
        print("（LLM 联动跳过：", e, "）")
