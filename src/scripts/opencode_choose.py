"""opencode（OpenCode 桌面端 / OpenCode.exe）：选模型 + 调强度 + 读当前状态。

**定位主体使用 cv2/numpy/ctypes；运行入口共用 qi 的锁和自身输入标记。**

用法：
    python opencode_choose.py --list                      读当前模型和强度
    python opencode_choose.py --model "Claude Opus 5.5"   只切模型（名字给够精确）
    python opencode_choose.py --model glm_53_flash_free   切到 GLM-5.3-Flash Free
    python opencode_choose.py --effort max                只调强度
    python opencode_choose.py --model deepseek_v41_flash_free --effort low
    python opencode_choose.py --probe                     只报模板命中，不点击

## 可读回的模型（模板库只覆盖这三个）

    glm_53_flash_free          GLM-5.3-Flash Free        （CARD-06c 第 6 条加的）
    deepseek_v41_flash_free    DeepSeek V4.1 Flash Free
    claude_opus_55             Claude Opus 5.5

GLM 这一档只用于 `--purpose accept_lite`（验链路连通性），**不进 accept**：
accept 仍是 Claude Opus 5.5 那一档，验收结论也要写 PASS(GLM-lite) 以示区分。

## 界面结构（2026-11-07 实测，窗口 1296x808）

输入框底部一行：

    +    OpenCode / GLM-5.3-Flash Free ⌄     Default ⌄        [↑]

**模型和强度是两个独立芯片**，各自带一个 ⌄：
    · 点第 1 个 ⌄ -> 模型面板（顶部有「🔍 搜索模型」搜索框 + Recent/供应商分组 + 可滚列表）
    · 点第 2 个 ⌄ -> 强度小菜单（Default / Low / High / Max，四行，右侧打勾）

注意：**不是每个模型都有强度芯片**。实测 Claude Opus 5.5 没有 variant，
第二个 ⌄ 直接不出现（这时 --effort 会明确报 no_effort_chip）；GLM-5.3-Flash Free
在本机也只读到 1 个 ⌄（effort=none）。这只影响 --effort，不影响 --model。

### 空白新会话：composer 在**页面中央**

    2026-11-07 实测：发送键 (1051,421)、模型文字 (339,427)、⌄ (476,432)、
    占位文字 (225,387) —— 全在页面中央，离底 200px 以上。

所以**不能再按"距底 170px"找工具条**（旧实现就是这样，结果 model=unknown、
chevrons=0）。现在的判据是 `composer_row()`：先找发送键（整窗唯一），
找不到再拿输入框占位文字 +29px 当基线，两个都没有就返回 None（fail-closed）。

### 模型身份：UIA 按钮名优先，模板只作兜底

    ButtonControl 'OpenCode / GLM-5.3-Flash Free'   ← 控件身份，精确
    TextControl  'GLM-5.3-Flash Free'               ← 只是屏幕上的字，不证明是芯片

`read_model()` 先读这个按钮名（`chip_node()`），尾部必须**恰好等于**清单里的
显示名；读不到控件才退回模板，并且模板必须落在 composer 行里、**紧挨着自己的 ⌄**
（`CHIP_TEXT_GAP`），否则算"没读到"——宁可不确认，也不把别处的同名文字当模型。
退回模板时结果里带 `source=template`，UIA 命中带 `source=uia`，失败留证能看懂。

### 首页 / 会话列表：没有 composer

首页只有「搜索会话」框（600x30）和会话列表，没有发送键。`session_state()`
先看 UIA 有没有 composer 输入框（>=400x60，搜索框高度不够会被排掉），
再看画面有没有工具条；两条都不成立就是 `home`：`--list/--model/--effort`
会在**点任何东西之前**以 `session_not_open`（退出码 1）拒掉。
**绝不用"先打一句话把 composer 顶出来"这种做法试探**——那等于往用户输入框里塞垃圾。

## 选模型为什么走搜索框

模型列表有 30+ 项、分组重复（Recent 和正式分组里都有 Claude Haiku 5.5 等），
靠模板逐行找会串味、还得滚动。而面板自带搜索框：
    点芯片 -> 点搜索框 -> 输入名字 -> Enter
过滤后只剩匹配项，Enter 直接选中第一项。所以 --model 传的名字**要够精确**
（"Claude Opus 5.5" 而不是 "Claude"），否则选中的是过滤后的第一条。
搜索框优先按占位模板 `oc_search_ph` 定位（与面板几何无关），定位不到才退回
`panel_bbox()` 里算出来的位置。

## 定位方式

    oc_chevron      芯片右端那个 ⌄（8x10）。composer 行里按 x 排序：
                    第 1 个 = 模型芯片，第 2 个 = 强度芯片（可能没有）
    oc_e_default    强度菜单四行的文字，也用来读芯片上的档位
    oc_e_low
    oc_e_high
    oc_e_max
    oc_placeholder  输入框占位文字，命中 = 输入框是空的
    oc_search_ph    模型面板搜索框的占位文字（面板开着的硬证据）
    oc_send         发送键（深色圆角方块 + 白↑）。**形状一样、灰度不同**：
                    输入框空时是浅灰禁用态，有内容才变深色。
                    所以判"能不能发"要看这块的平均亮度，不能只看模板分数
                    （CCOEFF 对整体亮度平移不敏感，浅灰态也能匹到 0.999）。
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

PROC = "OpenCode.exe"
LAUNCH = r"shell:AppsFolder\ai.opencode.desktop"

TPL_CHEV = "oc_chevron"
TPL_SEND = "oc_send"
TPL_PH = "oc_placeholder"
TPL_CHECK = "oc_check"
TPL_E = {k: "oc_e_" + k for k in ("default", "low", "high", "max")}

TPL_THR = 0.90
BAND_H = 170            # 旧版"底部 170px"常量：只留给 band_top() 兼容，不再限制搜索区
CHEV_THR = 0.85

# 2026-11-07：新会话是"空白页 + **居中** composer"，底部 170px 里什么都没有。
# 工具条（模型芯片 + 强度芯片 + 发送键）现在按**发送键**定位，不再按距离底部的高度。
COMPOSER_ROW_H = 64     # 工具条带：以发送键中心为基线上下各 32px
CHIP_TEXT_GAP = 24      # 模型文字右边缘到它自己 ⌄ 中心的最大间距（控制身份）
TPL_SEARCH = "oc_search_ph"   # 模型面板里的搜索框占位文字（面板开着的硬证据）

# composer 输入框的最小尺寸（UIA EditControl）。首页/会话列表的"搜索会话"框
# 宽够但只有 ~30px 高，靠高度就能排掉，不会把搜索框当输入框。
MIN_COMPOSER_W, MIN_COMPOSER_H = 400, 60

EFFORTS = ["default", "low", "high", "max"]
EFFORT_LABEL = {"default": "Default", "low": "Low", "high": "High", "max": "Max"}
EFFORT_ALIAS = {"mid": "high", "medium": "high", "ultra": "max", "xhigh": "max"}

# read_model 的模板库：key = 给调用方看的名字，value = 模板名
# 模型选择只接受下列可读回验证的标识或完整显示名
MODELS = {
    "glm_53_flash_free": "oc_m_glm53free",
    "deepseek_v41_flash_free": "oc_m_deepseek41free",
    "claude_opus_55": "oc_m_claude_opus55",
}
MODEL_LABELS = {
    "glm_53_flash_free": "GLM-5.3-Flash Free",
    "deepseek_v41_flash_free": "DeepSeek V4.1 Flash Free",
    "claude_opus_55": "Claude Opus 5.5",
}
MODEL_THR = 0.90

TEMPLATE_DIR = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "assets", "library"))
DEBUG_DIR = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "evidence"))

# ============================================================ Win32

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32


def _init_dpi() -> str:
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"


DPI_LEVEL = _init_dpi()

SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "esc": 0x1B, "enter": 0x0D,
      "tab": 0x09, "delete": 0x2E, "left": 0x25, "up": 0x26, "right": 0x27,
      "down": 0x28, "space": 0x20, "backspace": 0x08}
for _i in range(26):
    VK[chr(ord("a") + _i)] = 0x41 + _i
for _i in range(10):
    VK[str(_i)] = 0x30 + _i


class _LII(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds():
    from qi.guard import idle_seconds as shared_idle
    return shared_idle()


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

    def cb(hwnd, _):
        out.append(int(hwnd))
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    return out


def find_window():
    """按进程名找主窗口（Electron 的标题会变成会话名，不能按标题找）。"""
    best = None
    for hwnd in _windows():
        if not user32.IsWindowVisible(hwnd):
            continue
        if _proc_name(_pid_of(hwnd)) != PROC:
            continue
        r = rect_of(hwnd)
        if r is None or r[2] < 400 or r[3] < 300:
            continue
        if best is None or r[2] * r[3] > rect_of(best)[2] * rect_of(best)[3]:
            best = hwnd
    return best


def _win32_size_api():
    """最小化相关入口：句柄必须声明成 HWND，否则 64 位句柄会 OverflowError。"""
    if getattr(_win32_size_api, "ready", False):
        return
    user32.IsIconic.argtypes, user32.IsIconic.restype = [wintypes.HWND], wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    _win32_size_api.ready = True


def _declare_win32():
    """声明 Desk 用到的句柄参数类型（bootstrap 卡：opencode_choose PrintWindow 崩）。

    2026-11-06 实测：`opencode_choose` 崩在 `PrintWindow` 上，报
    `ctypes.ArgumentError: argument 2: OverflowError: int too long to convert`。

    根因不是窗口句柄，而是 **HDC**：`qi.screen` 已经把
    `gdi32.CreateCompatibleDC.restype` 声明成 64 位 `HDC`，所以 `mdc` 是一个
    可能大于 2^31 的完整指针；而本文件从没声明过 `PrintWindow` 的参数类型，
    ctypes 按默认 32 位 `int` 传，第二个参数 `mdc` 直接溢出。
    同一个洞还盖着 `GetWindowDC`（restype 默认 int，会把 HDC 截断成垃圾值）、
    `ReleaseDC`、`SelectObject`、`GetDIBits` 等。

    本文件是独立脚本（不能指望 `qi.screen` 一定先被 import），所以在这一处把
    Desk 用到的句柄/绘图函数一次声明齐，值全部与 `qi.screen` 一致。
    """
    from ctypes import POINTER, c_int, c_uint, c_void_p

    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = c_int
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, c_uint]
    user32.PrintWindow.restype = wintypes.BOOL

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, c_int, c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.BitBlt.argtypes = [wintypes.HDC, c_int, c_int, c_int, c_int,
                             wintypes.HDC, c_int, c_int, wintypes.DWORD]
    gdi32.BitBlt.restype = wintypes.BOOL
    # 第二个参数是位图句柄，同样会超过 2^31（opencode_use 实测就是崩在这句）
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                wintypes.UINT, c_void_p, c_void_p, wintypes.UINT]
    gdi32.GetDIBits.restype = c_int


_declare_win32()


SENTINEL = -32000           # 最小化时 GetWindowRect 的哨兵坐标
MIN_WIN_W, MIN_WIN_H = 200, 120


def _raw_rect(hwnd):
    r = wintypes.RECT()
    if not user32.GetWindowRect(int(hwnd), ctypes.byref(r)):
        return None
    return (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))


def _usable_rect(rect):
    """不是哨兵、宽高过阈值。只判 w>0 不够：最小化时是 160x28 这种"小但正"的尺寸。"""
    if not rect:
        return False
    x, y, w, h = rect
    return x > SENTINEL + 1 and y > SENTINEL + 1 and w >= MIN_WIN_W and h >= MIN_WIN_H


def rect_of(hwnd):
    """窗口矩形。最小化先恢复并**轮询确认**（CARD-06a (c) / 06b 第 7 条）。

    **统一走 `qi.window.restore`**：网页端和四个桌面端同一份实现，判据只有
    "IsIconic 变假 + 矩形脱离 -32000 + 宽高过阈值"。恢复不了就把原矩形还回去，
    由调用方按退出码 7 处理。
    """
    if not hwnd:
        return None
    r = _raw_rect(hwnd)
    if r is not None and _usable_rect(r):
        return r
    try:
        from pathlib import Path
        source = str(Path(__file__).resolve().parents[1])
        if source not in sys.path:
            sys.path.insert(0, source)
        from qi import window as window_mod
        facts = window_mod.restore(int(hwnd), timeout=5.0)
        if facts["ok"]:
            return tuple(facts["rect"])
    except Exception:                       # noqa: BLE001 - 退回原判据，别让取矩形带崩
        pass
    return r

def place(hwnd, pos, size):
    user32.SetWindowPos(int(hwnd), 0, int(pos[0]), int(pos[1]), int(size[0]),
                        int(size[1]), SWP_NOACTIVATE | SWP_SHOWWINDOW)


def activate(hwnd, tries=3):
    # B16：与 *_use.py 同路径（section + SW_RESTORE + Alt 解前台锁 + topmost 闪）。
    # 只改函数体：名字、签名、调用点都不动（测试按名字打桩）。
    from qi import desk as _desk
    return _desk.ensure_foreground(hwnd, tries=max(3, int(tries)))


class Desk:
    def _mark_input(self):
        from qi.guard import mark_synthetic
        mark_synthetic("desk")

    """窗口截图 + 点击 + 按键。截图用 PrintWindow，被遮挡也能拿到完整画面。"""

    CLICK_MAX, CLICK_GAP = 16, 0.35

    def __init__(self, hwnd):
        self.hwnd = int(hwnd)
        self._cache = (0.0, None)
        self._pw_ok = True
        self._clicks = []

    # ---------------- 截图
    def _printwindow(self):
        r = rect_of(self.hwnd)
        if r is None:
            return None
        w, h = r[2], r[3]
        if w <= 0 or h <= 0:
            return None
        hdc = user32.GetWindowDC(self.hwnd)
        if not hdc:
            return None
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mdc, bmp)
        ok = user32.PrintWindow(self.hwnd, mdc, 2)      # PW_RENDERFULLCONTENT
        img = None
        if ok:
            img = self._dib(mdc, bmp, w, h)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(self.hwnd, hdc)
        return img

    @staticmethod
    def _dib(mdc, bmp, w, h):
        class BMI(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_ushort),
                        ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_uint),
                        ("biSizeImage", ctypes.c_uint), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint),
                        ("biClrImportant", ctypes.c_uint)]
        bi = BMI()
        bi.biSize = ctypes.sizeof(BMI)
        bi.biWidth = w
        bi.biHeight = -h
        bi.biPlanes = 1
        bi.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0):
            return None
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        return arr[:, :, :3].copy()

    def _screen_grab(self, region):
        x, y, w, h = region
        hdc = user32.GetDC(0)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mdc, bmp)
        gdi32.BitBlt(mdc, 0, 0, w, h, hdc, x, y, 0x00CC0020)
        img = self._dib(mdc, bmp, w, h)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(0, hdc)
        return img if img is not None else np.zeros((0, 0, 3), np.uint8)

    def grab(self, region=None, *, fresh=False):
        wr = rect_of(self.hwnd)
        if region is None:
            region = wr
        if region is None:
            return np.zeros((0, 0, 3), np.uint8)
        x, y, w, h = (int(v) for v in region)
        if w <= 0 or h <= 0:
            return np.zeros((0, 0, 3), np.uint8)
        if self._pw_ok:
            full = None
            ts, cached = self._cache
            if not fresh and cached is not None and time.time() - ts < 0.25:
                full = cached
            else:
                full = self._printwindow()
                if full is None:
                    self._pw_ok = False
                else:
                    self._cache = (time.time(), full)
            if full is not None:
                dx, dy = x - wr[0], y - wr[1]
                sub = full[max(0, dy):dy + h, max(0, dx):dx + w]
                if sub.shape[0] == h and sub.shape[1] == w:
                    return sub
        return self._screen_grab(region)

    # ---------------- 输入
    def _invalidate(self):
        self._cache = (0.0, None)

    def _assert_fg(self):
        fg = user32.GetForegroundWindow()
        if fg != self.hwnd:
            raise RuntimeError(f"目标窗口不在前台（前台={fg}，目标={self.hwnd}），已中止")

    def click(self, x, y):
        self._assert_fg()
        now = time.time()
        self._clicks = [t for t in self._clicks if now - t < 60]
        if len(self._clicks) >= self.CLICK_MAX:
            raise RuntimeError(f"60 秒内已点 {len(self._clicks)} 下，超过上限")
        if self._clicks:
            gap = self.CLICK_GAP - (now - self._clicks[-1])
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
        self._mark_input()
        self._invalidate()

    def press(self, key, times=1):
        self._assert_fg()
        vk = VK[key.lower()]
        for _ in range(times):
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(vk, 0, 2, 0)
            time.sleep(0.05)
        self._mark_input()
        self._invalidate()

    def hotkey(self, *keys):
        self._assert_fg()
        codes = [VK[k.lower()] for k in keys]
        for c in codes:
            user32.keybd_event(c, 0, 0, 0)
            time.sleep(0.02)
        for c in reversed(codes):
            user32.keybd_event(c, 0, 2, 0)
            time.sleep(0.02)
        self._mark_input()
        self._invalidate()

    def type_text(self, text, interval=0.004):
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

        for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
            self._assert_fg()
            if ch == "\n":
                self.hotkey("shift", "enter")
                continue
            encoded = ch.encode("utf-16-le")
            for i in range(0, len(encoded), 2):
                code = int.from_bytes(encoded[i:i + 2], "little")
                for flags in (0x0004, 0x0004 | 0x0002):
                    inp = _IN(type=1, u=_U(ki=_K(0, code, flags, 0, 0)))
                    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_IN))
                    time.sleep(interval)
        self._mark_input()
        self._invalidate()

    # ---------------- 模板
    def tpl(self, name):
        p = os.path.join(TEMPLATE_DIR, name + ".png")
        img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(f"缺模板 {p}")
        return img

    def find(self, name, threshold=TPL_THR, region=None):
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

    def find_all(self, name, threshold=0.85, region=None, nms=6):
        """返回所有超过阈值的位置（按 x 排序），用简单 NMS 去重。"""
        img = self.grab(region)
        if img.size == 0:
            return []
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        t = self.tpl(name)
        if t.shape[0] > gray.shape[0] or t.shape[1] > gray.shape[1]:
            return []
        res = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.nonzero(res >= threshold)
        cand = sorted(((float(res[y, x]), int(x), int(y)) for y, x in zip(ys, xs)),
                      reverse=True)
        out = []
        for sc, x, y in cand:
            # 去重要用 or：同一行上左右两个 ⌄ 的 y 相同，
            # 用 and 会把第二个当成重复直接丢掉（实测强度芯片的 ⌄ 就是这样消失的）
            if all(abs(x - ox) > nms or abs(y - oy) > nms for _, ox, oy in out):
                out.append((sc, x, y))
        ox, oy = (region[0], region[1]) if region else (0, 0)
        return sorted(((ox + x + t.shape[1] // 2, oy + y + t.shape[0] // 2, sc)
                       for sc, x, y in out), key=lambda p: p[0])


# ============================================================ 定位

def _band(win):
    """旧版底部带。**只保留给老调用方/测试**；判定一律走 composer_row()。"""
    x, y, w, h = win
    top = y + max(0, h - BAND_H)
    return (x, top, w, min(BAND_H, h))


def _content(win):
    """可搜索区域：整个窗口。标题栏里不会出现发送键/占位文字，不用特意裁。"""
    return (win[0], win[1], win[2], win[3])


def _box(match):
    """模板命中 -> (x, y, w, h)；形状不对就当没命中。

    `find()` 正常返回 5 元组（含分数）。这里只认前四位且必须是正数：
    焦点控件失效、测试桩返回 MagicMock、驱动返回半截元组时，
    上层按"没找到"处理（fail-closed），而不是在算术里抛 TypeError。
    """
    try:
        x, y, w, h = (int(v) for v in (match[0], match[1], match[2], match[3]))
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


def composer_row(d, win):
    """composer 底行的基线（屏幕 y）+ 搜索带，找不到返回 None。

    2026-11-07 实测（1296x808 窗口、空白新会话）：composer 在**页面中央**，
    发送键 (1051,421)+30x30、模型芯片文字 (339,427)+131x20、⌄ (476,432)，
    全都不在"距底 170px"那条带里 —— 旧实现因此 model=unknown / chevrons=0。

    现在的顺序（全部语言无关，只认图）：
      1. **发送键**：整窗唯一，居中/贴底都在同一行；找不到再
      2. **输入框占位文字**：工具条在它正下方约 29px（实测 387+20 -> 436）；
      3. 两个都没有 → None。调用方必须 fail-closed，**不许猜位置**。
    """
    send = _box(d.find(TPL_SEND, 0.80, _content(win)))
    if send is not None:
        y = send[1] + send[3] // 2
        return {"y": y, "source": "send", "send": send,
                "band": (win[0], max(win[1], y - COMPOSER_ROW_H // 2),
                         win[2], COMPOSER_ROW_H)}
    ph = _box(d.find(TPL_PH, 0.85, _content(win)))
    if ph is not None:
        y = ph[1] + ph[3] + 29
        return {"y": y, "source": "placeholder", "send": None,
                "band": (win[0], max(win[1], y - COMPOSER_ROW_H // 2),
                         win[2], COMPOSER_ROW_H)}
    return None


def chevrons(d, win):
    """composer 行里所有 ⌄ 的中心（屏幕坐标），按 x 排序。

    第 1 个 = 模型芯片，第 2 个 = 强度芯片（没有 variant 的模型只有一个）。
    发送键右边不会有 ⌄，所以有发送键时只认它左边的，避免把别处的箭头算进来。
    """
    row = composer_row(d, win)
    if row is None:
        return []
    found = d.find_all(TPL_CHEV, CHEV_THR, row["band"])
    if row.get("send") is not None:
        limit = row["send"][0] - 2
        found = [p for p in found if p[0] < limit]
    return found


# ---------------------------------------------------------------- UIA 身份
def _uia_root(d):
    """opencode 的 UIA 树（读不到返回 None）。只读，不点、不聚焦。

    先用 `IsWindow` 挡一层：拿到的不是真窗口（离线测试桩、已经关掉的句柄）
    就别去碰 UIA —— 对假句柄调 `ControlFromHandle` 只会抛异常/惊动离线护栏，
    对判定没有任何帮助。
    """
    try:
        hwnd = int(getattr(d, "hwnd", 0) or 0)
        if not hwnd or not user32.IsWindow(hwnd):
            return None
        from qi import ui
        docs = ui.documents(hwnd)
        if not docs:
            return None
        control, _url = docs[0]
        return ui.tree(control)
    except Exception:                       # noqa: BLE001 - UIA 不可用就退回模板
        return None


def chip_node(d):
    """UIA 里的模型芯片按钮（实测名字 `OpenCode / GLM-5.3-Flash Free`）。

    返回 (node, 按钮全名) 或 None。这是**控制身份**：模板只证明"屏幕上有这串字"，
    按钮名才证明"这串字就是当前模型芯片"。
    """
    root = _uia_root(d)
    if root is None:
        return None
    for node in root.walk():
        name = (node.name or "").strip()
        if node.kind != "ButtonControl" or not name:
            continue
        if name.lower().startswith("opencode") and "/" in name:
            return node, name
    return None


def _model_key(text: str):
    """按钮文本 -> 模型 key（**精确匹配**，认不出返回 None）。

    不做"包含"匹配：`GLM-5.3-Flash Free Plus` 这种更长的名字不能被当成
    `GLM-5.3-Flash Free`——那会把没验证过的模型写进回执。
    """
    raw = str(text or "").strip().strip("“”\"'’‘›>⌄ ").strip()
    low = raw.lower()
    for key, label in MODEL_LABELS.items():
        if label and low == label.lower():
            return key
    return None


def _model_from_uia(d):
    """UIA 读模型。读不到芯片返回 None（让模板兜底）；读到但认不出返回 unknown。"""
    got = chip_node(d)
    if got is None:
        return None
    _node, name = got
    tail = name.split("/", 1)[1].strip() if "/" in name else name
    key = _model_key(tail)
    if key:
        return {"model": key, "text": name, "chip": tail, "source": "uia"}
    return {"model": "unknown", "text": name, "chip": tail, "source": "uia",
            "hint": f"UIA 芯片读到 {tail!r}，不在已知模型清单里，不能确认"}


def composer_edit(d):
    """UIA 里的 composer 输入框（只读）。没有就返回 None。

    这是"现在到底是不是一个能输入的会话"最硬的证据：首页/会话列表也有一个
    宽输入框（「搜索会话」），但它只有 ~30px 高，被 MIN_COMPOSER_H 挡掉；
    实测会话里的 composer 是 872x205。UIA 不可用时返回 None，调用方再用
    `composer_row()` 的画面判据兜底 —— **两条都不成立才算首页**。
    """
    root = _uia_root(d)
    if root is None:
        return None
    best = None
    for node in root.walk():
        if node.kind != "EditControl":
            continue
        try:
            r = node.control.BoundingRectangle
            rect = (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))
        except Exception:                   # noqa: BLE001 - 控件失效就当没这个输入框
            continue
        if rect[2] >= MIN_COMPOSER_W and rect[3] >= MIN_COMPOSER_H:
            if best is None or rect[2] * rect[3] > best[0]:
                best = (rect[2] * rect[3], node, rect)
    if best is None:
        return None
    return {"node": best[1], "rect": best[2]}


def session_state(d, win):
    """当前窗口是不是"一个能输入的会话"。

    "session" = UIA 有 composer 输入框（source=uia），或画面上认得出工具条
                （发送键/输入框占位文字，source=send|placeholder）；
    "home"    = 两条都不成立：首页/会话列表/别的地方。**绝不用"先打一句话把
                composer 顶出来"这种办法试探** —— 那等于往用户的输入框里塞垃圾。
    "unknown" = 探测本身抛异常（COM 掉了/控件树读不了）。这种情况不在这里下结论，
                交给下一层：conversation.read() 找不到输入框会自己报错。

    fail-closed 的口径：只有**确定没有 composer**（home）才拦；确定有就放行；
    探测坏了按"不知道"记，不冒充任何一边。
    """
    try:
        got = composer_edit(d)
        if got is not None:
            return {"state": "session", "source": "uia", "composer": list(got["rect"])}
        row = composer_row(d, win)
        if row is not None:
            return {"state": "session", "source": row["source"], "composer": None}
    except Exception as exc:                # noqa: BLE001 - 探测失败不下结论
        return {"state": "unknown", "source": "none", "composer": None,
                "hint": f"会话探测失败：{type(exc).__name__}: {exc}"}
    return {"state": "home", "source": "none", "composer": None,
            "hint": "当前窗口没有 composer（停在首页/会话列表？）：先打开或新建一个会话"}


def panel_bbox(d, win):
    """模型面板的白色区域（屏幕坐标），没开返回 None。

    面板是窗口中央一整块纯白（实测 480x448），比逐行找文字稳。
    """
    x0 = win[0] + win[2] // 4
    y0 = win[1] + max(0, win[3] // 6)
    reg = (x0, y0, win[2] // 2, int(win[3] * 0.6))
    img = d.grab(reg)
    if img.size == 0:
        return None
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    white = (g > 250).astype(np.uint8)
    n, _lab, st, _c = cv2.connectedComponentsWithStats(white, 8)
    best = None
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in st[i][:5])
        # 尺寸要卡住：页面进入全选状态时白色会连成一大片（实测 690x655），
        # 不卡就会把整页当成面板，然后往页面中间点"搜索框"
        if 380 <= w <= 600 and 350 <= h <= 600 and a > 50000:
            if best is None or a > best[4]:
                best = (x, y, w, h, a)
    if best is None:
        return None
    return (reg[0] + best[0], reg[1] + best[1], best[2], best[3])


def panel_open(d, win):
    """面板开着吗：白色面板 **或** 面板里的搜索框占位文字（后者与位置无关）。"""
    if panel_bbox(d, win) is not None:
        return True
    return d.find(TPL_SEARCH, 0.85, _content(win)) is not None


def search_box(d, win):
    """面板搜索框的点击点：优先按占位文字模板定位（不依赖面板几何）。"""
    m = d.find(TPL_SEARCH, 0.85, _content(win))
    if m is not None:
        return (m[0] + m[2] // 2, m[1] + m[3] // 2)
    panel = panel_bbox(d, win)
    return None if panel is None else _search_box(panel)


def open_model_panel(d, win):
    """打开模型面板，已开则直接返回 True。

    先点 **UIA 认出来的模型芯片按钮**（控制身份），点不到才退回模板 ⌄。
    """
    if panel_open(d, win):
        return True
    got = chip_node(d)
    if got is not None:
        node, _name = got
        try:
            rect = node.control.BoundingRectangle
            d.click((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
            time.sleep(1.4)
        except Exception:                   # noqa: BLE001 - 控件失效就退回模板
            pass
        if panel_open(d, win):
            return True
    ch = chevrons(d, win)
    if not ch:
        return False
    d.click(ch[0][0], ch[0][1])
    time.sleep(1.4)
    return panel_open(d, win)


def close_panel(d, win):
    d.press("esc")
    time.sleep(0.5)
    return not panel_open(d, win)


def effort_menu_open(d, win):
    """强度小菜单开着吗 = 四档文字里有 ≥2 个能同时匹到。"""
    row = composer_row(d, win)
    if row is None:
        return False
    hit = 0
    for k in EFFORTS:
        if d.find(TPL_E[k], 0.85, row["band"]) is not None:
            hit += 1
            if hit >= 2:
                return True
    return False


def open_effort_menu(d, win):
    """点第 2 个 ⌄ 打开强度菜单。没有第 2 个 ⌄ = 这个模型没有强度可选。"""
    if effort_menu_open(d, win):
        return "open"
    if panel_open(d, win):
        close_panel(d, win)
    ch = chevrons(d, win)
    if len(ch) < 2:
        return "no_chip"
    d.click(ch[1][0], ch[1][1])
    time.sleep(1.3)
    return "open" if effort_menu_open(d, win) else "fail"


# ============================================================ 读 / 选

def read_effort(d, win):
    """读强度：芯片上那个词（Default/Low/High/Max）。

    **下限 0.85，不许再降（房主 2026-11-07 R4 明令）。**
    实测切到 Low 后同一张模板只匹到 **0.845**（"Low" 比 "High" 窄，芯片左边缘
    跟着变，亚像素错位一点就掉分）。这种情况现在**按读不到处理**：发送前的
    gate 会以 `tier_unreadable` 拒发，由人工确认页面/重截模板解决 ——
    宁可拒一次，也不拿"看起来像"的分数当档位证据。

    仍然保留"领先第二名 ≥0.15"这条独立判据（两档分数接近 = 分不清是哪一档，
    高分也可能只是错模板匹到了字形相近的地方）。
    """
    if len(chevrons(d, win)) < 2:
        return {"effort": "none", "hint": "这个模型没有强度芯片"}
    row = composer_row(d, win)
    if row is None:
        return {"effort": "unknown", "hint": "找不到 composer 行"}
    band = row["band"]
    best, bs, second = None, 0.0, 0.0
    for k in EFFORTS:
        m = d.find(TPL_E[k], 0.0, band)
        s = m[4] if m else 0.0
        if s > bs:
            best, second, bs = k, bs, s
        elif s > second:
            second = s
    if best is None or bs < 0.85 or bs - second < 0.15:
        return {"effort": "unknown", "score": round(bs, 3), "second": round(second, 3)}
    return {"effort": best, "label": EFFORT_LABEL[best], "score": round(bs, 3),
            "second": round(second, 3)}


def read_model(d, win):
    """读模型名。

    2026-11-07：优先读 **UIA 芯片按钮**（实测 `OpenCode / GLM-5.3-Flash Free`）——
    那是精确的控件身份，不再依赖模板；窗口是"空白新会话/居中 composer"时也照样读得到。
    UIA 不可用才退回模板，并且模板必须落在 composer 行里、紧挨着自己的 ⌄，
    否则算"没读到"（宁可不确认，也不把别处的同名文字当模型）。
    """
    uia = _model_from_uia(d)
    if uia is not None:
        return uia
    row = composer_row(d, win)
    if row is None:
        return {"model": "unknown", "source": "none",
                "hint": "没有可识别的 composer（首页会话列表？先打开或新建一个会话）"}
    band = row["band"]
    best, bs, box = None, 0.0, None
    for k, tpl in MODELS.items():
        m = d.find(tpl, 0.0, band)
        s = m[4] if m else 0.0
        if s > bs:
            bs, best, box = s, k, m
    if best is None or bs < MODEL_THR:
        return {"model": "unknown", "score": round(bs, 3), "source": "template",
                "hint": "composer 行里没匹到已知模型模板"}
    right = box[0] + box[2]
    near = [p for p in chevrons(d, win)
            if -8 <= p[0] - right <= CHIP_TEXT_GAP]
    if not near:
        return {"model": "unknown", "score": round(bs, 3), "source": "template",
                "hint": "模型文字旁边没有它自己的 ⌄，控制身份不成立，按未确认处理"}
    return {"model": best, "score": round(bs, 3), "source": "template",
            "chip_x": right}


def _search_box(panel):
    """面板顶部搜索框的点击点。

    实测面板顶 y=354、搜索框 366..400，中心在 panel.y+30。
    写成 +46 会点到搜索框下边缘外面 —— 焦点留在页面上，
    后面的 Ctrl+A 就变成"全选整页文字"（实测把回答全选、还弹出 Quote 按钮）。
    """
    x, y, w, h = panel
    return (x + w // 2, y + 30)


def set_model(d, win, name):
    """切模型：开面板 -> 搜索框输入名字 -> Enter。

    接受 MODEL_LABELS 中的标识或完整显示名，选择后核对实际模型标识。
    """
    requested = name.strip().casefold()
    key = next((key for key, label in MODEL_LABELS.items()
                if requested in (key.casefold(), label.casefold())), None)
    if key is None:
        return {"ok": False, "stage": "unsupported_model", "want": name,
                "options": MODEL_LABELS}
    if not open_model_panel(d, win):
        return {"ok": False, "stage": "panel_not_open",
                "hint": "模型面板打不开：UIA 芯片按钮和模板 ⌄ 都没点到"}
    box = search_box(d, win)
    if box is None:
        close_panel(d, win)
        return {"ok": False, "stage": "panel_bbox_none",
                "hint": "面板开了但定位不到搜索框（面板几何/占位模板都没命中）"}
    d.click(*box)
    time.sleep(0.4)
    d.hotkey("ctrl", "a")
    time.sleep(0.1)
    d.press("delete")
    time.sleep(0.2)
    d.type_text(MODEL_LABELS[key])
    time.sleep(0.9)
    # 确认字真的进了搜索框：占位文字「🔍 搜索模型」应该消失。
    # 不确认的话，焦点一旦留在页面上，Ctrl+A 会全选整页、字打进页面（实测踩过）。
    if d.find(TPL_SEARCH, 0.85, _content(win)) is not None:
        dbg = os.path.join(DEBUG_DIR, "opencode_search_not_focused.png")
        cv2.imwrite(dbg, d.grab(win, fresh=True))
        close_panel(d, win)
        return {"ok": False, "stage": "search_not_focused",
                "hint": "字没进搜索框（占位文字还在），已放弃，没有回车",
                "debug_shot": dbg}
    d.press("enter")
    time.sleep(1.5)
    if panel_open(d, win):                   # 没关上 = 没选中
        dbg = os.path.join(DEBUG_DIR, "opencode_model_fail.png")
        cv2.imwrite(dbg, d.grab(win, fresh=True))
        close_panel(d, win)
        return {"ok": False, "stage": "not_selected", "want": name,
                "hint": "回车后面板没关，可能没匹配到；看 debug_shot",
                "debug_shot": dbg}
    got = read_model(d, win)
    ok = got["model"] == key
    return {"ok": ok, "verified": ok, "want": key, "after": got["model"],
            "score": got.get("score"), "read_source": got.get("source"),
            "text": got.get("text"), "stage": "done" if ok else "unverified"}


def set_effort(d, win, key):
    """调强度：点第 2 个 ⌄ -> 点对应那行 -> 复核芯片。"""
    key = EFFORT_ALIAS.get(key, key)
    if key not in EFFORTS:
        return {"ok": False, "stage": "bad_arg", "options": EFFORTS}
    before = read_effort(d, win)
    st = open_effort_menu(d, win)
    if st == "no_chip":
        return {"ok": False, "stage": "no_effort_chip",
                "hint": "这个模型的芯片行只有一个 ⌄（没有 variant），换模型再调"}
    if st != "open":
        return {"ok": False, "stage": "menu_not_open"}
    row = composer_row(d, win)
    band = row["band"] if row is not None else _band(win)
    m = d.find(TPL_E[key], 0.85, band)
    if m is None:
        close_panel(d, win)
        return {"ok": False, "stage": "item_not_found", "want": key}
    d.click(m[0] + m[2] // 2, m[1] + m[3] // 2)
    time.sleep(1.2)
    if effort_menu_open(d, win):
        d.press("esc")
        time.sleep(0.4)
    after = read_effort(d, win)
    ok = after.get("effort") == key
    return {"ok": ok, "before": before.get("effort"), "want": key,
            "after": after.get("effort"), "stage": "done" if ok else "unverified"}


def list_state(d, win):
    # 两边的分不能都叫 score：后写的会把先写的盖掉（dsh 踩过一次，
    # 结果看起来像"模型只匹到 0.566"）
    row = composer_row(d, win)
    model, effort = read_model(d, win), read_effort(d, win)
    out = {"model": model["model"], "model_score": model.get("score"),
           "model_source": model.get("source"),
           "effort": effort["effort"], "effort_score": effort.get("score"),
           "composer": None if row is None else row.get("source")}
    if model.get("text"):
        out["model_text"] = model["text"]
    for name, extra in (("label", effort.get("label")),
                        ("hint", model.get("hint") or effort.get("hint"))):
        if extra:
            out[name] = extra
    out["chevrons"] = len(chevrons(d, win))
    return out


# ============================================================ 提问

def send_state(d, win):
    """发送键状态：ready / disabled / missing。

    **不能只看模板分数**：禁用态（浅灰）和启用态（深色）是同一个形状，
    CCOEFF 对整体亮度平移不敏感，浅灰也能匹到 0.999。要看这块的平均亮度。
    """
    row = composer_row(d, win)
    if row is None:
        return "missing"
    band = row["band"]
    m = row.get("send") or d.find(TPL_SEND, 0.80, band)
    if m is None:
        return "missing"
    img = d.grab(band)
    if img.size == 0:
        return "missing"
    x0 = m[0] - band[0]
    y0 = m[1] - band[1]
    patch = img[max(0, y0):y0 + m[3], max(0, x0):x0 + m[2]]
    if patch.size == 0:
        return "missing"
    mean = float(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).mean())
    return "ready" if mean < 120 else "disabled"


def band_top(win):
    """旧版底部带的上沿。只为兼容保留；判定不再用它。"""
    return win[1] + max(0, win[3] - BAND_H)


def ask(d, win, prompt, timeout=180.0):
    from pathlib import Path
    source = str(Path(__file__).resolve().parents[1])
    if source not in sys.path:
        sys.path.insert(0, source)
    from qi.screen import Screen
    from qi.conversation import Conversation, ask as send_and_receive
    screen = Screen(verbose=False)
    screen.bind(d.hwnd)
    return send_and_receive(Conversation(screen, "opencode"), prompt, timeout)


# ============================================================ CLI

def probe(d, win):
    """只读探针：窗口、composer 行、UIA 芯片、模板分数。不点任何东西。"""
    row = composer_row(d, win)
    band = row["band"] if row is not None else _band(win)
    rows = []
    for name in (TPL_CHEV, TPL_SEND, TPL_PH, TPL_SEARCH) + tuple(TPL_E.values()):
        m = d.find(name, 0.30, band)
        rows.append({"name": name, "score": None if m is None else round(m[4], 4),
                     "at_window": None if m is None else (m[0] - win[0], m[1] - win[1])})
    models = []
    for key, tpl in MODELS.items():
        m = d.find(tpl, 0.30, band)
        models.append({"key": key, "template": tpl,
                       "score": None if m is None else round(m[4], 4),
                       "at_window": None if m is None else (m[0] - win[0], m[1] - win[1])})
    got = chip_node(d)
    pan = panel_bbox(d, win)
    return {"window": list(win),
            "composer": None if row is None else {"y": row["y"], "source": row["source"],
                                                  "band": list(band)},
            "uia_chip": None if got is None else got[1],
            "templates": rows,
            "models": models,
            "chevrons": [(x - win[0], y - win[1]) for x, y, _s in chevrons(d, win)],
            "panel": None if pan is None else
            (pan[0] - win[0], pan[1] - win[1], pan[2], pan[3])}


def _record_receipt(hwnd, dimension, target, readback, raw):
    """写档位回执（CARD-06a (a)）。**失败一律不影响选择结果。**

    本脚本刻意不 import 项目包（单文件契约），所以按需把 src 挂上 sys.path。
    读不回档位时 readback 传 None：use 会因此拒发——"点了但没读回来"
    本来就不能当确认过。
    """
    try:
        from pathlib import Path
        source = str(Path(__file__).resolve().parents[1])
        if source not in sys.path:
            sys.path.insert(0, source)
        from qi import tier_guard
        tier_guard.record_choice("opencode", target=target, readback=readback,
                                 dimension=dimension, hwnd=hwnd, raw=raw)
    except Exception:                                  # noqa: BLE001 - 回执不是主流程
        pass


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--list", "-l", action="store_true")
    ap.add_argument("--model", default=None)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--wait", type=float, default=120.0)
    ap.add_argument("--idle", type=float, default=2.0)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    if not (a.list or a.model or a.effort or a.probe):
        print(__doc__)
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
        print("启动 opencode …")
        subprocess.Popen(["explorer.exe", LAUNCH])
        end = time.time() + 40
        while time.time() < end and hwnd is None:
            time.sleep(1.0)
            hwnd = find_window()
    if hwnd is None:
        print(json.dumps({"ok": False, "stage": "no_window"}, ensure_ascii=False))
        return 1

    win = rect_of(hwnd)
    # 最小化且轮询 5s 也没恢复：rect_of 会把哨兵矩形(-32000,...)原样还回来。
    # 这时绝不能拿它算点击位置 —— 退出码 7（CARD-06a (c)）。
    if win and (win[0] <= -31999 or win[1] <= -31999):
        print(json.dumps({"ok": False, "stage": "window_minimized",
                          "hint": "窗口最小化且没能恢复，先手动还原窗口再跑"},
                         ensure_ascii=False))
        return 7

    d = Desk(hwnd)

    if a.probe:
        print(json.dumps(probe(d, win), ensure_ascii=False, indent=1))
        return 0

    if not activate(hwnd):
        print(json.dumps({"ok": False, "stage": "not_foreground",
                          "hint": "窗口提不到前台，已退出，不硬点"}, ensure_ascii=False))
        return 4
    time.sleep(0.7)
    d.grab(win, fresh=True)
    win = rect_of(hwnd)

    try:
        # 首页/会话列表没有 composer：**先拦下来**，不点、不打字。
        # （2026-11-07：以前会在 README 首页上找芯片，最后只报一个含糊的
        #  panel_not_open；现在给一个能照做的 stage。）
        sess = session_state(d, win)
        if sess["state"] == "home":
            out = {"ok": False, "stage": "session_not_open", "session": sess,
                   "hint": (sess.get("hint") or "") +
                           "；本脚本不会先打一句话去把 composer 顶出来"}
            print(json.dumps(out, ensure_ascii=False))
            return 1

        if a.list:
            out = list_state(d, win)
            print(json.dumps(out, ensure_ascii=False))
            if panel_open(d, win):
                close_panel(d, win)
            # 读不出来就当失败：验证器看的是退出码，返回 0 等于把"没读到"记成通过
            return 1 if out.get("model") == "unknown" else 0

        if a.model:
            r = set_model(d, win, a.model)
            print(json.dumps(r, ensure_ascii=False))
            if not r.get("ok"):
                return 1
            rec = globals().get("_record_receipt")
            if callable(rec):
                rec(hwnd, "model", r.get("want") or a.model,
                    None if r.get("after") in (None, "unknown") else r.get("after"), r)
        if a.effort:
            r = set_effort(d, win, a.effort)
            print(json.dumps(r, ensure_ascii=False))
            if not r.get("ok"):
                return 1
            rec = globals().get("_record_receipt")
            if callable(rec):
                rec(hwnd, "effort", r.get("want") or a.effort,
                    None if r.get("after") in (None, "unknown") else r.get("after"), r)
    except RuntimeError as e:
        print(json.dumps({"ok": False, "stage": "aborted", "hint": str(e)},
                         ensure_ascii=False))
        return 4
    return 0


if __name__ == "__main__":
    # 入口编码（CARD-08 第 1 条）：借 qi.console；装不上就自己 reconfigure，绝不挡住主流程
    _lock = None
    try:
        _source = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _source not in sys.path:
            sys.path.insert(0, _source)
        from qi.console import setup as _setup_console
        from qi import uilock as _uilock

        _setup_console()
        _lock = _uilock
    except Exception:                                  # noqa: BLE001
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:                          # noqa: BLE001
                pass
    # UI 前台锁（CARD-09f 第 1 条）：choose 也要和服务端/use 抢同一把 runs\ui.lock
    if _lock is None:
        print(json.dumps({"ok": False, "stage": "ui_busy", "reason": "lock_runtime_unavailable"}))
        raise SystemExit(8)
    raise SystemExit(_lock.entry("opencode_choose", main))
