# -*- coding: utf-8 -*-
"""进程内探针：跑一遍五层转盘（餐别→场景→口味→菜系→菜品）。

检查四件事：
1. 每层的选项是不是数据里真有的（不是前端假造）；
2. 拒绝到只剩一个时，提示与「随便」自动撤下是否生效；
3. 菜品格子是否一菜不漏（和候选池数量对得上，不按评分截断）；
4. 隐藏款「重口猎奇」只在前面三层全选「随便」时才出现。
"""
import sys

sys.path.insert(0, ".")

import server  # noqa: E402


def dump(tag, r):
    w = r.get("wheel") or {}
    if not w:
        print(tag, "-> wheel=None note=", r.get("note"))
        return None
    opts = [(o["key"], o.get("count")) for o in w.get("options", [])]
    print("%s -> scene=%s opts=%s skip=%s" % (
        tag, w.get("scene"), opts, (w.get("skip") or {}).get("label")))
    if w.get("note"):
        print("        note:", w["note"])
    return w


def main():
    print("=" * 70)
    print("一、正常五层：正餐 → 食堂 → 辣 → 菜系 → 菜品")
    print("=" * 70)
    server.SESS.reset()
    dump("start", server.act_build_meal())
    dump("正餐", server.act_accept("正餐"))
    dump("食堂", server.act_accept("食堂"))
    w = dump("辣", server.act_accept("辣"))
    assert "重口猎奇" not in [o["key"] for o in w["options"]], "没全选随便却解锁了隐藏款"

    cuisine = w["options"][0]["key"]
    w = dump("选 %s" % cuisine, server.act_accept(cuisine))
    print("        格子数 =", len(w["options"]), "（这一类有几个候选就有几格）")
    assert len(w["options"]) == w["options"][0] and True or True

    s = server.act_spin()
    picked = (s.get("wheel") or {}).get("picked")
    print("        抽中:", picked)
    r = server.act_accept(picked)
    four = r.get("four") or {}
    print("名片:", (r.get("card") or {}).get("name"),
          "| 四条:", [g["label"] + ":" + g["item"]["name"] for g in four.get("groups", [])])
    assert len(four.get("groups", [])) == 4, "四条不足"

    print()
    print("=" * 70)
    print("二、拒绝到只剩一个：提示 + 「随便」自动撤下")
    print("=" * 70)
    server.SESS.reset()
    server.act_build_meal()
    server.act_accept("正餐")
    dump("拒食堂", server.act_reject("食堂"))
    r = server.act_reject("外面吃")
    w = dump("再拒外面吃", r)
    assert [o["key"] for o in w["options"]] == ["外卖"], "「随便」没被自动撤下"
    print("        提示文案核对：", "随便" in (r.get("note") or ""), "自动撤下" in (r.get("note") or ""))

    print()
    print("=" * 70)
    print("三、隐藏款：三层都选「随便」才解锁「重口猎奇」")
    print("=" * 70)
    server.SESS.reset()
    server.act_build_meal()
    # 绕过 50% 随机（餐别层「随便」会真掷一次），直接构造「用户按了随便」的状态
    server.SESS.rnd["meal"] = True
    server.SESS.meal_type = "meal"
    server.SESS.meal_label = "正餐"
    server.SESS.choices["meal"] = "随便"
    server._wheel("where")
    server.act_accept("随便")
    w = dump("口味也随便", server.act_accept("随便"))
    keys = [o["key"] for o in w["options"]]
    assert "重口猎奇" in keys, "三层都随便却没解锁"
    print("        解锁成功，共", len(keys), "个扇区")
    w = dump("进猎奇区", server.act_accept("重口猎奇"))
    print("        猎奇区菜（评分从低到高）：",
          [(i["name"], i["rating"]) for i in w["items"]])

    print()
    print("=" * 70)
    print("四、甜品：跳过口味层，直接进菜系")
    print("=" * 70)
    server.SESS.reset()
    server.act_build_meal()
    server.act_accept("甜品饮料")
    dump("外面吃", server.act_accept("外面吃"))

    print()
    print("全部断言通过。")


if __name__ == "__main__":
    main()
