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
from qi import guard  # noqa: E402
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
# 芯片左边界随内容变宽变窄（"选择推理强度" / "GPT-6.1 Sol 轻度"），
# 但**右端**是固定的，所以锚在麦克风上、点它左边 40px（落在芯片右端/箭头上）。
CHIP_DX, CHIP_DY = -40, -3
# 弹层里「选择模型」那一行，相对语音键的偏移（实测）
MENU_MODEL_DX, MENU_MODEL_DY = -75, 41
# 模型按写入器模式分开：两种模式的列表完全不同（聊天 2 项 / 工作 9 项）
MODELS = {
    "chat": {
        "gpt6": ("GPT-6（默认）", "gpt_model_gpt6"),
        "gpt56": ("GPT-5.6 Sol", "gpt_model_gpt56"),
    },
    "work": {
        "default": ("默认（推荐模型集）", "gpt_wm2_default"),
        "gpt61_sol": ("GPT-6.1 Sol", "gpt_wm2_gpt61_sol"),
        "gpt61_terra": ("GPT-6.1 Terra", "gpt_wm2_gpt61_terra"),
        "gpt6_astra": ("GPT-6 Astra", "gpt_wm2_gpt6_astra"),
        "gpt6_sol": ("GPT-6 Sol", "gpt_wm2_gpt6_sol"),
        "gpt6_luna": ("GPT-6 Luna", "gpt_wm2_gpt6_luna"),
        "gpt56_sol": ("GPT-5.6 Sol", "gpt_wm2_gpt56_sol"),
        "gpt56_terra": ("GPT-5.6 Terra", "gpt_wm2_gpt56_terra"),
        "gpt56_luna": ("GPT-5.6 Luna", "gpt_wm2_gpt56_luna"),
    },
}
ALL_MODEL_KEYS = {k: v[0] for m in MODELS.values() for k, v in m.items()}
TRACK_DX, TRACK_W, TRACK_DY = -200, 230, -46


