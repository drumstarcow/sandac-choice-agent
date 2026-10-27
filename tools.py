"""杉达选择助手·今天吃啥大转盘 —— 业务工具层（路径B：普通功能 + 自然语言驱动）

实现“帮助大学生做选择”所需的若干可被调用的业务操作：
- build_wheel：根据决策层级（where/spice/dish）从真实数据生成转盘配置
- finalize_decision：把最终决定写成产出物（Markdown 文件）
- share_with_roommate：把决定模拟发给室友（危险操作，需确认）

数据位于 data/food_data.json（当前为示例数据，替换为真实校园数据即可作为真实数据源）。
危险操作（发给室友）由 Agent 层做确认拦截。
"""
import json
import datetime
from pathlib import Path

DATA_PATH = Path(__file__).parent / "data" / "food_data.json"
OUTPUT_DIR = Path(__file__).parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)


def _load():
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


# ------------------------- 转盘工具 -------------------------
def build_wheel(scene, preference=""):
    """根据决策层级生成一个转盘配置（真实选项来自数据文件）。

    参数:
      scene: 决策层级
        - "where" 第一层：今天在哪吃（食堂/外卖/外面吃）
        - "spice" 第二层：口味（辣/不辣/随便）
        - "dish"  第三层：具体菜品（按 preference 口味过滤；preference 为空或“随便”则全量）
      preference: 上层已确定的上下文（如口味“辣”/“不辣”/“随便”，或为空）
    返回: 一个 JSON 字符串，含 {"__wheel__": true, "title", "scene", "options", "ctx"}
    """
    data = _load()
    if scene == "where":
        options = data.get("where_options", [])
        title = "📍 今天在哪吃？"
        ctx = ""
    elif scene == "spice":
        options = ["辣", "不辣", "随便"]
        title = "🌶️ 今天想吃点啥口味？"
        ctx = preference
    elif scene == "dish":
        dishes = data.get("dishes", [])
        spice = (preference or "").strip()
        if spice in ("辣",):
            pool = [d for d in dishes if "辣" in d.get("tags", [])]
        elif spice in ("不辣",):
            pool = [d for d in dishes if "不辣" in d.get("tags", [])]
        else:  # 随便 / 空 -> 全量
            pool = list(dishes)
        # 去重保序，按价格升序方便看
        seen = set()
        ordered = []
        for d in sorted(pool, key=lambda x: x.get("price", 0)):
            if d["name"] not in seen:
                seen.add(d["name"])
                ordered.append(d)
        options = [d["name"] for d in ordered]
        title = "🍽️ 转到哪道菜？（不喜欢就拒绝，剔掉重转）"
        ctx = spice
    else:
        return json.dumps({"error": f"未知层级：{scene}"})

    return json.dumps({
        "__wheel__": True,
        "title": title,
        "scene": scene,
        "options": options,
        "ctx": ctx,
    }, ensure_ascii=False)


def get_dish_card(name):
    """查询某道菜的“美食名片”信息（特写描述 + 价格 + 位置 + 标签），用于勾起食欲。"""
    data = _load()
    for d in data.get("dishes", []):
        if d["name"] == name:
            return d
    return None


# ------------------------- 产出物 / 危险操作 -------------------------
def finalize_decision(where, spice, dish, note=""):
    """把最终决定写成产出物（Markdown 文件），返回摘要 + 文件路径。"""
    d = get_dish_card(dish) or {}
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUTPUT_DIR / f"今日决定_{ts}.md"
    spice_txt = spice if spice and spice != "随便" else "不挑口味（随便）"
    body = [
        "# 🍽️ 今日决定 · 杉达选择助手",
        "",
        f"- **在哪吃**：{where}",
        f"- **口味**：{spice_txt}",
        f"- **最终菜品**：{dish}",
        "",
        "## 美食名片",
        f"- 菜品：**{d.get('name', dish)}**  ￥{d.get('price', '?')}",
        f"- 位置：{d.get('canteen', '（未指定）')} / {d.get('window', '（未指定）')}",
        f"- 标签：{', '.join(d.get('tags', []))}",
        f"- 介绍：{d.get('desc', '（暂无介绍）')}",
        "",
        f"> 备注：{note or '（无）'}",
        "",
        "_由「杉达选择助手·今天吃啥大转盘」生成，数据为示例，以实际供应为准。_",
    ]
    out.write_text("\n".join(body), encoding="utf-8")
    return f"已生成今日决定并保存到 {out}：在【{where}】吃【{spice_txt}】的【{dish}】（￥{d.get('price','?')}）。"


def share_with_roommate(content):
    """【危险操作】模拟把一段内容发给室友（不真实外发，仅演示）。"""
    return (f"【模拟发送·未真实外发】已把以下内容发给室友：\n“{content}”\n"
            f"（这是课堂演示用的模拟发送，不会真实对外发出任何消息。）")


# ------------------------- 注册表 / Schema / 危险标记 -------------------------
TOOL_REGISTRY = {
    "build_wheel": build_wheel,
    "finalize_decision": finalize_decision,
    "share_with_roommate": share_with_roommate,
}

# 危险操作：涉及对外发送，必须经用户确认才能执行
DANGEROUS_TOOLS = {"share_with_roommate"}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "build_wheel",
            "description": "生成一个“决策大转盘”的配置。参数 scene 为决策层级：where=第一层(食堂/外卖/外面吃)；spice=第二层(辣/不辣/随便)；dish=第三层(具体菜品，按 preference 口味过滤，preference 为“辣/不辣/随便”，空或“随便”表示不过滤全量)。preference 为上层已确定的上下文。返回的转盘选项来自真实数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "description": "决策层级：where / spice / dish"},
                    "preference": {"type": "string", "description": "上层上下文，如口味“辣/不辣/随便”，可为空"}
                },
                "required": ["scene"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "finalize_decision",
            "description": "把最终决定写成产出物（Markdown 文件）。参数 where 在哪吃、spice 口味、dish 最终菜品、note 备注（可选）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "where": {"type": "string", "description": "在哪吃：食堂/外卖/外面吃"},
                    "spice": {"type": "string", "description": "口味：辣/不辣/随便"},
                    "dish": {"type": "string", "description": "最终确定的菜品名"},
                    "note": {"type": "string", "description": "备注，可选"}
                },
                "required": ["where", "spice", "dish"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "share_with_roommate",
            "description": "【危险操作·需用户确认】把一段内容模拟发送给室友（演示用，不真实外发）。参数 content 发送内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "发送给室友的内容"}
                },
                "required": ["content"]
            }
        }
    },
]


def call_tool(name, args):
    fn = TOOL_REGISTRY.get(name)
    if not fn:
        return f"未知工具：{name}"
    try:
        return str(fn(**args))
    except TypeError as e:
        return f"工具 {name} 参数错误：{e}"
    except Exception as e:
        return f"工具 {name} 执行出错：{e}"


if __name__ == "__main__":
    import pprint
    print("== where ==")
    pprint.pprint(json.loads(build_wheel("where")))
    print("== spice ==")
    pprint.pprint(json.loads(build_wheel("spice", "食堂")))
    print("== dish(辣) ==")
    pprint.pprint(json.loads(build_wheel("dish", "辣")))
    print("== dish(随便) ==")
    pprint.pprint(json.loads(build_wheel("dish", "随便")))
