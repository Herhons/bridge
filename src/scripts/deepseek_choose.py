"""deepseek 网页版：只做选择，不发提问。

DeepSeek 没有模型选择器，只有「深度思考」「智能搜索」两个开关，所以这里管这两个。

用法：
    python deepseek_choose.py --list
    python deepseek_choose.py --think on
    python deepseek_choose.py --think off --search on

判断开关状态**不能靠模板分数**：归一化相关对亮度不敏感，
开/关两态在同一位置都能拿到 0.96 以上。实测可靠的是颜色——
关：浅蓝描边，B-R≈37；开：白底黑字，B-R≈0.6。
所以模板只用来定位胶囊，开关状态看 B-R。

读状态前必须先把鼠标移开：悬停会把胶囊变成灰底，读出来像"开"。
"""

from __future__ import annotations

import json
import os
import sys
import time

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import ForegroundLost, Library, Screen, console, uilock  # noqa: E402
from qi.browser import ensure_site  # noqa: E402
from qi import failure, guard  # noqa: E402
from qi import zoom as zoom_mod  # noqa: E402

# 每个开关一张定位图（截"关"的样子即可，两态都能定位到）
TEMPLATES = {"think": "ds_think", "search": "ds_search"}
REQUIRED = list(TEMPLATES.values())
TITLE = "Google Chrome"


def _win(s: Screen):
    """目标窗口矩形。不限定就是在整个虚拟桌面里找，主屏上开着同款页面会先命中那边。"""
    return s.window_rect(TITLE, on_secondary=True)


OFF_MIN = 25.0   # B-R 大于这个值算"关"（浅蓝描边）
ON_MAX = 15.0    # 小于这个值算"开"（白底黑字）


def _read(s: Screen, kind: str) -> dict:
    m = s.find(TEMPLATES[kind], region=_win(s))
    if m is None:
        return {"state": "missing", "blue": None, "hit": None}
    # 把鼠标移开再量，否则悬停态会被读成"开"。
    # 不能写 max(0, ...)：0 是虚拟桌面坐标里的一个"合法但随机"的点，
    # 开关在屏幕上偏上时会正好落到别的元素上，反而触发新的悬停。改为夹在窗口内。
    _w = _win(s)
    ty = max(_w[1] + 20, m.center[1] - 400) if _w else max(0, m.center[1] - 400)
    s.move_to(m.center[0], ty)
    time.sleep(0.35)
    b, _g, r = s.region_mean(m)
    blue = round(b - r, 1)
    state = "off" if blue > OFF_MIN else ("on" if blue < ON_MAX else "unknown")
    return {"state": state, "blue": blue, "hit": m}


def list_state(s: Screen) -> dict:
    out = {}
    for kind in TEMPLATES:
        d = _read(s, kind)
        d.pop("hit", None)
        out[kind] = d
    return out


def set_state(s: Screen, kind: str, want: str) -> dict:
    cur = _read(s, kind)
    before = cur["state"]
    if before in ("missing", "unknown"):
        return {"ok": False, "kind": kind, "stage": before, "before": before, "blue": cur["blue"],
                "hint": "定位图没找到，或 B-R 落在 15~25 之间；确认页面是浅色主题、开关没被悬停"}
    if before == want:
        return {"ok": True, "kind": kind, "stage": "already", "before": before, "after": before,
                "blue": cur["blue"]}
    s.click(cur["hit"])
    time.sleep(0.9)
    after = _read(s, kind)["state"]
    return {"ok": after == want, "kind": kind, "stage": "done" if after == want else "unverified",
            "before": before, "after": after}


def _parse(argv: list[str]) -> dict:
    o = {"list": False, "think": None, "search": None, "tab": None, "bad": [], "help": False}
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
        elif a == "--tab":
            if i + 1 >= len(argv):
                o["bad"].append("--tab 缺值")
            else:
                i += 1
                try:
                    o["tab"] = int(argv[i])
                except ValueError:
                    o["bad"].append(f"--tab 要数字，收到 {argv[i]!r}")
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
    print(f"  python {name} --list --tab N            # 先按 Ctrl+N 切标签页")
    print("  （发提问请用 deepseek_use.py）")


def _record_receipt(hwnd, dimension, target, readback, raw):
    """写档位回执（CARD-06a (a)）。**失败一律不影响选择结果。**

    本脚本刻意不 import 项目包（单文件契约），所以按需把 src 挂上 sys.path。
    """
    try:
        from pathlib import Path
        source = str(Path(__file__).resolve().parents[1])
        if source not in sys.path:
            sys.path.insert(0, source)
        from qi import tier_guard
        tier_guard.record_choice("deepseek", target=target, readback=readback,
                                 dimension=dimension, hwnd=hwnd, raw=raw)
    except Exception:                                  # noqa: BLE001 - 回执不是主流程
        pass


def main() -> int:
    name = os.path.basename(__file__)
    need, wait, force, rest = guard.parse_guard_args(list(sys.argv[1:]))
    o = _parse(rest)
    if o.get("help"):
        usage(name)
        return 0
    if o["list"] and (o["think"] or o["search"]):
        o["bad"].append("--list 不能与 --think/--search 同用")
    if o["bad"]:
        print("参数有问题：", o["bad"], "（写错参数不会被当成提问）")
        usage(name)
        return 2

    lib = Library()
    print(lib.report(REQUIRED))
    if lib.missing(REQUIRED):
        return 3

    if not guard.guard(need, wait, force):
        return 4
    s = Screen()
    hwnd = ensure_site("deepseek", tab=o["tab"])
    if hwnd is None:
        print("开不出浏览器窗口（检查 Chrome 路径）")
        return 1
    s.bind(hwnd)
    if not s.focus():
        print("窗口提不到前台")
        return 1
    s.tab_site = "deepseek"
    time.sleep(1.2)
    try:
        # 页面缩放预检（CARD-06b 第 6 条）：非 100% 先 Ctrl+0，仍不是就退出
        zoom_mod.ensure(s, verbose=True)
        if o["list"]:
            print(json.dumps(list_state(s), ensure_ascii=False))
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
                return 1
            # 只有「深度思考」算档位（本端没有模型选择器）；搜索开关不进档位表。
            # 回执失败不影响选择结果（CARD-06a (a)）。
            if kind == "think":
                try:
                    rec = globals().get("_record_receipt")
                    if callable(rec):
                        rec(hwnd, "think", want, r.get("after"), r)
                except Exception:                      # noqa: BLE001
                    pass
        return rc
    except ForegroundLost as e:
        print("失败:", json.dumps({"ok": False, "stage": "user_took_over",
                                  "hint": str(e)}, ensure_ascii=False))
        return 4
    except zoom_mod.ZoomNot100 as e:
        result = {**e.as_result(), "ok": False}
        failure.record("deepseek", result, screen=s, hwnd=hwnd)
        print("失败:", json.dumps(result, ensure_ascii=False))
        return e.code


if __name__ == "__main__":
    console.setup()          # 入口编码：GBK 控制台不再被 \u200b 打崩（CARD-08 第 1 条）
    raise SystemExit(uilock.entry("deepseek_choose", main))
