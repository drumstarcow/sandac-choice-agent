"""吃饱饱助手 —— Agent 核心（自然语言驱动工具调用）

- 模型后端可切换：默认云（DeepSeek），无 key / 断网自动回退本地 Ollama（见 config.py）
- 真实 Tool Calling：由模型自己决定调哪个工具、传什么参数
- 分阶段工具作用域（TOOL_SCOPES）：小模型一次只看到本阶段要用的 1~3 个工具，显著提升正确率
- 危险操作（发给室友 / 写长期记忆）需用户确认
- 返回结构化 tool_calls（不是从 emoji 里猜），供界面渲染「工具调用」卡片
"""
import json
import urllib.error
import urllib.request

import config
from tools import DANGEROUS_TOOLS, TOOL_SCHEMAS, call_tool

SYSTEM_PROMPT = """你是「吃饱饱助手」——一个跟大学生混熟了的饭友，专门帮选择困难的人做吃饭决定。

【说话方式】像朋友，别像客服：简短、直接、不谄媚、不写“作为您的私人助手我很高兴”那种话；一次说一件事；抽到什么就说什么，不硬凑吉祥话；不知道就说不知道（比如「这家没收录」），别编。

【开场分流】同学进来你先问「今天想好要吃啥了不？」并给两个出口：
- ① 已经有想法（说「想吃xx/想来xx/就要xx/来份xx」）：别转盘，直接调 search_dish(keyword) 点名直达，给他四条并列方案（综合最佳/最近/最便宜/评分最高）。
- ② 没想法（说「随便/不知道/你定/帮我选」）：来一句「既然没想法，那不如把选择权交给命运吧🎰」，然后调 build_wheel(scene="meal") 从「餐别」起盘。

【四层转盘】meal(正餐/甜品饮料/随便) → where(食堂/外卖/外面吃) → spice(辣/不辣/随便) → dish(具体菜，按餐别+口味+预算过滤)。每层用对应 build_wheel 展示。第三层用户可「拒绝」不想要的，你务必明说「这个你拒过了，不会再推给你」，界面会剔掉重转（概率真的会变）。

【就近推荐】用户问「附近吃啥/性价比高的/外面吃去哪」时调 recommend_nearby_food；以校区为圆心（get_profile 读 campus），haversine 真实算距。

【记忆·读】回答前先 get_profile 读画像（校区/预算/口味/忌口/饮食限制）。也可用 get_recent_profile 读最近 10 次真实选择聚合的近期画像。

【记忆·写】用户透露偏好时：
- 校区 / 预算 / 称呼 这类不敏感设置 → 用 set_profile 直接记。
- 口味 / 忌口 / 饮食限制 → 用 save_preference，**必须先回显确认**（如「要记住你不吃辣，对吗？」）再写；用户说「忘掉不吃辣」时用 save_preference 把对应值置空。

【危险操作】用户要把决定发给室友 → share_with_roommate，是对外动作，必须先确认再执行。

【边界·别硬拽】遇到无关话题（比如「今天好累」）接一句人话就行。所有数据先调工具拿，严禁编造菜品/价格/店名/距离；涉及供应提醒「数据为示例，以实际为准」。"""


# ============ 分阶段工具作用域（针对小模型的能力约束） ============
# 实测：把全部工具一次性给 0.5B，它会随手挑错工具。按对话阶段只暴露该阶段真正需要的
# 1~3 个工具后，模型能稳定选对工具并正确填参数。模型仍自主决定「调哪个工具、传什么参数」，
# 不是关键词 if-else——作用域只决定「这个阶段允许用哪几把工具」。
TOOL_SCOPES = {
    "opening": ["search_dish", "recommend_nearby_food", "build_wheel"],
    "wheel": ["build_wheel"],
    "settle": ["finalize_decision", "finalize_recommend"],
    "settle_dish": ["finalize_decision"],
    "settle_shop": ["finalize_recommend"],
    "memory": ["set_profile", "save_preference", "get_profile"],
}

