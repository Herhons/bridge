"""chatgpt 网页版：只做选择，不发提问。

用法：
    python chatgpt_choose.py --list [--tab N]
    python chatgpt_choose.py --writer chat|work
    python chatgpt_choose.py --model gpt61_terra        （模型分模式，先定 --writer）
    python chatgpt_choose.py --writer work --model gpt6_sol

打开「选择推理强度」弹层的正确方式是**快捷键 Ctrl+Shift+M**（芯片 tooltip 上写着），
前提是输入框处于聚焦状态，所以链路是：点输入框空白 → 按快捷键 → 点弹层里的
「GPT-…›」那一行 → 进模型列表。**全程不碰右下角那个橙色圆钮。**

为什么不能用"橙钮 + 偏移"去点芯片（2026-10-29 实测，踩了两次录音）：
    · 橙钮左侧 43px 是**麦克风图标**，用 -40 / -44 各踩了一次，一点就开始录音；
    · 橙钮左侧 74px 才是芯片箭头，但芯片文字长短会变（「选择推理强度」比
      「GPT-6.1 Terra Max」宽一倍），右边有时还多一个刷新图标，偏移量追不上；
    · 点 -110（文字中间）只让输入框获得焦点，弹层不出现。
根因不是偏移量没调准，是**用参照物算坐标**这件事本身错：那个参照物自己在变
（空箱是麦克风、有字是发送）。参照物只能用来"取坐标"，不能用来"算坐标"。

本端只做到「选模式 + 选模型」。思考强度（instant/mid/high）还没做：
滑杆的几何要重新量（老代码的 TRACK_DX 是挂在橙钮上的，已经不能用了），
而且改强度会实际发请求，等页面空下来再补。
"""

from __future__ import annotations

import json
import os
import sys
import time

import cv2
import numpy as np

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import ForegroundLost, Library, Screen, guard  # noqa: E402
from qi.flow import Site, _open_new_chat  # noqa: E402

TITLE = "Google Chrome"

# ---------------------------------------------------------------- 模式（聊天/工作）
# 两个词**各自**模板定位，各在自己的框里取最暗值。
# 不用"左词 + 固定间距"推右词：间距随窗口宽度变（老代码写死 114，实测 100）。
TAB_WORDS = {"chat": "gpt_tab_chat_word", "work": "gpt_tab_work_word"}
TAB_INK_ON = 55          # 选中词的最暗值上限（实测 13）
TAB_INK_GAP = 25         # 两词至少要差这么多（实测 13 vs 93）

# ---------------------------------------------------------------- 模型
# 两种模式的列表完全不同（聊天 2 项 / 工作 9 项）
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
# 「默认（推荐模型集）」停不住：点下去它解析成某个具体模型，芯片上写的也是那个名字，
# 所以永远复核不上，只按"菜单关闭 + 芯片变了"验证，不参与读值。
NO_READ = {"default"}

CHECK = "gpt_check"                  # 列表里当前项左边/右边的勾
COMPOSER = "gpt_composer"            # 输入框左下角那个「+」
COMPOSER_DX, COMPOSER_DY = 250, -20  # 从「+」点到输入框空白处（只为了聚焦）
CHIP_HOTKEY = ("ctrl", "shift", "m")  # 芯片 tooltip 上写的快捷键

# ---------------------------------------------------------------- 操作节流
# 2026-10-29：为了"稳"加循环重试，同一个位置半分钟点了十几次，把工作额度打到剩 3%。
# 硬线：单次运行点击封顶、两下之间强制间隔、距橙钮太近直接拒绝。
CLICK_BUDGET = 8
CLICK_MIN_GAP = 0.35
CLICK_SAFE_R = 60
_click = {"n": 0, "last": 0.0}


def _win(s: Screen):
    return s.window_rect(TITLE, on_secondary=True)


