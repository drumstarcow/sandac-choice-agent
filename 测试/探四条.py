# -*- coding: utf-8 -*-
"""进程内探针：跑一遍四层转盘，检查扇区选项、拒绝到只剩一个的表现、以及名片后的四条。"""
import sys

sys.path.insert(0, ".")

import server  # noqa: E402


def dump(tag, r):
    w = r.get("wheel") or {}
    if not w:
        print(tag, "-> wheel=None note=", r.get("note"))
        return
    print("%s -> scene=%s opts=%s skip=%s note=%s" % (
        tag, w.get("scene"), [o["key"] for o in w.get("options", [])],
        (w.get("skip") or {}).get("label"), w.get("note")))


def main():
    server.SESS.reset()
    dump("start", server.act_build_meal())
    dump("accept 正餐", server.act_accept("正餐"))
    dump("accept 食堂", server.act_accept("食堂"))
    dump("reject 辣（剩一个）", server.act_reject("辣"))
    dump("accept 不辣", server.act_accept("不辣"))
    s = server.act_spin()
    picked = (s.get("wheel") or {}).get("picked")
    print("spin picked:", picked)
    r = server.act_accept(picked)
    four = r.get("four") or {}
    print("名片:", (r.get("card") or {}).get("name"),
          "四条:", [g["label"] + ":" + g["item"]["name"] for g in four.get("groups", [])])

    # 再验一次「随便」跳过：餐别层点「随便」，应 50/50 roll 出餐别
    server.SESS.reset()
    server.act_build_meal()
    r = server.act_accept("随便")
    print("餐别层点随机 ->", server.SESS.meal_label,
          "scene=", (r.get("wheel") or {}).get("scene"))


if __name__ == "__main__":
    main()