STAGE_PROMPTS = {
    "opening": """你是「吃饱饱助手」，一个帮大学生决定吃什么的饭友。说话像朋友，简短，不用敬语。
只做一件事：根据用户这句话，立刻调用一个工具，不要先聊天。

【判断规则】
- 用户说了具体想吃/想喝的东西（如"想吃火锅""来份牛肉面"）→ 调 search_dish，keyword 只填食物名（如"火锅"）。
- 用户问"附近有啥""性价比高的"但没点具体东西 → 调 recommend_nearby_food。
- 用户说没想法/随便/不知道/你定/帮我选 → 调 build_wheel，scene 填 "meal"。

【示例】
用户：想吃火锅 → search_dish(keyword="火锅")
用户：来份牛肉面 → search_dish(keyword="牛肉面")
用户：附近有啥便宜的 → recommend_nearby_food(criteria="value")
用户：随便 → build_wheel(scene="meal")
用户：不知道吃啥 → build_wheel(scene="meal")

严禁自己编店名、价格、距离。""",
    "wheel": """你是「吃饱饱助手」。用户已经选了本层，你需要生成下一层转盘。
立刻调用 build_wheel，scene 填下一层：meal 之后是 where，where 之后是 spice，spice 之后是 dish。
不要自己列菜品。只调用工具，不要多说话。""",
    "settle": """你是「吃饱饱助手」。用户已确定选择，立刻调用工具把结果落盘，不要反问。

【规则】
- 定的是**菜品** → finalize_decision(where=, spice=, dish=)
- 定的是**店铺** → finalize_recommend(name=, address=, distance_km=, avg_price=, rating=)

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
    if isinstance(s, dict):
        return s
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


def _extract_marked(text_or_list, marker):
    """从工具结果里提取带 marker 的 JSON（如 __wheel__ / __recommend__）。"""
    cands = [text_or_list] if isinstance(text_or_list, str) else list(text_or_list or [])
    key = '{"' + marker + '"'
    for c in cands:
        if not isinstance(c, str):
            continue
        idx = c.find(key)
        if idx == -1:
            continue
        end = c.rfind("}")
        while end > idx:
            try:
                return json.loads(c[idx:end + 1])
            except Exception:
                end = c.rfind("}", idx, end)
                if end == -1:
                    break
    return None


def extract_wheel(text_or_steps):
    return _extract_marked(text_or_steps, "__wheel__")


def extract_recommend(text_or_steps):
    return _extract_marked(text_or_steps, "__recommend__") or _extract_marked(text_or_steps, "__four__")


class ChoiceAgent:
    def __init__(self):
        self.model = config.current().model
        self.last_error = ""
        self.switched_to_local = False

    # ------------------------- 底层调用 -------------------------
    def _call_llm(self, messages, tools, tool_choice="auto"):
        """调用当前后端的 OpenAI 兼容 /chat/completions。云失败自动切本地并重试一次。"""
        for attempt in (0, 1):
            bk = config.current()
            self.model = bk.model
            payload = {
                "model": bk.model,
                "messages": messages,
                "temperature": 0,
                "stream": False,
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = tool_choice
            req = urllib.request.Request(
                f"{bk.base_url}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {bk.api_key}"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "ignore")[:300]
                self.last_error = f"HTTP {e.code}: {detail}"
                # 云端 key 无效/欠费 → 切本地再来一次
                if bk.is_cloud and attempt == 0:
                    if config.switch_local(f"云端调用失败（HTTP {e.code}）"):
                        self.switched_to_local = True
                        continue
                return {"error": self.last_error}
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                if bk.is_cloud and attempt == 0:
                    if config.switch_local("网络不可用"):
                        self.switched_to_local = True
                        continue
                return {"error": self.last_error}
        return {"error": self.last_error or "unknown"}

    @staticmethod
    def _parse_msg(raw):
        try:
            return raw["choices"][0]["message"]
        except Exception:
            return None

    def one_shot(self, prompt, system=None, max_tokens=None):
        """不带工具的纯文本生成（用于一句打卡文案这类小任务）。失败返回 ""。"""
        msgs = [{"role": "system", "content": system or "你是「吃饱饱助手」，说话像朋友，简短有画面感。"},
                {"role": "user", "content": prompt}]
        raw = self._call_llm(msgs, [])
        if raw.get("error"):
            return ""
        msg = self._parse_msg(raw) or {}
        return (msg.get("content") or "").strip()

    # ------------------------- 主对话 -------------------------
    def chat(self, user_text, history, force_approve=False, scope=None):
        """一轮对话。

        返回 dict（除 status/content 外，关键是结构化的 calls）：
          {"status": "confirm", "tool", "args", "calls": [...]}
          {"status": "wheel", "wheel": {...}, "calls": [...]}
          {"status": "recommend", "recommend": {...}, "calls": [...]}
          {"status": "done", "content", "calls": [...]}
        其中 calls = [{"name","args","result"}]，是真实的 tool_calls 执行记录，
        **不是**从步骤文本里的 emoji 推断出来的。
        """
        system = STAGE_PROMPTS.get(scope, SYSTEM_PROMPT) if scope else SYSTEM_PROMPT
        tools = scoped_tools(scope)
        messages = [{"role": "system", "content": system}]
        for m in history:
            if m.get("role") in ("user", "assistant") and m.get("content"):
                messages.append({"role": m["role"], "content": m["content"]})
        messages.append({"role": "user", "content": user_text})

        steps, calls = [], []
        forced = "auto"
        if scope and len(tools) == 1:
            # 单工具阶段强制 tool_choice：小模型不会「忘了调」，但参数仍由模型自己填
            forced = {"type": "function", "function": {"name": tools[0]["function"]["name"]}}

        for _ in range(6):
            raw = self._call_llm(messages, tools, tool_choice=forced)
            if raw.get("error"):
                return {"status": "error", "content": "", "steps": steps, "calls": calls,
                        "error": raw["error"]}
            msg = self._parse_msg(raw)
            if msg is None:
                return {"status": "error", "content": "", "steps": steps, "calls": calls,
                        "error": "模型未返回 choices"}

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return {"status": "done", "content": (msg.get("content") or "").strip(),
                        "steps": steps, "calls": calls, "tool_calls": 0}

            asst = {
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": [
                    {
                        "id": tc.get("id") or f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": tc.get("function", {}).get("name", ""),
                            "arguments": tc.get("function", {}).get("arguments", "{}"),
                        },
                    }
                    for i, tc in enumerate(tool_calls)
                ],
            }

            dangerous = [tc for tc in tool_calls
                         if tc.get("function", {}).get("name") in DANGEROUS_TOOLS and not force_approve]
            if dangerous:
                tc = dangerous[0]
                name = tc.get("function", {}).get("name")
                args = _safe_json(tc.get("function", {}).get("arguments", "{}"))
                return {
                    "status": "confirm", "tool": name, "args": args,
                    "tool_call_id": tc.get("id") or "call_0",
                    "assistant_msg": asst, "messages_so_far": messages,
                    "content": (msg.get("content") or "").strip(),
                    "steps": steps, "calls": calls, "tool_calls": len(tool_calls),
                }

            messages.append(asst)
            wheel = rec = None
            for tc in tool_calls:
                name = tc.get("function", {}).get("name", "")
                args = _safe_json(tc.get("function", {}).get("arguments", "{}"))
                result = call_tool(name, args)
                calls.append({"name": name, "args": args, "result": result})
                steps.append(f"🔧 调用 {name}({args})")
                steps.append(f"↳ {result}")
                messages.append({"role": "tool", "tool_call_id": tc.get("id") or f"call_{len(calls)}",
                                 "content": result})
                w = extract_wheel(result)
                if w:
                    wheel = w
                r = extract_recommend(result)
                if r:
                    rec = r

            base = {"content": (msg.get("content") or "").strip(), "steps": steps,
                    "calls": calls, "tool_calls": len(tool_calls)}
            if rec:
                return {"status": "recommend", "recommend": rec, **base}
            if wheel:
                return {"status": "wheel", "wheel": wheel, **base}
            # 工具执行完但没有需要展示的产出 → 让模型继续（最多 6 轮）
            continue

        return {"status": "done", "content": "（已达到最大推理步数，请换种说法再试）",
                "steps": steps, "calls": calls, "tool_calls": len(calls)}

    def resume_after_approve(self, pending, user_text, history):
        """用户确认危险操作后，从挂起点继续执行并得到最终回答。"""
        messages = pending["messages_so_far"] + [pending["assistant_msg"]]
        name, args = pending["tool"], pending["args"]
        result = call_tool(name, args)
        messages.append({"role": "tool", "tool_call_id": pending["tool_call_id"], "content": result})
        raw = self._call_llm(messages, [])
        msg = self._parse_msg(raw) or {}
        return {"status": "done", "content": (msg.get("content") or "").strip(),
                "steps": [f"✅ 用户已确认执行 {name}({args})", f"↳ {result}"],
                "calls": [{"name": name, "args": args, "result": result}]}


if __name__ == "__main__":
    import tools as T
    config.announce()
    a = ChoiceAgent()
    print(a.chat("想吃火锅", [], scope="opening")["status"])
    print(T.build_wheel("meal"))