def _voice_center(s: Screen):
    """右下角橙色圆钮的中心（屏幕坐标）。

    **只用来算安全距离，绝不对它点击。** 空箱时它是麦克风、有字时是发送键，
    两者位置相同、热区相邻，点它或点它旁边 40px 都会出事。
    所以这里只做一件事：给出一个"禁区圆心"。

    只认近似正方形的小块（20~60px、面积>400），取最靠右下的那个——
    侧栏里也有橙色头像/图标，按面积最大取会抓错（实测返回过窗口 x=334 的位置）。
    """
    win = _win(s)
    if win is None:
        return None
    img = s.grab(win)
    if img.size == 0:
        return None
    b = img[:, :, 0].astype(int)
    r = img[:, :, 2].astype(int)
    orange = ((r - b > 45) & (r > 145)).astype(np.uint8)
    n, _lb, st, _c = cv2.connectedComponentsWithStats(orange, 8)
    cands = [(int(st[i][4]), int(st[i][0]), int(st[i][1]), int(st[i][2]), int(st[i][3]))
             for i in range(1, n)
             if 20 <= st[i][2] <= 60 and 20 <= st[i][3] <= 60 and st[i][4] > 400]
    if not cands:
        return None
    _a, x, y, w, h = max(cands, key=lambda c: c[1] + c[2])
    return (win[0] + x + w // 2, win[1] + y + h // 2)


def orange_near(s: Screen, center: tuple[int, int], radius: int = 12) -> int:
    """点击点周围 12px 内的橙色像素数（判"这一点是不是压在橙钮上"）。"""
    x, y = center
    img = s.grab((x - radius, y - radius, radius * 2, radius * 2))
    if img.size == 0:
        return 9999
    b = img[:, :, 0].astype(int)
    r = img[:, :, 2].astype(int)
    mask = ((r - b > 45) & (r > 145)).astype(np.uint8)
    return int(mask.sum())


def safe_click(s: Screen, target, *, voice=None) -> tuple[bool, str]:
    """所有点击都必须走这里。返回 (是否点了, 原因)。

    三道闸，从硬到软：
      1. 预算/间隔——单次运行封顶 8 下，两下之间至少 0.35s；
      2. 距离——点击点离橙钮必须 ≥60px。**这条才是挡麦克风的关键**：
         麦克风是灰白色图标，橙色护盾对它完全无效，只有距离能挡；
      3. 橙色——点击点 12px 内橙色 >60 就拒绝（挡圆钮本身）。
    """
    if isinstance(target, tuple):
        cx, cy = target
    else:
        cx, cy = target.center
    cx, cy = int(cx), int(cy)

    if _click["n"] >= CLICK_BUDGET:
        return False, f"本次运行已点 {_click['n']} 下，达到上限 {CLICK_BUDGET}，停止"
    gap = CLICK_MIN_GAP - (time.time() - _click["last"])
    if gap > 0:
        time.sleep(gap)

    if voice is None:
        voice = _voice_center(s)
    if voice is not None:
        d = max(abs(cx - int(voice[0])), abs(cy - int(voice[1])))
        if d < CLICK_SAFE_R:
            return False, f"点击点离橙色圆钮只有 {d}px（<{CLICK_SAFE_R}），会压到麦克风/发送键，拒绝"
        # 橙色检查只在橙钮附近做。全屏查橙色会把**弹层里那个橙色的档位字**也算进去：
        # 入口行就在「轻度」下面 2px，实测因此被拦（"12px 内有 102 个橙色像素"）。
        # 半径取 100：麦克风最危险时的距离是 43px，100 足够覆盖，又放得开入口行（131px）。
        if d < 100:
            blob = orange_near(s, (cx, cy), 12)
            if blob > 60:
                return False, f"点击点 12px 内有 {blob} 个橙色像素，落在录音/发送键上，拒绝"
    else:
        blob = orange_near(s, (cx, cy), 12)
        if blob > 60:
            return False, f"点击点 12px 内有 {blob} 个橙色像素，落在录音/发送键上，拒绝"

    try:
        s.click_xy(cx, cy)
    except ForegroundLost:
        raise                                 # 前台丢了是全局状态，交给上层中止，不当成"点歪了"
    except Exception as e:                    # 光标挪不过去就别点
        return False, f"光标无法就位：{e}"
    _click["n"] += 1
    _click["last"] = time.time()
    return True, "ok"


# ---------------------------------------------------------------- 页面类型
def page_kind(s: Screen) -> str:
    """新聊天页 vs 会话页。

    模式按钮只在新聊天页出现，输入框也在页面中间；
    会话页没有「聊天/工作」，输入框贴底。拿新页的坐标去会话页点，必然点空。
    """
    win = _win(s)
    if win is None:
        return "session"
    for tpl in TAB_WORDS.values():
        if s.find(tpl, threshold=0.75, region=win) is not None:
            return "new"
    return "session"


# ---------------------------------------------------------------- 模式读写
def _tab_boxes(s: Screen) -> dict:
    """两个模式词各自的 bbox（屏幕坐标），各自独立匹配。"""
    win = _win(s)
    out = {}
    for key, tpl in TAB_WORDS.items():
        m = s.find(tpl, threshold=0.75, region=win)
        if m is not None:
            out[key] = m
    return out


def _ink(s: Screen, box) -> int:
    """一个词框里最暗的像素值。框跟着匹配结果走，位置漂多少都不影响。"""
    img = s.grab((box.x, box.y, box.w, box.h))
    if img.size == 0:
        return 255
    return int(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).min())


