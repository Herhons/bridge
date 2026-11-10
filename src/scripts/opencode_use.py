"""opencode（OpenCode 桌面端 / OpenCode.exe）：选模型 + 调强度 + 发消息。

**单文件，只依赖 cv2 / numpy / ctypes，不 import 本项目的任何模块。**
（定位代码和 opencode_choose.py 同一套，这个脚本多一个发消息入口。）

用法：
    python opencode_use.py "只回复两个字：收到"
    python opencode_use.py --list                       读当前模型和强度
    python opencode_use.py --model "Claude Opus 5.5"    只切模型
    python opencode_use.py --effort max                 只调强度
    python opencode_use.py --model deepseek_v41_flash_free --effort low "问题"
    python opencode_use.py --probe                      只报模板命中，不点击

**发消息只到"确认发出去"为止**：取回答还缺复制按钮模板（要 hover 才出现），
所以带文案调用会返回 stage=no_copy_template / sent=true，不会假装读到了回答。
发送前会校验发送键是不是真的变成深色启用态（输入框空时它是浅灰禁用态）。

界面结构、定位方式、已知限制都写在 opencode_choose.py 文件头里。
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
BAND_H = 170            # 底部工具条带：距底 170px
CHEV_THR = 0.85

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

# 用途 -> 验收结论上的写法（CARD-06c 第 6 条）。
# 本脚本不 import 项目包，所以这份表在 tier_guard.PURPOSE_LABELS 里还有一份；
# 测试会核对两处一致。accept_lite 只给 GLM-5.3-Flash-Free 验链路用。
PURPOSES = ("test", "review", "accept", "accept_lite")
PURPOSE_LABELS = {"test": "TEST", "review": "REVIEW",
                  "accept": "PASS", "accept_lite": "PASS(GLM-lite)"}

TEMPLATE_DIR = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "assets", "library"))
DEBUG_DIR = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "evidence"))

# ============================================================ Win32

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32


def _declare_win32():
    """声明句柄参数的类型。

    2026-11-01 CARD-WEB：64 位下窗口句柄可以大于 2^31，ctypes 不声明就按
    32 位 int 传，直接抛 `OverflowError: int too long to convert`
    （实测 opencode_use --list 崩在 IsWindowVisible 上，一条命令都跑不完）。
    只声明句柄相关的这一组，别的不动。
    """
    from ctypes import POINTER, c_int, c_uint, c_void_p

    for fn in (user32.IsWindowVisible, user32.IsWindow):
        fn.argtypes, fn.restype = [wintypes.HWND], wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, c_int, c_int,
                                    c_int, c_int, c_uint]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = c_int
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, c_uint]
    user32.PrintWindow.restype = wintypes.BOOL

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                    wintypes.LPWSTR,
                                                    POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

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
    # GetDIBits 的第二个参数是位图句柄，同样会超过 2^31（实测截图就在这句崩的）
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                wintypes.UINT, c_void_p, c_void_p, wintypes.UINT]
    gdi32.GetDIBits.restype = c_int


_declare_win32()


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


_CURRENT_HWND = {"v": None}   # 失败留证用：当前操作的窗口
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

    **统一走 `qi.window.restore`**：网页端和四个桌面端用的是同一份恢复实现，
    判据只有一条——IsIconic 变假、矩形脱离 -32000 哨兵、宽高过阈值。
    恢复不了就把原矩形（可能是哨兵）还回去，由调用方按退出码 7 处理，
    不在"取矩形"这一步抛异常。
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
    """把窗口提到前台。前台锁会让 SetForegroundWindow 静默失败，所以附加到前台线程。"""
    if user32.GetForegroundWindow() == int(hwnd):
        return True
    for _ in range(tries):
        user32.SetForegroundWindow(int(hwnd))
        if user32.GetForegroundWindow() == int(hwnd):
            return True
        fg = user32.GetForegroundWindow()
        t1 = user32.GetWindowThreadProcessId(fg, None)
        t2 = kernel32.GetCurrentThreadId()
        if t1 and t1 != t2:
            user32.AttachThreadInput(t2, t1, True)
            user32.SetForegroundWindow(int(hwnd))
            user32.SetFocus(int(hwnd))
            user32.AttachThreadInput(t2, t1, False)
        if user32.GetForegroundWindow() == int(hwnd):
            return True
        time.sleep(0.3)
    return user32.GetForegroundWindow() == int(hwnd)


class Desk:
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
        self._invalidate()

    def press(self, key, times=1):
        self._assert_fg()
        vk = VK[key.lower()]
        for _ in range(times):
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(vk, 0, 2, 0)
            time.sleep(0.05)
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
    x, y, w, h = win
    top = y + max(0, h - BAND_H)
    return (x, top, w, min(BAND_H, h))


def chevrons(d, win):
    """底部行里所有 ⌄ 的中心（屏幕坐标），按 x 排序。

    第 1 个 = 模型芯片，第 2 个 = 强度芯片（没有 variant 的模型只有一个）。
    """
    return d.find_all(TPL_CHEV, CHEV_THR, _band(win))


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


def open_model_panel(d, win):
    """打开模型面板，已开则直接返回 True。"""
    if panel_bbox(d, win) is not None:
        return True
    ch = chevrons(d, win)
    if not ch:
        return False
    d.click(ch[0][0], ch[0][1])
    time.sleep(1.4)
    return panel_bbox(d, win) is not None


def close_panel(d, win):
    d.press("esc")
    time.sleep(0.5)
    return panel_bbox(d, win) is None


def effort_menu_open(d, win):
    """强度小菜单开着吗 = 四档文字里有 ≥2 个能同时匹到。"""
    row = _band(win)
    hit = 0
    for k in EFFORTS:
        if d.find(TPL_E[k], 0.85, row) is not None:
            hit += 1
            if hit >= 2:
                return True
    return False


def open_effort_menu(d, win):
    """点第 2 个 ⌄ 打开强度菜单。没有第 2 个 ⌄ = 这个模型没有强度可选。"""
    if effort_menu_open(d, win):
        return "open"
    if panel_bbox(d, win) is not None:
        close_panel(d, win)
    ch = chevrons(d, win)
    if len(ch) < 2:
        return "no_chip"
    d.click(ch[1][0], ch[1][1])
    time.sleep(1.3)
    return "open" if effort_menu_open(d, win) else "fail"


# ============================================================ 读 / 选

def read_effort(d, win):
    """读强度：芯片上那个词（Default/Low/High/Max）。"""
    if len(chevrons(d, win)) < 2:
        return {"effort": "none", "hint": "这个模型没有强度芯片"}
    row = _band(win)
    best, bs = None, 0.0
    for k in EFFORTS:
        m = d.find(TPL_E[k], 0.0, row)
        s = m[4] if m else 0.0
        if s > bs:
            bs, best = s, k
    if best is None or bs < 0.85:
        return {"effort": "unknown", "score": round(bs, 3)}
    return {"effort": best, "label": EFFORT_LABEL[best], "score": round(bs, 3)}


def read_model(d, win):
    """读模型名。只在模板库覆盖到的模型上可靠（未覆盖返回 unknown）。"""
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
        return {"ok": False, "stage": "panel_not_open"}
    panel = panel_bbox(d, win)
    if panel is None:
        return {"ok": False, "stage": "panel_bbox_none"}
    d.click(*_search_box(panel))
    time.sleep(0.4)
    d.hotkey("ctrl", "a")
    time.sleep(0.1)
    d.press("delete")
    time.sleep(0.2)
    d.type_text(MODEL_LABELS[key])
    time.sleep(0.9)
    # 确认字真的进了搜索框：占位文字「🔍 搜索模型」应该消失。
    # 不确认的话，焦点一旦留在页面上，Ctrl+A 会全选整页、字打进页面（实测踩过）。
    if d.find("oc_search_ph", 0.85, panel) is not None:
        dbg = os.path.join(DEBUG_DIR, "opencode_search_not_focused.png")
        cv2.imwrite(dbg, d.grab(win, fresh=True))
        close_panel(d, win)
        return {"ok": False, "stage": "search_not_focused",
                "hint": "字没进搜索框（占位文字还在），已放弃，没有回车",
                "debug_shot": dbg}
    d.press("enter")
    time.sleep(1.5)
    if panel_bbox(d, win) is not None:       # 没关上 = 没选中
        dbg = os.path.join(DEBUG_DIR, "opencode_model_fail.png")
        cv2.imwrite(dbg, d.grab(win, fresh=True))
        close_panel(d, win)
        return {"ok": False, "stage": "not_selected", "want": name,
                "hint": "回车后面板没关，可能没匹配到；看 debug_shot",
                "debug_shot": dbg}
    got = read_model(d, win)
    ok = got["model"] == key
    return {"ok": ok, "verified": ok, "want": key, "after": got["model"],
            "score": got.get("score"), "stage": "done" if ok else "unverified"}


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
    m = d.find(TPL_E[key], 0.85, _band(win))
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
    out = {}
    out.update(read_model(d, win))
    out.update(read_effort(d, win))
    out["chevrons"] = len(chevrons(d, win))
    return out


# ============================================================ 提问

def send_state(d, win):
    """发送键状态：ready / disabled / missing。

    **不能只看模板分数**：禁用态（浅灰）和启用态（深色）是同一个形状，
    CCOEFF 对整体亮度平移不敏感，浅灰也能匹到 0.999。要看这块的平均亮度。
    """
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
    mean = float(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).mean())
    return "ready" if mean < 120 else "disabled"


def band_top(win):
    return win[1] + max(0, win[3] - BAND_H)


def _reread_tier(d, win, dimension):
    """use 发送前的**只读**复核：只读芯片，一个地方都不点。

    读不出来就返回 None（"这个端现在读不到"），gate 会记 drift=unchecked 不拦。
    读到且与回执不符 → gate 按退出码 5 拒发（use 绝不自己切档）。
    """
    try:
        if dimension == "model":
            got = read_model(d, win).get("model")
        elif dimension == "effort":
            got = read_effort(d, win).get("effort")
        else:
            return None
    except Exception:                                  # noqa: BLE001 - 读失败不等于档位变了
        return None
    return None if got in (None, "unknown", "none") else got


def ask(d, win, prompt, timeout=180.0, purpose=None):
    from pathlib import Path
    source = str(Path(__file__).resolve().parents[1])
    if source not in sys.path:
        sys.path.insert(0, source)
    from qi import runlog, tier_guard
    from qi.screen import Screen
    from qi.conversation import Conversation, ask as send_and_receive
    started = time.monotonic()
    # 第 6 条：高档（review/accept）等待上限下限 900s，不得 300s 就判超时
    timeout = tier_guard.floor_timeout(purpose, timeout)

    def door():
        """CARD-06b 第 3 条：门放在**写入文本之后、点发送之前**。

        只读复核仍然只读芯片，但**读不到不再放行**（第 1 条）：`_reread_tier`
        返回 None 时 gate 会以 `reason=tier_unreadable` 拒发。
        """
        tier_guard.gate("opencode", purpose,
                        reread=lambda dimension: _reread_tier(d, win, dimension),
                        hwnd=d.hwnd)
        return {}

    screen = Screen(verbose=False)
    screen.bind(d.hwnd)
    result = send_and_receive(Conversation(screen, "opencode"), prompt, timeout,
                              pre_send=door if purpose is not None else None)
    result.setdefault("elapsed", round(time.monotonic() - started, 1))
    if purpose is not None:
        # CARD-06c 第 6 条：验收结论要能分开写。accept -> PASS；
        # accept_lite -> PASS(GLM-lite)，免得 GLM 低档跑通了被当成旗舰档验收过。
        result["purpose"] = purpose
        result["purpose_label"] = PURPOSE_LABELS.get(purpose, purpose)
    # 超时分型由 conversation.ask 算，这里只保证字段一定在（见 runlog.with_timeout_kind）
    return runlog.with_timeout_kind(result)


# ============================================================ CLI

def probe(d, win):
    band = _band(win)
    rows = []
    for name in (TPL_CHEV, TPL_SEND, TPL_PH) + tuple(TPL_E.values()):
        m = d.find(name, 0.30, band)
        rows.append({"name": name, "score": None if m is None else round(m[4], 4),
                     "at_window": None if m is None else (m[0] - win[0], m[1] - win[1])})
    pan = panel_bbox(d, win)
    return {"window": list(win), "templates": rows,
            "chevrons": [(x - win[0], y - win[1]) for x, y, _s in chevrons(d, win)],
            "panel": None if pan is None else
            (pan[0] - win[0], pan[1] - win[1], pan[2], pan[3])}


# 运行日志（2026-10-31 任务卡04 / 04c）记在 main 里的 done()：
#   * 正常结束都从 `return done(...)` 出去，漏不掉；
#   * 异常分三段兜底——入口段（解析参数、等你空闲）、初始化段（04b）、动作段，
#     三段合起来盖住从入口到出口的每一行；`logged` 是闸门，一次运行只落一行；
#   * done() 自己吞掉所有异常，写日志失败不影响打印和退出码。
def main(argv=None):
    """入口：跑一次，结束时**正好**记一行运行日志，返回它的退出码。

    2026-10-31 任务卡04：每次运行结束（成功 / 超时 / 报错 / 参数错后的正常退出）
    都走 `done()` 记一行；`--help` 由 argparse 直接 SystemExit(0)，不记。
    任务卡04c：`idle_seconds()` 和等待循环原来在保护区外，一炸就一行日志都没有。
    现在入口段也包进 try：任何一步抛异常都先记一行 stage=exception，再照原样抛出
    （异常实例、类型、堆栈都不动）。
    """
    started = time.time()
    logged = []

    def done(result, code):
        """记一行运行日志，再把退出码原样返回——写在 return 里就不会漏。

        失败时把 hint / error 一起交出去（`runlog.failure_note` 兜底），
        **不写提问和回复正文**。`logged` 是闸门：内层 except 已经记过的，出口
        兜底不会再记第二行。写日志失败一律吞掉：连 import 失败都不能影响原来的
        输出和退出码。
        """
        if logged:
            return code
        logged.append(True)
        try:
            import sys
            from pathlib import Path
            source = str(Path(__file__).resolve().parents[1])
            if source not in sys.path:
                sys.path.insert(0, source)
            from qi import failure, runlog
            result = dict(result)                      # 不就地改调用方的 dict
            result.setdefault("elapsed", round(time.time() - started, 1))
            # 失败留证（CARD-06b 第 9 条）：tier_*/zoom_*/human_check/input_mismatch
            failure.record("opencode", result, hwnd=_CURRENT_HWND.get("v"))
            runlog.record("opencode", runlog.with_timeout_kind(result))
        except Exception:                              # noqa: BLE001
            pass
        return code

    # 2026-10-31 任务卡04c：从解析参数到「等你空闲」这一整段原来都在异常保护区外，
    # idle_seconds() 一抛异常就一行日志都不留。现在整段包进 try——异常先记一行，
    # 再照原样抛出去；等待循环里 `return done(busy, 4)` 那条正常出口不受影响。
    try:
        ap = argparse.ArgumentParser(add_help=True)
        ap.add_argument("prompt", nargs="?", default=None)
        ap.add_argument("--new", action="store_true", help="先打开一个新对话")
        ap.add_argument("--list", "-l", action="store_true")
        ap.add_argument("--model", default=None)
        ap.add_argument("--effort", default=None)
        ap.add_argument("--probe", action="store_true")
        ap.add_argument("--timeout", type=float, default=180.0)
        # 这里用字面量而不是 PURPOSES：本脚本的 main 会被测试单独抠出来 exec
        # （没有模块级常量），写成常量会 NameError。与模块级 PURPOSES 一致，测试会核对。
        ap.add_argument("--purpose", choices=["test", "review", "accept", "accept_lite"],
                        default=None,
                        help="test=最低档，review/accept=最高档，accept_lite=GLM-lite 验链路（发送必填，无默认）")
        ap.add_argument("--wait", type=float, default=120.0)
        ap.add_argument("--idle", type=float, default=2.0)
        ap.add_argument("--force", action="store_true")
        a = ap.parse_args(argv)                # SystemExit 交给入口段的 except SystemExit

        if not (a.list or a.model or a.effort or a.probe or a.prompt):
            print(__doc__)
            print("可选强度：" + ", ".join(EFFORTS))
            return done({"ok": False, "stage": "bad_args", "hint": "没有给任何动作"}, 2)

        # 「等你空闲」也在保护区里（04c）：idle_seconds 一炸就先记一行再抛出。
        if not a.force:
            end = time.time() + a.wait
            while time.time() < end:
                if idle_seconds() >= a.idle:
                    break
                time.sleep(0.2)
            else:
                busy = {"ok": False, "stage": "user_busy",
                        "hint": f"{a.wait}s 内你一直在用电脑"}
                print(json.dumps(busy, ensure_ascii=False))
                return done(busy, 4)

    except SystemExit as exc:                          # argparse：--help / 参数错
        if not exc.code:
            raise                                      # --help：不记
        done({"ok": False, "stage": "bad_args", "hint": "参数解析失败"}, exc.code)
        raise
    except Exception as e:                             # noqa: BLE001 - 入口异常也要留痕
        done({"ok": False, "stage": "exception",
              "error": f"{type(e).__name__}: {e}"}, 0)   # 返回值用不上，记完原样抛
        raise

    # 初始化（找/开窗口、建会话、抓首帧、置前）原来在异常保护区外面：这一段抛异常时
    # 一行日志都不会留（CARD-04b 打回的第 2 条）。现在整段包进 try，异常照原样往上
    # 抛——退出方式和堆栈都不变，只是先记一次，带异常类型。
    try:
        hwnd = find_window()
        if hwnd is None:
            print("启动 opencode …")
            subprocess.Popen(["explorer.exe", LAUNCH])
            end = time.time() + 40
            while time.time() < end and hwnd is None:
                time.sleep(1.0)
                hwnd = find_window()
        _CURRENT_HWND["v"] = hwnd            # 失败留证（第 9 条）要截这个窗口
        if hwnd is None:
            gone = {"ok": False, "stage": "no_window"}
            print(json.dumps(gone, ensure_ascii=False))
            return done(gone, 1)

        win = rect_of(hwnd)
        # 最小化且轮询 5s 也没恢复：rect_of 会把哨兵矩形(-32000,...)原样还回来。
        # 这时绝不能拿它算点击位置 —— 退出码 7（CARD-06a (c)）。
        if win and (win[0] <= -31999 or win[1] <= -31999):
            mini = {"ok": False, "stage": "window_minimized",
                    "hint": "窗口最小化且没能恢复，先手动还原窗口再跑"}
            print(json.dumps(mini, ensure_ascii=False))
            return done(mini, 7)
        d = Desk(hwnd)

        if a.probe:
            print(json.dumps(probe(d, win), ensure_ascii=False, indent=1))
            return done({"ok": True, "stage": "probe"}, 0)

        if not activate(hwnd):
            cold = {"ok": False, "stage": "not_foreground",
                    "hint": "窗口提不到前台，已退出，不硬点"}
            print(json.dumps(cold, ensure_ascii=False))
            return done(cold, 4)
        time.sleep(0.7)
        d.grab(win, fresh=True)
        win = rect_of(hwnd)
    except Exception as e:                             # noqa: BLE001 - 初始化异常也要留痕
        done({"ok": False, "stage": "exception",
              "error": f"{type(e).__name__}: {e}"}, 0)   # 返回值用不上，记完原样抛
        raise

    r = {"ok": True, "stage": "noop"}
    try:
        if a.new:
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from qi.screen import Screen
            from qi.workbuddy import new_chat
            screen = Screen(verbose=False)
            screen.bind(hwnd)
            new_chat(screen, "opencode")
            d._invalidate()
        if a.list:
            out = list_state(d, win)
            print(json.dumps(out, ensure_ascii=False))
            if panel_bbox(d, win) is not None:
                close_panel(d, win)
            return done({"ok": True, "stage": "list"}, 0)

        if a.model:
            r = set_model(d, win, a.model)
            print(json.dumps(r, ensure_ascii=False))
            if not r.get("ok"):
                return done(r, 1)
        if a.effort:
            r = set_effort(d, win, a.effort)
            print(json.dumps(r, ensure_ascii=False))
            if not r.get("ok"):
                return done(r, 1)
        if a.prompt:
            # --purpose 必填（无默认）：发送前要拿 *_choose 写下的档位回执过闸
            if not a.purpose:
                # 不用 ap.error：那会直接 SystemExit(2)，绕过 done() 的运行日志
                # （CARD-04 的契约是"每次运行正好一行"）。
                bad = {"ok": False, "stage": "bad_args",
                       "hint": "发送必须带 --purpose test|review|accept|accept_lite（无默认）"}
                print(json.dumps(bad, ensure_ascii=False))
                return done(bad, 2)
            r = ask(d, win, a.prompt, a.timeout, a.purpose)
            print(json.dumps(r, ensure_ascii=False))
            if not r.get("ok"):
                # 档位没确认 = 5（与 docs/exit-codes.md 同一张表）
                return done(r, 5 if r.get("stage") == "tier_unconfirmed" else 1)
    except RuntimeError as e:
        # 自带 stage/code 的异常（ui.UiDocsLost=6、window.WindowMinimized=7）按它自己的码走；
        # 本脚本不 import 项目包，用鸭子类型认，避免多一处模块级依赖。
        if hasattr(e, "as_result"):
            print(json.dumps(e.as_result(), ensure_ascii=False))
            return done(e.as_result(), getattr(e, "code", 6))
        r = {"ok": False, "stage": "aborted", "hint": str(e)}
        print(json.dumps(r, ensure_ascii=False))
        return done(r, 4)
    except Exception as e:                             # noqa: BLE001 - 意外异常也留痕
        done({"ok": False, "stage": "exception",
              "error": f"{type(e).__name__}: {e}"}, 0)   # 返回值用不上，记完原样抛
        raise
    return done(r, 0)


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
    # UI 前台锁（CARD-09a 第 6 条）：和服务端抢同一把 runs\ui.lock
    raise SystemExit(_lock.run("opencode_use", main) if _lock else main())
