"""chatgpt 网页版：选模式、选模型、调推理强度。不发提问。

用法：
    python chatgpt_choose.py --list
    python chatgpt_choose.py --writer chat|work
    python chatgpt_choose.py --model gpt61_terra        （模型分模式，先定 --writer）
    python chatgpt_choose.py --effort low|mid|high|xhigh|max     （工作模式五档）
    python chatgpt_choose.py --effort instant|mid|high           （聊天模式三档）
    python chatgpt_choose.py --model gpt6 --effort high

## 弹层是**两层**，不是一层（2026-10-29 用户指出，实测确认）

    点芯片（`选择 ChatGPT 模型`）
        └─> effort 层：一条滑杆 + 一项「选择模型」
                点「选择模型」
                    └─> models 层：模型列表（聊天 2 项 / 工作 9 项）
                选完模型
                    └─> 自动退回 effort 层   ← 这里是调强度的位置
    拖滑杆调档位 -> Esc 关闭 -> 强度才生效（芯片这时才显示「GPT-6 Astra 高」）

旧版把这两层当成一层：选完模型就按 Esc 把整个弹层收掉，等于把第二步吃掉了。
表现就是「选完 default 之后老断」「调不了强度」。

## CARD-06c 第 4 条：开弹层**点芯片**，不再按 Ctrl+Shift+M

Ctrl+Shift+M 会被 Chrome 截成「个人资料菜单」，弹层根本没开，后面全链路都在
错的前提上跑。现在改成：
  · 从 UIA 控件树取 `选择 ChatGPT 模型` 按钮的**真实矩形**，点它的中心；
  · 点前**唯一性校验**：0 个 -> 先点一下输入框再找（输入框空/未聚焦时芯片可能没渲染），
    多于 1 个 -> 直接失败，不猜点哪一个；
  · 点前**距离校验**：点击点不能落在相邻的听写/语音/发送按钮矩形里，
    与右下角橙钮（麦克风/发送键同一个槽位）至少 60px；
  · 点后**复核**：等一拍再判一次 `_layer()`，仍 `closed` 就是没开，判失败。
点击预算/最小间隔/橙色像素三道闸仍然走 `safe_click`。

## 位置都是量出来的，不是猜的

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

import argparse
import json
import os
import re
import sys
import time

import cv2
import numpy as np

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import ForegroundLost, Library, Screen, console, guard, uilock  # noqa: E402
from qi import zoom as zoom_mod  # noqa: E402
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

# ---------------------------------------------------------------- UIA 只读档位（CARD-06b 第 10 条）
# 2026-11-02 实测（125% 缩放、聊天模式）：
#   芯片（关）：ButtonControl `选择 ChatGPT 模型` + 子 TextControl `高`
#   强度弹层开：同按钮子文本变成 `思考强度`，另有一条
#               `高，第 3 项，共 3 项。`（滑杆的可访问值，UIA 直接读数）
#   模型列表开：RadioButtonControl `GPT-6` / `GPT-5.6 Sol`，SelectionItemPattern.IsSelected
# 这些都比模板可靠：模板会随缩放/改版失配，名字和选中态不会。
CHIP_BUTTON = "选择 ChatGPT 模型"

# 档位词 -> key。长词在前，避免「极高」先命中「高」。
# 中英都认（CARD-06c 第 1 条）；聊天模式的「即时」= instant，工作模式的「轻度」= low。
_CHIP_EFFORT_WORDS = (
    ("最高", "max"), ("极高", "xhigh"),
    ("Extra High", "xhigh"), ("extra high", "xhigh"),
    ("即时", "instant"), ("Instant", "instant"), ("instant", "instant"),
    ("轻度", "low"), ("Light", "low"), ("light", "low"),
    ("中", "mid"), ("Medium", "mid"), ("medium", "mid"),
    ("高", "high"), ("High", "high"), ("high", "high"),
    ("Max", "max"), ("max", "max"),
)


def _name_key(text: str) -> str:
    """显示名归一：去掉括号注释与空白，用来把 UIA 名字对回内部 key。

    `GPT-6（默认）` -> `gpt-6`，`GPT-6` -> `gpt-6`；
    `默认（推荐模型集）` -> `默认`。
    """
    import re

    raw = str(text or "").replace("（", "(").replace("）", ")")
    raw = re.sub(r"\([^)]*\)", "", raw)
    return "".join(raw.split()).lower()


def _doc_root(s: Screen):
    from qi import ui

    control, _url = ui.document(s.target_hwnd, "chatgpt")
    return ui.tree(control)


def _chip_text(s: Screen, root=None) -> str:
    """芯片上的文字（`高` / `GPT-6 Astra 高` / 弹层开着时的 `思考强度`）。读不到给空串。"""
    root = root if root is not None else _doc_root(s)
    for node in root.walk():
        if node.kind != "ButtonControl" or (node.name or "").strip() != CHIP_BUTTON:
            continue
        parts = []
        for child in node.walk():
            if child is node:
                continue
            if child.kind == "TextControl" and child.name:
                parts.append(child.name.strip())
        return " ".join(parts)
    return ""


def _effort_of_text(text: str) -> str | None:
    for word, key in _CHIP_EFFORT_WORDS:
        if word in (text or ""):
            return key
    return None


def read_chip_effort(s: Screen) -> dict:
    """只读芯片，返回 {"effort","raw","source"}。不点任何东西。"""
    text = _chip_text(s)
    return {"effort": _effort_of_text(text), "raw": text,
            "source": "uia_chip" if text else "none"}


def _announce_effort(s: Screen, root=None, writer: str | None = None) -> dict:
    """弹层开着时读滑杆的可访问值：`高，第 3 项，共 5 项。` -> high。

    这不是"从键盘位置推断"：它是 UIA 把滑杆当前值念出来的文本，属于本次读数。

    2026-11-09：新版界面把档位词从芯片里拿掉了（芯片 Button 只剩
    「选择 ChatGPT 模型」一个名字、没有子文本），这条可访问值成了唯一可靠的
    UIA 读数。它带「第 N 项，共 M 项」，优先按序号映射档位——旧实现按词串匹配，
    「最高」含「高」会被误判成 high，导致 Max 被当成高。
    """
    root = root if root is not None else _doc_root(s)
    order = effort_order(writer) if writer else []
    for node in root.walk():
        if node.kind != "TextControl":
            continue
        name = (node.name or "").strip()
        if "项，共" not in name and "项,共" not in name:
            continue
        match = re.search(r"第\s*(\d+)\s*项", name)
        if match and order:
            index = int(match.group(1)) - 1
            if 0 <= index < len(order):
                return {"effort": order[index], "raw": name, "source": "uia_slider"}
        head = name.split("，", 1)[0].split(",", 1)[0].strip()
        key = _effort_of_text(head)
        if key:
            return {"effort": key, "raw": name, "source": "uia_slider"}
    return {"effort": None, "raw": "", "source": "none"}


def _offscreen(node) -> bool:
    """控件是否在视口外。读不到就当 False（老版本 UIA 没这个属性）。"""
    try:
        return bool(node.control.IsOffscreen)
    except Exception:                                  # noqa: BLE001
        return False


def _ui_model_rows(s: Screen) -> list:
    """当前列表里的模型项（RadioButton），返回 [(key, name, rect, node, offscreen)]。

    不限 writer：聊天/工作的模型名合起来认，认不出名字的跳过。

    B17：带上 node 和 offscreen。work 模式 9 项、一屏只放得下 8 项，
    最后一项（GPT-5.6 Luna）常态在折叠线以下；以前只给 rect，调用方没法知道
    "这个矩形在视口外"，点过去等于点在列表外。
    """
    keys = {}
    for table in MODELS.values():
        keys.update({_name_key(display): key for key, (display, _tpl) in table.items()})
    out = []
    try:
        root = _doc_root(s)
    except Exception:                                  # noqa: BLE001
        return out
    for node in root.walk():
        if node.kind != "RadioButtonControl":
            continue
        key = keys.get(_name_key(node.name))
        if key is None:
            continue
        try:
            rect = node.control.BoundingRectangle
            box = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        except Exception:                              # noqa: BLE001
            box = None
        out.append((key, node.name, box, node, _offscreen(node)))
    return out


def read_model_uia(s: Screen, writer: str) -> dict:
    """读模型列表里的选中项（RadioButton + IsSelected）。列表必须已经开着。

    B17：只采信**在视口内**的选中项。列表收起后 UIA 树里可能还挂着上一层的
    RadioButton，它们的 IsSelected 是旧值；只按名字读会把「已经关掉的旧列表」
    当成当前状态，复核就会报出上一次的模型。
    """
    keys = {_name_key(display): key
            for key, (display, _tpl) in MODELS.get(writer, {}).items()}
    selected, hidden = [], []
    try:
        root = _doc_root(s)
    except Exception as exc:                           # noqa: BLE001 - 读不到就说读不到
        return {"model": "unknown", "reason": f"uia_error: {type(exc).__name__}", "source": "uia"}
    for node in root.walk():
        if node.kind != "RadioButtonControl":
            continue
        key = keys.get(_name_key(node.name))
        if key is None:
            continue
        try:
            is_on = bool(node.control.GetSelectionItemPattern().IsSelected)
        except Exception:                              # noqa: BLE001 - 没这个模式就跳过
            continue
        if not is_on:
            continue
        if _offscreen(node):
            hidden.append((key, node.name))
        else:
            selected.append((key, node.name))
    if len(selected) == 1:
        return {"model": selected[0][0], "name": selected[0][1], "source": "uia"}
    if len(selected) > 1:
        return {"model": "unknown", "reason": "multiple_selected",
                "names": [n for _k, n in selected], "source": "uia"}
    if hidden:
        # 有选中项，但全在视口外：可能是列表收起后的残影，也可能是当前项被滚出
        # 视口。两种都不许当读数，交给模板复核（模板只看可见区域）。
        return {"model": "unknown", "reason": "selected_offscreen",
                "names": [n for _k, n in hidden], "source": "uia"}
    return {"model": "unknown", "reason": "no_selected_radio", "source": "uia"}


COMPOSER = "gpt_composer"            # 输入框左下角那个「+」
COMPOSER_DX, COMPOSER_DY = 250, -20  # 从「+」点到输入框空白处（只为了聚焦）

# CARD-06c 第 4 条：点芯片前要验的距离。
# 芯片和「听写/语音/发送」是同一行相邻的按钮（实测芯片右边缘与听写按钮左边缘只差 1px），
# 所以点之前必须确认落点在芯片自己里面、且不在任何一个相邻按钮的矩形里。
CHIP_NEAR_NAMES = ("听写", "开始语音", "发送", "发送消息", "停止", "停止生成", "语音输入")
CHIP_MIN_VOICE = 60                  # 与右下角橙钮（麦克风/发送键同槽位）的最小距离
CHIP_BOX_LIMIT = (420, 90)           # 芯片矩形的合理范围（宽, 高），超出就是读错了控件

# 最近一次「开弹层」失败的原因，给上层拼 stage/reason 用（不改 open_effort 的 bool 契约）
OPEN_STATE: dict = {}

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

    只在**页头那一条**里找（见 `TAB_BAND_DY`）：侧栏里也有「聊天」，
    全窗口找会让会话页永远被认成新聊天页。
    """
    region = _tab_region(s)
    if region is None:
        return "session"
    for tpl in TAB_WORDS.values():
        if s.find(tpl, threshold=0.75, region=region) is not None:
            return "new"
    return "session"