def read_writer(s: Screen) -> dict:
    """读当前是聊天还是工作：比两个词**各自**的墨色。

    选中那个是近黑（实测 13），未选是灰（实测 93），差 80，很干净。
    之所以不用整组按钮的像素差：两态胶囊底色只差十几个灰阶（10~12），
    淹没在噪声里；也不用固定框取样：按钮纵向会漂 22px，框会切到边框上。
    """
    boxes = _tab_boxes(s)
    if len(boxes) < 2:
        miss = [k for k in TAB_WORDS if k not in boxes]
        return {"writer": "unknown",
                "hint": f"找不到模式词 {miss}（可能不在新聊天页，或缩放变了要重截模板）"}
    inks = {k: _ink(s, m) for k, m in boxes.items()}
    info = {"ink": inks, "at": {k: [m.x, m.y, m.w, m.h] for k, m in boxes.items()}}
    c, w = inks["chat"], inks["work"]
    if c <= TAB_INK_ON and w - c >= TAB_INK_GAP:
        return {"writer": "chat", **info}
    if w <= TAB_INK_ON and c - w >= TAB_INK_GAP:
        return {"writer": "work", **info}
    return {"writer": "unknown", "hint": "两个词墨色一样，认不出哪个被选中", **info}


def choose_writer(s: Screen, key: str) -> dict:
    """切模式。点的是目标词**自己**的框中心。"""
    if key not in TAB_WORDS:
        return {"ok": False, "stage": "bad_arg", "want": key, "options": list(TAB_WORDS)}
    cur = read_writer(s)
    if cur["writer"] == "unknown":
        return {"ok": False, "stage": "unreadable", "hint": cur.get("hint", "")}
    if cur["writer"] == key:
        return {"ok": True, "stage": "already", "before": cur["writer"], "after": cur["writer"]}

    boxes = _tab_boxes(s)
    if key not in boxes:
        return {"ok": False, "stage": "tabs_not_found", "before": cur["writer"]}
    x, y = boxes[key].center
    ok, why = safe_click(s, (x, y))
    if not ok:
        return {"ok": False, "stage": "click_blocked", "detail": why, "before": cur["writer"]}

    after, deadline = cur, time.time() + 5.0
    while time.time() < deadline:
        time.sleep(0.6)
        s.move_to(x - 300, y - 120)
        time.sleep(0.15)
        after = read_writer(s)
        if after["writer"] == key:
            break
    ok = after["writer"] == key
    return {"ok": ok, "stage": "done" if ok else "unverified",
            "before": cur["writer"], "after": after["writer"]}


# ---------------------------------------------------------------- 菜单
def _items_visible(s: Screen, writer: str) -> bool:
    """模型列表开着吗。

    不能只凭"某一项命中"：页面上别处也会出现「默认」这类词（实测误判成已开，
    后面全盘读空）。判据：找到勾，或至少两项模型标题同时命中。
    """
    win = _win(s)
    if s.find(CHECK, threshold=0.85, region=win) is not None:
        return True
    n = sum(1 for _n, tpl in MODELS.get(writer, {}).values()
            if s.find(tpl, threshold=0.85, region=win) is not None)
    return n >= 2