def _voice_center(s: Screen):
    """右下角橙色语音键的中心（屏幕坐标）。

    之前的写法按"面积最大的橙色块"取，结果抓到侧栏里的橙色头像/图标（实测返回了
    窗口 x=334 的位置，完全不对）。改成只认**近似正方形的小块**（20~60px、面积>400），
    再取**最靠右下**的那个——语音键永远在输入框最右侧。
    """
    win = s.window_rect("Google Chrome", on_secondary=True)
    if win is None:
        return None
    img = s.grab(win)
    if img.size == 0:
        return None
    b = img[:, :, 0].astype(int)
    r = img[:, :, 2].astype(int)
    orange = ((r - b > 45) & (r > 145)).astype(np.uint8)
    n, _lb, st, _c = cv2.connectedComponentsWithStats(orange, 8)
    cands = [(st[i][4], st[i][0], st[i][1], st[i][2], st[i][3]) for i in range(1, n)
             if 20 <= st[i][2] <= 60 and 20 <= st[i][3] <= 60 and st[i][4] > 400]
    if not cands:
        return None
    _a, x, y, w, h = max(cands, key=lambda c: c[1] + c[2])   # 最靠右下
    return (win[0] + x + w // 2, win[1] + y + h // 2)



# ---------------------------------------------------------------- 安全前提
def page_kind(s: Screen) -> str:
    """当前是"新聊天页"还是"会话页"。

    **这两页的布局不一样**（用户指出，实测确认）：
      新聊天页：顶部有「聊天/工作」，大输入框在页面中间，芯片在输入框右下
      会话页  ：没有「聊天/工作」，输入框在页面底部
    所以模式按钮只在新聊天页存在；在会话页读它一定是 unknown，
    拿新聊天页的偏移去会话页点，必然点空甚至点到别的按钮。
    """
    if s.find("gpt_tab_chat_word", threshold=0.75) is not None:
        return "new"
    return "session"


def orange_near(s: Screen, center: tuple[int, int], radius: int = 40) -> int:
    """目标点周围有多少橙色像素。

    橙色圆钮空输入时是**录音键**、有文字时是**发送键**，绝不能被误点。
    任何点击之前都先跑这个：>40 就放弃。
    """
    x, y = center
    img = s.grab((x - radius, y - radius, radius * 2, radius * 2))
    if img.size == 0:
        return 999
    b = img[:, :, 0].astype(int)
    r = img[:, :, 2].astype(int)
    return int(((r - b > 45) & (r > 145)).sum())


def safe_click(s: Screen, target, *, radius: int = 40) -> tuple[bool, str]:
    """带橙色护盾的点击。返回 (是否点了, 原因)。"""
    if isinstance(target, tuple):
        cx, cy = target
    else:
        cx, cy = target.center
    ink = orange_near(s, (cx, cy), radius)
    if ink > 40:
        return False, f"目标 {radius}px 内有 {ink} 个橙色像素，疑似录音/发送键，拒绝点击"
    s.click_xy(cx, cy)
    return True, "ok"


def _models(writer: str) -> dict:
    return MODELS.get(writer, {})


def _items_visible(s: Screen, writer: str) -> bool:
    """列表开着吗？

    不能只凭"某一个模型模板命中"：页面上别处也会出现"默认"这类词，会误判
    （实测：误判成已开 → 跳过打开 → 后面全盘读空）。
    判据改成：找到勾，或至少两项模型标题同时命中。
    """
    if s.find("gpt_check", threshold=0.85) is not None:
        return True
    n = sum(1 for _n, tpl in _models(writer).values() if s.find(tpl, threshold=0.85) is not None)
    return n >= 2


def _open_model_list(s: Screen, writer: str) -> bool:
    """点开模型列表。

    两种模式的弹层结构不一样，所以"模型入口"要试两个偏移：
      聊天：芯片 → 弹层里的「选择模型」一行
      工作：芯片 → 弹层里的「GPT-x.x Xxx ›」一行（芯片上写的是"选择推理强度"，不是模型名）
    先判状态：弹层已经开着时再点芯片会把它关掉（实测踩过）。
    """
    if _items_visible(s, writer):
        return True
    if page_kind(s) != "new":
        return False       # 会话页没有模式按钮，任何偏移都会点空
    v = _voice_center(s)
    if v is None:
        return False
    # 两种模式的"模型入口"位置不同，按模式选，不要盲试两个——
    # 盲试时先点错位置会碰到滑杆，把弹层状态搞乱（实测）。
    dx, dy = (-100, 36) if writer == "work" else (MENU_MODEL_DX, MENU_MODEL_DY)
    for _ in range(2):
        s.press("esc")
        time.sleep(0.35)
        ok, why = safe_click(s, (v[0] + CHIP_DX, v[1] + CHIP_DY))
        if not ok:
            return False            # 橙色护盾拦下＝再点就是录音/发送
        time.sleep(0.9)
        if _items_visible(s, writer):
            return True
        ok, why = safe_click(s, (v[0] + dx, v[1] + dy))
        if not ok:
            return False
        time.sleep(1.0)
        if _items_visible(s, writer):
            return True
    return False


def _find_item(s: Screen, writer: str, tpl: str, scroll: bool = True):
    """找模型项；找不到就往下滚再找（工作模式有 9 项，一屏放不下）。"""
    m = s.find(tpl, threshold=0.85)
    if m is not None or not scroll:
        return m
    import ctypes

    # 滚轮滚的是鼠标底下那个东西：先把鼠标移到菜单里，否则滚的是整个页面
    anchor = None
    for _n, t in _models(writer).values():
        anchor = s.find(t, threshold=0.85)
        if anchor is not None:
            break
    if anchor is not None:
        s.move_to(*anchor.center)
        time.sleep(0.25)

    for _ in range(3):
        ctypes.windll.user32.mouse_event(0x0800, 0, 0, -120, 0)   # wheel down
        time.sleep(0.45)
        m = s.find(tpl, threshold=0.85)
        if m is not None:
            return m
    return None


def read_model(s: Screen, writer: str) -> dict:
    """读当前模型：找那个勾，看它离哪一行最近。

    不能用"数深色像素"：工作模式是白底浅色主题，文字本身就是深色，
    数出来的是字而不是勾（实测把 GPT-6 Astra 误判成选中，而实际是 GPT-6.1 Terra）。
    勾是固定形状的小图标，用模板找最稳，深浅主题都能用。
    """
    models = _models(writer)
    if not models:
        return {"model": None}
    if not _open_model_list(s, writer):
        time.sleep(0.5)
        if not _open_model_list(s, writer):
            return {"model": "unknown", "hint": "打不开模型列表"}

    chk = s.find("gpt_check", threshold=0.85)
    best, best_d, dists = None, 1e9, {}
    if chk is not None:
        cy = chk.center[1]
        for k, (_d, tpl) in models.items():
            m = s.find(tpl, threshold=0.85)
            if m is None:
                continue
            d = abs(m.center[1] - cy)
            dists[k] = int(d)
            if d < best_d:
                best, best_d = k, d
    s.press("esc")
    time.sleep(0.3)
    if best is None or best_d > 20:
        return {"model": "unknown", "dist": dists, "check_found": chk is not None}
    return {"model": best, "dist": dists}


def set_model(s: Screen, key: str) -> dict:
    writer = read_writer_gpt(s)["writer"]
    models = _models(writer)
    if key not in models:
        return {"ok": False, "stage": "bad_arg", "want": key, "writer": writer,
                "options": list(models), "hint": f"当前是 {writer} 模式，模型名要从这个列表里选"}
    cur = read_model(s, writer)["model"]
    if cur == key:
        return {"ok": True, "stage": "already", "before": cur, "after": cur}
    if not _open_model_list(s, writer):
        return {"ok": False, "stage": "menu_not_open", "before": cur}
    m = _find_item(s, writer, models[key][1])
    if m is None:
        s.press("esc")
        return {"ok": False, "stage": "item_not_found", "before": cur, "want": key}
    s.click(m)
    time.sleep(1.3)
    s.move_to(-1200, 100)
    time.sleep(0.4)
    after = read_model(s, writer)["model"]
    return {"ok": after == key, "stage": "done" if after == key else "unverified",
            "before": cur, "after": after}


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


# 模式按钮组相对窗口左上角的横向范围是固定的，**纵向会移动**（实测两次差 22px），
# 所以 y 每次现扫，不能写死。
# 模式按钮用「聊天」两个字的文字模板定位，不写死坐标。
# 归一化相关对亮度不敏感，所以黑字（选中）和灰字（未选）都能匹配同一张模板（实测切完仍 1.000）。
# 两个按钮中心相距 114px，宽度固定，所以右按钮 = 左按钮 + 114。
TAB_WORD = "gpt_tab_chat_word"
TAB_GAP = 114
TAB_SAMPLE = (22, 10)      # 采样半宽/半高


def _tab_centers(s: Screen):
    w = s.find(TAB_WORD, threshold=0.75)
    if w is None:
        return None
    cx, cy = w.center
    return (cx, cy), (cx + TAB_GAP, cy)


def read_writer_gpt(s: Screen) -> dict:
    """读当前是聊天还是工作：比两边文字的墨色。

    不用像素差比整组：这按钮的边框样式会随页面变（有时白胶囊、有时白胶囊+黑边框），
    模板两态都截不全，diff 常在 10~12 之间，等于读不出来（实测）。
    也不写死坐标：按钮随侧栏/窗口变化，之前固定 x 一改窗口就废。

    判据是**文字深浅**：选中那个字是纯黑（min≈13），未选是灰（min≈93），差 80。
    """
    centers = _tab_centers(s)
    if centers is None:
        return {"writer": "unknown", "hint": "找不到「聊天/工作」按钮（可能不在新聊天页）"}
    (lx, ly), (rx, ry) = centers
    hw, hh = TAB_SAMPLE

    def darkest(cx, cy):
        img = s.grab((cx - hw, cy - hh, hw * 2, hh * 2))
        if img.size == 0:
            return 255
        return int(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).min())

    lm, rm = darkest(lx, ly), darkest(rx, ry)
    info = {"left_min": lm, "right_min": rm, "at": [lx, ly, rx, ry]}
    if lm <= 60 and lm + 30 <= rm:
        return {"writer": "chat", **info}
    if rm <= 60 and rm + 30 <= lm:
        return {"writer": "work", **info}
    return {"writer": "unknown", "hint": "两侧文字深浅差不多，认不出哪个被选中", **info}


