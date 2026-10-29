"""codex（Codex 桌面端 / ChatGPT.exe）：选模型 + 调强度 + 提问收回答。

**单文件，只依赖 cv2 / numpy / ctypes，不 import 本项目的任何模块。**

用法：
    python codex_use.py "只回复两个字：收到"
    python codex_use.py --list                          读当前模型和强度
    python codex_use.py --model gpt6_sol                只切模型
    python codex_use.py --effort high                   只调强度
    python codex_use.py --model gpt6_sol --effort max   两个都调
    python codex_use.py --model gpt6_sol --effort max "问题"

## 坐标的适用范围（重要）

    · 窗口**位置**变了 —— 不影响。每次运行都把窗口摆到 --pos，不依赖它原来在哪。
    · **窗口尺寸 / 屏幕分辨率变了 —— 会影响。** 下面所有偏移都是 WIN_SIZE 下的像素值。
      换显示器后如果放不下 1380x1340，用 --size 改小——但模型列表的模板是按
      1380x1340 裁的，尺寸一变模板匹配分数会掉，需要重裁。
    · 发送键用**相对位置**（SEND_REL，0~1）定位，这是唯一不随尺寸失效的锚点；
      其余控件都相对发送键算，所以窗口等比缩放时基本还能用。

## 界面结构（2026-10-29 实测，窗口 1380x1340）

输入框在底部，右下角：`GPT-6 Astra Ultra ∨`  麦克风  ●（橙色发送键）
点芯片弹出强度层：

    ┌─────────────────────────┐
    │ ⚡        Ultra        ↻ │   ← 强度档位字
    │        GPT-6 Astra ›    │   ← 点这一行进模型列表
    │  ●━━━━━━━━━━━━━━━━━━    │   ← 紫色滑杆
    └─────────────────────────┘

## 已知限制

1. 模型列表有固定最大高度，一屏最多露 7 项。最后两项（GPT-5.6 Terra / GPT-5.6 Luna）
   既露不出来、滚轮也无效（实测滚 9 格纹丝不动，窗口 1340 拉到 1900 也一样），
   会明确报 unreachable。
2. 取回答还没接上：codex 的复制按钮要 hover 才出现，模板还没裁。
   现在 `"问题"` 会发出去、等生成结束，但取回会返回 need_copy_template。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

import cv2
import numpy as np

# ============================================================ 常量

TITLE_PROC = "ChatGPT.exe"                 # 进程名，不是窗口标题
LAUNCH = r"shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"
WIN_SIZE = (1380, 1340)
WIN_POS = (-1440, -560)

# 发送键在窗口内的**相对位置**（0~1）——唯一不随分辨率失效的锚点
SEND_REL = (1180 / 1380, 1298 / 1340)

# 其余控件相对**发送键圆心**的像素偏移（由 UI 布局决定，窗口尺寸固定时有效）
CHIP_DX = -139
COMPOSER_DX, COMPOSER_DY = -480, -58

SLIDER_ABOVE = 45          # 模型行在滑杆上边上方 45px
ITEM_GAP = 31              # 模型列表行距
CLICK_MAX, CLICK_GAP = 14, 0.4

EFFORTS = ["low", "mid", "high", "xhigh", "max"]
EFFORT_LABEL = {"low": "轻度", "mid": "中", "high": "高", "xhigh": "极高", "max": "Ultra"}

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "assets", "library")

MODELS = {
    "default":     "codex_m_default",
    "gpt61_sol":   "codex_m_gpt61_sol",
    "gpt6_astra":  "codex_m_gpt6_astra",
    "gpt61_terra": "codex_m_gpt61_terra",
    "gpt6_sol":    "codex_m_gpt6_sol",
    "gpt6_luna":   "codex_m_gpt6_luna",
}
UNREACHABLE = {
    "gpt56_sol": "第 8 项，只能露半截",
    "gpt56_terra": "第 9 项，被列表固定高度截断",
    "gpt56_luna": "第 10 项，被列表固定高度截断",
}
CHECK_TPL = "codex_check"

# ============================================================ Win32

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "esc": 0x1B, "enter": 0x0D,
      "tab": 0x09, "delete": 0x2E, "left": 0x25, "up": 0x26, "right": 0x27,
      "down": 0x28, "a": 0x41, "c": 0x43, "v": 0x56, "n": 0x4E}


class _LII(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds():
    lii = _LII(ctypes.sizeof(_LII), 0)
    user32.GetLastInputInfo(ctypes.byref(lii))
    return (kernel32.GetTickCount() - lii.dwTime) / 1000.0


def _pid_of(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _proc_name(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(h)


def _windows():
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            out.append((int(hwnd), r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumWindows(cb, 0)
    return out


def find_window():
    """按进程名找主窗口。不能按标题——codex 窗口也叫 ChatGPT，
    和 Chrome 的 `ChatGPT - Google Chrome` 会撞（v0.0.8 的基线就是这么废的）。"""
    want = TITLE_PROC.lower()
    best, area = None, 0
    for hwnd, _x, _y, w, h in _windows():
        if w < 400 or h < 300:
            continue
        if _proc_name(_pid_of(hwnd)).lower() != want:
            continue
        if w * h > area:
            area, best = w * h, hwnd
    return best


def rect_of(hwnd):
    r = wintypes.RECT()
    if not user32.GetWindowRect(int(hwnd), ctypes.byref(r)):
        return None
    if r.right - r.left <= 0:
        return None
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def place(hwnd, pos, size):
    user32.SetWindowPos(int(hwnd), 0, int(pos[0]), int(pos[1]),
                        int(size[0]), int(size[1]), SWP_NOACTIVATE | SWP_SHOWWINDOW)


def activate(hwnd, tries=3):
    """提到前台。SetForegroundWindow 常被前台锁拒绝，用 Alt + topmost 兜底。"""
    for i in range(tries):
        if user32.GetForegroundWindow() == int(hwnd):
            return True
        user32.SetForegroundWindow(int(hwnd))
        time.sleep(0.25)
        if user32.GetForegroundWindow() == int(hwnd):
            return True
        if i == 0:
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            time.sleep(0.05)
        elif i == 1:
            user32.SetWindowPos(int(hwnd), -1, 0, 0, 0, 0, 0x0001 | 0x0002)
            time.sleep(0.2)
            user32.SetWindowPos(int(hwnd), -2, 0, 0, 0, 0, 0x0001 | 0x0002)
            time.sleep(0.2)
    return user32.GetForegroundWindow() == int(hwnd)


# ============================================================ 操作器

class Desk:
    def __init__(self, hwnd):
        self.hwnd = int(hwnd)
        self._clicks = []

    def grab(self, region=None):
        if region is None:
            region = rect_of(self.hwnd)
            if region is None:
                return np.zeros((0, 0, 3), np.uint8)
        x, y, w, h = (int(v) for v in region)
        if w <= 0 or h <= 0:
            return np.zeros((0, 0, 3), np.uint8)
        hdc = user32.GetDC(0)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mdc, bmp)
        gdi32.BitBlt(mdc, 0, 0, w, h, hdc, x, y, 0x00CC0020)

        class _BI(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                        ("biClrImportant", ctypes.c_uint32)]

        buf = ctypes.create_string_buffer(w * h * 4)
        bi = _BI(ctypes.sizeof(_BI), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        img = np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, :3].copy()
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(0, hdc)
        return img

    def _assert_fg(self):
        fg = user32.GetForegroundWindow()
        if fg != self.hwnd:
            raise RuntimeError(f"目标窗口不在前台（前台={fg}，目标={self.hwnd}），已中止")

    def click(self, x, y):
        self._assert_fg()
        now = time.time()
        self._clicks = [t for t in self._clicks if now - t < 60]
        if len(self._clicks) >= CLICK_MAX:
            raise RuntimeError(f"60 秒内已点 {len(self._clicks)} 下，超过上限")
        if self._clicks:
            gap = CLICK_GAP - (now - self._clicks[-1])
            if gap > 0:
                time.sleep(gap)
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.03)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        if abs(pt.x - int(x)) > 2 or abs(pt.y - int(y)) > 2:
            raise RuntimeError(f"光标没能移到 ({x},{y})，实际 ({pt.x},{pt.y})")
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        self._clicks.append(time.time())

    def press(self, key, times=1):
        self._assert_fg()
        vk = VK[key.lower()]
        for _ in range(times):
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(vk, 0, 2, 0)
            time.sleep(0.05)

    def hotkey(self, *keys):
        self._assert_fg()
        codes = [VK[k.lower()] for k in keys]
        for c in codes:
            user32.keybd_event(c, 0, 0, 0)
            time.sleep(0.02)
        for c in reversed(codes):
            user32.keybd_event(c, 0, 2, 0)
            time.sleep(0.02)

    def type_text(self, text, interval=0.004):
        """逐字符 SendInput UNICODE，中文也能打。"""
        self._assert_fg()

        class _M(ctypes.Structure):
            _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                        ("mouseData", ctypes.c_uint), ("dwFlags", ctypes.c_uint),
                        ("time", ctypes.c_uint), ("dwExtraInfo", ctypes.c_size_t)]

        class _K(ctypes.Structure):
            _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                        ("dwFlags", ctypes.c_uint), ("time", ctypes.c_uint),
                        ("dwExtraInfo", ctypes.c_size_t)]

        class _H(ctypes.Structure):
            _fields_ = [("uMsg", ctypes.c_uint), ("wParamL", ctypes.c_ushort),
                        ("wParamH", ctypes.c_ushort)]

        class _U(ctypes.Union):
            _fields_ = [("mi", _M), ("ki", _K), ("hi", _H)]

        class _IN(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", ctypes.c_uint), ("u", _U)]

        for ch in text:
            if ch == "\n":
                self.press("enter")
                continue
            code = ord(ch)
            for flags in (0x0004, 0x0004 | 0x0002):
                inp = _IN(type=1, u=_U(ki=_K(0, code, flags, 0, 0)))
                user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_IN))
                time.sleep(interval)

    def set_clipboard(self, text):
        data = ctypes.create_unicode_buffer(text)
        user32.OpenClipboard(0)
        try:
            user32.EmptyClipboard()
            h = kernel32.GlobalAlloc(0x0002, ctypes.sizeof(data))
            p = kernel32.GlobalLock(h)
            ctypes.memmove(p, data, ctypes.sizeof(data))
            kernel32.GlobalUnlock(h)
            user32.SetClipboardData(13, h)
        finally:
            user32.CloseClipboard()

    def clipboard(self):
        user32.OpenClipboard(0)
        try:
            h = user32.GetClipboardData(13)
            if not h:
                return ""
            p = kernel32.GlobalLock(h)
            try:
                return ctypes.wstring_at(p)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()

    def tpl(self, name):
        p = os.path.join(TEMPLATE_DIR, name + ".png")
        img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(f"缺模板 {p}")
        return img

    def find(self, name, threshold=0.86, region=None):
        """返回 (屏幕x, 屏幕y, w, h, score) 或 None。"""
        img = self.grab(region)
        if img.size == 0:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        t = self.tpl(name)
        if t.shape[0] > gray.shape[0] or t.shape[1] > gray.shape[1]:
            return None
        res = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
        _mn, mx, _ml, ml = cv2.minMaxLoc(res)
        if mx < threshold:
            return None
        ox, oy = (region[0], region[1]) if region else (0, 0)
        return (ox + ml[0], oy + ml[1], t.shape[1], t.shape[0], float(mx))


# ============================================================ 定位

def find_send(d, win):
    """发送键圆心（屏幕坐标）。按橙色筛，取最靠右下那块；认不出就用相对位置。"""
    img = d.grab(win)
    if img.size:
        b = img[:, :, 0].astype(int)
        g = img[:, :, 1].astype(int)
        r = img[:, :, 2].astype(int)
        m = ((r > 190) & (b < 130) & (g > 70) & (g < 190)).astype(np.uint8)
        h, w = m.shape
        m[: int(h * 0.6), :] = 0
        m[:, : int(w * 0.5)] = 0
        n, _lb, st, _c = cv2.connectedComponentsWithStats(m, 8)
        best = None
        for i in range(1, n):
            x, y, cw, ch, a = (int(st[i][0]), int(st[i][1]), int(st[i][2]),
                               int(st[i][3]), int(st[i][4]))
            if 22 <= cw <= 40 and 22 <= ch <= 40 and a > 120:
                if best is None or x + y > best[0] + best[1]:
                    best = (x, y, cw, ch)
        if best is not None:
            x, y, cw, ch = best
            return (win[0] + x + cw // 2, win[1] + y + ch // 2)
    return (win[0] + int(win[2] * SEND_REL[0]), win[1] + int(win[3] * SEND_REL[1]))


def locate(d, win):
    send = find_send(d, win)
    if send is None:
        return None
    return {"send": send,
            "chip": (send[0] + CHIP_DX, send[1]),
            "composer": (send[0] + COMPOSER_DX, send[1] + COMPOSER_DY)}


def find_slider(d, win, chip):
    """强度层里的紫色滑杆，返回窗口内 (x,y,w,h) 或 None。"""
    img = d.grab(win)
    if img.size == 0:
        return None
    b = img[:, :, 0].astype(int)
    g = img[:, :, 1].astype(int)
    r = img[:, :, 2].astype(int)
    m = ((b > 200) & (r > 140) & (r < 225) & (g < 165)).astype(np.uint8)
    cx, cy = chip[0] - win[0], chip[1] - win[1]
    m[: max(0, cy - 340), :] = 0
    m[max(0, cy - 25):, :] = 0
    m[:, : max(0, cx - 330)] = 0
    m[:, cx + 240:] = 0
    n, _lb, st, _c = cv2.connectedComponentsWithStats(m, 8)
    best = None
    for i in range(1, n):
        x, y, w, h, a = (int(st[i][0]), int(st[i][1]), int(st[i][2]),
                         int(st[i][3]), int(st[i][4]))
        if a > 700 and w > 110 and (best is None or a > best[4]):
            best = (x, y, w, h, a)
    return best[:4] if best else None


def slider_fill_x(d, win, slider):
    """滑杆紫色填充的最右端（窗口内 x）。滑块停在最低档时返回 None（没有紫色）。"""
    x, y, w, h = slider
    img = d.grab(win)
    if img.size == 0:
        return None
    b = img[:, :, 0].astype(int)
    g = img[:, :, 1].astype(int)
    r = img[:, :, 2].astype(int)
    m = ((b > 200) & (r > 140) & (r < 225) & (g < 165)).astype(np.uint8)
    _ys, xs = np.where(m[y:y + h, x:x + w])
    if len(xs) < 60:
        return None
    return x + int(xs.max())


def items_visible(d, win):
    hit = 0
    for name in MODELS.values():
        if d.find(name, 0.86, win):
            hit += 1
            if hit >= 2:
                return True
    return False


def list_band(d, win):
    """列表的纵向范围。用能匹配到的项聚成一组，排除芯片上同名文字。"""
    ys = []
    for name in MODELS.values():
        m = d.find(name, 0.86, win)
        if m:
            ys.append(m[1] + m[3] // 2)
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
    return (win[0], gp[0] - 28, win[2], gp[-1] - gp[0] + 56)


# ============================================================ 选模型

def enter_list(d, win, chip):
    """点芯片 -> 找滑杆 -> 点模型行 -> 列表打开。"""
    if items_visible(d, win):
        return True
    d.click(*chip)
    time.sleep(1.4)
    sl = find_slider(d, win, chip)
    if sl is None:
        return False
    x, y, w, _h = sl
    d.click(win[0] + x + w // 2, win[1] + y - SLIDER_ABOVE)
    time.sleep(1.3)
    return items_visible(d, win)


def read_model(d, win):
    band = list_band(d, win)
    if band is None:
        return {"model": "unknown", "hint": "列表里一项都没匹配到"}
    chk = d.find(CHECK_TPL, 0.80, band)
    if chk is None:
        return {"model": "unknown", "hint": "找不到勾"}
    cy = chk[1] + chk[3] // 2
    best, bd = None, 10 ** 9
    for key, name in MODELS.items():
        m = d.find(name, 0.86, band)
        if m is None:
            continue
        dist = abs(m[1] + m[3] // 2 - cy)
        if dist < bd:
            bd, best = dist, key
    if best is None or bd > 16:
        return {"model": "unknown", "hint": f"勾不在任何一行上（最近 {bd}px）"}
    return {"model": best, "dist": bd}


def set_model(d, win, chip, key):
    if key in UNREACHABLE:
        return {"ok": False, "stage": "unreachable",
                "hint": UNREACHABLE[key] + "；列表固定高度且不响应滚轮"}
    if key not in MODELS:
        return {"ok": False, "stage": "bad_arg", "want": key, "options": list(MODELS)}
    if not enter_list(d, win, chip):
        return {"ok": False, "stage": "list_not_open"}
    before = read_model(d, win)
    if before.get("model") == key:
        return {"ok": True, "stage": "already", "before": key, "after": key}

    band = list_band(d, win)
    if band is None:
        return {"ok": False, "stage": "list_band_none"}
    chk = d.find(CHECK_TPL, 0.80, band)
    keys = list(MODELS)
    if chk is not None and before.get("model") in keys:
        # 用「勾的位置 + 固定行距 31px」推算目标行。
        # 不做整窗模板搜索：列表位置每次都变，模板分数会掉到 0.88，
        # 而 GPT-6.1 Sol 和 GPT-6 Sol 文字极像，整窗搜会串味
        # （实测点了 gpt6_sol 却切成了 gpt61_sol）。
        cur_idx = keys.index(before["model"])
        tgt_idx = keys.index(key)
        cy = chk[1] + chk[3] // 2 + (tgt_idx - cur_idx) * ITEM_GAP
        m = d.find(MODELS[key], 0.86, band)
        cx = (m[0] + m[2] // 2) if m else (band[0] + 60)
        d.click(cx, cy)
    else:
        m = d.find(MODELS[key], 0.86, band)
        if m is None:
            return {"ok": False, "stage": "item_not_visible", "want": key}
        d.click(m[0] + m[2] // 2, m[1] + m[3] // 2)
    # 点完模型项，弹层会收起。这时直接点芯片会被"关闭动画"吃掉
    # （实测 reverify_failed）。先点一下输入框空白：既把残余弹层关掉，
    # 又把键盘焦点还给页面，比按 Esc 稳（Esc 会吞掉下一次点击）。
    time.sleep(1.0)
    pm = locate(d, win)
    if pm is not None:
        d.click(*pm["composer"])
    time.sleep(1.2)

    if not enter_list(d, win, chip):
        return {"ok": False, "stage": "reverify_failed", "before": before.get("model")}
    after = read_model(d, win)
    ok = after.get("model") == key
    return {"ok": ok, "before": before.get("model"), "after": after.get("model"),
            "want": key, "stage": "done" if ok else "unverified"}


# ============================================================ 调强度

def _open_effort(d, chip):
    d.press("esc")
    time.sleep(0.6)
    d.click(*chip)
    time.sleep(1.4)


def read_effort(d, win, chip):
    _open_effort(d, chip)
    sl = find_slider(d, win, chip)
    if sl is None:
        d.press("esc")
        return {"effort": "unknown", "hint": "没找到滑杆"}
    x, y, w, h = sl
    right = slider_fill_x(d, win, sl)
    d.press("esc")
    time.sleep(0.4)
    if right is None:
        return {"effort": "low", "label": EFFORT_LABEL["low"], "at_min": True,
                "note": "滑杆没有紫色 = 停在最低档"}
    frac = (right - x) / max(1, w)
    idx = min(len(EFFORTS) - 1, max(0, int(round(frac * (len(EFFORTS) - 1)))))
    return {"effort": EFFORTS[idx], "label": EFFORT_LABEL[EFFORTS[idx]],
            "frac": round(frac, 3)}


def set_effort(d, win, chip, key):
    """调强度：点滑块 -> 左键到底 -> 右键走 N 次 -> Esc 生效。

    **不能拖**：实测拖 -52px 实际走 -82px（过冲），而且连续拖同一控件
    从第二次开始就不动，一次只能落在两档中间。
    """
    if key not in EFFORTS:
        return {"ok": False, "stage": "bad_arg", "want": key, "options": EFFORTS}
    _open_effort(d, chip)
    sl = find_slider(d, win, chip)
    if sl is None:
        return {"ok": False, "stage": "no_slider"}
    x, y, w, h = sl
    before = read_effort(d, win, chip).get("effort")
    _open_effort(d, chip)
    sl = find_slider(d, win, chip)
    if sl is None:
        return {"ok": False, "stage": "no_slider"}
    x, y, w, h = sl
    right = slider_fill_x(d, win, sl)
    knob_x = (right if right is not None else x + 16)
    d.click(win[0] + knob_x - 4, win[1] + y + h // 2)
    time.sleep(0.6)
    for _ in range(len(EFFORTS) + 1):
        d.press("left")
        time.sleep(0.3)
    for _ in range(EFFORTS.index(key)):
        d.press("right")
        time.sleep(0.3)
    time.sleep(0.4)
    after = read_effort(d, win, chip)
    ok = after.get("effort") == key
    return {"ok": ok, "before": before, "want": key, "after": after.get("effort"),
            "stage": "done" if ok else "unverified"}


# ============================================================ 提问

def ask(d, win, prompt, timeout=180.0):
    pos = locate(d, win)
    if pos is None:
        return {"ok": False, "stage": "no_send_button"}
    d.click(*pos["composer"])
    time.sleep(0.4)
    d.hotkey("ctrl", "a")
    time.sleep(0.1)
    d.press("delete")
    time.sleep(0.15)
    d.type_text(prompt)
    time.sleep(0.5)
    pos = locate(d, win)
    d.click(*pos["send"])

    # 等生成结束：发送键从「生成中的方块」恢复成橙色圆钮
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(2.0)
        p = locate(d, win)
        if p is None:
            continue
        img = d.grab(win)
        if img.size == 0:
            continue
        x, y = p["send"][0] - win[0], p["send"][1] - win[1]
        patch = img[max(0, y - 12):y + 12, max(0, x - 12):x + 12]
        if patch.size == 0:
            continue
        b = patch[:, :, 0].astype(int)
        r = patch[:, :, 2].astype(int)
        if int(((r - b) > 60).sum()) > 60:
            break
    else:
        return {"ok": False, "stage": "timeout", "hint": f"{timeout}s 内没生成完"}

    time.sleep(1.0)
    return {"ok": False, "stage": "need_copy_template",
            "hint": "回答已生成，但 codex 的复制按钮要 hover 才出现，模板还没裁"}


# ============================================================ CLI

def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("prompt", nargs="?", default=None)
    ap.add_argument("--list", "-l", action="store_true")
    ap.add_argument("--model", default=None)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--size", default=None, help="WxH，默认 1380x1340")
    ap.add_argument("--pos", default=None, help="X,Y，默认 -1440,-560")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--wait", type=float, default=120.0)
    ap.add_argument("--idle", type=float, default=2.0)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    size, pos = WIN_SIZE, WIN_POS
    if a.size:
        try:
            w, h = a.size.lower().split("x")
            size = (int(w), int(h))
        except ValueError:
            print("--size 要写成 1380x1340")
            return 2
    if a.pos:
        try:
            x, y = a.pos.split(",")
            pos = (int(x), int(y))
        except ValueError:
            print("--pos 要写成 -1440,-560")
            return 2

    if not a.prompt and not a.list and not a.model and not a.effort:
        print(__doc__)
        print("可选模型：" + ", ".join(MODELS))
        print("看不到的：" + ", ".join(UNREACHABLE))
        print("可选强度：" + ", ".join(EFFORTS))
        return 2

    if not a.force:
        end = time.time() + a.wait
        while time.time() < end:
            if idle_seconds() >= a.idle:
                break
            time.sleep(0.2)
        else:
            print(json.dumps({"ok": False, "stage": "user_busy",
                              "hint": f"{a.wait}s 内你一直在用电脑"}, ensure_ascii=False))
            return 4

    hwnd = find_window()
    if hwnd is None:
        print("启动 codex …")
        subprocess.Popen(["explorer.exe", LAUNCH])
        end = time.time() + 30
        while time.time() < end and hwnd is None:
            time.sleep(1.0)
            hwnd = find_window()
    if hwnd is None:
        print(json.dumps({"ok": False, "stage": "no_window"}, ensure_ascii=False))
        return 1

    place(hwnd, pos, size)
    time.sleep(0.6)
    if not activate(hwnd):
        print("警告：没能提到前台，后续点击会被拦", file=sys.stderr)
    time.sleep(1.0)
    d = Desk(hwnd)
    win = rect_of(hwnd)
    if win is None:
        print(json.dumps({"ok": False, "stage": "no_rect"}, ensure_ascii=False))
        return 1
    pm = locate(d, win)
    if pm is None:
        print(json.dumps({"ok": False, "stage": "no_send_button",
                          "hint": "找不到发送键：窗口尺寸不对或没登录"}, ensure_ascii=False))
        return 1
    chip = pm["chip"]
    rc = 0

    try:
        if a.list:
            out = {}
            if enter_list(d, win, chip):
                out.update(read_model(d, win))
                d.press("esc")
                time.sleep(0.6)
            else:
                out.update({"model": "unknown", "hint": "打不开模型列表"})
            out.update(read_effort(d, win, chip))
            print(json.dumps(out, ensure_ascii=False))
            return 0

        if a.model:
            r = set_model(d, win, chip, a.model)
            print(json.dumps(r, ensure_ascii=False))
            rc = 0 if r.get("ok") else 1
        if a.effort:
            r = set_effort(d, win, chip, a.effort)
            print(json.dumps(r, ensure_ascii=False))
            rc = 0 if r.get("ok") else 1
        if a.prompt:
            r = ask(d, win, a.prompt, a.timeout)
            print(json.dumps(r, ensure_ascii=False))
            rc = 0 if r.get("ok") else 1
    except RuntimeError as e:
        print(json.dumps({"ok": False, "stage": "aborted", "hint": str(e)},
                         ensure_ascii=False))
        return 4
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