def _menu_entry_pos(s: Screen):
    """算弹层里入口行（「GPT-6.1 Terra ›」）的坐标，**完全不看文字**。

    为什么不用文字模板：入口行写的是当前模型名，切一次模型就换一次字
    （「GPT-6.1 Terra」→「GPT-6 Sol」），模板必然失效——实测切到 GPT-6 Sol 后
    匹配分数掉到 0.71 以下，于是"打不开模型列表"。

    改用几何：弹层里从上到下三样东西——档位字（轻度/中/高）、入口行、滑杆。
    取"最上面那个块的上边"和"最下面那个块的下边"的中点，就是入口行的高度；
    横向取所有块的外接范围的中心。

    判据用**非背景**（灰度<210）而不是橙色：档位字笔画细，橙色像素只有 40 来个，
    按橙色面积筛会把它滤掉，只剩滑杆一个块（实测因此返回 None、菜单打不开）。
    """
    win = _win(s)
    v = _voice_center(s)
    if win is None or v is None:
        return None
    img = s.grab(win)
    if img.size == 0:
        return None
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = (g < 210).astype(np.uint8)
    n, _lb, st, _c = cv2.connectedComponentsWithStats(mask, 8)
    vx, vy = int(v[0]) - win[0], int(v[1]) - win[1]
    x0s, y0s, x1s, y1s = [], [], [], []
    for i in range(1, n):
        x, y, w, h, a2 = (int(st[i][0]), int(st[i][1]), int(st[i][2]),
                          int(st[i][3]), int(st[i][4]))
        if a2 < 20:
            continue
        if not (vy + 15 <= y and y + h <= vy + 170):     # 弹层在橙钮下方这一段
            continue
        if not (vx - 480 <= x and x + w <= vx - 10):     # 整块在橙钮左边
            continue
        x0s.append(x)
        y0s.append(y)
        x1s.append(x + w)
        y1s.append(y + h)
    if len(x0s) < 2:
        return None

    # 纵向：入口行夹在"最上面那一行"和"滑杆"之间
    min_y = min(y0s)
    top_bottom = max(y1 for y0, y1 in zip(y0s, y1s) if y0 <= min_y + 12)
    lower_top = min((y0 for y0 in y0s if y0 > min_y + 20), default=None)
    if lower_top is None:
        return None
    entry_y = (top_bottom + lower_top) // 2

    # 横向：入口行和档位字同轴。但最上面那一行最左边还挂着一个菱形图标
    # （实测 x=973，和档位字之间空 65px），不切掉会把中心拽偏 35px，点空。
    top_blocks = sorted((x0, x1) for x0, y0, x1 in zip(x0s, y0s, x1s) if y0 <= min_y + 12)
    cut = 0
    best_gap = 0
    for i in range(1, len(top_blocks)):
        g = top_blocks[i][0] - top_blocks[i - 1][1]
        if g > best_gap:
            best_gap, cut = g, i
    if best_gap > 40:
        top_blocks = top_blocks[cut:]
    entry_x = (top_blocks[0][0] + top_blocks[-1][1]) // 2
    return (win[0] + entry_x, win[1] + entry_y)


def focus_composer(s: Screen) -> bool:
    """点一下输入框空白处，让它获得键盘焦点。

    快捷键只在输入框聚焦时生效（实测：不聚焦按 Ctrl+Shift+M 毫无反应）。
    点的是「+」右边 250px、上 20px 的空白，离橙钮 400px 以上。
    """
    m = s.find(COMPOSER, threshold=0.85, region=_win(s))
    if m is None:
        return False
    ok, _why = safe_click(s, (m.center[0] + COMPOSER_DX, m.center[1] + COMPOSER_DY))
    return ok