# ---------------------------------------------------------------- 模式读写
# 模式词只在页头这一条横条里。**不能全窗口找**：侧栏第一个菜单项也叫「聊天」
# （实测 y=347，而模式按钮在 y=295），全窗口找会在工作模式下把侧栏那一项当成
# 未选中的聊天词，两个词墨色都是 13，直接读成 unknown。
# 100% 缩放下实测：窗口内 y=+129..165 是模式按钮，侧栏从 +186 起。
TAB_BAND_DY, TAB_BAND_H = 95, 85
TAB_ROW_TOL = 14                      # 两个词必须同一行，否则视为没找到


def _tab_region(s: Screen):
    win = _win(s)
    if win is None:
        return None
    return (win[0], win[1] + TAB_BAND_DY, win[2], TAB_BAND_H)


def _tab_boxes(s: Screen) -> dict:
    """两个模式词各自的 bbox（屏幕坐标），各自独立匹配，且必须在同一行。"""
    region = _tab_region(s)
    if region is None:
        return {}
    out = {}
    for key, tpl in TAB_WORDS.items():
        m = s.find(tpl, threshold=0.75, region=region)
        if m is not None:
            out[key] = m
    if len(out) == 2 and abs(out["chat"].center[1] - out["work"].center[1]) > TAB_ROW_TOL:
        return {}                      # 匹到了两条不同的横条：宁可不认
    return out


def _ink(s: Screen, box) -> int:
    """一个词框里最暗的像素值。框跟着匹配结果走，位置漂多少都不影响。"""
    img = s.grab((box.x, box.y, box.w, box.h))
    if img.size == 0:
        return 255
    return int(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).min())


def _read_writer_once(s: Screen) -> dict:
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


# 模式按钮的可访问名。实测（2026-11-02）：两个都是 ButtonControl，且都带
# TogglePattern——选中的那个 ToggleState=1、未选的是 0。这比"比墨色"稳得多：
# 不受缩放影响，也不会被 Chrome 的标签页悬停预览卡片挡住（那张卡会盖住页面
# 左上角，实测能让 work 模式的墨色判据整个读成 unknown）。
WRITER_LABELS = {"chat": "聊天", "work": "工作"}


def _toggle_state(node):
    try:
        return int(node.control.GetTogglePattern().ToggleState)
    except Exception:                                  # noqa: BLE001 - 没这个模式
        return None


def read_writer_uia(s: Screen) -> dict:
    """用 TogglePattern 读当前模式。读不准（不是恰好一开一关）就给 unknown。"""
    try:
        root = _doc_root(s)
    except Exception as exc:                           # noqa: BLE001
        return {"writer": "unknown", "hint": f"读不到控件树：{type(exc).__name__}"}
    states: dict[str, int] = {}
    for node in root.walk():
        if node.kind != "ButtonControl":
            continue
        name = (node.name or "").strip()
        key = next((k for k, label in WRITER_LABELS.items() if name == label), None)
        if key is None or key in states:
            continue
        state = _toggle_state(node)
        if state is not None:
            states[key] = state
    on = [k for k, v in states.items() if v == 1]
    off = [k for k, v in states.items() if v == 0]
    if len(on) == 1 and len(off) == 1:
        return {"writer": on[0], "source": "uia_toggle", "toggle": states}
    return {"writer": "unknown", "source": "uia_toggle", "toggle": states,
            "hint": f"TogglePattern 不是一开一关（{states or '没读到'}）"}


