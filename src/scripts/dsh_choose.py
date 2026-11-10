"""dsh（DeepSeek Harness 桌面端 / DeepSeek Harness.exe）：选模型 + 调强度。

**定位主体使用 cv2/numpy/ctypes；运行入口共用 qi 的锁和自身输入标记。**

用法：
    python dsh_choose.py --list                        读当前模型和强度
    python dsh_choose.py --model "GPT-6 Luna"         只切模型（名字给够精确）
    python dsh_choose.py --effort max                  只调强度
    python dsh_choose.py --model deepseek_v41_flash --effort low
    python dsh_choose.py --probe                       只报模板命中，不点击

## 界面结构（2026-10-29 实测，窗口 1380x1340）

输入框底部一行：

    ＋   工作区内修改 ⌄              DeepSeek-V4.1-Flash  1017  Max ⌄   (↑蓝圆)

点最右那个 ⌄ 弹出**子菜单**，贴在芯片上方：

    ┌──────────────────────────────┐
    │ 模型        DeepSeek-V4.1-Flash 1017  › │   ← 点它进模型面板
    │ 推理等级                          Max  › │   ← 点它进强度菜单
    └──────────────────────────────┘

**行数会变**：选了没有推理等级的模型（实测 GPT-6 Luna）时只有「模型」一行，
菜单整体贴着芯片往上排，行位置也跟着变。所以不能写死 y，要扫描菜单区域找行。

两个子面板：
    · 模型面板 248x358，顶部有搜索框，下面按「DeepSeek 账号 / KOW」分组
      -> 和 opencode 一样走搜索：输入名字 -> Enter 选第一项
    · 强度菜单 4 行：Off / Low / High / Max，当前项右侧打勾

## 定位方式

    dsh_chevron    芯片右端那个 ⌄（12x7）
    dsh_e_0..3     强度四档的词（Off/Low/High/Max），也用来读芯片上的档位
    dsh_send       蓝色圆形 ↑ 发送键（BGR≈[245,200,179]）
    dsh_placeholder 输入框占位文字，命中 = 输入框是空的
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

PROC = "DeepSeek Harness.exe"
LAUNCH = r"shell:AppsFolder\electron.app.DeepSeek Harness"

TPL_CHEV = "dsh_chevron"
TPL_SEND = "dsh_send"
TPL_PH = "dsh_placeholder"
TPL_E = {"off": "dsh_e_0", "low": "dsh_e_1", "high": "dsh_e_2", "max": "dsh_e_3"}

TPL_THR = 0.90
CHEV_THR = 0.85
BAND_H = 170

EFFORTS = ["off", "low", "high", "max"]
EFFORT_LABEL = {"off": "Off", "low": "Low", "high": "High", "max": "Max"}
EFFORT_ALIAS = {"default": "off", "none": "off", "medium": "high",
                "mid": "high", "ultra": "max", "xhigh": "max"}

# 模型选择只接受下列可读回验证的标识或完整显示名
MODELS = {
    "deepseek_v41_flash": "dsh_m_deepseek",
    "gpt6_luna": "dsh_m_luna",
}
MODEL_LABELS = {
    "deepseek_v41_flash": "DeepSeek-V4.1-Flash",
    "gpt6_luna": "GPT-6 Luna",
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
    # B16：choose 也统一走 desk.ensure_foreground（B15 只改了三个 *_use.py）。
    # 旧实现没有 SW_RESTORE：前台是桌面 WorkerW 时 SetForegroundWindow 一律失败，
    # 这正是 dsh choose 在 4 席并发下 `menu_not_open` 的高频原因。
    # 只改函数体：名字、签名、调用点都不动（测试按名字打桩）。
    from qi import desk as _desk
    return _desk.ensure_foreground(hwnd, tries=max(3, int(tries)))


class Desk:
    def _mark_input(self):
        from qi.guard import mark_synthetic
        mark_synthetic("desk")

    CLICK_MAX, CLICK_GAP = 16, 0.35

    def __init__(self, hwnd):
        self.hwnd = int(hwnd)
        self._cache = (0.0, None)
        self._pw_ok = True
        self._clicks = []

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
        # 64 位下不声明 argtypes 时，HDC 会被当 32 位 int 传，直接
        # `OverflowError: int too long to convert`（2026-11-08 dsh 真机）。
        user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        user32.PrintWindow.restype = wintypes.BOOL
        ok = user32.PrintWindow(self.hwnd, mdc, 2)
        img = self._dib(mdc, bmp, w, h) if ok else None
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

    def tpl(self, name):
        p = os.path.join(TEMPLATE_DIR, name + ".png")
        img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(f"缺模板 {p}")
        return img

    def find(self, name, threshold=TPL_THR, region=None):
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
            # 去重要用 or：同一行上左右两个 ⌄ 的 y 相同，用 and 会把第二个丢掉
            if all(abs(x - ox) > nms or abs(y - oy) > nms for _, ox, oy in out):
                out.append((sc, x, y))
        ox, oy = (region[0], region[1]) if region else (0, 0)
        return sorted(((ox + x + t.shape[1] // 2, oy + y + t.shape[0] // 2, sc)
                       for sc, x, y in out), key=lambda p: p[0])


# ============================================================ 几何校准
# B17：dsh 首页把输入框摆在窗口中部（「探索未至之境」下面），会话页才贴底。
# 旧 `_band()` 固定取窗口底部 170px，首页里连发送键都找不到：R4 实测
# `dsh_send` 全窗 1.000、band 内 0。发送键模板是可靠锚点（x 稳定、y 随布局
# 大幅变化），所以这一帧的全部 composer 相对量都从它推导。
_GEO = {}


def _geo(win):
    """取当前窗口的校准几何；没校准 / 窗口变了返回 None（严格回退）。"""
    cache = globals().get("_GEO")
    if not isinstance(cache, dict) or cache.get("win") != tuple(win):
        return None
    return cache


def calibrate(d, win):
    """整窗找蓝色发送键，算好这一帧的全部 composer 相对量。

    `Desk.find` 传 region 时返回屏幕绝对 5 元组 `(x,y,w,h,score)`。
    只认形状与分数，避免测试桩 / MagicMock 污染缓存导致整轮走偏。
    """
    global _GEO
    _GEO = {}
    try:
        m = d.find(globals().get("TPL_SEND", "dsh_send"), 0.80, win)
    except Exception:                                  # noqa: BLE001
        return False
    if not (isinstance(m, tuple) and len(m) == 5 and float(m[4]) >= 0.80):
        return False
    x, y, w, h = win
    scx = m[0] - x + m[2] // 2
    scy = m[1] - y + m[3] // 2
    if not (0 <= scx <= w and 0 <= scy <= h):
        return False
    band_h = globals().get("BAND_H", 170)
    top = max(0, min(scy - 130, h - band_h))
    menu_x0 = max(0, scx - 300)
    _GEO = {"win": tuple(win), "send": (scx, scy), "top": top,
            "menu_x0": menu_x0, "menu_x1": min(w, scx + 20),
            # B17 真机菜单态实测（evidence/batch17/dsh_menu_open_S1.png）：
            # 卡片紧贴芯片上方，窗口 y≈669..749（send 中心 768），两行文字
            # 在 688..701 / 720..735。旧的 scy-240..scy-84 完全错过它。
            "menu_y0": max(0, scy - 110), "menu_y1": max(0, scy - 20),
            "click_x": menu_x0 + 90,
            "effort_y0": max(0, min(scy - 260, h - 300))}
    return True


def _click_x(win):
    helper = globals().get("_geo")
    geo = helper(win) if callable(helper) else None
    return geo["click_x"] if geo else 950


def _effort_y0(win):
    helper = globals().get("_geo")
    geo = helper(win) if callable(helper) else None
    return geo["effort_y0"] if geo else max(0, win[3] - 300)


# ============================================================ 定位

def _band(win):
    x, y, w, h = win
    helper = globals().get("_geo")
    geo = helper(win) if callable(helper) else None
    band_h = globals().get("BAND_H", 170)
    if geo:
        return (x, y + geo["top"], w, min(band_h, h))
    return (x, y + max(0, h - band_h), w, min(band_h, h))


def _deselect_page(d, win):
    """折叠可能残留的「整页全选」，再让调用方去读芯片。

    B17 真机（job_R4h）：一旦 Ctrl+A 落到页面上而不是面板搜索框，整页文字变成
    **白字蓝底**，芯片也被一起染蓝，模板匹配必然崩 —— 实测 `dsh_m_deepseek`
    从 0.956 掉到 **0.415**、`dsh_e_*` 全在 0.5 上下，于是 `read_model` 报
    `model=unknown`、`set_model` 报 `unverified`；而截图里芯片明明写着
    `DeepSeek-V4.1-Flash Off`（evidence/batch17/dsh_chip_now_zoom.png）。
    表现像"选择失败"，其实是"读数被选中态骗了"。

    点输入框里的空白占位区即可折叠选中：不改任何状态、不覆盖草稿、不动滚动位置。
    """
    helper = globals().get("_band")
    try:
        band_top = helper(win)[1] - win[1] if callable(helper) else max(0, win[3] - 170)
    except Exception:                                  # noqa: BLE001 - 算不出就用兜底
        band_top = max(0, win[3] - 170)
    # 发送键上方约 60px 是占位文字那一行（实测 send 中心 y=1277 → 空白带 1215）
    d.click(win[0] + win[2] // 2, win[1] + band_top + 45)
    time.sleep(0.35)


def chip_chevron(d, win):
    """芯片右端那个 ⌄ 的中心（屏幕坐标），找不到返回 None。

    **不靠模板**：⌄ 的颜色随芯片文字变（模型名是深色时 ⌄ 深，浅色时更浅，
    实测 GPT-6 Luna 下就比 DeepSeek 那边淡一档），模板会匹不上。
    改用几何：以蓝色发送键为右锚点，在它左边 80px 条带里找**最靠右的一撮
    非白像素**——那就是 ⌄（它和左边的模型名之间隔着 10px 空白）。
    """
    band = _band(win)
    # B17：⌄ 模板在首页截图上有 0.966 的清晰命中；纯几何「最右非白列组」
    # 在居中的 composer 上会误挑到旁边的深色像素（实测返回 x=1111 而非 1069）。
    # 先用高阈值模板，没命中才退回原来的几何推断。
    try:
        tpl_hit = d.find(globals().get("TPL_CHEV", "dsh_chevron"), 0.90, band)
    except Exception:                                  # noqa: BLE001
        tpl_hit = None
    if tpl_hit is not None:
        return (tpl_hit[0] + tpl_hit[2] // 2, tpl_hit[1] + tpl_hit[3] // 2)
    m = d.find(TPL_SEND, 0.80, band)
    if m is None:
        return None
    img = d.grab(band)
    if img.size == 0:
        return None
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    top = band[1]
    sx = m[0] - win[0]
    sy = m[1] - top + m[3] // 2
    x0, x1 = max(0, sx - 80), max(0, sx - 4)
    y0, y1 = max(0, sy - 12), min(g.shape[0], sy + 12)
    if x1 - x0 < 20 or y1 - y0 < 8:
        return None
    strip = g[y0:y1, x0:x1]
    cols = (strip < 235).sum(axis=0)
    groups, st = [], None
    for i, c in enumerate(cols):
        if c > 0 and st is None:
            st = i
        elif c == 0 and st is not None:
            groups.append((x0 + st, x0 + i - 1))
            st = None
    if st is not None:
        groups.append((x0 + st, x0 + len(cols) - 1))
    if not groups:
        return None
    gl, gr = groups[-1]
    if not (3 <= gr - gl + 1 <= 16):
        return None
    ys, xs = np.nonzero(strip[:, gl - x0:gr - x0 + 1] < 235)
    if len(ys) == 0:
        return None
    return (win[0] + (gl + gr) // 2, top + y0 + int(round(ys.mean())))


def menu_rows(d, win):
    """子菜单里每个文字行的 (top, bottom)（窗口内 y），从上到下排序。

    菜单贴在芯片上方，**有几行不固定**（没有推理等级的模型只有「模型」一行），
    所以不能写死 y，就在芯片上方这段里扫"有深色文字的行"，按空白切组。
    """
    img = d.grab(win)
    if img.size == 0:
        return []
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h = g.shape[0]
    helper = globals().get("_geo")
    geo = helper(win) if callable(helper) else None
    if geo:
        x0, x1 = geo["menu_x0"], geo["menu_x1"]
        y0, y1 = geo["menu_y0"], geo["menu_y1"]
    else:
        x0, x1 = 860, min(g.shape[1], 1160)
        y0, y1 = max(0, h - 240), max(0, h - 84)
    if y1 - y0 < 60 or x1 - x0 < 100:
        return []
    counts = (g[y0:y1, x0:x1] < 200).sum(axis=1)
    groups, st = [], None
    for i, c in enumerate(counts):
        if c > 2 and st is None:
            st = i
        elif c <= 2 and st is not None:
            if i - st >= 4:
                groups.append((y0 + st, y0 + i - 1))
            st = None
    if st is not None and len(counts) - st >= 4:
        groups.append((y0 + st, y0 + len(counts) - 1))
    return groups


def menu_open(d, win):
    return len(menu_rows(d, win)) >= 1


def open_menu(d, win):
    """点芯片打开子菜单。返回 `(行列表, 原因)`；行空 = 没开。

    B16：以前只返空列表，上层只能吐一个没有 hint 的 `menu_not_open`，
    服务端从 stdout 末行 JSON 取不到 hint，最终只剩 `choose exit code 1`。
    现在把「本来就没关」「找不到 ⌄」「点了但一行都没扫到」分开报。
    """
    if menu_open(d, win):
        return menu_rows(d, win), "already_open"
    c = chip_chevron(d, win)
    if c is None:
        return [], "chip_not_found"
    d.click(c[0], c[1])
    time.sleep(1.3)
    rows = menu_rows(d, win)
    return rows, ("opened" if rows else "rows_empty")


def _menu_fail(d, win, why, tag):
    """菜单没打开时统一留证：hint + 调试图 + 可复核的 probe。

    原来的分支什么字段都没有，D4b 那轮 `exit_code=1 / stderr="" / 5.985s`
    只能靠排除法猜是这里。现在失败现场直接落盘，不需要再猜。
    """
    dbg = os.path.join(DEBUG_DIR, f"dsh_menu_not_open_{tag}.png")
    try:
        cv2.imwrite(dbg, d.grab(win, fresh=True))
    except Exception:                                  # noqa: BLE001 - 留证失败不掩盖主因
        dbg = None
    probe = {"why": why, "rows": 0, "win": list(win)}
    try:
        probe["send"] = d.find(TPL_SEND, 0.80, _band(win)) is not None
        probe["chevron"] = chip_chevron(d, win) is not None
    except Exception as exc:                           # noqa: BLE001
        probe["error"] = f"{type(exc).__name__}: {exc}"
    return {"ok": False, "stage": "menu_not_open", "why": why,
            "hint": f"子菜单没打开（{why}）：窗口是否在前台、发送键/⌄ 模板是否失配？看 debug_shot",
            "debug_shot": dbg, "probe": probe}


def panel_bbox(d, win):
    """模型面板的白色区域（屏幕坐标），没开返回 None。"""
    # 搜索区要够宽：面板贴在芯片上方、右边缘能顶到窗口右侧
    # （实测面板窗口内 x 820..1100，旧的 win[2]//2 只到 690，白块被切掉一半）
    x0 = win[0] + win[2] // 5
    y0 = win[1] + max(0, win[3] // 6)
    reg = (x0, y0, win[2] * 4 // 5, int(win[3] * 0.75))
    img = d.grab(reg)
    if img.size == 0:
        return None
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    white = (g > 250).astype(np.uint8)
    n, _lab, st, _c = cv2.connectedComponentsWithStats(white, 8)
    segs = []
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in st[i][:5])
        # 面板实测 248x358（只列 Recent 时），换成整组模型时更高更宽。
        # 上限要放到 450x700：卡太紧会把"分组多"的面板判成没开
        # （实测切 GPT-6 Luna 时面板高 500+，旧上限 520 刚好卡掉）。
        # 下限用来排掉页面全选时那种 1000x1200 的大白块。
        if 150 <= w <= 450 and 40 <= h <= 700 and a > 3000:
            segs.append([x, y, w, h, a])
    # B17（job_R4h）：面板里"当前选中"那一行是浅灰 #F4F4F5（244），过不了 >250 的
    # 白色阈值，于是**同一块面板被切成上下两段**：上段是搜索框（240x62）、下段是其余
    # 列表（240x218）。旧代码只取"面积最大的单块"，拿到的是下段 —— 于是
    # `set_model` 点 `panel[1]+16` 落在模型行上而不是搜索框，接着 Ctrl+A 又选中整页，
    # 芯片变白字蓝底、模板全崩（0.956 → 0.348），外面看到的是 `unverified`。
    # 现在把同 x、同宽、竖直相邻（间隙 <= 一整行）的碎块重新合成一整块再挑。
    segs.sort(key=lambda s: (s[0], s[1]))
    merged = []
    for seg in segs:
        for m in merged:
            if (abs(seg[0] - m[0]) <= 6 and abs(seg[2] - m[2]) <= 12
                    and seg[1] - (m[1] + m[3]) <= 130):
                m[3] = max(m[1] + m[3], seg[1] + seg[3]) - m[1]
                m[4] += seg[4]
                break
        else:
            merged.append(list(seg))
    band_top = _band(win)[1] - y0          # 芯片带在 reg 里的相对 y
    best = None
    for x, y, w, h, a in merged:
        if not (150 <= w <= 450 and 120 <= h <= 700 and a > 40000):
            continue
        # 面板一定贴在芯片上方（允许压到芯片带一点点），否则就是别处的白块
        if y + h > band_top + 120:
            continue
        if best is None or a > best[4]:
            best = (x, y, w, h, a)
    if best is None:
        return None
    return (reg[0] + best[0], reg[1] + best[1], best[2], best[3])


def close_all(d, win):
    d.press("esc")
    time.sleep(0.5)
    if panel_bbox(d, win) is not None:
        d.press("esc")
        time.sleep(0.4)


# ============================================================ 读 / 选

def read_effort(d, win):
    """读档位：芯片行里 Off / Low / High / Max 哪个能匹上。"""
    row = _band(win)
    best, bs, second = None, 0.0, 0.0
    for k, tpl in TPL_E.items():
        m = d.find(tpl, 0.0, row)
        s = m[4] if m else 0.0
        if s > bs:
            second, bs, best = bs, s, k
        elif s > second:
            second = s
    if best is None or bs < 0.85 or bs - second < 0.03:
        return {"effort": "unknown", "effort_score": round(bs, 3)}
    return {"effort": best, "label": EFFORT_LABEL[best],
            "effort_score": round(bs, 3)}


def read_model(d, win):
    row = _band(win)
    best, bs = None, 0.0
    for k, tpl in MODELS.items():
        m = d.find(tpl, 0.0, row)
        s = m[4] if m else 0.0
        if s > bs:
            bs, best = s, k
    if best is None or bs < MODEL_THR:
        return {"model": "unknown", "score": round(bs, 3),
                "hint": "当前模型无法由模板库识别，不能确认模型选择结果"}
    return {"model": best, "score": round(bs, 3)}


def set_model(d, win, name):
    """切模型：点芯片 -> 点「模型」行 -> 面板搜索框输入 -> Enter。"""
    requested = name.strip().casefold()
    key = next((key for key, label in MODEL_LABELS.items()
                if requested in (key.casefold(), label.casefold())), None)
    if key is None:
        return {"ok": False, "stage": "unsupported_model", "want": name,
                "options": MODEL_LABELS}
    # B17：先把残留的整页选中折掉，否则 `panel_bbox` 的"白块"统计也会被蓝底干扰。
    # 单测按函数名抠单个 FunctionDef 再 exec，桩命名空间里没有模块级 helper ——
    # 用 globals().get 软引用，取不到就跳过（本仓库既有的 `_geo` / `_click_x` 同款写法）。
    def _clean():
        helper = globals().get("_deselect_page")
        if callable(helper):
            helper(d, win)

    _clean()
    # B16：`open_menu` 现在返 `(rows, why)`；仍兼容只返 rows 的旧调用/测试桩。
    opened = open_menu(d, win)
    rows, why = opened if isinstance(opened, tuple) else (opened, "unknown")
    if not rows:
        return _menu_fail(d, win, why, "model")
    top, bot = rows[0]                      # 第一行永远是「模型」
    helper = globals().get("_click_x")
    cx = helper(win) if callable(helper) else 950
    d.click(win[0] + cx, win[1] + (top + bot) // 2)
    time.sleep(1.7)
    panel = panel_bbox(d, win)
    if panel is None:
        # B17：等完 UI 锁、窗口刚回到前台时，这一下点击会落在动画里（R4e 真机：
        # 菜单明明开了，点「模型」行却没反应）。再点一次、多等一拍，仍不行才判死。
        d.click(win[0] + cx, win[1] + (top + bot) // 2)
        time.sleep(2.5)
        panel = panel_bbox(d, win)
    if panel is None:
        dbg = os.path.join(DEBUG_DIR, "dsh_model_panel_fail.png")
        cv2.imwrite(dbg, d.grab(win, fresh=True))
        close_all(d, win)
        return {"ok": False, "stage": "panel_not_open", "debug_shot": dbg}
    d.click(panel[0] + panel[2] // 2, panel[1] + 16)
    time.sleep(0.4)
    # B17：面板刚打开时搜索框本来就是空的，只有**上一次尝试的残留**才需要清空。
    # 无条件按 Ctrl+A 有真实代价 —— 焦点若没落在搜索框（点在面板边缘/动画里），
    # Ctrl+A 会选中整页，芯片变成白字蓝底，后续模板全崩（job_R4h 就是这么挂的：
    # 0.956 → 0.348，read_model 报 unknown、set_model 报 unverified）。
    # 占位文字还在 = 框是空的，直接用，一个 Ctrl+A 都不发。
    if d.find("dsh_search_ph", 0.85, panel) is None:
        d.hotkey("ctrl", "a")
        time.sleep(0.1)
        d.press("delete")
        time.sleep(0.2)
    d.type_text(MODEL_LABELS[key])
    time.sleep(1.1)
    # 确认字真的进了搜索框：占位文字「搜索模型…」应该消失。
    # 不确认的话，焦点一旦留在页面上，Ctrl+A 会全选整页、字打进页面，
    # 回车只是把面板关掉 —— 面板关了看起来像"选中了"，其实什么都没换。
    if d.find("dsh_search_ph", 0.85, panel) is not None:
        dbg = os.path.join(DEBUG_DIR, "dsh_search_not_focused.png")
        cv2.imwrite(dbg, d.grab(win, fresh=True))
        close_all(d, win)
        return {"ok": False, "stage": "search_not_focused",
                "hint": "字没进搜索框（占位文字还在），已放弃，没有回车",
                "debug_shot": dbg}
    d.press("enter")
    time.sleep(1.6)
    if panel_bbox(d, win) is not None:
        dbg = os.path.join(DEBUG_DIR, "dsh_model_not_selected.png")
        cv2.imwrite(dbg, d.grab(win, fresh=True))
        close_all(d, win)
        return {"ok": False, "stage": "not_selected", "want": name,
                "hint": "回车后面板没关，可能没匹配到；看 debug_shot",
                "debug_shot": dbg}
    got = read_model(d, win)
    if got.get("model") == "unknown":
        # 读成 unknown 多半是被"白字蓝底"骗了：清一次选中再读，别把读不到当没设上
        _clean()
        got = read_model(d, win)
    ok = got["model"] == key
    return {"ok": ok, "verified": ok, "want": key, "after": got["model"],
            "score": got.get("score"), "stage": "done" if ok else "unverified"}


def set_effort(d, win, key):
    """调强度：点芯片 -> 点「推理等级」行 -> 点档位词 -> 复核芯片。"""
    key = EFFORT_ALIAS.get(key, key)
    if key not in EFFORTS:
        return {"ok": False, "stage": "bad_arg", "options": EFFORTS}
    # B17：同 set_model —— 先折掉整页选中，再读 before / 找面板
    def _clean():
        helper = globals().get("_deselect_page")
        if callable(helper):
            helper(d, win)

    _clean()
    before = read_effort(d, win)
    opened = open_menu(d, win)
    rows, why = opened if isinstance(opened, tuple) else (opened, "unknown")
    if not rows:
        return _menu_fail(d, win, why, "effort")
    if len(rows) < 2:
        close_all(d, win)
        return {"ok": False, "stage": "no_effort_row",
                "hint": "这个模型的菜单只有「模型」一行（没有推理等级），换模型再调"}
    top, bot = rows[1]                      # 第二行 =「推理等级」
    helper = globals().get("_click_x")
    cx = helper(win) if callable(helper) else 950
    d.click(win[0] + cx, win[1] + (top + bot) // 2)
    time.sleep(1.5)
    helper = globals().get("_effort_y0")
    ey = helper(win) if callable(helper) else max(0, win[3] - 300)
    m = d.find(TPL_E[key], 0.85, (win[0], win[1] + ey, win[2], 300))
    if m is None:
        dbg = os.path.join(DEBUG_DIR, "dsh_effort_item_fail.png")
        cv2.imwrite(dbg, d.grab(win, fresh=True))
        close_all(d, win)
        return {"ok": False, "stage": "item_not_found", "want": key,
                "debug_shot": dbg}
    d.click(m[0] + m[2] // 2, m[1] + m[3] // 2)
    time.sleep(1.3)
    if menu_open(d, win):
        d.press("esc")
        time.sleep(0.4)
    after = read_effort(d, win)
    if after.get("effort") != key:
        # B17：读成 unknown 多半是选中态/动画，清一次再读，别把"读不到"当"没设上"
        _clean()
        after = read_effort(d, win)
    ok = after.get("effort") == key
    return {"ok": ok, "before": before.get("effort"), "want": key,
            "after": after.get("effort"), "stage": "done" if ok else "unverified"}


def list_state(d, win):
    out = {}
    out.update(read_model(d, win))
    out.update(read_effort(d, win))
    return out


# ============================================================ 提问

def band_top(win):
    return _band(win)[1]


def send_state(d, win):
    """发送键状态：ready / disabled / missing（看蓝圆的饱和度）。"""
    m = d.find(TPL_SEND, 0.80, _band(win))
    if m is None:
        return "missing"
    img = d.grab(_band(win))
    if img.size == 0:
        return "missing"
    x0 = m[0] - win[0]
    y0 = m[1] - band_top(win)
    patch = img[max(0, y0):y0 + m[3], max(0, x0):x0 + m[2]]
    if patch.size == 0:
        return "missing"
    b, g, r = [patch[:, :, i].astype(int) for i in range(3)]
    strong = int(((b > 200) & (b - r > 30)).sum())
    return "ready" if strong > 150 else "disabled"


def ask(d, win, prompt, timeout=180.0):
    from pathlib import Path
    source = str(Path(__file__).resolve().parents[1])
    if source not in sys.path:
        sys.path.insert(0, source)
    from qi.screen import Screen
    from qi.conversation import Conversation, ask as send_and_receive
    screen = Screen(verbose=False)
    screen.bind(d.hwnd)
    return send_and_receive(Conversation(screen, "dsh"), prompt, timeout)


# ============================================================ CLI

def probe(d, win):
    band = _band(win)
    rows = []
    for name in (TPL_CHEV, TPL_SEND, TPL_PH) + tuple(TPL_E.values()):
        m = d.find(name, 0.30, band)
        rows.append({"name": name, "score": None if m is None else round(m[4], 4),
                     "at_window": None if m is None else (m[0] - win[0], m[1] - win[1])})
    pan = panel_bbox(d, win)
    helper = globals().get("_geo")
    geo = helper(win) if callable(helper) else None
    keys = ("send", "top", "menu_x0", "menu_x1", "menu_y0", "menu_y1",
            "click_x", "effort_y0")
    return {"window": list(win), "templates": rows,
            "menu_rows": menu_rows(d, win),
            "panel": None if pan is None else
            (pan[0] - win[0], pan[1] - win[1], pan[2], pan[3]),
            "calibrated": bool(geo),
            "geo": None if geo is None else {k: geo[k] for k in keys}}


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
        tier_guard.record_choice("dsh", target=target, readback=readback,
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
        print("启动 dsh …")
        subprocess.Popen(["explorer.exe", LAUNCH])
        end = time.time() + 40
        while time.time() < end and hwnd is None:
            time.sleep(1.0)
            hwnd = find_window()
    if hwnd is None:
        print(json.dumps({"ok": False, "stage": "no_window",
                          "hint": "dsh 窗口没找到：40s 内没拉起来，先手动打开应用再跑"},
                         ensure_ascii=False))
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
    # B17：整窗找发送键、校准 composer 几何。probe 不要求前台，所以放在激活之前；
    # 用 globals().get 是为了不打破「按函数名抠出来 exec」的测试桩命名空间。
    _cal = globals().get("calibrate")
    if callable(_cal):
        try:
            _cal(d, win)
        except Exception:                             # noqa: BLE001
            pass

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
    if callable(_cal):
        try:
            _cal(d, win)
        except Exception:                             # noqa: BLE001
            pass

    try:
        if a.list:
            out = list_state(d, win)
            print(json.dumps(out, ensure_ascii=False))
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
    # B16：兜底放 __main__、不包 main（测试按函数名 exec main，包一层会缺名字）。
    try:
        code = _lock.entry("dsh_choose", main)
    except Exception as exc:                          # noqa: BLE001
        print(json.dumps({"ok": False, "stage": "internal_error",
                          "error_type": type(exc).__name__,
                          "hint": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        code = 1
    raise SystemExit(code)
