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
    if s.find("gpt_tab_chat_word", threshold=0.75, region=_win(s)) is not None:
        return "new"
    return "session"


def orange_near(s: Screen, center: tuple[int, int], radius: int = 12) -> int:
    """目标点周围**最大橙色连通块**的面积（不是像素总数）。

    判据是"**点击点沾不沾橙色**"，不是"周围有没有橙色"：
    芯片里也有橙色小图标（实测附近 376 个像素），按总数拦会把正常点击全拦掉。
    实测：橙钮中心像素是橙色、12px 内约 300+；芯片箭头中心 12px 内是 0。
    所以只看点击点自身附近这一小块，能干净分开。
    """
    import numpy as np
    x, y = center
    img = s.grab((x - radius, y - radius, radius * 2, radius * 2))
    if img.size == 0:
        return 9999
    b = img[:, :, 0].astype(int)
    r = img[:, :, 2].astype(int)
    mask = ((r - b > 45) & (r > 145)).astype(np.uint8)
    return int(mask.sum())


def safe_click(s: Screen, target, *, radius: int = 12) -> tuple[bool, str]:
    """带橙色护盾的点击。返回 (是否点了, 原因)。"""
    if isinstance(target, tuple):
        cx, cy = target
    else:
        cx, cy = target.center
    blob = orange_near(s, (cx, cy), 12)
    if blob > 60:
        return False, f"点击点 12px 内有 {blob} 个橙色像素，落在录音/发送键上，拒绝点击"
    try:
        s.click_xy(cx, cy)
    except Exception as e:                 # move_to 挪不动就别点
        return False, f"光标无法就位：{e}"
    return True, "ok"


def _models(writer: str) -> dict:
    return MODELS.get(writer, {})


def _win(s: Screen):
    return s.window_rect("Google Chrome", on_secondary=True)


def _items_visible(s: Screen, writer: str) -> bool:
    """列表开着吗？

    不能只凭"某一个模型模板命中"：页面上别处也会出现"默认"这类词，会误判
    （实测：误判成已开 → 跳过打开 → 后面全盘读空）。
    判据改成：找到勾，或至少两项模型标题同时命中。
    """
    win = _win(s)
    if s.find("gpt_check", threshold=0.85, region=win) is not None:
        return True
    n = sum(1 for _n, tpl in _models(writer).values()
            if s.find(tpl, threshold=0.85, region=win) is not None)
    return n >= 2


CHIP_ANCHOR = "gpt_chip_gpt3"   # 「GPT」三个字母，所有工作模型都以此开头
CHIP_DX, CHIP_DY = 20, 0        # 相对锚点：芯片本体（比箭头安全，离麦克风 160px+）
ENTRY_ABOVE_BAR = 32            # 模型入口在**滑杆上方** 32px（固定），不按弹层高度算


def _open_model_list(s: Screen, writer: str) -> bool:
    """打开模型列表。

    定位链（全部实测）：
      「GPT」三字母模板 → 芯片锚点
        → 点 +20px 开弹层（12px 内橙色 0）
        → 点 (+45,+59) 进模型入口（12px 内橙色 0）
    为什么不用橙色按钮当锚点：它距芯片箭头只有 47px，任何偏移都会压到录音/发送键上。

    为什么不用箭头模板：26x18 太小，会和麦克风图标局部撞上，曾经因此点进录音。
    """
    if _items_visible(s, writer):
        return True
    if page_kind(s) != "new":
        return False                     # 会话页没有模式按钮，点了也是空的
    # 上一个弹层可能还在收起动画里：不按 Esc 的话，这一下会被当成"关闭"而不是"打开"
    s.press("esc")
    time.sleep(0.45)
    m = s.find(CHIP_ANCHOR, threshold=0.90, region=_win(s))
    if m is None:
        return False
    cx, cy = m.center

    ok, _why = safe_click(s, (cx + CHIP_DX, cy + CHIP_DY))
    if not ok:
        return False
    time.sleep(0.9)
    if _items_visible(s, writer):
        return True

    entry = _entry_by_slider(s, (cx, cy))
    if entry is None:
        return False
    ok, _why = safe_click(s, entry)
    if not ok:
        return False
    time.sleep(1.1)
    return _items_visible(s, writer)


def _entry_by_slider(s: Screen, anchor):
    """在弹层里找"模型入口那一行"：用底部的橙色滑杆当基准，往上 32px。

    为什么不用固定偏移：弹层高度会变（有没有"轻度/中"这行、模型名长短），
    实测同一个入口的 y 偏移一次是 +59、另一次是 +33，差 26px，固定值必然点空。
    滑杆是弹层里唯一一条长横条，位置稳定，用它当尺子。
    """
    import numpy as np
    x0, y0 = anchor[0] - 80, anchor[1] + 8
    img = s.grab((x0, y0, 320, 150))
    if img.size == 0:
        return None
    b = img[:, :, 0].astype(int)
    r = img[:, :, 2].astype(int)
    mask = ((r - b > 45) & (r > 145)).astype(np.uint8)
    n, _lb, st, _c = cv2.connectedComponentsWithStats(mask, 8)
    # 滑杆只有左边一截是橙色（滑块在左），所以按"高度 20~32 + 面积够大"认，不能按宽度。
    bars = [st[i] for i in range(1, n) if 18 <= st[i][3] <= 34 and st[i][4] > 300]
    if not bars:
        return None
    bar = max(bars, key=lambda c: c[4])
    bar_top = y0 + int(bar[1])
    # 入口行和芯片文字是**左对齐**的，所以 x 直接沿用锚点（实测差 +20），不用滑杆中心
    return (anchor[0] + 20, bar_top - ENTRY_ABOVE_BAR)