def read_writer(s: Screen) -> dict:
    """读当前是聊天还是工作。

    **UIA 的 TogglePattern 优先**（不受缩放/遮挡影响），读不到才回落到"比两个词
    的墨色"：选中那个是近黑（实测 13），未选是灰（实测 93），差 80。
    之所以不用整组按钮的像素差：两态胶囊底色只差十几个灰阶（10~12），淹没在噪声里。

    **读不到要再等一拍**：刚切完模式/刚开完新对话时页面还在重排，第一下
    常常读不成（实测切到 work 后立刻读会 unknown），直接返回 unknown 会让
    紧跟其后的 `--model` 报 no_writer。最多试 3 次、间隔 0.6s。
    """
    last = {"writer": "unknown"}
    for attempt in range(3):
        uia = read_writer_uia(s)
        if uia.get("writer") in ("chat", "work"):
            return uia
        last = _read_writer_once(s)
        if last.get("writer") in ("chat", "work"):
            last.setdefault("source", "ink")
            return last
        if attempt < 2:
            time.sleep(0.6)
    last.setdefault("uia", read_writer_uia(s))
    return last


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

    B17：UIA 行也必须是**看得见**的。列表收起后残影还在树里挂了半秒，
    以前会据此返回 True，`enter_models` 就跳过重新打开，复核读到的是旧列表。
    """
    if any(box and not off for _k, _n, box, _node, off in _ui_model_rows(s)):
        return True          # 有看得见的 UIA 行：不再依赖模板/缩放
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
        "instant": ("即时", 1013),
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


def _effort_item(s: Screen):
    """强度层里「强度」这一行的矩形（屏幕坐标）。滑块就在这一行里。

    2026-11-09 新版界面：弹层整体左移，`_slider` 原来按「橙钮附近找长灰条」
    会撞上输入框底边（实测 cy 差 17px），点下去落在滑杆外面，方向键自然不动。
    UIA 的 MenuItemControl「强度」是稳定锚点：点它的中心能让滑杆拿到焦点，
    之后左右方向键才生效。
    """
    try:
        root = _doc_root(s)
    except Exception:                                  # noqa: BLE001
        return None
    for node in root.walk():
        if node.kind != "MenuItemControl":
            continue
        if (node.name or "").strip() != "强度":
            continue
        if _offscreen(node):
            continue
        try:
            rect = node.control.BoundingRectangle
        except Exception:                              # noqa: BLE001
            return None
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width < 100 or height < 20:
            return None
        return {"box": (rect.left, rect.top, width, height),
                "center": ((rect.left + rect.right) // 2,
                           (rect.top + rect.bottom) // 2)}
    return None


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


def _box(node) -> tuple[int, int, int, int]:
    r = node.control.BoundingRectangle
    return (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))


def _inside(box, x: int, y: int, pad: int = 0) -> bool:
    bx, by, bw, bh = box
    return bx + pad <= x <= bx + bw - pad and by + pad <= y <= by + bh - pad


def _open_fail(stage: str, hint: str = "", **detail) -> bool:
    """记下这次失败的原因，返回 False（open_effort 的契约不变）。"""
    OPEN_STATE.clear()
    OPEN_STATE.update({"stage": stage, "reason": stage, "hint": hint, **detail})
    return False


def chip_nodes(s: Screen) -> list:
    """页面上的 `选择 ChatGPT 模型` 按钮（0 个 / 1 个 / 多个都要能分辨）。"""
    try:
        root = _doc_root(s)
    except Exception:                                  # noqa: BLE001 - 读不到当没有
        return []
    return [n for n in root.walk()
            if n.kind == "ButtonControl" and (n.name or "").strip() == CHIP_BUTTON]


def _near_boxes(s: Screen) -> list:
    """同一行相邻的听写/语音/发送按钮的矩形（用来挡误点，不点它们）。"""
    try:
        root = _doc_root(s)
    except Exception:                                  # noqa: BLE001
        return []
    out = []
    for node in root.walk():
        if node.kind != "ButtonControl":
            continue
        name = (node.name or "").strip()
        if not any(w in name for w in CHIP_NEAR_NAMES):
            continue
        if name == CHIP_BUTTON:                        # 芯片自己不算相邻按钮
            continue
        try:
            out.append((name, _box(node)))
        except Exception:                              # noqa: BLE001 - 元素失效就跳过
            continue
    return out


def open_chip_menu(s: Screen, writer: str) -> dict:
    """点芯片打开强度层（CARD-06c 第 1/4 条）。返回 {"ok","stage","hint",...}。

    **不按 Ctrl+Shift+M**：那个组合被 Chrome 截成个人资料菜单，弹层根本没开，
    而脚本会以为开了、继续按错的前提往下跑。

    四道检查，顺序从"便宜"到"贵"：
      1. 唯一性：控件树里必须**正好一个**芯片按钮；0 个先点一下输入框再找
         （输入框空/未聚焦时芯片可能还没渲染）；
      2. 几何：芯片矩形要在合理范围内，点击点取矩形中心；
      3. 相邻：落点不能落在听写/语音/发送按钮的矩形里（它们紧贴芯片）；
      4. 距离：落点离橙钮（麦克风/发送键）≥60px；最后仍交给 `safe_click`。
    点完必须复核：`_layer()` 还是 `closed` 就是没开。
    """
    OPEN_STATE.clear()
    nodes = chip_nodes(s)
    if not nodes:
        # 卡面第 1 条：输入框空/未聚焦时先点输入框再定位芯片
        if focus_composer(s):
            time.sleep(0.6)
            nodes = chip_nodes(s)
    if not nodes:
        return {"ok": False, "stage": "chip_not_found",
                "hint": f"控件树里没有「{CHIP_BUTTON}」按钮（点过输入框再找也没有）；"
                        "可能不在新聊天页，或页面还没渲染完"}
    if len(nodes) > 1:
        return {"ok": False, "stage": "chip_not_unique",
                "boxes": [_box(n) for n in nodes],
                "hint": f"找到 {len(nodes)} 个「{CHIP_BUTTON}」按钮，不猜点哪一个"}
    box = _box(nodes[0])
    if not (16 <= box[2] <= CHIP_BOX_LIMIT[0] and 16 <= box[3] <= CHIP_BOX_LIMIT[1]):
        return {"ok": False, "stage": "chip_geometry", "chip": box,
                "hint": f"芯片矩形 {box[2]}x{box[3]} 不像芯片（可能读到了整行容器）"}
    cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2

    near = _near_boxes(s)
    for name, nb in near:
        if _inside(nb, cx, cy):
            return {"ok": False, "stage": "chip_overlaps_control", "chip": box,
                    "near": {"name": name, "box": nb},
                    "hint": f"芯片中心 ({cx},{cy}) 落在「{name}」按钮 {nb} 里，拒绝点击"}
    voice = _voice_center(s)
    distance = None
    if voice is not None:
        distance = max(abs(cx - int(voice[0])), abs(cy - int(voice[1])))
        if distance < CHIP_MIN_VOICE:
            return {"ok": False, "stage": "chip_too_close", "chip": box,
                    "voice": list(voice), "distance": distance,
                    "hint": f"芯片中心离橙钮只有 {distance}px（<{CHIP_MIN_VOICE}），"
                            "可能压到麦克风/发送键，拒绝点击"}

    ok, why = safe_click(s, (cx, cy), voice=voice)
    if not ok:
        return {"ok": False, "stage": "click_blocked", "chip": box,
                "distance": distance, "hint": why}
    time.sleep(1.1)
    layer = _layer(s, writer)
    if layer == "closed":
        return {"ok": False, "stage": "menu_not_open", "chip": box,
                "distance": distance,
                "hint": "点了芯片但弹层没出现（可能被别的元素挡住或页面在切）"}
    return {"ok": True, "stage": "opened", "layer": layer, "chip": box,
            "distance": distance}


def focus_composer(s: Screen) -> bool:
    """点一下输入框空白处，让它获得键盘焦点。

    点的是「+」右边 250px、上 20px 的空白，离橙钮 400px 以上。
    现在只用于"芯片还没渲染出来"时的兜底（卡面第 1 条），开弹层不再靠快捷键。
    """
    m = s.find(COMPOSER, threshold=0.85, region=_win(s))
    if m is None:
        return False
    ok, _why = safe_click(s, (m.center[0] + COMPOSER_DX, m.center[1] + COMPOSER_DY))
    return ok


def open_effort(s: Screen, writer: str) -> bool:
    """打开强度层（第一步）。失败原因记在 `OPEN_STATE` 里。

    已经在 models 层时按一次 Esc 退回 effort 层——不再重新点芯片：
    芯片是 toggle，弹层开着再点一次会把整层关掉（实测）。
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
        return _open_fail("not_new_page",
                          "模式按钮/模型芯片只在新聊天页出现（先开新对话）")
    result = open_chip_menu(s, writer)
    if result["ok"]:
        OPEN_STATE.clear()
        return True
    return _open_fail(result.get("stage", "menu_not_open"),
                      result.get("hint", ""), **{k: v for k, v in result.items()
                                                 if k not in ("ok", "stage", "hint")})


