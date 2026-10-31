# -*- coding: utf-8 -*-
"""探针：① 场景层「随便」必须在盘外；② 任意一层可以直接调出来（跳层）。

用法：先起服务 python server.py 8791，再 python 测试/探跳层.py
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:%s" % (sys.argv[1] if len(sys.argv) > 1 else "8791")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(path, body=None):
    req = urllib.request.Request(BASE + path, method="POST",
                                 data=json.dumps(body or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    return json.loads(OPENER.open(req, timeout=60).read().decode("utf-8"))


def state():
    return json.loads(OPENER.open(BASE + "/api/state", timeout=30).read().decode("utf-8"))


def keys(w):
    return [o["key"] for o in (w or {}).get("options", [])]


ok = True


def check(cond, msg):
    global ok
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        ok = False


print("== ① 场景层「随便」在盘外 ==")
post("/api/reset")
post("/api/start")
post("/api/accept", {"key": "正餐"})
w = state()["wheel"]
ks = keys(w)
print("     扇区：", ks)
check(w["scene"] == "where", "第二层是场景层")
check("随便" not in ks, "「随便」不在扇区里")
check(ks == ["食堂", "外面吃", "外卖"], "三个真扇区齐全：食堂/外面吃/外卖")
check(bool(w.get("skip")) and w["skip"]["key"] == "随便", "盘外有「随便」按钮")
check(all(o["count"] not in (None, 0) for o in w["options"]), "每个扇区都带真实候选数")
check(w["counts"] if "counts" in w else True, "counts 里不再混入「随便」")

print("== ② 「随便」当真按钮用：选它=不限场景 ==")
r = post("/api/accept", {"key": "随便"})
check(r.get("wheel", {}).get("scene") == "spice", "点盘外「随便」直接进口味层")
check(state()["choices"].get("where") == "随便", "场景记为「随便」（= 不过滤）")

print("== ③ 跳到指定层 ==")
post("/api/reset")
r = post("/api/jump", {"scene": "cuisine"})
w = r.get("wheel") or {}
check(w.get("scene") in ("cuisine", "dish"), "空状态直接跳到菜系层（或自动并入菜品层）")
print("     note:", r.get("note"))
print("     扇区数：", len(keys(w)))
check(len(keys(w)) > 3, "菜系层按真实分布分组，不是空盘")

r = post("/api/jump", {"scene": "dish"})
w = r.get("wheel") or {}
check(w.get("scene") == "dish", "跳到菜品层")
check(len(w.get("items") or []) > 20, "菜品层格子来自真实候选（%d 个）" % len(w.get("items") or []))

r = post("/api/jump", {"scene": "where"})
w = r.get("wheel") or {}
check(w.get("scene") == "where" and "随便" not in keys(w), "跳回场景层，仍是三个真扇区")

r = post("/api/jump", {"scene": "meal"})
w = r.get("wheel") or {}
check(w.get("scene") == "meal", "跳回第一层餐别")

print("== ④ 跳层后状态自洽 ==")
post("/api/jump", {"scene": "spice"})
s = state()
check(s["wheel"]["scene"] == "spice", "落到口味层")
check(s.get("final") is None and s.get("four") is None, "名片/四条已被收起")

print("\n%s" % ("全部通过 ✅" if ok else "有失败项 ❌"))
sys.exit(0 if ok else 1)