def open_menu(s: Screen, writer: str) -> bool:
    """打开模型列表。

    链路：点输入框空白 → Ctrl+Shift+M 开强度弹层 → 点「GPT-…›」进模型列表。

    **不按 Esc**：上一个弹层（比如刚选完「默认」时那个「选择模型」层）在 Esc 之后
    要过好几百毫秒才收完，这期间的第一次点击会被当成"点外部关闭"吃掉，
    结果就是输入框没聚焦、快捷键没反应、整条链报 menu_not_open（实测连断两个）。
    而"点输入框空白"这一下同时干两件事：关掉残留弹层 + 把键盘焦点给输入框。

    只走一遍，不重试：重试不会让定位变准，只会把点击频率堆上去。
    """
    if _items_visible(s, writer):
        return True
    if page_kind(s) != "new":
        return False

    if not focus_composer(s):
        return False
    time.sleep(0.5)

    s.hotkey(*CHIP_HOTKEY)
    time.sleep(1.2)
    if _items_visible(s, writer):
        return True
    pos = _menu_entry_pos(s)
    if pos is None:
        return False
    ok, _why = safe_click(s, pos)
    if not ok:
        return False
    time.sleep(1.2)
    return _items_visible(s, writer)


def _find_item(s: Screen, writer: str, tpl: str, scroll: bool = True):
    """找模型项；找不到就往下滚再找（工作模式 9 项，一屏放不下）。"""
    win = _win(s)
    m = s.find(tpl, threshold=0.85, region=win)
    if m is not None or not scroll:
        return m

    import ctypes

    # 滚轮滚的是鼠标底下那个东西：先把鼠标移到菜单里，否则滚的是整个页面
    anchor = None
    for _n, t in MODELS.get(writer, {}).values():
        anchor = s.find(t, threshold=0.85, region=win)
        if anchor is not None:
            break
    if anchor is not None:
        s.move_to(*anchor.center)
        time.sleep(0.25)

    for _ in range(3):
        ctypes.windll.user32.mouse_event(0x0800, 0, 0, -120, 0)   # wheel down
        time.sleep(0.45)
        m = s.find(tpl, threshold=0.85, region=win)
        if m is not None:
            return m
    return None


def chip_signature(s: Screen):
    """截下芯片那一块的外观，只用来判断"模型换没换"。

    不用模板定位：芯片上的字随模型变（「GPT-6.1 Terra Max」/「选择推理强度」），
    同一个模板不可能两种都匹上。用橙钮**取坐标**（只读位置，绝不点它），
    窗口往左开得宽一点把整个芯片包进去。
    """
    v = _voice_center(s)
    if v is None:
        return None
    img = s.grab((int(v[0]) - 240, int(v[1]) - 16, 210, 32))
    if img.size == 0:
        return None
    return cv2.resize(img, (210, 32), interpolation=cv2.INTER_AREA)


def _scan_model(s: Screen, writer: str):
    """扫一次列表：返回每一项相对勾的**带符号**距离（负=在勾上方）。

    用符号而不是绝对值，因为要区分两种"距离大"：
      · 勾在选中行，但那一行的模板没匹上（各项都在勾下方，全为正）；
      · 勾根本不在列表里（有正有负）。
    """
    win = _win(s)
    chk = s.find(CHECK, threshold=0.85, region=win)
    if chk is None:
        return None
    cy = chk.center[1]
    signed = {}
    for k, (_d, tpl) in MODELS.get(writer, {}).items():
        m = s.find(tpl, threshold=0.85, region=win)
        if m is None:
            continue
        signed[k] = int(m.center[1] - cy)
    if not signed:
        return None
    return {"signed": signed, "cy": cy}


def _pick_model(s: Screen, writer: str) -> dict:
    """在**已经开着的**菜单里读当前模型。不负责开关菜单。

    单独拆出来是因为一次切换要读两次（切之前、切之后），如果读一次就开关一遍菜单，
    一次 --model 要开 3 遍菜单、点 7 下，直接把点击预算撞穿——超预算之后每一下都被
    拒，表现为"第一个失败、后面全报打不开"（2026-10-29 实测）。

    「默认（推荐模型集）」那一行是**两行文字**（下面还有"推荐模型集"小字），
    比别的行高，它的模板只有 100x18，实测时灵时不灵。所以留一条兜底：
    勾在所有可读项**上方**时，选中的就是列表第一行「默认」。

    采信前要求**连续两次扫描结果一致**：列表淡入时读一次会得到
    "勾和每行都错开 36px"的等差假结果（实测）。
    """
    snap, prev = None, None
    for _ in range(6):
        cur = _scan_model(s, writer)
        if cur is not None and prev is not None and cur["signed"] == prev["signed"]:
            snap = cur
            break
        prev = cur
        time.sleep(0.4)
    if snap is None:
        snap = prev
    if snap is None:
        return {"model": "unknown", "hint": "列表里找不到勾，也匹不上任何模型名"}

    signed = snap["signed"]
    dists = {k: abs(v) for k, v in signed.items()}
    near = [k for k, d in dists.items() if d <= 20]
    if near:
        best = min(near, key=lambda k: dists[k])
        return {"model": best, "dist": dists}
    if all(v > 20 for v in signed.values()):
        return {"model": "default", "dist": dists,
                "inferred": "勾在所有可读项上方，选中的是第一行「默认」"}
    return {"model": "unknown", "dist": dists, "hint": "勾落在两行之间，认不出"}