def choose_writer_gpt(s: Screen, key: str) -> dict:
    """切模式。点击位置按左右半格算，切完轮询到读数变成目标为止（切换有动画）。"""
    if key not in WRITER.writers:
        return {"ok": False, "stage": "bad_arg", "want": key, "options": list(WRITER.writers)}
    cur = read_writer_gpt(s)
    if cur["writer"] == "unknown":
        return {"ok": False, "stage": "unreadable", "hint": cur.get("hint", "")}
    if cur["writer"] == key:
        return {"ok": True, "stage": "already", "before": cur["writer"], "after": cur["writer"]}

    centers = _tab_centers(s)
    if centers is None:
        return {"ok": False, "stage": "tabs_not_found", "before": cur["writer"]}
    x, y = centers[0] if key == "chat" else centers[1]
    ok, why = safe_click(s, (x, y), radius=30)
    if not ok:
        return {"ok": False, "stage": "guard_blocked", "detail": why, "before": cur["writer"]}

    after, deadline = cur, time.time() + 5.0
    while time.time() < deadline:
        time.sleep(0.6)
        s.move_to(x - 300, max(0, y - 400))
        time.sleep(0.15)
        after = read_writer_gpt(s)
        if after["writer"] == key:
            break
    ok = after["writer"] == key
    return {"ok": ok, "stage": "done" if ok else "unverified",
            "before": cur["writer"], "after": after["writer"]}


