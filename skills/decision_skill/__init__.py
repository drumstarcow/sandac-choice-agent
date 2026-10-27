"""校园决策技能（今天吃啥大转盘）—— Skill 入口

把“杉达选择助手”封装为一个可被复用的 Skill：自然语言理解 + 决策大转盘 +
美食名片。本模块仅做轻量封装，真正的智能体逻辑在 agent.py / tools.py。
"""
from agent import ChoiceAgent
from tools import TOOL_SCHEMAS

SKILL = {
    "name": "校园决策技能（今天吃啥大转盘）",
    "version": "1.0",
    "tools": [t["function"]["name"] for t in TOOL_SCHEMAS],
    "entry_point": "activate",
}


def activate():
    """返回该 Skill 背后的智能体实例。"""
    return ChoiceAgent()


def info():
    """返回 Skill 元信息。"""
    return SKILL


if __name__ == "__main__":
    print("Skill:", info())
    a = activate()
    print("Agent 就绪，可用工具：", info()["tools"])