def _model_entry(s: Screen):
    """弹层里的「选择模型」入口（UIA MenuItemControl）。找不到返回 None。

    UIA 优先：它可 Invoke，也比按像素找入口行稳——缩放/改版都不会让它消失。
    """
    try:
        for node in _doc_root(s).walk():
            if node.kind == "MenuItemControl" and (node.name or "").strip() == "选择模型":
                return node
    except Exception:                                  # noqa: BLE001 - 读不到就走老路
        return None
    return None


def enter_models(s: Screen, writer: str) -> dict:
    """进模型列表（第二步）。返回 {"ok","stage","hint",...}。

    CARD-06c 第 3 条：强度层开着、但里面**没有「选择模型」入口**时，
    返回 `stage=no_model_selector` 并 fail-closed——不沿用上次档位、不猜菜单、
    也不假装"选好了"（工作模式实测就没有这个入口）。
    """
    if _items_visible(s, writer):
        return {"ok": True, "stage": "already"}
    if not open_effort(s, writer):
        return {"ok": False, "stage": OPEN_STATE.get("stage") or "menu_not_open",
                "reason": OPEN_STATE.get("reason") or "menu_not_open",
                "hint": OPEN_STATE.get("hint", ""),
                "detail": {k: v for k, v in OPEN_STATE.items()
                           if k not in ("stage", "reason", "hint")}}

    entry = _model_entry(s)
    pos = None if entry is not None else _menu_entry_pos(s)
    if entry is None and pos is None:
        close_all(s, writer)
        return {"ok": False, "stage": "no_model_selector", "writer": writer,
                "reason": "model_entry_missing",
                "hint": "强度弹层里没有「选择模型」入口：这个模式没有模型选择器，"
                        "不沿用上一次的档位（fail-closed）"}
    clicked = False
    if entry is not None:
        try:
            entry.control.GetInvokePattern().Invoke()
            clicked = True
        except Exception:                              # noqa: BLE001 - 没有 Invoke 就点矩形
            try:
                r = entry.control.BoundingRectangle
                clicked, _why = safe_click(s, ((r.left + r.right) // 2,
                                               (r.top + r.bottom) // 2))
            except Exception:                          # noqa: BLE001
                clicked = False
    if not clicked:
        clicked, _why = safe_click(s, pos)
    if not clicked:
        close_all(s, writer)
        return {"ok": False, "stage": "entry_not_clickable", "writer": writer,
                "hint": "找到了「选择模型」入口，但点不动（被遮挡/在动画里）"}
    time.sleep(1.2)
    if not _items_visible(s, writer):
        close_all(s, writer)
        return {"ok": False, "stage": "model_list_not_open", "writer": writer,
                "hint": "点了「选择模型」但列表没出现"}
    return {"ok": True, "stage": "opened"}


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
    """读当前强度。**先读芯片（UIA，只读）**，读不到才开关一次弹层读滑杆值。

    2026-11-02（CARD-06b 第 10 条）：芯片的 UIA 文本就是当前档位词，开弹层
    反而是多余的点击。返回值标明 `source`，看日志时能分清读数来源。

    2026-11-09：新版界面芯片不再暴露档位文本（Button 没有子 TextControl），
    弹层里的滑杆可访问值（`最高，第 5 项，共 5 项。`）成为唯一可靠的 UIA 读数。
    它带序号，按序号映射档位；**不再用滑杆像素位置推算**（那会和旧坐标一起漂）。
    """
    w = writer or read_writer(s).get("writer")
    if w not in EFFORTS:
        return {"effort": "unknown", "hint": "读不出当前模式"}
    chip = read_chip_effort(s)
    if chip["effort"]:
        if chip["effort"] not in EFFORTS[w]:
            # 词表是跨模式共用的（即时/轻度都在里面）。读到不属于当前模式的档名，
            # 说明模式读错了，按"没读到"处理，不硬套表。
            return {"effort": "unknown", "reason": "effort_not_in_writer",
                    "hint": f"芯片读到 {chip['effort']!r}，但它不属于 {w} 模式的档位表"}
        return {"effort": chip["effort"], "label": EFFORTS[w][chip["effort"]][0],
                "source": chip["source"], "raw": chip["raw"]}
    if not open_effort(s, w):
        return {"effort": "unknown", "stage": OPEN_STATE.get("stage") or "menu_not_open",
                "reason": OPEN_STATE.get("reason") or "menu_not_open",
                "hint": OPEN_STATE.get("hint") or "打不开强度弹层（只在新聊天页有）"}
    announce = _announce_effort(s, writer=w)
    close_all(s, w)
    if announce.get("effort"):
        return {"effort": announce["effort"], "label": EFFORTS[w][announce["effort"]][0],
                "source": announce.get("source") or "uia_slider",
                "raw": announce.get("raw")}
    return {"effort": "unknown", "reason": "effort_unreadable",
            "hint": "弹层打开了，但 UIA 读不到滑杆的档位值（既没有第 N 项，也没有档位词）"}


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
        extra = {k: v for k, v in OPEN_STATE.items()
                 if k not in ("stage", "reason", "hint")}
        return {"ok": False, "stage": OPEN_STATE.get("stage") or "menu_not_open",
                "reason": OPEN_STATE.get("reason") or "menu_not_open",
                "writer": w, "hint": OPEN_STATE.get("hint", ""),
                **({"detail": extra} if extra else {})}

    item = _effort_item(s)
    sl = _slider(s)
    if item is None and sl is None:
        return {"ok": False, "stage": "no_slider", "writer": w}
    before_info = _announce_effort(s, writer=w)
    before = before_info.get("effort")
    if before is None and sl is not None:
        before, _bd = _effort_of(sl["knob"], w)     # 旧界面兜底，只作记录

    # 点「强度」行中心让滑杆拿到焦点。点击会把值跳到点击处（中心≈mid），
    # 紧接着就归零，所以跳到哪里都无所谓；关键是**必须点在这一行里**。
    # 2026-11-09：新版弹层左移，`_slider` 的 cy 会落到输入框底边上（差 17px），
    # 点下去滑杆拿不到焦点，方向键一个都不生效——这就是 R4b 后冒烟失败的根因。
    if item is not None:
        point = item["center"]
    else:
        win = sl["win"]
        point = (win[0] + sl["knob"], win[1] + sl["cy"])
    ok, why = safe_click(s, point)
    if not ok:
        return {"ok": False, "stage": "click_blocked", "detail": why, "before": before}
    time.sleep(0.6)

    order = effort_order(w)
    # 归零：按左键到底，多按两下
    for _ in range(len(order) + 1):
        s.press("left")
        time.sleep(0.45)
    # 往上走到目标档
    idx = order.index(key)
    for _ in range(idx):
        s.press("right")
        time.sleep(0.45)
    time.sleep(0.5)

    # 弹层还开着时读 UIA 的滑杆值（`轻度，第 1 项，共 5 项。`）。位置只留作诊断。
    announce = _announce_effort(s, writer=w)
    after_uia = announce.get("effort")
    after_sl = _slider(s)
    after, ad = _effort_of(after_sl["knob"], w) if after_sl else (None, None)
    knob = after_sl["knob"] if after_sl else None
    close_all(s, w)                        # Esc 才会让强度生效
    time.sleep(0.5)
    # 判定：**芯片优先**（用户看到的档位）。新版芯片不再暴露档位文本时，
    # 退到滑杆的可访问值——它是滑杆自己念出来的当前值，不是按键次数/像素位置推断。
    chip_after = read_chip_effort(s)
    after_value = chip_after["effort"] or after_uia
    read = ("uia_chip" if chip_after["effort"] else
            ("uia_slider" if after_uia else "none"))
    ok2 = after_value == key
    out = {"ok": ok2, "stage": "done" if ok2 else "unverified",
           "before": before, "after": after_value, "want": key, "writer": w,
           "read": read, "chip": chip_after["raw"], "announce": after_uia,
           "slider_geometry": after, "knob": knob, "off": ad, "by": "keyboard"}
    if not ok2:
        if after_value is None:
            out["reason"] = "effort_unreadable"
            out["hint"] = ("芯片和滑杆可访问值都读不回强度；按第 10 条整体判失败，"
                           "不用按键次数或像素位置代替读数")
        else:
            out["reason"] = "readback_mismatch"
            out["hint"] = (f"读数显示 {after_value!r}，要的是 {key!r}"
                           f"（芯片 {chip_after['effort']!r}，"
                           f"滑杆 {after_uia!r}，位置推算 {after!r}）")
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


class _Row:
    """UIA 读到的模型行：只要 `.center`，和 Match 的用法保持一致。"""

    def __init__(self, name, box):
        self.name = name
        self.score = 1.0
        self.scale = 1.0
        self.x, self.y, self.w, self.h = box

    @property
    def center(self):
        return self.x + self.w // 2, self.y + self.h // 2


def _scroll_node(node) -> bool:
    """把控件滚进视口。qi.ui 里已有实现，这里包一层是为了离线能打桩。"""
    from qi import ui
    return bool(ui.scroll_into_view(node))


def _find_item_uia(s: Screen, key: str):
    """按 UIA 名字找目标模型行（不受页面缩放/模板失配影响）。

    B17：work 模式 9 项、一屏只放得下 8 项。以前只要 UIA 给得出矩形就直接
    返回——第 9 项（GPT-5.6 Luna）的矩形在折叠线以下，点它的中心等于点在
    列表外，选中态不会变，复核就报 readback_mismatch（S2 的 chatgpt 现场）。
    现在先看 IsOffscreen：在视口外就先 ScrollIntoView，再重新枚举取新矩形；
    滚不动返回 None，交给 `_find_item` 里模板 + `_scroll_list` 的兜底。
    """
    rows = _ui_model_rows(s)
    for row_key, name, box, node, off in rows:
        if row_key != key or not box:
            continue
        if not off:
            return _Row(name, box)
        if _scroll_node(node):
            time.sleep(0.4)
            for k2, n2, b2, _node2, _off2 in _ui_model_rows(s):
                if k2 == key and b2:
                    return _Row(n2, b2)
        return None
    return None


def _find_item(s: Screen, writer: str, tpl: str, scroll: bool = True, key: str | None = None):
    """找模型项：**UIA 优先**，找不到再按模板 + 滚动（工作模式 9 项，一屏只放得下 8 项）。

    逐格滚而不是一次滚 4 格：列表只溢出 1 项，滚多了会把目标又推出去
    （虽然实测到底就停住，但不同窗口高度下不一定）。
    """
    if key:
        row = _find_item_uia(s, key)
        if row is not None:
            return row
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


def _pick_model_templates(s: Screen, writer: str) -> dict:
    """模板兜底：勾 + 每行标题的相对位置。**不做任何推断**（第 10 条）。

    旧版有一条"勾在所有可读项上方 → 选中的是第一行「默认」"的推断。06b r1
    的假读数就是它：`gpt_model_gpt6` 模板没命中，勾的位置被推成 default，
    最后仍然报 ok。现在推不出来就返回 unknown，让上层整体判失败。
    """
    snap, prev = None, None
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
        return {"model": "unknown", "source": "template_template",
                "reason": "no_check_mark", "hint": "列表里找不到勾，也匹不上任何模型名"}
    signed = snap["signed"]
    dists = {k: abs(v) for k, v in signed.items()}
    near = [k for k, d in dists.items() if d <= 20]
    if near:
        best = min(near, key=lambda k: dists[k])
        return {"model": best, "source": "template", "dist": dists}
    return {"model": "unknown", "source": "template", "dist": dists,
            "reason": "ambiguous_check", "hint": "勾落在两行之间，认不出（不再推断「默认」）"}


def _pick_model(s: Screen, writer: str) -> dict:
    """在**已经开着的**列表里读当前模型。不负责开关菜单。

    读法优先级（第 10 条）：
      1. **UIA**：模型项是 RadioButtonControl，`SelectionItemPattern.IsSelected`
         直接给真假（2026-11-02 实测：`GPT-6` sel=True / `GPT-5.6 Sol` sel=False）；
      2. 模板：勾 + 标题相对位置，但**不许推断**，认不出就是 unknown。

    UIA 不依赖模板，也不受页面缩放影响；06b r1 的两个假读数（默认推断、
    gpt_model_gpt6 不命中仍报 ok）都不会再出现。
    """
    uia = read_model_uia(s, writer)
    if uia.get("model") not in (None, "unknown"):
        return {**uia, "source": uia.get("source", "uia")}
    tpl = _pick_model_templates(s, writer)
    if tpl.get("model") not in (None, "unknown"):
        return tpl
    return {**tpl, "uia_reason": uia.get("reason")}


def read_model(s: Screen, writer: str) -> dict:
    """读当前模型。链路：开强度层 -> 进列表 -> 读勾 -> 全关。

    打不开就带 `stage` 回去（`no_model_selector` / `menu_not_open` / `chip_*`），
    不写 "unknown + 一句打不开" 了事——上层和真机证据都要能分清是哪一步。
    """
    if not MODELS.get(writer):
        return {"model": None}
    entered = enter_models(s, writer)
    if not entered["ok"]:
        out = {"model": "unknown", "stage": entered.get("stage", "menu_not_open"),
               "writer": writer, "hint": entered.get("hint") or "打不开模型列表"}
        if entered.get("detail"):
            out["detail"] = entered["detail"]
        return out
    out = _pick_model(s, writer)
    close_all(s, writer)
    return out


def _dump_unverified(s: Screen, key: str, after_info: dict) -> None:
    """复核失配时留现场：截图 + 列表行清单（名字/选中/是否在视口/矩形）。

    只取证、不改判定：取证自己挂了也不能把 fail 变成异常，更不能变成 pass。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        rows = []
        for row_key, name, box, node, off in _ui_model_rows(s):
            try:
                selected = bool(node.control.GetSelectionItemPattern().IsSelected)
            except Exception:                          # noqa: BLE001
                selected = None
            rows.append({"key": row_key, "name": name, "selected": selected,
                         "offscreen": off,
                         "rect": None if box is None else list(box)})
        shot = s.snapshot(f"chatgpt_unverified_{stamp}")
        print(json.dumps({"stage": "unverified_dump", "want": key,
                          "after": after_info.get("model"),
                          "read": after_info.get("source"),
                          "shot": str(shot), "rows": rows}, ensure_ascii=False))
    except Exception as exc:                           # noqa: BLE001
        print(json.dumps({"stage": "unverified_dump_failed",
                          "error_type": type(exc).__name__}, ensure_ascii=False))


def set_model(s: Screen, key: str) -> dict:
    """切模型。

    按用户指出的两步结构：开强度层 -> 进列表 -> 点目标 -> **自动退回强度层**。
    不再像旧版那样在选完之后按 Esc 强行收弹层——那一层本来就是留给人调强度的，
    收掉它等于把第二步吃掉了，也是「选完 default 之后老断」的原因之一。

    点击次数：芯片 1 + 入口 1 + 目标项 1 + 复核再进列表 1 = 4 下，预算 8 够用。
    CARD-06c 第 3 条：找不到模型入口就 `stage=no_model_selector`，不沿用上次档位。
    """
    state = read_writer(s)
    w = state.get("writer")
    if w in MODELS and key not in MODELS[w]:
        # B16：`--new` 把页面重置成了默认的「聊天」模式，而服务端按
        # `_detect_writer`（或默认 work 表）挑了 `gpt56_luna` 这类工作模式档位，
        # 于是 R2 四席那轮直接 `bad_arg / options=["gpt6","gpt56"]`。
        # 新聊天页上模式是可切的：切到目标档位所属的那套表再继续，
        # 同时让后面的 effort 也落到正确那套（chat 只有 instant/mid/high）。
        other = next((name for name, table in MODELS.items()
                      if name != w and key in table), None)
        if other:
            switch = globals().get("choose_writer")
            switched = switch(s, other) if callable(switch) else {"ok": False}
            if not switched.get("ok"):
                return {"ok": False, "stage": "writer_switch_failed",
                        "writer": w, "want_writer": other, "want": key,
                        "hint": "目标模型属于另一个模式，但在新聊天页切模式失败"}
            state = read_writer(s)
            w = state.get("writer")
    if w not in MODELS:
        # B16：把「为什么读不出」带出来。4 席并发时最常见的是新页还没渲染完
        # （uia 里 toggle 为空）或被别人切到了会话页，光一句 hint 分不清。
        detail = {key: state.get(key) for key in
                  ("writer", "source", "uia", "toggle", "hint")
                  if state.get(key) is not None}
        return {"ok": False, "stage": "no_writer", "writer_detail": detail,
                "hint": "读不出当前是聊天还是工作，先用 --writer 定一下（只能在新聊天页做）"}
    models = MODELS[w]
    if key not in models:
        return {"ok": False, "stage": "bad_arg", "writer": w, "want": key,
                "options": list(models)}

    before_sig = chip_signature(s)
    entered = enter_models(s, w)
    if not entered["ok"]:
        return {"ok": False, "stage": entered.get("stage", "menu_not_open"),
                "writer": w, "want": key, "before": None,
                "reason": entered.get("reason"),
                "hint": entered.get("hint") or "打不开模型列表",
                **({"detail": entered["detail"]} if entered.get("detail") else {})}

    picked = _pick_model(s, w)
    cur = picked["model"]
    if cur == key:
        close_all(s, w)
        return {"ok": True, "stage": "already", "before": cur, "after": cur,
                "writer": w, "read": picked.get("source")}

    m = _find_item(s, w, models[key][1], key=key)
    if m is None:
        close_all(s, w)
        return {"ok": False, "stage": "item_not_found", "before": cur, "want": key,
                "writer": w}
    ok, why = safe_click(s, m.center)
    if not ok:
        close_all(s, w)
        return {"ok": False, "stage": "click_blocked", "detail": why,
                "before": cur, "writer": w}
    # 点完会**自动退回强度层**（这是正常行为，不是失败）
    time.sleep(1.4)

    win = _win(s)
    if win:
        vx, vy, vw, vh = s.bounds()
        s.move_to(max(vx + 8, min(win[0] + 60, vx + vw - 8)),
                  max(vy + 8, min(win[1] + 60, vy + vh - 8)))
    time.sleep(0.4)

    if key in NO_READ:
        # 「默认（推荐模型集）」点下去会解析成某个具体模型，芯片写的是那个名字，
        # 永远读不回原名。这里只记录"点完确实回到了强度层 + 芯片变了"这两个
        # **观测**，但按第 10 条**不把它们当成读回**：ok=False，
        # 让上层不要再报"切好了"（旧版就是在这里报 ok=True 的）。
        back_to_effort = _slider(s) is not None
        after_sig = chip_signature(s)
        changed = (before_sig is not None and after_sig is not None and
                   float(np.abs(before_sig.astype(int) - after_sig.astype(int)).mean()) > 2.0)
        close_all(s, w)
        return {"ok": False, "stage": "unverified", "reason": "readback_unavailable",
                "before": cur, "after": None, "want": key, "writer": w,
                "verified_by": "退回强度层+芯片变化",
                "back_to_effort": back_to_effort, "chip_changed": changed,
                "hint": "「默认（推荐模型集）」会解析成具体模型、读不回原名；"
                        "按第 10 条不算读回，整体判失败（use 也会因为 readback 为空拒发）"}

    # 复核：再进一次列表读选中项（此时在强度层，进列表只要 1 下点击）
    again = enter_models(s, w)
    if not again["ok"]:
        close_all(s, w)
        return {"ok": False, "stage": "reverify_failed", "before": cur,
                "writer": w, "reason": again.get("stage")}
    # B17：复核前先把目标项滚进视口。目标可能就是列表最后一项（GPT-5.6 Luna），
    # 勾落在折叠线下时 UIA/模板都读不到，会被误判成 unknown。
    _find_item(s, w, models[key][1], key=key)
    after_info = _pick_model(s, w)
    after = after_info["model"]
    if after != key:
        _dump_unverified(s, key, after_info)           # 列表还开着，先取证再收
    close_all(s, w)
    ok2 = after == key
    out = {"ok": ok2, "stage": "done" if ok2 else "unverified",
           "before": cur, "after": after, "writer": w,
           "read": after_info.get("source")}
    if not ok2:
        out["reason"] = after_info.get("reason") or "readback_mismatch"
        out["hint"] = (f"重新打开列表读到的选中项是 {after!r}，不是 {key!r}；"
                       "按第 10 条整体判失败，不沿用点击得到的结果")
    return out


# ---------------------------------------------------------------- CLI
def _zoom_guard(s: Screen) -> None:
    """页面缩放预检（第 6 条）。不满足就抛 `qi.zoom.ZoomNot100`。

    策略集中在 `qi/zoom.py`：只对浏览器窗口发 Ctrl+0，读不到进程名不动，
    发完必须重新检测。这里只负责调它。
    """
    zoom_mod.ensure(s, verbose=True)


def _evidence(endpoint, screen, hwnd, result, dimension=None):
    """失败留证（第 9 条）：只在 tier_*/zoom_*/human_check/input_mismatch 时留。"""
    try:
        from qi import failure
    except Exception:                                  # noqa: BLE001 - 证据不是主流程
        return
    if dimension:
        result.setdefault("dimension", dimension)
        if not result.get("template"):
            result["template"] = {"name": f"gpt_model_or_chip:{dimension}",
                                  "score": result.get("chip_score"),
                                  "threshold": None, "scale": None,
                                  "hit": False}
    failure.record(endpoint, result, screen=screen, hwnd=hwnd)


def _record_receipt(hwnd, dimension, target, readback, raw, writer=None):
    """写档位回执（CARD-06a (a)）。**失败一律不影响选择结果。**

    本脚本刻意不 import 项目包（单文件契约），所以按需把 src 挂上 sys.path。
    readback 传 None 的有两种情况：没读回来，以及"默认（推荐模型集）"这种
    点了会变成别的模型、永远读不回原名的档位。两种都该让 use 拒发。

    CARD-06c 第 2 条：带上 writer（chat/work）。ChatGPT 两个模式的模型表和
    强度表完全不同，use 要按 writer 去取对应的白名单。
    """
    try:
        from pathlib import Path
        source = str(Path(__file__).resolve().parents[1])
        if source not in sys.path:
            sys.path.insert(0, source)
        from qi import tier_guard
        tier_guard.record_choice("chatgpt", target=target, readback=readback,
                                 dimension=dimension, hwnd=hwnd, raw=raw,
                                 writer=writer)
    except Exception:                                  # noqa: BLE001 - 回执不是主流程
        pass


def _record_composite(screen, hwnd, targets, readbacks=None):
    """写两维回执。先试芯片快照；新版芯片读不到时，用各维度**设完之后**的读数。

    2026-11-09：新版界面的芯片 Button 不再挂档位文本（名字只有
    「选择 ChatGPT 模型」、没有子 TextControl），`chip.chatgpt_snapshot`
    的 model/effort 都是空的。此时退回 `readbacks`：
      · model = `set_model` 重新打开列表读到的选中项（UIA RadioButton）
      · effort = `set_effort` 在弹层里读到的滑杆可访问值（第 N 项）
    这两个都是动作之后的独立 UIA 读数，不是点击结果的回显；来源写在
    raw.source 里（`post_action_readback`），面板和日志能分清。
    """
    from qi import chip, tier_guard
    try:
        snapshot = chip.chatgpt_snapshot(screen)
        values = dict(snapshot.get("dimensions") or {})
        writer = snapshot.get("writer")
        source = "uia_snapshot"
        if not all(values.get(dim) for dim in ("model", "effort")):
            values = {dim: ((readbacks or {}).get(dim) or values.get(dim))
                      for dim in ("model", "effort")}
            source = "post_action_readback"
        complete = bool(writer and all(values.get(dim) for dim in ("model", "effort")))
        matched = complete and all(values.get(key) == value for key, value in targets.items())
        blocks = {dim: {"target": targets.get(dim, values.get(dim)),
                        "readback": values.get(dim) if matched else None}
                  for dim in ("model", "effort")}
        primary = "model" if "model" in targets else "effort"
        raw = {"chip": snapshot, "post": readbacks, "source": source}
        saved = tier_guard.record_choice(
            "chatgpt", target=blocks[primary]["target"], readback=blocks[primary]["readback"],
            dimension=primary, writer=writer, hwnd=hwnd, raw=raw, dimensions=blocks)
        return {"ok": bool(matched and saved.get("written")), "stage": "composite_readback",
                "source": source, "writer": writer, "dimensions": blocks, "receipt": saved}
    except Exception as exc:
        # Invalidate the previous receipt even when the final snapshot fails.
        _record_receipt(hwnd, "model", targets.get("model"), None,
                        {"error": f"{type(exc).__name__}: {exc}"})
        return {"ok": False, "stage": "composite_readback", "hint": str(exc)}


def _wait_new_page(s, site, timeout: float) -> dict:
    """`--new` 之后等新聊天页真正可读，再放行后面的模型/强度选择。

    B16：4 席并发下 SPA 重排 + 别的席位切标签，固定 `sleep(1.0)` 常常不够；
    `set_model` 第一句 `read_writer` 读到 unknown 就直接 `no_writer` 退出。
    这里同时等两个条件：页头条带显示「聊天/工作」（`page_kind == "new"`）、
    且 `read_writer` 真的读得出模式。一轮等满仍不行就再点一次新聊天，
    第二轮还不行才 fail-closed —— 绝不猜一个模式继续选。

    `timeout=0` 时调用方根本不会进来（保持旧的固定 sleep 行为）。
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    polls = 0
    last: dict = {"page_kind": None, "writer": None}
    for round_no in range(2):
        while time.monotonic() < deadline:
            polls += 1
            kind = page_kind(s)
            state = read_writer(s) if kind == "new" else {"writer": "unknown"}
            last = {"page_kind": kind, "writer": state.get("writer"),
                    "writer_source": state.get("source"),
                    "writer_hint": state.get("hint")}
            if kind == "new" and state.get("writer") in ("chat", "work"):
                return {"ok": True, "stage": "new_page_ready",
                        "polls": polls, "round": round_no, **last}
            time.sleep(0.5)
        if round_no == 0:
            again = _open_new_chat(s, site)
            last["reopen"] = {"ok": bool(again.get("ok")),
                              "stage": again.get("stage")}
            if not again.get("ok"):
                break
    return {"ok": False, "stage": "new_page_not_ready",
            "polls": polls, "timeout": float(timeout),
            "hint": "新聊天页在预算内没渲染出模式按钮（或读不出聊天/工作）",
            **last}


def main() -> int:
    idle_need, idle_wait, force, args = guard.parse_guard_args(list(sys.argv[1:]))
    parser = argparse.ArgumentParser(description="选择 ChatGPT 模式、模型和强度，不发送消息")
    parser.add_argument("--list", "-l", action="store_true", help="读取当前状态")
    parser.add_argument("--new", action="store_true", help="先开新对话")
    parser.add_argument("--writer", choices=TAB_WORDS, type=str.lower)
    parser.add_argument("--model", choices=sorted({key for items in MODELS.values() for key in items}),
                        type=str.lower)
    parser.add_argument("--effort", choices=sorted({key for items in EFFORTS.values() for key in items}),
                        type=str.lower)
    parser.add_argument("--tab", type=int, choices=range(1, 10))
    parser.add_argument("--ready-wait", type=float, default=0.0, metavar="SEC",
                        help="--new 之后等新聊天页可读的最长秒数（0=沿用旧的固定 1 秒）")
    options = parser.parse_args(args)
    if not 0.0 <= options.ready_wait <= 60.0:
        parser.error("--ready-wait 要在 0~60 秒之间")
    do_list = options.list
    want_model, want_writer, want_effort = options.model, options.writer, options.effort
    if do_list and any(value is not None for value in (want_model, want_writer, want_effort)):
        parser.error("--list 不能与 --writer、--model、--effort 同用；--new --list 可以同用")
    if not (do_list or options.new or want_model or want_writer or want_effort):
        parser.print_help()
        return 2
    if want_writer is not None and want_model is not None and want_model not in MODELS[want_writer]:
        parser.error(f"模型 {want_model} 不属于 {want_writer} 模式")
    if want_writer is not None and want_effort is not None and want_effort not in EFFORTS[want_writer]:
        parser.error(f"强度 {want_effort} 不属于 {want_writer} 模式")

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
    hwnd = ensure_site("chatgpt", tab=options.tab)
    if hwnd is None:
        print("开不出浏览器窗口（检查 Chrome 路径）")
        return 1
    s.bind(hwnd)
    if not s.focus():
        print("窗口提不到前台")
        return 1
    s.tab_site = "chatgpt"
    time.sleep(1.2)
    done_dims = {}
    try:
        if options.new or want_writer is not None:
            # 模式按钮只在新对话页出现，所以切模式前先确保在新页
            _NEWCHAT = Site(name="chatgpt", title_hint=TITLE, composer="gpt_composer",
                            send="gpt_send_on", actions="gpt_actions", copy="gpt_copy",
                            composer_offset=(240, -5),
                            newchat=("gpt_newchat", "gpt_newchat_icon"), sidebar="gpt_sidebar",
                            send_mode="exists")
            result = _open_new_chat(s, _NEWCHAT)
            print(json.dumps(result, ensure_ascii=False))
            if not result["ok"]:
                return 1
            if options.ready_wait > 0:
                ready = _wait_new_page(s, _NEWCHAT, options.ready_wait)
                print(json.dumps(ready, ensure_ascii=False))
                if not ready["ok"]:
                    return 1
            else:
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
            # 模式/模型/强度任一读不出来都算失败：返回 0 会把"没读到"记成通过
            bad = (out.get("writer") in (None, "unknown")
                   or out.get("model") in (None, "unknown")
                   or out.get("effort") in (None, "unknown"))
            return 1 if bad else 0

        # 页面缩放预检（第 6 条）：非 100% 先 Ctrl+0，仍不是就退出。只对浏览器窗口发。
        _zoom_guard(s)
        if want_writer is not None:
            r = choose_writer(s, want_writer)
            print(json.dumps(r, ensure_ascii=False))
            if not r["ok"]:
                return 1
        if want_model is not None:
            r = set_model(s, want_model)
            done_dims["model"] = r
            print(json.dumps(r, ensure_ascii=False))
            rec = globals().get("_record_receipt")
            if not r["ok"]:
                # 失败也要落一张回执（readback 为空）：否则上一次的成功回执
                # 还留在 runs\tier\chatgpt.json 里，use 会拿着它当"现在也是这档"
                # ——CARD-06c 第 3 条要求 fail-closed，不沿用上次档位。
                if callable(rec):
                    rec(hwnd, "model", want_model, None, r, r.get("writer"))
                _evidence("chatgpt", s, hwnd, r, "model")
                return 1

        if want_effort is not None:
            # 顺序很重要：必须先选模型再调强度。
            # 因为选完模型会自动退回强度层，正好是调强度的位置；
            # 反过来先调强度再选模型，选完还得重开一次弹层。
            r = set_effort(s, want_effort)
            done_dims["effort"] = r
            print(json.dumps(r, ensure_ascii=False))
            rec = globals().get("_record_receipt")
            if not r["ok"]:
                if callable(rec):                      # 同上：失败即作废旧回执
                    rec(hwnd, "effort", want_effort, None, r, r.get("writer"))
                _evidence("chatgpt", s, hwnd, r, "effort")
                return 1
        recorder = globals().get("_record_composite")
        if callable(recorder) and (done_dims or want_writer is not None):
            targets = {key: value for key, value in
                       (("model", want_model), ("effort", want_effort)) if value is not None}
            readbacks = {dim: (done_dims.get(dim) or {}).get("after")
                         for dim in ("model", "effort")}
            overall = recorder(s, hwnd, targets, readbacks)
            print(json.dumps(overall, ensure_ascii=False))
            if not overall.get("ok"):
                _evidence("chatgpt", s, hwnd, overall)
                return 1
    except ForegroundLost as e:
        # 目标窗口被切走/被盖住：立刻停，后面的点击一个都不发
        print(json.dumps({"ok": False, "stage": "user_took_over",
                          "hint": str(e)}, ensure_ascii=False))
        return 4
    except zoom_mod.ZoomNot100 as e:
        # 页面缩放不是 100% 且 Ctrl+0 也救不回来（CARD-06b 第 6 条）
        result = {**e.as_result(), "ok": False}
        _evidence("chatgpt", s, hwnd, result)
        print(json.dumps(result, ensure_ascii=False))
        return e.code
    if done_dims:
        # 整体结论（第 10 条）：任一维度没读回 → 整体 ok=false。
        # 单看某一步的 ok 会被"这一步只读到了滑杆"骗过去，所以最后给一条总账。
        print(json.dumps({"ok": all(x.get("ok") for x in done_dims.values()),
                          "stage": "done", "dimensions": done_dims,
                          "readback": {k: v.get("after") for k, v in done_dims.items()}},
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    console.setup()          # 入口编码：GBK 控制台不再被 \u200b 打崩（CARD-08 第 1 条）
    raise SystemExit(uilock.entry("chatgpt_choose", main))