def _close_menu(s: Screen, writer: str) -> None:
    """关列表并**等它真收完**：只 sleep 0.3s 的话，下一段逻辑的第一下点击会被
    残余的收起动画吃掉（实测连断两个）。"""
    s.press("esc")
    deadline = time.time() + 2.0
    while time.time() < deadline:
        time.sleep(0.2)
        if not _items_visible(s, writer):
            return


def read_model(s: Screen, writer: str) -> dict:
    """读当前模型（会开关一次菜单）。

    勾是固定形状的小图标，用模板最稳。不能数深色像素：浅色主题下文字本身就是深色，
    数出来的是字不是勾（实测把 GPT-6 Astra 误判成选中，实际是 GPT-6.1 Terra）。
    """
    if not MODELS.get(writer):
        return {"model": None}
    if not open_menu(s, writer):
        return {"model": "unknown", "hint": "打不开模型列表"}
    out = _pick_model(s, writer)
    _close_menu(s, writer)
    return out


def set_model(s: Screen, key: str) -> dict:
    """切模型。一次切换只开**两遍**菜单：切之前读一次、切之后复核一次。

    之前是"read_model 开一遍 + set_model 再开一遍 + 复核又开一遍"，
    一次调用点 7 下，撞穿 8 下的预算，超了以后每一下都被拒。
    """
    w = read_writer(s)["writer"]
    if w not in MODELS:
        return {"ok": False, "stage": "no_writer",
                "hint": "读不出当前是聊天还是工作，先用 --writer 定一下（只能在新聊天页做）"}
    models = MODELS[w]
    if key not in models:
        return {"ok": False, "stage": "bad_arg", "writer": w, "want": key,
                "options": list(models)}

    before_sig = chip_signature(s)
    if not open_menu(s, w):
        return {"ok": False, "stage": "menu_not_open", "writer": w}

    cur = _pick_model(s, w)["model"]
    if cur == key:
        _close_menu(s, w)
        return {"ok": True, "stage": "already", "before": cur, "after": cur}

    m = _find_item(s, w, models[key][1])
    if m is None:
        _close_menu(s, w)
        return {"ok": False, "stage": "item_not_found", "before": cur, "want": key}
    ok, why = safe_click(s, m.center)
    if not ok:
        _close_menu(s, w)
        return {"ok": False, "stage": "click_blocked", "detail": why, "before": cur}
    time.sleep(1.4)

    win = _win(s)
    if win:
        vx, vy, vw, vh = s.bounds()
        s.move_to(max(vx + 8, min(win[0] + 60, vx + vw - 8)),
                  max(vy + 8, min(win[1] + 60, vy + vh - 8)))
    time.sleep(0.4)

    if key in NO_READ:
        still = _items_visible(s, w)
        after_sig = chip_signature(s)
        changed = (before_sig is not None and after_sig is not None and
                   float(np.abs(before_sig.astype(int) - after_sig.astype(int)).mean()) > 2.0)
        ok2 = (not still) and changed
        return {"ok": ok2, "stage": "done_menu_closed" if ok2 else "unverified",
                "before": cur, "after": "（推荐模型集→具体模型，不可读）",
                "verified_by": "菜单关闭+芯片变化", "chip_changed": changed}

    after = read_model(s, w)["model"]
    return {"ok": after == key, "stage": "done" if after == key else "unverified",
            "before": cur, "after": after}