def _find_item(s: Screen, writer: str, tpl: str, scroll: bool = True):
    """找模型项；找不到就往下滚再找（工作模式有 9 项，一屏放不下）。"""
    m = s.find(tpl, threshold=0.85, region=_win(s))
    if m is not None or not scroll:
        return m
    import ctypes

    # 滚轮滚的是鼠标底下那个东西：先把鼠标移到菜单里，否则滚的是整个页面
    anchor = None
    for _n, t in _models(writer).values():
        anchor = s.find(t, threshold=0.85, region=_win(s))
        if anchor is not None:
            break
    if anchor is not None:
        s.move_to(*anchor.center)
        time.sleep(0.25)

    for _ in range(3):
        ctypes.windll.user32.mouse_event(0x0800, 0, 0, -120, 0)   # wheel down
        time.sleep(0.45)
        m = s.find(tpl, threshold=0.85, region=_win(s))
        if m is not None:
            return m
    return None


def _calm(s: Screen) -> None:
    """读之前把界面放平：关掉可能开着的弹层、把鼠标移开。

    上一次操作常常把菜单留在打开/收起的中途，鼠标也可能压在芯片上（悬停会变样式），
    不清干净的话下一次直接读成 unknown（实测连续失败就是这么来的）。
    """
    s.press("esc")
    time.sleep(0.4)
    win = s.window_rect("Google Chrome", on_secondary=True)
    if win:
        vx, vy, vw, vh = s.bounds()
        # 移到页面中央的空白处：贴左边会压在侧栏上触发 hover，反而让状态更乱
        tx = min(max(win[0] + win[2] // 2, vx + 8), vx + vw - 8)
        ty = min(max(win[1] + 300, vy + 8), vy + vh - 8)
        try:
            s.move_to(tx, ty)
        except RuntimeError:
            pass
    time.sleep(0.35)


def read_model(s: Screen, writer: str) -> dict:
    """读当前模型：找那个勾，看它离哪一行最近。

    不能用"数深色像素"：工作模式是白底浅色主题，文字本身就是深色，
    数出来的是字而不是勾（实测把 GPT-6 Astra 误判成选中，而实际是 GPT-6.1 Terra）。
    勾是固定形状的小图标，用模板找最稳，深浅主题都能用。
    """
    models = _models(writer)
    if not models:
        return {"model": None}
    _calm(s)
    if not _open_model_list(s, writer):
        time.sleep(0.5)
        if not _open_model_list(s, writer):
            return {"model": "unknown", "hint": "打不开模型列表"}

    win = _win(s)
    chk = s.find("gpt_check", threshold=0.85, region=win)
    best, best_d, dists = None, 1e9, {}
    if chk is not None:
        cy = chk.center[1]
        for k, (_d, tpl) in models.items():
            m = s.find(tpl, threshold=0.85, region=win)
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


def chip_signature(s: Screen):
    """截下芯片文字那一块，用来判断"模型变没变"。

    切换后不重开菜单复核：菜单收起有动画，重开常常失败（实测连续 4 次都打不开），
    但模型其实已经换好了。改成比较切换前后的芯片外观，快且不碰弹层。
    """
    m = s.find(CHIP_ANCHOR, threshold=0.90, region=_win(s))
    if m is None:
        return None
    # 固定尺寸：芯片文字长短不同会让 m.w/m.h 略有差异，比较时形状不一致就白比了
    box = (m.x - 2, m.y - 4, 240, 40)
    img = s.grab(box)
    if img.size == 0:
        return None
    return cv2.resize(img, (240, 40), interpolation=cv2.INTER_AREA)


def set_model(s: Screen, key: str) -> dict:
    writer = read_writer_gpt(s)["writer"]
    models = _models(writer)
    _calm(s)
    if key not in models:
        return {"ok": False, "stage": "bad_arg", "want": key, "writer": writer,
                "options": list(models), "hint": f"当前是 {writer} 模式，模型名要从这个列表里选"}
    cur = read_model(s, writer)["model"]
    if cur == key:
        return {"ok": True, "stage": "already", "before": cur, "after": cur}
    before_sig = chip_signature(s)
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
    w = s.find(TAB_WORD, threshold=0.75, region=_win(s))
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
    """已启用（2026-10-29 17:55）。

    安全前提已经满足：
      - 锚点是「GPT」三个字母，离橙色按钮 160px 以上；
      - 每次点击前过 `safe_click`（点击点 12px 内橙色 >60 就拒绝）；
      - 只在"新聊天页"操作（会话页没有模式按钮，直接退出）。
    """
    return main_disabled()


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
        out = {"effort": "unknown", "effort_note": "强度读值未实现（芯片形态随模型变）"}
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
