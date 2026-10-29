"""chatgpt 网页版：只做选择，不发提问。

用法：
    python chatgpt_choose.py --list [--tab N]
    python chatgpt_choose.py --effort instant|mid|high [--tab N]

本端跟别家不一样：点开芯片弹出来的**不是模型列表，而是一个强度滑杆**
（上面写着当前档位，比如「中 ›」，下面一条橙色滑杆）。
所以这里只能管"思考强度"，模型选择还没做。

定位基准是**右下角那个橙色语音键**：
输入框宽度会变、芯片文字长短会变，只有这个橙色圆钮的位置和颜色稳定。
实测偏移：芯片 = 语音键中心左 70、上 3；滑杆 = 左 200 起、宽 230、上移 46。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time

import cv2
import numpy as np

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import Library, Screen  # noqa: E402
from qi.choose import Chooser, choose_writer, read_writer  # noqa: E402
from qi.flow import Site, _open_new_chat  # noqa: E402

# 模式按钮只在新对话页出现，所以动 writer 之前先确保在新对话页
_NEWCHAT = Site(name="chatgpt", title_hint="Google Chrome", composer="gpt_composer",
                send="gpt_send_on", actions="gpt_actions", copy="gpt_copy",
                composer_offset=(240, -5),
                newchat=("gpt_newchat", "gpt_newchat_icon"), sidebar="gpt_sidebar",
                send_mode="exists")

# 写入器模式：聊天的整组按钮模板 + 左右两格
WRITER = Chooser(
    name="chatgpt",
    anchor="gpt_composer",
    open_offset=(0, 0),
    prefix="gpt_",
    writer_tabs=("gpt_writer_tabs_chat", "gpt_writer_tabs_work"),
    writers={"chat": 0, "work": 1},
)

EFFORTS = {
    "instant": {"chip": "gpt_chip_low", "frac": 0.22, "desc": "即时（最低）"},
    "mid": {"chip": "gpt_chip_mid", "frac": 0.50, "desc": "中"},
    "high": {"chip": "gpt_chip_high", "frac": 0.78, "desc": "高"},
}
CHIP_DX, CHIP_DY = -70, -3
# 弹层里「选择模型」那一行，相对语音键的偏移（实测）
MENU_MODEL_DX, MENU_MODEL_DY = -75, 41
MODELS = {"gpt6": "GPT-6（默认）", "gpt56": "GPT-5.6 Sol"}
TRACK_DX, TRACK_W, TRACK_DY = -200, 230, -46


def _voice_center(s: Screen) -> tuple[int, int] | None:
    """右下角橙色语音键的中心（屏幕坐标）。找不到返回 None。

    只在**副屏**里找：整块虚拟桌面扫的话，主屏上的橙色元素（视频、图标）会抢先命中。
    """
    win = s.window_rect("Google Chrome", on_secondary=True)
    if win is None:
        return None
    band = s.grab(win)
    h = band.shape[0]
    b = band[:, :, 0].astype(int)
    r = band[:, :, 2].astype(int)
    orange = ((r - b > 80) & (r > 200)).astype(np.uint8)
    n, _lb, st, _ce = cv2.connectedComponentsWithStats(orange, 8)
    cands = [(st[i][4], st[i][0], st[i][1], st[i][2], st[i][3]) for i in range(1, n)
             if 800 < st[i][4] < 3000]
    if not cands:
        return None
    _a, x, y, w, hh = max(cands)
    return (win[0] + x + w // 2, win[1] + y + hh // 2)


def _calm(s: Screen) -> None:
    """读之前先把界面"放平"：关掉可能开着的弹层、把鼠标移开。

    不这么做会踩两个坑：弹层开着会盖住芯片；鼠标停在芯片上会变成另一种底色。
    这两种状态下模板都对不上，读出来就是 unknown。
    """
    s.press("esc")
    time.sleep(0.3)
    s.move_to(-700, 300)
    time.sleep(0.35)


def read_current(s: Screen) -> dict:
    """读当前强度：匹配芯片图。

    已知不稳：芯片形态会变（有时显示「5.6 中」，有时只显示「思考强度」），
    换模型或改版都要重截 gpt_chip_*。读不出来就说 unknown，不猜。
    """
    for key, cfg in EFFORTS.items():
        m = s.find(cfg["chip"], threshold=0.90)
        if m is not None:
            return {"effort": key, "score": round(m.score, 3)}
    return {"effort": "unknown",
            "hint": "当前芯片和 instant/mid/high 三张图都对不上（芯片形态或主题变了，需重截）"}


def set_effort(s: Screen, key: str) -> dict:
    cur = read_current(s)
    if cur["effort"] == "unknown":
        return {"ok": False, "stage": "unreadable", "hint": cur["hint"]}

    v = _voice_center(s)
    if v is None:
        return {"ok": False, "stage": "no_voice_button",
                "hint": "找不到橙色语音键；输入框里可能有文字了（清空后再试）"}
    vx, vy = v

    s.click_xy(vx + CHIP_DX, vy + CHIP_DY)          # 打开弹层
    time.sleep(0.8)
    # 滑杆必须按住拖：单击只会让把手弹回原位（实测右侧单击永远改不动）
    target_x = int(vx + TRACK_DX + EFFORTS[key]["frac"] * TRACK_W)
    s.drag(vx + TRACK_DX + TRACK_W // 2, vy + TRACK_DY, target_x, vy + TRACK_DY)
    time.sleep(0.6)
    s.press("esc")
    time.sleep(0.5)
    s.move_to(vx - 400, vy - 500)                   # 移开鼠标，避免 hover 影响判读
    time.sleep(0.4)

    after = read_current(s)
    ok = after["effort"] == key
    return {"ok": ok, "stage": "done" if ok else "unverified",
            "before": cur["effort"], "after": after["effort"]}


# ------------------------------------------------------------------ 模型
def _items_visible(s: Screen) -> bool:
    return any(s.find(f"gpt_model_{k}", threshold=0.80) is not None for k in MODELS)


def _open_model_list(s: Screen) -> bool:
    """点开"选择模型"那一层。

    要先判状态：弹层已经开着的时候再点芯片会把它关掉（实测踩过）。
    """
    if _items_visible(s):
        return True
    v = _voice_center(s)
    if v is None:
        return False
    s.press("esc")
    time.sleep(0.35)
    s.click_xy(v[0] + CHIP_DX, v[1] + CHIP_DY)
    time.sleep(0.9)
    if _items_visible(s):
        return True
    s.click_xy(v[0] + MENU_MODEL_DX, v[1] + MENU_MODEL_DY)
    time.sleep(0.9)
    return _items_visible(s)


def read_model(s: Screen) -> dict:
    """读当前模型：看哪一行的行尾有勾。

    模型名确实显示在芯片上（"5.6 中"），但和强度粘在一起，做模板会变成组合爆炸。
    菜单项行尾的 ✓ 是干净的判据：勾上时右端有 10+ 个深色像素，没勾是 0。
    """
    if not _open_model_list(s):
        time.sleep(0.5)
        if not _open_model_list(s):       # 弹层有过渡态，给一次重试
            return {"model": "unknown", "hint": "打不开模型列表"}
    ink = {}
    for k in MODELS:
        m = s.find(f"gpt_model_{k}", threshold=0.80)
        if m is not None:
            band = s.grab((m.x, m.y, m.w, m.h))[:, -50:]
            g = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
            ink[k] = int((g < 120).sum())
    s.press("esc")
    time.sleep(0.3)
    if not ink:
        return {"model": "unknown", "hint": "模型项一个都没定位到"}
    best = max(ink, key=ink.get)
    return {"model": best if ink[best] > 5 else "unknown", "ink": ink}


def set_model(s: Screen, key: str) -> dict:
    cur = read_model(s)["model"]
    if cur == key:
        return {"ok": True, "stage": "already", "before": cur, "after": cur}
    if not _open_model_list(s):
        return {"ok": False, "stage": "menu_not_open", "before": cur}
    m = s.find(f"gpt_model_{key}", threshold=0.80)
    if m is None:
        s.press("esc")
        return {"ok": False, "stage": "item_not_found", "before": cur, "want": key}
    s.click(m)
    time.sleep(1.3)
    s.move_to(-1200, 100)
    time.sleep(0.4)
    after = read_model(s)["model"]
    return {"ok": after == key, "stage": "done" if after == key else "unverified",
            "before": cur, "after": after}


def main() -> int:
    name = os.path.basename(__file__)
    args = sys.argv[1:]
    want, tab, bad, writer, model = None, None, [], None, None
    do_list = "--list" in args or "-l" in args
    if "--effort" in args:
        i = args.index("--effort")
        if i + 1 < len(args):
            want = args[i + 1].strip().lower()
        else:
            bad.append("--effort 缺值")
    if "--model" in args:
        i = args.index("--model")
        if i + 1 < len(args):
            model = args[i + 1].strip().lower()
        else:
            bad.append("--model 缺值")
    if "--writer" in args:
        i = args.index("--writer")
        if i + 1 < len(args):
            writer = args[i + 1].strip().lower()
        else:
            bad.append("--writer 缺值")
    if "--tab" in args:
        i = args.index("--tab")
        if i + 1 < len(args):
            try:
                tab = int(args[i + 1])
            except ValueError:
                bad.append("--tab 要数字")
        else:
            bad.append("--tab 缺值")

    if bad:
        print("参数有问题：", bad)
        return 2
    if not do_list and want is None and writer is None and model is None:
        print("用法：")
        print(f"  python {name} --list [--tab N]")
        print(f"  python {name} --effort instant|mid|high [--tab N]")
        print(f"  python {name} --writer chat|work [--effort X]   # 换模式会换掉强度档位")
        print(f"  python {name} --model <{'|'.join(MODELS)}>")
        for k, v in MODELS.items():
            print(f"    {k:8} {v}")
        for k, v in EFFORTS.items():
            print(f"    {k:8} {v['desc']}")
        print("  说明：本端只支持强度；模型选择（弹层里「中 ›」那层子菜单）还没做")
        return 2
    if want is not None and want not in EFFORTS:
        print(f"没有这个档位：{want}；可选 {', '.join(EFFORTS)}")
        return 2

    lib = Library()
    need = ([cfg["chip"] for cfg in EFFORTS.values()]
            + ["gpt_writer_tabs_chat", "gpt_writer_tabs_work"]
            + [f"gpt_model_{k}" for k in MODELS])
    print(lib.report(need))
    if lib.missing(need):
        return 3

    s = Screen()
    if not s.bring_to_front("Google Chrome", on_secondary=True):
        print("找不到浏览器窗口")
        return 1
    if tab:
        s.hotkey("ctrl", str(tab))
        time.sleep(1.2)

    if writer is not None or do_list:
        # 先回到新对话页，否则模式按钮根本不在页面上
        _open_new_chat(s, _NEWCHAT)
        time.sleep(1.0)

    if do_list:
        out = dict(read_current(s))
        out.update(read_writer(s, WRITER))
        out.update(read_model(s))
        print(json.dumps(out, ensure_ascii=False))
        return 0

    rc = 0
    if writer is not None:
        w = choose_writer(s, WRITER, writer)
        print(json.dumps(w, ensure_ascii=False))
        if not w.get("ok"):
            rc = 1
    if model is not None:
        r = set_model(s, model)
        print(json.dumps(r, ensure_ascii=False))
        rc = 0 if r.get("ok") else 1
    if want is not None:
        r = set_effort(s, want)
        print(json.dumps(r, ensure_ascii=False))
        rc = 0 if r.get("ok") else 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
