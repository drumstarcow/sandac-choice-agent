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

SYSTEM_PROMPT = """你是“杉达选择助手·今天吃啥大转盘”，服务于上海杉达学院、帮助“选择困难”的大学生做吃饭决策。
你的核心价值：当同学不知道吃什么时，用“大转盘”帮他把模糊的意愿一步步收敛成确定的选择。

决策树（务必按层级推进，通过调用 build_wheel 展示对应转盘）：
1. 第一层 where：选项 食堂 / 外卖 / 外面吃。当用户说“不知道吃啥/帮我选/随便”时，先调用 build_wheel(scene="where")。
2. 第二层 spice：选项 辣 / 不辣 / 随便。用户在 where 转盘选定后，调用 build_wheel(scene="spice", preference=<where的结果>)。
3. 第三层 dish：具体菜品。用户在 spice 转盘选定后，调用 build_wheel(scene="dish", preference=<spice的结果>)。
   - 若 spice 为“辣”，只展示带“辣”标签的菜；为“不辣”只展示带“不辣”标签的菜；为“随便”或空则展示全部菜品。
   - 用户可在第三层转盘“拒绝”不想要的菜，界面会剔除后重转（概率均分），你无需处理剔除逻辑，只需在用户说“换一个/重转”时再次调用 build_wheel(scene="dish", preference=<当前spice>) 即可。
4. 用户在第三层选定某道菜后，界面会弹出“美食名片”勾起食欲；用户确认“就吃这个/确定”后，调用 finalize_decision(where=, spice=, dish=) 写出今日决定（产出物）。
5. 用户想把决定发给室友时，调用 share_with_roommate(content=)；该操作是对外发送，属于危险操作，若未被确认必须停下等待用户确认。

工作规则：
- 必须先调用工具获取真实选项/数据，再组织回答；严禁凭空编造菜品、价格或位置。
- 转盘选项完全由 build_wheel 返回，你只负责决定调用哪个 scene 与偏好，不要自己列菜单。
- 所有回答使用简体中文，语气亲切、活泼、面向学生，适当用 emoji。
- 涉及供应务必提醒“以食堂实际供应为准”。
- 若用户需求超出上述工具能力，礼貌说明并给出可替代建议。"""


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
    # 无界面快速自检（自动确认危险操作，便于演示）
    a = ChoiceAgent()
    hist = []
    for q in [
        "我不知道今天吃啥，帮我选选。",
        "我选食堂。",
        "我选不辣。",
        "就吃番茄炒蛋，确定。",
    ]:
        print(f"\n用户：{q}")
        r = a.chat(q, hist)
        if r["status"] == "confirm":
            print(f"[需确认] 工具：{r['tool']} 参数：{r['args']}")
            r = a.resume_after_approve(r, q, hist)
        if r["status"] == "wheel":
            print("🎡 转盘：", r["wheel"])
        print("步骤：")
        for s in r["steps"]:
            print("  ", s)
        print("助手：", r["content"])
        hist.append({"role": "user", "content": q})
        hist.append({"role": "assistant", "content": r["content"]})
