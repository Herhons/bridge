"""deepseek 网页版：只做选择，不发提问。

DeepSeek 没有模型选择器，只有「深度思考」「智能搜索」两个开关，所以这里管这两个。

用法：
    python deepseek_choose.py --list
    python deepseek_choose.py --think on
    python deepseek_choose.py --think off --search on

需要资源库里有 4 张图：
    ds_think_on / ds_think_off / ds_search_on / ds_search_off
截法：python tools/snip.py --save ds_think_on --region x,y,w,h

判断"开/关"靠图：两张状态图分别匹配，谁的分高算谁；分差太小就报 unknown，
不硬猜（v0.0.8 的教训是"看起来对、结果全错"）。
"""

from __future__ import annotations

import json
import os
import sys

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import Library, Screen  # noqa: E402

REQUIRED = ["ds_think_on", "ds_think_off", "ds_search_on", "ds_search_off"]
TOGGLES = {"think": ("ds_think_on", "ds_think_off"), "search": ("ds_search_on", "ds_search_off")}
MARGIN = 0.03  # 两张图分数差小于这个就判 unknown


def read_state(s: Screen, kind: str) -> dict:
    on_name, off_name = TOGGLES[kind]
    a = s.find(on_name)
    b = s.find(off_name)
    s_on = a.score if a else 0.0
    s_off = b.score if b else 0.0
    if abs(s_on - s_off) < MARGIN:
        return {"state": "unknown", "score_on": round(s_on, 3), "score_off": round(s_off, 3)}
    state = "on" if s_on > s_off else "off"
    return {"state": state, "score_on": round(s_on, 3), "score_off": round(s_off, 3),
            "hit": a if state == "on" else b}


def set_state(s: Screen, kind: str, want: str) -> dict:
    cur = read_state(s, kind)
    before = cur["state"]
    if before == "unknown":
        return {"ok": False, "kind": kind, "stage": "unreadable", "before": before,
                "hint": "两张状态图分数太接近，或都没找到；先确认资源库里的图是当前主题下截的"}
    if before == want:
        return {"ok": True, "kind": kind, "stage": "already", "before": before, "after": before}
    hit = cur.get("hit")
    if hit is None:
        return {"ok": False, "kind": kind, "stage": "no_target", "before": before}
    s.click(hit)
    s.wait(TOGGLES[kind][0] if want == "on" else TOGGLES[kind][1], timeout=3.0)
    after = read_state(s, kind)["state"]
    return {"ok": after == want, "kind": kind, "stage": "done" if after == want else "unverified",
            "before": before, "after": after}


def _parse(argv: list[str]) -> dict:
    o = {"list": False, "think": None, "search": None, "bad": []}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-l", "--list"):
            o["list"] = True
        elif a in ("--think", "--search"):
            key = a[2:]
            if i + 1 >= len(argv):
                o["bad"].append(f"{a} 缺值")
            else:
                v = argv[i + 1].strip().lower()
                i += 1
                if v in ("on", "1", "true", "开", "开启"):
                    o[key] = "on"
                elif v in ("off", "0", "false", "关", "关闭"):
                    o[key] = "off"
                else:
                    o["bad"].append(f"{a} 的值只能是 on/off，收到 {v!r}")
        elif a in ("-h", "--help"):
            o["help"] = True
        else:
            o["bad"].append(a)
        i += 1
    return o


def usage(name: str) -> None:
    print("用法：")
    print(f"  python {name} --list                    # 读当前两个开关（只读）")
    print(f"  python {name} --think on|off")
    print(f"  python {name} --search on|off")
    print("  （发提问请用 deepseek_use.py）")


def main() -> int:
    name = os.path.basename(__file__)
    o = _parse(sys.argv[1:])
    if o.get("help"):
        usage(name)
        return 0
    if o["bad"]:
        print("参数有问题：", o["bad"], "（写错参数不会被当成提问）")
        usage(name)
        return 2

    lib = Library()
    print(lib.report(REQUIRED))
    if lib.missing(REQUIRED):
        return 3

    s = Screen()
    if o["list"]:
        print(json.dumps({k: read_state(s, k) for k in ("think", "search")}, ensure_ascii=False))
        return 0

    todo = {k: v for k, v in (("think", o["think"]), ("search", o["search"])) if v}
    if not todo:
        usage(name)
        return 2
    rc = 0
    for kind, want in todo.items():
        r = set_state(s, kind, want)
        print(json.dumps(r, ensure_ascii=False))
        if not r.get("ok"):
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
