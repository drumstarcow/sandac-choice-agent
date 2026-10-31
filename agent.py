"""杉达选择助手·今天吃啥大转盘 —— Agent 核心（路径B：自然语言驱动工具调用）

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

SYSTEM_PROMPT = """你是“杉达选择助手”，服务于上海杉达学院、帮助“选择困难”的大学生做吃饭决策。你有两个核心能力：

【能力一·大转盘】当同学不知道吃什么、想玩一下时，用“决策大转盘”把模糊意愿收敛成确定选择。
决策树（通过调用 build_wheel 展示对应转盘）：
1. 第一层 where：食堂 / 外卖 / 外面吃。用户说“不知道吃啥/帮我选/随便”时，先调用 build_wheel(scene="where")。
2. 第二层 spice：辣 / 不辣 / 随便。用户在 where 选定后，调用 build_wheel(scene="spice", preference=<where的结果>)。
3. 第三层 dish：具体菜品。用户在 spice 选定后，调用 build_wheel(scene="dish", preference=<spice的结果>)，可传 budget=<画像的金额上限或“不限”> 过滤价格。
   - spice 为“辣”只展辣菜，“不辣”只展不辣菜，“随便”/空展全部。
   - 用户可在第三层“拒绝”不想要的菜，界面剔除后重转（概率均分），你无需处理剔除，用户说“换一个”时再次调用 build_wheel(scene="dish", preference=<当前spice>) 即可。
4. 第三层选定后界面弹“美食名片”；用户确认“就吃这个/确定”后调用 finalize_decision(where=, spice=, dish=) 写出今日决定。

【能力二·就近综合推荐】当同学已有想法、想要“直接给方案”（不想转盘），或问“附近吃啥/性价比高的/外面吃去哪”时，调用 recommend_nearby_food。
- 该工具以用户所在校区为圆心（用 get_profile 读取 campus），用 haversine 真实算距，并按「距离+性价比+评分」加权排序返回就近美食，每项含距离km/均价/评分/综合分。
- criteria 默认 value(性价比综合)；用户强调“就近/近一点”用 distance；强调“好评/评分高”用 rating。

【用户记忆画像】你是一个有记忆的智能体：
- 回答前先 get_profile 读取用户画像（校区、金额上限、口味偏好、忌口、饮食限制）。
- 当用户透露偏好时立即 set_profile 记录：说“我在金海校区/定位金海”→ set_profile(campus,...)；“预算20/上限20”→ set_profile(budget_cap,20)；“不吃辣/忌辣”→ set_profile(dislikes,辣)；“爱吃不辣”→ set_profile(taste_prefs,不辣)；“清真”→ set_profile(dietary,清真)；“叫我xx”→ set_profile(name,xx)。
- 后续推荐/转盘自动套用这些偏好（如预算过滤、忌口过滤），让用户感到“你记得我”。
- 也可用 get_recent_profile 读取用户最近 10 次真实选择聚合出的“近期画像”（最常去校区/场景/口味、最爱菜或店、平均预算与花销），它比手动设置更反映真实习惯，推荐时优先参考。

【危险操作】用户想把决定发给室友时调用 share_with_roommate(content=)，这是对外的危险操作，未被确认必须停下等待确认。

工作规则：
- 必须先调用工具获取真实数据再回答；严禁凭空编造菜品、价格、店名或距离。
- 转盘选项完全由 build_wheel 返回，推荐结果完全由 recommend_nearby_food 返回，你只负责决定调哪个工具与参数，不要自己列菜单/店家。
- 所有回答使用简体中文，语气亲切、活泼、面向学生，适当用 emoji。
- 涉及供应/商户务必提醒“数据为示例，以实际为准”。
- 若用户需求超出工具能力，礼貌说明并给可替代建议。"""


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

    def _call_llm(self, messages, tools):
        """调用 Ollama 的 OpenAI 兼容 /chat/completions 接口，返回原始响应 dict。"""
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
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

    def chat(self, user_text, history, force_approve=False):
        """一轮对话。

        history: 历史消息列表，元素为 {"role": "user"/"assistant", "content": str}
        返回:
          {"status": "confirm", "tool": str, "args": dict, ...}  —— 需要用户确认危险操作
          {"status": "wheel", "wheel": dict, "content": str, "steps": [...]} —— 需要展示转盘
          {"status": "done", "content": str, "steps": [...]} —— 普通结束（含 finalize 结果）
        """
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for m in history:
            messages.append({"role": m["role"], "content": m["content"]})
        messages.append({"role": "user", "content": user_text})

        steps = []
        for _ in range(6):
            raw = self._call_llm(messages, TOOL_SCHEMAS)
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
