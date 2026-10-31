"""吃饱饱助手 · 启动自检（答辩前跑一次，三行 OK 就放心上台）

    python preflight.py

依次检查：模型 → 数据 → 工具。任一失败红字提示并退出码 1。
"""
import json
import sys
import urllib.error
import urllib.request

import config
import tools
from tools import WheelEngine


def ok(msg):
    print(f"{msg}") if not sys.stdout.isatty() else print(f"\033[32m{msg}\033[0m")


def bad(msg):
    print(f"\033[31m{msg}\033[0m" if sys.stdout.isatty() else msg)


def check_model():
    b = config.current()
    if b.is_cloud:
        payload = {
            "model": b.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
        req = urllib.request.Request(
            f"{b.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {b.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:160]
            bad(f"模型 FAIL：云端 1 token 探活失败 HTTP {e.code} {detail}")
            return False
        except Exception as e:
            bad(f"模型 FAIL：连接不上云端 {b.base_url}（{type(e).__name__}）")
            return False
        ok(f"模型 OK：{b.label}")
        return True

    names = config.local_model_names()
    if not names:
        bad(f"模型 FAIL：本地 Ollama 没响应（{b.base_url}），也没配 CLOUD_API_KEY")
        return False
    if b.model not in names:
        bad(f"模型 FAIL：本地没有 {b.model}，已装：{names[:4]}")
        return False
    ok(f"模型 OK：{b.label}｜本机已装 {len(names)} 个模型")
    return True


def check_data():
    try:
        doc = tools._load()
    except Exception as e:
        bad(f"数据 FAIL：读不到 {tools.DATA_PATH}（{e}）")
        return False
    dishes = doc.get("dishes", [])
    shops = tools._load_shops().get("shops", [])
    if not dishes or not shops:
        bad(f"数据 FAIL：菜品 {len(dishes)} 条 / 店铺 {len(shops)} 条，不能为 0")
        return False
    meal = sum(1 for d in dishes if d.get("meal_type") == "meal")
    dessert = sum(1 for d in dishes if d.get("meal_type") == "dessert")
    ok(f"数据 OK：菜品 {len(dishes)} 条（正餐 {meal} / 甜品 {dessert}）· 店铺 {len(shops)} 条")
    return True


def check_tools():
    try:
        w = json.loads(tools.build_wheel("where"))
    except Exception as e:
        bad(f"工具 FAIL：build_wheel('where') 抛异常 {e}")
        return False
    if not w.get("__wheel__") or not w.get("options"):
        bad(f"工具 FAIL：build_wheel('where') 返回不合法 {w}")
        return False

    # 一次 reroll：真实权重抽取 → 衰减 → 概率重排
    eng = WheelEngine()
    opts = w["options"]
    p1 = eng.probabilities("where", opts)
    picked = opts[0]
    eng.decay("where", picked)
    p2 = eng.probabilities("where", opts)
    if abs(p1[picked] - p2[picked]) < 1e-6:
        bad("工具 FAIL：抽中后概率没有衰减（WheelEngine 异常）")
        return False

    # 四条推荐
    pool = tools.candidates(meal_type="meal", where="食堂")
    four = json.loads(tools.finalize(pool))
    if len(four.get("groups", [])) != 4:
        bad(f"工具 FAIL：四条推荐只返回 {len(four.get('groups', []))} 条")
        return False

    ok(f"工具 OK：转盘 {len(opts)} 项 / 衰减 {p1[picked]:.2f}→{p2[picked]:.2f} / 四条推荐 {len(four['groups'])} 条")
    return True


def main():
    print("=" * 58)
    print("  吃饱饱助手 · 启动自检")
    print("=" * 58)
    results = [check_model(), check_data(), check_tools()]
    print("-" * 58)
    if all(results):
        print("全部通过，可以上台演示：python app.py（或双击 启动吃饱饱.bat）")
        return 0
    bad("自检未通过，按上面的红字修一下再开始。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
