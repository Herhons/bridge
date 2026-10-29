"""chatgpt 网页版：选模式、选模型、调推理强度。不发提问。

用法：
    python chatgpt_choose.py --list
    python chatgpt_choose.py --writer chat|work
    python chatgpt_choose.py --model gpt61_terra        （模型分模式，先定 --writer）
    python chatgpt_choose.py --effort low|mid|high|xhigh|max
    python chatgpt_choose.py --model gpt6_sol --effort max

## 弹层是**两层**，不是一层（2026-10-29 用户指出，实测确认）

    点芯片 / Ctrl+Shift+M
        └─> effort 层：一条滑杆 + 一行「当前模型名 ›」
                点那行模型名
                    └─> models 层：模型列表（9 项）
                选完模型
                    └─> 自动退回 effort 层   ← 这里是调强度的位置
    拖滑杆调档位 -> Esc 关闭 -> 强度才生效（芯片这时才显示「GPT-6 Astra 高」）

旧版把这两层当成一层：选完模型就按 Esc 把整个弹层收掉，等于把第二步吃掉了。
表现就是「选完 default 之后老断」「调不了强度」。

## 位置都是量出来的，不是猜的

    · 打开弹层：点输入框空白聚焦 -> Ctrl+Shift+M（芯片 tooltip 上写的快捷键）。
      **不点芯片**：芯片就在橙钮左边 44~110px，麦克风在 43px，偏移量算错一次就录音
      （-40 / -44 各踩过一次）。
    · 滑杆：轨道 x=954..1239，滑块圆心 y = 橙钮 y + 94。
      滑块圆心 x = 橙色块右端 - 16，这条对最低档（橙色只有滑块本身）也成立。
    · 五档等分：981=轻度 1041=中 1102=高 1163=极高 1223=Max。
    · 入口行在滑块圆心上方 38px，横向取轨道中点。

## 判据

    _layer() 分 closed / effort / models 三态：
      models = 找到勾或 ≥2 个模型项
      effort = _slider() 找到滑杆（橙色块在橙钮下方 60~140px、且在轨道 x 范围内）
      closed = 都不是
    旧版用「非背景块的外接矩形」判弹层，页面上任何文字都满足，弹层没开也返回坐标，
    后面全链路都在错的前提上跑。
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
from qi.browser import ensure_site  # noqa: E402
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
        # 橙色检查只在橙钮贴身范围内做。
        # 半径不能大：滑块拖到 Max 时离橙钮只有 94px，橙色填充一路铺到那里，
        # 用 100 会把它当录音键拦掉（实测 570 个橙色像素，--effort 直接失败）。
        # 挡麦克风靠的是上面那条距离检查（麦克风在 43px，阈值 60 够用）。
        if d < 40:
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
    if win is None:
        return False
    if s.find(CHECK, threshold=0.85, region=win) is not None:
        return True
    n = sum(1 for _n, tpl in MODELS.get(writer, {}).values()
            if s.find(tpl, threshold=0.85, region=win) is not None)
    return n >= 2


# ---------------------------------------------------------------- 强度滑杆
# 2026-10-29 实测（窗口 1380x1340、副屏）：弹层里从上到下三样东西
#   入口行「GPT-6 Astra ›」   ← 点它才进模型列表
#   滑杆（轨道 x 954..1239）
# 五档等分，滑块圆心 x 与档位的对应关系是量出来的，不是猜的：
#   981=轻度 1041=中 1102=高 1163=极高 1223=Max
# 档位表**按模式分开**：两个模式的滑杆长度和档数都不一样（实测 2026-10-29）
#   work: 5 档，读数 988/1046/1097/1154/1211（间距 58）
#   chat: 3 档，读数 1013/1115/1217（间距 102），最低档界面上写「即时」
# 读数是「橙色块右端 - 16」，比真实滑块圆心系统偏小 6~12px；但在同一模式内
# 所有档位偏得一样，不影响"取最近档"。
EFFORTS = {
    "work": {
        "low":   ("轻度", 988),
        "mid":   ("中",   1046),
        "high":  ("高",   1097),
        "xhigh": ("极高", 1154),
        "max":   ("Max",  1211),
    },
    "chat": {
        "low":  ("即时", 1013),
        "mid":  ("中", 1115),
        "high": ("高", 1217),
    },
}
def effort_order(writer: str) -> list[str]:
    """某个模式的档位顺序（从低到高）。"""
    return list(EFFORTS.get(writer, {}))

SLIDER_L, SLIDER_R = 954, 1239   # 轨道两端（窗口内 x）
KNOB_R = 16                      # 滑块半径
KNOB_DY = 94                     # 滑块圆心 y = 橙钮 y + 94
ENTRY_DY = -38                   # 入口行 y = 滑块圆心 y - 38
SLIDER_BAND = (60, 140)          # 滑杆纵向搜索带（相对橙钮 y）
EFFORT_GAP = 14                  # 兜底容差；实际按各模式档位间距算（见 _gap_limit）


def _slider(s: Screen):
    """强度滑杆的几何（窗口内坐标）。None = **强度层没开**。

    这是"强度层开着"的唯一判据，所以判据本身必须稳。踩过三次：

    1. 用「非背景块外接矩形」：页面上任何文字都满足，弹层没开也返回坐标。
    2. 用橙色：**滑块停在最低档时整条滑杆是灰的、一点橙色都没有**，
       弹层开着也被判成没开（--effort low 之后紧跟 --effort mid 直接报 menu_not_open）。
    3. 用「轨道行最长灰段」：滑块在最左时滑块是白的，最长灰段从滑块右边缘才开始，
       滑块被排除在外；而且下方「打开桌面应用」那一带也是长灰条，会被并进来
       导致轨道中心偏 14px（实测 cy 算成 748，实际 734）。

    现在的做法：
      · 逐行找长 170~340px 的连续灰段，**按连续行分组**，取高度 20~40 的组
        （真轨道 24~28 行高，下面那一片只有 15 行，被排除）；
      · 滑块用**轨道上下方凸出**那几行找：滑块直径 32 > 轨道高 24，
        上下各凸出 4px，那几行只有滑块的边框有像素（宽度 8~46）。
        在轨道行上找白色段不行——滑块贴着轨道左端时，滑块的白色和弹层的白色
        连成一片，宽度远超上限，永远匹配不上。
    """
    win = _win(s)
    v = _voice_center(s)
    if win is None or v is None:
        return None
    img = s.grab(win)
    if img.size == 0:
        return None
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    vx, vy = int(v[0]) - win[0], int(v[1]) - win[1]
    x0, x1 = max(0, vx - 380), min(g.shape[1], vx + 15)
    y0, y1 = max(0, vy + 40), min(g.shape[0], vy + 170)
    if x1 - x0 < 200 or y1 - y0 < 40:
        return None

    # 1) 候选轨道行。
    #    判据不能用「最长连续段」：滑块是白的，会把轨道切成左右两段，
    #    滑块在中间时最长的一段只有 130px，达不到 170 的下限，真轨道直接被排除
    #    （实测候选里只剩下方「打开桌面应用」那一片阴影）。
    #    改成看「整行首尾跨度」+「像素占比」：轨道是实心横条，跨度 220 里
    #    190 个像素是深的；文字行跨度也大但像素稀。
    cand = []
    for y in range(y0, y1):
        idx = np.where(g[y, x0:x1] < 250)[0]
        if len(idx) < 150:
            continue
        span = int(idx[-1] - idx[0] + 1)
        if 170 <= span <= 340 and len(idx) >= span * 0.55:
            cand.append((y, x0 + int(idx[0]), x0 + int(idx[-1])))
    if len(cand) < 8:
        return None

    # 2) 定轨道上下边。
    #    不能按「连续行分组」：滑块和橙色填充会把轨道打断，实测 723..746 这一段
    #    被切成 2 行 / 3 行 / 7 行三小段，没有一段凑得够 20 行，全被丢掉，
    #    候选里只剩下方「打开桌面应用」那片阴影。
    #    改用「宽度接近最宽行」来收集：轨道是等宽的（实测 268~270 稳定），
    #    下方阴影是渐变的（260 一路收到 208），一筛就分开。
    widest = max(c[2] - c[1] + 1 for c in cand)
    same = [c for c in cand if (c[2] - c[1] + 1) >= widest - 6]
    if not same:
        return None
    ys = [c[0] for c in same]
    top, bottom = min(ys), max(ys)
    if not (14 <= bottom - top + 1 <= 42):
        return None
    cy = (top + bottom) // 2
    tx0 = min(c[1] for c in same)
    tx1 = max(c[2] for c in same)

    # 3) 滑块位置。只有两种情况，不要试图用「凸出」或「白色段」来通用判定——
    #    实测那两条都会在某个档位翻车（凸出部分在圆顶处只有 11px 宽、
    #    白色段在最低档时和弹层白连成一片，还有轨道左圆角的残留会被误当成滑块）。
    #
    #    (a) 轨道上有橙色（填充 + 滑块）：圆心 = 橙色最右端 - 滑块半径。
    #        这条对最低档以外的所有档位都成立，包括填满的 Max。
    #    (b) 轨道全灰 = 橙色填充宽度为 0 = 停在最低档。
    #        此时滑块贴着轨道左端，圆心 ≈ tx0 + 33
    #        （33 = 半径 16 + 圆角内缩 17，work tx0=954 -> 987、chat tx0=980 -> 1013，
    #         和两个模式实测的最低档读数 988 / 1013 都对得上）。
    b = img[:, :, 0].astype(int)
    r = img[:, :, 2].astype(int)
    om = ((r - b > 40) & (r > 140)).astype(np.uint8)
    _yy, xx = np.where(om[top:bottom + 1, tx0:tx1 + 1])
    if len(xx) >= 80:
        return {"knob": tx0 + int(xx.max()) - KNOB_R, "cy": cy, "win": win,
                "track": (tx0, tx1)}
    return {"knob": tx0 + 33, "cy": cy, "win": win, "track": (tx0, tx1),
            "at_min": True}


def _gap_limit(writer: str) -> int:
    """某个模式允许的最大"离档距离"。

    不能用一个固定值：两个模式的档距差一倍（work 58 / chat 102），
    读数误差也跟着差（work 实测 5~12px，chat 到过 24px）。
    固定 14 会让 chat 每次都报"不在档上"，固定 30 又会把 work 的相邻档混起来。
    按档距的 40% 取，两个模式都刚好。
    """
    px = sorted(v[1] for v in EFFORTS.get(writer, {}).values())
    if len(px) < 2:
        return EFFORT_GAP
    return max(8, int(min(b - a for a, b in zip(px, px[1:])) * 0.4))


def _effort_of(knob: int, writer: str):
    """滑块圆心 x -> 档位。返回 (key, 距离)，取最近的一档。"""
    best, bd = None, 10 ** 9
    for k, (_name, px) in EFFORTS.get(writer, {}).items():
        d = abs(knob - px)
        if d < bd:
            best, bd = k, d
    return best, bd


def _menu_entry_pos(s: Screen):
    """入口行（点它进模型列表）的屏幕坐标。**必须先有滑杆**。

    不能用固定偏移：两个模式的弹层高度不一样，入口行到滑块圆心的距离
    工作模式是 38px、聊天模式是 59px（实测），写死一个必然点空
    （聊天模式那次点在了入口行和滑杆之间的空白，弹层毫无反应）。

    改成扫「滑块上方第一行有文字的地方」——两个模式的入口行都是弹层里最上面那行字。
    横向取那一行深色像素的实际范围中心，不用轨道中心：聊天模式的入口行只有
    「高 ›」几个字、很窄，用轨道中心会点偏 43px 落在字外面。
    """
    sl = _slider(s)
    if sl is None:
        return None
    win = sl["win"]
    img = s.grab(win)
    if img.size == 0:
        return None
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    ky = sl["cy"]
    x0, x1 = SLIDER_L - 40, SLIDER_R + 40
    y_start = None
    # 扫描范围 20~80px，**不能更大**：滑块上方 94px 处是芯片
    # （工作模式 825-94=731 正好是橙钮那一行），扫进去会把芯片当入口行，
    # 点下去等于点弹层外面，弹层直接被关掉（实测）。
    # 范围 18~60px：入口行到滑块圆心的距离是 work 38 / chat 44，
    # 而芯片在 78px（橙钮那一行），范围一大就会先扫到芯片，
    # 点下去等于点弹层外面，弹层直接被关掉（实测）。
    for dy in range(60, 18, -1):
        y = ky - dy
        if not (0 <= y < g.shape[0]):
            continue
        if int((g[y, x0:x1] < 150).sum()) >= 6:
            y_start = y
            break
    if y_start is None:
        return None
    # 把这一行往下并成一块（字可能占 12~20px 高）
    y_end = y_start
    for y in range(y_start, min(y_start + 26, g.shape[0])):
        if int((g[y, x0:x1] < 150).sum()) >= 3:
            y_end = y
    mid = (y_start + y_end) // 2
    idx = np.where(g[mid, x0:x1] < 150)[0]
    if len(idx) == 0:
        idx = np.where(g[y_start, x0:x1] < 150)[0]
    cx = x0 + (int(idx.min()) + int(idx.max())) // 2
    return (win[0] + cx, win[1] + mid)


def _layer(s: Screen, writer: str) -> str:
    """弹层现在是哪一层。

    用户指出的结构（2026-10-29）：
      点芯片 -> effort 层（滑杆 + 当前模型名那一行）
      在 effort 层点模型名 -> models 层（模型列表）
      选完模型 -> **自动退回 effort 层**，这时可以调强度
      调完关掉 -> 整个收起
    所以这三层必须分开判，不能像原来那样把"列表没开"当成"弹层没开"。
    """
    if _items_visible(s, writer):
        return "models"
    if _slider(s) is not None:
        return "effort"
    return "closed"


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


def open_effort(s: Screen, writer: str) -> bool:
    """打开强度层（第一步）。

    链路：点输入框空白聚焦 -> Ctrl+Shift+M。
    **不点芯片**：芯片就在橙钮左边 44~110px，麦克风在 43px，偏移量算错一次就录音
    （2026-10-29 用 -40 / -44 各踩了一次）。快捷键是芯片 tooltip 上写的，等价且安全。

    已经在 models 层时按一次 Esc 退回 effort 层，不重新走快捷键——
    快捷键是 toggle，在弹层开着时按会把整层关掉（实测）。
    """
    lay = _layer(s, writer)
    if lay == "effort":
        return True
    if lay == "models":
        s.press("esc")
        time.sleep(0.7)
        if _slider(s) is not None:
            return True
    if page_kind(s) != "new":
        return False                    # 会话页没有模式按钮，也没有这个弹层

    if not focus_composer(s):
        return False
    time.sleep(0.5)
    s.hotkey(*CHIP_HOTKEY)
    time.sleep(1.3)
    return _slider(s) is not None


def enter_models(s: Screen, writer: str) -> bool:
    """进模型列表（第二步）。已经开着就直接返回。"""
    if _items_visible(s, writer):
        return True
    if not open_effort(s, writer):
        return False
    pos = _menu_entry_pos(s)
    if pos is None:
        return False
    ok, _why = safe_click(s, pos)
    if not ok:
        return False
    time.sleep(1.2)
    return _items_visible(s, writer)


def close_all(s: Screen, writer: str) -> None:
    """把整个弹层收起，并等它真收完。

    Esc 关闭会让强度设置生效（实测：拖完滑块芯片仍显示占位「选择推理强度」，
    Esc 之后才变成「GPT-6 Astra 高」）。所以改强度后必须走这一步。

    只 sleep 0.3s 的话，下一段逻辑的第一下点击会被残余的收起动画吃掉
    （实测连断两个），所以轮询到真关掉为止。
    """
    for _ in range(3):
        if _layer(s, writer) == "closed":
            return
        s.press("esc")
        time.sleep(0.6)


def read_effort(s: Screen, writer: str | None = None) -> dict:
    """读当前强度。会开关一次弹层（Esc 不会改设置，只关）。"""
    w = writer or read_writer(s).get("writer")
    if w not in EFFORTS:
        return {"effort": "unknown", "hint": "读不出当前模式"}
    if not open_effort(s, w):
        return {"effort": "unknown", "hint": "打不开强度弹层（只在新聊天页有）"}
    sl = _slider(s)
    close_all(s, w)
    if sl is None:
        return {"effort": "unknown", "hint": "弹层里没找到滑杆"}
    key, d = _effort_of(sl["knob"], w)
    gap = _gap_limit(w)
    out = {"effort": key, "label": EFFORTS[w][key][0], "knob": sl["knob"], "off": d,
           "tol": gap, "at_min": sl.get("at_min", False)}
    if d > gap:
        out["hint"] = (f"滑块停在 {sl['knob']}，离最近档位（{EFFORTS[w][key][0]}）"
                       f"差 {d}px，不在档上")
    return out


def set_effort(s: Screen, key: str, writer: str | None = None) -> dict:
    """设强度。

    **用键盘方向键，不用拖。** 拖拽实测不可靠：拖 -52px 实际走 -82px（过冲），
    而且连续拖同一个控件从第二次开始就不动了，一次只能落在两档中间
    （实测 1072，离两边各 30px）。

    方向键是精确的：点一下滑块让它拿到焦点，然后
      · 先按左键到底（多按两下确保归零）
      · 再按右键走 idx 次
    每按一次正好一档（work: high 1096 -> xhigh 1154 -> Max 1211，间距 58；
    chat: 1013 -> 1115 -> 1217，间距 102）。

    档位表按模式分开：work 5 档，chat 3 档。传错模式的档位名会直接报 bad_arg。
    """
    w = writer or read_writer(s).get("writer")
    if w not in EFFORTS:
        return {"ok": False, "stage": "no_writer",
                "hint": "读不出当前是聊天还是工作，先用 --writer 定一下"}
    table = EFFORTS[w]
    if key not in table:
        return {"ok": False, "stage": "bad_arg", "writer": w, "want": key,
                "options": {k: v[0] for k, v in table.items()}}
    if not open_effort(s, w):
        return {"ok": False, "stage": "menu_not_open", "writer": w}

    sl = _slider(s)
    if sl is None:
        return {"ok": False, "stage": "no_slider"}
    before, _bd = _effort_of(sl["knob"], w)

    # 点滑块拿焦点（点击会让它跳到点击处，无所谓——紧接着就归零）
    win = sl["win"]
    ok, why = safe_click(s, (win[0] + sl["knob"], win[1] + sl["cy"]))
    if not ok:
        return {"ok": False, "stage": "click_blocked", "detail": why, "before": before}
    time.sleep(0.6)

    order = effort_order(w)
    # 归零：按左键到底，多按两下
    for _ in range(len(order) + 1):
        s.press("left")
        time.sleep(0.35)
    # 往上走到目标档
    idx = order.index(key)
    for _ in range(idx):
        s.press("right")
        time.sleep(0.35)
    time.sleep(0.4)

    after_sl = _slider(s)
    after, ad = _effort_of(after_sl["knob"], w) if after_sl else (None, None)
    knob = after_sl["knob"] if after_sl else None
    close_all(s, w)                        # Esc 才会让强度生效
    time.sleep(0.5)
    # 只要求"最近档 == 想要的"不够：滑块可能停在两档正中间，那时 UI 其实没定下来。
    on_target = after == key and ad is not None and ad <= _gap_limit(w)
    out = {"ok": on_target, "stage": "done" if on_target else "unverified",
           "before": before, "after": after, "want": key, "knob": knob, "off": ad,
           "by": "keyboard"}
    if not on_target:
        out["hint"] = (f"滑块停在 {knob}，离「{table[key][0]}」{ad}px，"
                       f"超出容差 {_gap_limit(w)}")
    return out


def _list_band(s: Screen, writer: str, win):
    """列表所在的纵向范围（屏幕坐标）。把搜索限制在列表里。

    不能在整个窗口里找：**芯片上也会出现模型名**（选中 GPT-5.6 Luna 时芯片就
    写着「GPT-5.6 Luna 高」），实测 gpt56_luna 模板在芯片上匹到 0.928，
    过了 0.85 的阈值，而且比列表项还先撞上，点下去点到列表外面。

    做法：把能匹配到的项按 y 排序，按「相邻间距 ≤60px」分组，取最长的那组
    （列表项间距 36px，芯片离列表 70px 以上，会单独成组被丢掉），
    再上下各留 40px 余量 —— 滚动后新露出来的项也在这个范围里。
    """
    ys = []
    for _k, (_d, tpl) in MODELS.get(writer, {}).items():
        m = s.find(tpl, threshold=0.90, region=win)
        if m is not None:
            ys.append(int(m.center[1]))
    if len(ys) < 2:
        return None
    ys.sort()
    groups = [[ys[0]]]
    for y in ys[1:]:
        if y - groups[-1][-1] <= 60:
            groups[-1].append(y)
        else:
            groups.append([y])
    gp = max(groups, key=len)
    if len(gp) < 2:
        return None
    return (win[0], gp[0] - 40, win[2], gp[-1] - gp[0] + 80)


def _list_anchor(s: Screen, writer: str):
    """找一个当前可见的模型项，用来定位「列表在屏幕上的位置」。

    滚轮事件发给鼠标当前位置下面的窗口，鼠标必须落在列表上才滚得动。
    原来用窗口中心，那是页面正中，滚轮根本不在列表上（实测毫无反应）。
    """
    win = _win(s)
    band = _list_band(s, writer, win) or win
    for _k, (_d, tpl) in MODELS.get(writer, {}).items():
        m = s.find(tpl, threshold=0.90, region=band)
        if m is not None:
            return m
    return None


def _scroll_list(s: Screen, writer: str, notches: int) -> bool:
    """在列表上滚 notches 格（负数向下）。返回是否找到了定位点。"""
    anchor = _list_anchor(s, writer)
    if anchor is None:
        return False
    s.scroll(notches, at=anchor.center)
    time.sleep(0.5)
    return True


def _find_item(s: Screen, writer: str, tpl: str, scroll: bool = True):
    """找模型项；找不到就往下滚再找（工作模式 9 项，一屏只放得下 8 项）。

    逐格滚而不是一次滚 4 格：列表只溢出 1 项，滚多了会把目标又推出去
    （虽然实测到底就停住，但不同窗口高度下不一定）。
    """
    win = _win(s)
    band = _list_band(s, writer, win) or win
    m = s.find(tpl, threshold=0.85, region=band)
    if m is not None or not scroll:
        return m
    if _list_anchor(s, writer) is None:
        return None
    for _ in range(4):
        if not _scroll_list(s, writer, -1):
            return None
        band = _list_band(s, writer, win) or win     # 滚动后范围会变
        m = s.find(tpl, threshold=0.85, region=band)
        if m is not None:
            return m
    return None


def chip_signature(s: Screen):
    """截下芯片那一块，用来判断"模型变没变"。

    用橙钮取坐标（只读位置，不点它）。切换后不重开菜单复核的情况下靠它判断，
    快且不碰弹层。
    """
    v = _voice_center(s)
    if v is None:
        return None
    box = (int(v[0]) - 300, int(v[1]) - 40, 240, 40)
    img = s.grab(box)
    if img.size == 0:
        return None
    return cv2.resize(img, (240, 40), interpolation=cv2.INTER_AREA)


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
    """在**已经开着的**列表里读当前模型。不负责开关菜单。

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
    # 三次机会：原位 -> 滚到顶 -> 滚到底。
    # 勾可能落在视口外：列表默认停在顶部，选中项在底部时勾看不见（实测
    # 选中 GPT-5.6 Luna 后复核读成 unknown，但芯片明明已经变成 GPT-5.6 Luna 高）；
    # 反过来为了找靠下的模型把列表滚到底，顶部「默认」那一行的勾又会滚出去。
    for attempt in range(3):
        prev = None
        for _ in range(4):
            cur = _scan_model(s, writer)
            if cur is not None and prev is not None and cur["signed"] == prev["signed"]:
                snap = cur
                break
            prev = cur
            time.sleep(0.35)
        if snap is None:
            snap = prev
        if snap is not None:
            break
        if attempt == 0:
            _scroll_list(s, writer, 8)
        elif attempt == 1:
            _scroll_list(s, writer, -8)
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