def main() -> int:
    # 安全停用：这台机器上 ChatGPT 的模型/模式按钮紧贴音频按钮，自动点击多次误触录音。
    # 在能保证"绝不碰橙色按钮"之前，宁可不做。只读也不给——读也要点菜单。
    print(json.dumps({
        "ok": False,
        "stage": "disabled_for_safety",
        "hint": "ChatGPT 的自动点击已停用：模型/模式按钮紧贴录音键，脚本多次误触录音。"
                "请在网页上手动切换；其他三端（deepseek/grok/gemini）不受影响。",
    }, ensure_ascii=False))
    return 1


def main_disabled() -> int:
    name = os.path.basename(__file__)
    idle_need, idle_wait, force, args = guard.parse_guard_args(list(sys.argv[1:]))
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
        print(f"  python {name} --model <名字>   （模型分模式，先定 --writer）")
        for wr, items in MODELS.items():
            print(f"    [{wr}] " + ", ".join(f"{k}({d})" for k, (d, _t) in items.items()))
        for k, v in EFFORTS.items():
            print(f"    {k:8} {v['desc']}")
        print("  说明：本端只支持强度；模型选择（弹层里「中 ›」那层子菜单）还没做")
        return 2
    if want is not None and want not in EFFORTS:
        print(f"没有这个档位：{want}；可选 {', '.join(EFFORTS)}")
        return 2

    lib = Library()
    need = ([cfg["chip"] for cfg in EFFORTS.values()]
            + ["gpt_writer_tabs_chat", "gpt_writer_tabs_work", "gpt_check"]
            + [tpl for items in MODELS.values() for _d, tpl in items.values()])
    print(lib.report(need))
    if lib.missing(need):
        return 3

    if not guard.guard(idle_need, idle_wait, force):
        return 4
    s = Screen()
    if not s.bring_to_front("Google Chrome", on_secondary=True):
        print("找不到浏览器窗口")
        return 1
    if tab:
        s.hotkey("ctrl", str(tab))
        time.sleep(1.8)

    if writer is not None:
        # 只有真要切模式才需要回新对话页——模式按钮只在那里
        _open_new_chat(s, _NEWCHAT)
        time.sleep(1.0)

    if do_list:
        # 读状态不许改页面：不再顺手点「新聊天」，否则会把用户正在聊的会话重置掉
        out = dict(read_current(s))
        out.update(read_writer_gpt(s))
        if out.get("writer") in (None, "unknown"):
            out["note"] = "模式按钮只在新聊天页出现；这里不改页面，所以读不到模式。要读请先手动开新聊天。"
        else:
            out.update(read_model(s, out["writer"]))
        print(json.dumps(out, ensure_ascii=False))
        return 0

    rc = 0
    if writer is not None:
        w = choose_writer_gpt(s, writer)
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