# ---------------------------------------------------------------- CLI
def main() -> int:
    name = os.path.basename(__file__)
    idle_need, idle_wait, force, args = guard.parse_guard_args(list(sys.argv[1:]))
    want_model, want_writer, tab, bad = None, None, None, []
    do_list = "--list" in args or "-l" in args

    def take(flag):
        i = args.index(flag)
        if i + 1 >= len(args):
            bad.append(f"{flag} 缺值")
            return None
        return args[i + 1].strip().lower()

    if "--model" in args:
        want_model = take("--model")
    if "--writer" in args:
        want_writer = take("--writer")
    if "--tab" in args:
        raw = take("--tab")
        if raw is not None:
            try:
                tab = int(raw)
            except ValueError:
                bad.append("--tab 要数字")
    if "--effort" in args:
        bad.append("--effort 还没做（滑杆几何要重量，会实际发请求，等页面空下来再补）")

    if bad:
        print("参数有问题：", bad)
        return 2
    if not do_list and want_model is None and want_writer is None:
        print("用法：")
        print(f"  python {name} --list [--tab N]")
        print(f"  python {name} --writer chat|work")
        print(f"  python {name} --model <名字>      （模型分模式，先定 --writer）")
        print("  --tab N  先切到第 N 个标签页（左边数）")
        print("  --idle 2 / --wait 120 / --force   你正在用电脑时是否等待")
        for wr, items in MODELS.items():
            print(f"    [{wr}] " + ", ".join(f"{k}({d})" for k, (d, _t) in items.items()))
        print("  说明：思考强度（instant/mid/high）暂未实现")
        return 2
    if want_writer is not None and want_writer not in TAB_WORDS:
        print(f"没有这个模式：{want_writer}；可选 {', '.join(TAB_WORDS)}")
        return 2
    if want_model is not None:
        known = set(MODELS["chat"]) | set(MODELS["work"])
        if want_model not in known:
            print(f"没有这个模型：{want_model}")
            for wr, items in MODELS.items():
                print(f"  [{wr}] " + ", ".join(items))
            return 2

    lib = Library()
    need = ([COMPOSER, CHECK]
            + list(TAB_WORDS.values())
            + [tpl for items in MODELS.values() for _d, tpl in items.values()])
    print(lib.report(need))
    if lib.missing(need):
        return 3

    if not guard.guard(idle_need, idle_wait, force):
        return 4
    s = Screen()
    if not s.bring_to_front(TITLE, on_secondary=True):
        print("找不到副屏的浏览器窗口")
        return 1
    if tab:
        s.hotkey("ctrl", str(tab))
        time.sleep(1.8)

    try:
        if want_writer is not None:
            # 模式按钮只在新对话页出现，所以切模式前先确保在新页
            _NEWCHAT = Site(name="chatgpt", title_hint=TITLE, composer="gpt_composer",
                            send="gpt_send_on", actions="gpt_actions", copy="gpt_copy",
                            composer_offset=(240, -5),
                            newchat=("gpt_newchat", "gpt_newchat_icon"), sidebar="gpt_sidebar",
                            send_mode="exists")
            _open_new_chat(s, _NEWCHAT)
            time.sleep(1.0)

        if do_list:
            # 读状态不许改页面：这里不点「新聊天」，否则会把用户正在聊的会话重置掉
            out = {"effort": "unknown", "effort_note": "思考强度未实现"}
            out.update(read_writer(s))
            if out.get("writer") in (None, "unknown"):
                out["note"] = "模式按钮只在新聊天页出现；这里不主动改页面，读不到就手动开个新聊天。"
            else:
                out.update(read_model(s, out["writer"]))
            print(json.dumps(out, ensure_ascii=False))
            return 0

        rc = 0
        if want_writer is not None:
            r = choose_writer(s, want_writer)
            print(json.dumps(r, ensure_ascii=False))
            if not r.get("ok"):
                rc = 1
        if want_model is not None:
            r = set_model(s, want_model)
            print(json.dumps(r, ensure_ascii=False))
            rc = 0 if r.get("ok") else 1
    except ForegroundLost as e:
        # 目标窗口被切走/被盖住：立刻停，后面的点击一个都不发
        print(json.dumps({"ok": False, "stage": "user_took_over",
                          "hint": str(e)}, ensure_ascii=False))
        return 4
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