def read_model(s: Screen, writer: str) -> dict:
    """读当前模型。链路：开强度层 -> 进列表 -> 读勾 -> 全关。"""
    if not MODELS.get(writer):
        return {"model": None}
    if not enter_models(s, writer):
        return {"model": "unknown", "hint": "打不开模型列表"}
    out = _pick_model(s, writer)
    close_all(s, writer)
    return out


def set_model(s: Screen, key: str) -> dict:
    """切模型。

    按用户指出的两步结构：开强度层 -> 进列表 -> 点目标 -> **自动退回强度层**。
    不再像旧版那样在选完之后按 Esc 强行收弹层——那一层本来就是留给人调强度的，
    收掉它等于把第二步吃掉了，也是「选完 default 之后老断」的原因之一。

    点击次数：聚焦 1 + 入口行 1 + 目标项 1 + 复核再进列表 1 = 4 下，预算 8 够用。
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
    if not enter_models(s, w):
        return {"ok": False, "stage": "menu_not_open", "writer": w}

    cur = _pick_model(s, w)["model"]
    if cur == key:
        close_all(s, w)
        return {"ok": True, "stage": "already", "before": cur, "after": cur}

    m = _find_item(s, w, models[key][1])
    if m is None:
        close_all(s, w)
        return {"ok": False, "stage": "item_not_found", "before": cur, "want": key}
    ok, why = safe_click(s, m.center)
    if not ok:
        close_all(s, w)
        return {"ok": False, "stage": "click_blocked", "detail": why, "before": cur}
    # 点完会**自动退回强度层**（这是正常行为，不是失败）
    time.sleep(1.4)

    win = _win(s)
    if win:
        vx, vy, vw, vh = s.bounds()
        s.move_to(max(vx + 8, min(win[0] + 60, vx + vw - 8)),
                  max(vy + 8, min(win[1] + 60, vy + vh - 8)))
    time.sleep(0.4)

    if key in NO_READ:
        # 「默认（推荐模型集）」停不住：点下去解析成某个具体模型，芯片写的是那个名字，
        # 永远复核不上。只按"退回强度层 + 芯片变了"验证。
        back_to_effort = _slider(s) is not None
        after_sig = chip_signature(s)
        changed = (before_sig is not None and after_sig is not None and
                   float(np.abs(before_sig.astype(int) - after_sig.astype(int)).mean()) > 2.0)
        close_all(s, w)
        ok2 = back_to_effort and changed
        return {"ok": ok2, "stage": "done_menu_closed" if ok2 else "unverified",
                "before": cur, "after": "（推荐模型集→具体模型，不可读）",
                "verified_by": "退回强度层+芯片变化",
                "back_to_effort": back_to_effort, "chip_changed": changed}

    # 复核：再进一次列表读勾（此时在强度层，进列表只要 1 下点击）
    if not enter_models(s, w):
        close_all(s, w)
        return {"ok": False, "stage": "reverify_failed", "before": cur}
    after = _pick_model(s, w)["model"]
    close_all(s, w)
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
    want_effort = None
    if "--effort" in args:
        want_effort = take("--effort")

    if bad:
        print("参数有问题：", bad)
        return 2
    if not do_list and want_model is None and want_writer is None and want_effort is None:
        print("用法：")
        print(f"  python {name} --list [--tab N]")
        print(f"  python {name} --writer chat|work")
        print(f"  python {name} --model <名字>      （模型分模式，先定 --writer）")
        print(f"  python {name} --effort low|mid|high|xhigh|max")
        print("    强度滑杆是单独一步：开弹层(滑杆+模型名) -> 点模型名进列表 -> 选完自动回滑杆 -> 拖档位 -> Esc 生效")
        print("  --tab N  先切到第 N 个标签页（左边数）")
        print("  --idle 2 / --wait 120 / --force   你正在用电脑时是否等待")
        for wr, items in MODELS.items():
            print(f"    [{wr}] " + ", ".join(f"{k}({d})" for k, (d, _t) in items.items()))
        for wr, tab in EFFORTS.items():
            print(f"    [{wr}] 强度 " + ", ".join(f"{k}({v[0]})" for k, v in tab.items()))
        return 2
    if want_writer is not None and want_writer not in TAB_WORDS:
        print(f"没有这个模式：{want_writer}；可选 {', '.join(TAB_WORDS)}")
        return 2
    if want_effort is not None and not any(want_effort in v for v in EFFORTS.values()):
        print(f"没有这个强度：{want_effort}")
        for wr, tab in EFFORTS.items():
            print(f"  [{wr}] " + ", ".join(f"{k}({v[0]})" for k, v in tab.items()))
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
    hwnd = ensure_site("chatgpt")
    if hwnd is None:
        print("开不出浏览器窗口（检查 Chrome 路径）")
        return 1
    s.bind(hwnd)
    if not s.focus():
        print("窗口提不到前台")
        return 1
    time.sleep(1.2)
    # --tab 不再生效：ensure_site 保证当前标签就是要操作的那个页面，
    # 再切一下反而会把目标页切走。参数保留只为兼容旧命令。
    _ = tab

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
            out = {}
            out.update(read_writer(s))
            if out.get("writer") in (None, "unknown"):
                out["note"] = "模式按钮只在新聊天页出现；这里不主动改页面，读不到就手动开个新聊天。"
            else:
                out.update(read_model(s, out["writer"]))
                eff = read_effort(s, out["writer"])
                out["effort"] = eff.pop("effort")
                out.update({f"effort_{k}": v for k, v in eff.items()})
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

        if want_effort is not None:
            # 顺序很重要：必须先选模型再调强度。
            # 因为选完模型会自动退回强度层，正好是调强度的位置；
            # 反过来先调强度再选模型，选完还得重开一次弹层。
            r = set_effort(s, want_effort)
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
