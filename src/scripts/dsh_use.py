"""dsh（DeepSeek Harness 桌面端 / DeepSeek Harness.exe）：选模型 + 调强度 + 发消息。

**单文件，只依赖 cv2 / numpy / ctypes，不 import 本项目的任何模块。**
（定位代码和 dsh_choose.py 同一套，这个脚本多一个发消息入口。）

用法：
    python dsh_use.py "只回复两个字：收到"
    python dsh_use.py --list                        读当前模型和强度
    python dsh_use.py --model "GPT-6 Luna"          只切模型
    python dsh_use.py --effort max                  只调强度
    python dsh_use.py --model deepseek_v41_flash --effort low "问题"
    python dsh_use.py --probe                       只报模板命中，不点击

**发消息只到"确认发出去"为止**：取回答还缺复制按钮模板（要 hover 才出现），
所以带文案调用会返回 stage=no_copy_template / sent=true，不会假装读到了回答。
发送前会校验发送键是不是真的可点（蓝色圆的饱和度）。

界面结构、定位方式、已知限制都写在 dsh_choose.py 文件头里。
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


def rect_of(hwnd):
    if not hwnd:
        return None
    r = wintypes.RECT()
    if not user32.GetWindowRect(int(hwnd), ctypes.byref(r)):
        return None
    return (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))


def place(hwnd, pos, size):
    user32.SetWindowPos(int(hwnd), 0, int(pos[0]), int(pos[1]), int(size[0]),
                        int(size[1]), SWP_NOACTIVATE | SWP_SHOWWINDOW)


def activate(hwnd, tries=3):
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


# ============================================================ 定位

def _band(win):
    x, y, w, h = win
    top = y + max(0, h - BAND_H)
    return (x, top, w, min(BAND_H, h))


def chip_chevron(d, win):
    """芯片右端那个 ⌄ 的中心（屏幕坐标），找不到返回 None。

    **不靠模板**：⌄ 的颜色随芯片文字变（模型名是深色时 ⌄ 深，浅色时更浅，
    实测 GPT-6 Luna 下就比 DeepSeek 那边淡一档），模板会匹不上。
    改用几何：以蓝色发送键为右锚点，在它左边 80px 条带里找**最靠右的一撮
    非白像素**——那就是 ⌄（它和左边的模型名之间隔着 10px 空白）。
    """
    m = d.find(TPL_SEND, 0.80, _band(win))
    if m is None:
        return None
    img = d.grab(_band(win))
    if img.size == 0:
        return None
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    top = win[1] + max(0, win[3] - BAND_H)
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
    """点芯片打开子菜单。返回行列表（空 = 没开）。"""
    if menu_open(d, win):
        return menu_rows(d, win)
    c = chip_chevron(d, win)
    if c is None:
        return []
    d.click(c[0], c[1])
    time.sleep(1.3)
    return menu_rows(d, win)


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
    best = None
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in st[i][:5])
        # 面板实测 248x358（只列 Recent 时），换成整组模型时更高更宽。
        # 上限要放到 450x700：卡太紧会把"分组多"的面板判成没开
        # （实测切 GPT-6 Luna 时面板高 500+，旧上限 520 刚好卡掉）。
        # 下限用来排掉页面全选时那种 1000x1200 的大白块。
        if 150 <= w <= 450 and 120 <= h <= 700 and a > 40000:
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
    rows = open_menu(d, win)
    if not rows:
        return {"ok": False, "stage": "menu_not_open"}
    top, bot = rows[0]                      # 第一行永远是「模型」
    d.click(win[0] + 950, win[1] + (top + bot) // 2)
    time.sleep(1.7)
    panel = panel_bbox(d, win)
    if panel is None:
        dbg = os.path.join(DEBUG_DIR, "dsh_model_panel_fail.png")
        cv2.imwrite(dbg, d.grab(win, fresh=True))
        close_all(d, win)
        return {"ok": False, "stage": "panel_not_open", "debug_shot": dbg}
    d.click(panel[0] + panel[2] // 2, panel[1] + 16)
    time.sleep(0.4)
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
    ok = got["model"] == key
    return {"ok": ok, "verified": ok, "want": key, "after": got["model"],
            "score": got.get("score"), "stage": "done" if ok else "unverified"}


def set_effort(d, win, key):
    """调强度：点芯片 -> 点「推理等级」行 -> 点档位词 -> 复核芯片。"""
    key = EFFORT_ALIAS.get(key, key)
    if key not in EFFORTS:
        return {"ok": False, "stage": "bad_arg", "options": EFFORTS}
    before = read_effort(d, win)
    rows = open_menu(d, win)
    if not rows:
        return {"ok": False, "stage": "menu_not_open"}
    if len(rows) < 2:
        close_all(d, win)
        return {"ok": False, "stage": "no_effort_row",
                "hint": "这个模型的菜单只有「模型」一行（没有推理等级），换模型再调"}
    top, bot = rows[1]                      # 第二行 =「推理等级」
    d.click(win[0] + 950, win[1] + (top + bot) // 2)
    time.sleep(1.5)
    m = d.find(TPL_E[key], 0.85, (win[0], win[1] + max(0, win[3] - 300), win[2], 300))
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
    return win[1] + max(0, win[3] - BAND_H)


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
    return {"window": list(win), "templates": rows,
            "menu_rows": menu_rows(d, win),
            "panel": None if pan is None else
            (pan[0] - win[0], pan[1] - win[1], pan[2], pan[3])}


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("prompt", nargs="?", default=None)
    ap.add_argument("--new", action="store_true", help="先打开一个新对话")
    ap.add_argument("--list", "-l", action="store_true")
    ap.add_argument("--model", default=None)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--wait", type=float, default=120.0)
    ap.add_argument("--idle", type=float, default=2.0)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    if not (a.list or a.model or a.effort or a.probe or a.prompt):
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
        print(json.dumps({"ok": False, "stage": "no_window"}, ensure_ascii=False))
        return 1

    win = rect_of(hwnd)
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
        if a.new:
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from qi.screen import Screen
            from qi.workbuddy import new_chat
            screen = Screen(verbose=False)
            screen.bind(hwnd)
            new_chat(screen, "dsh")
            d._invalidate()
        if a.list:
            print(json.dumps(list_state(d, win), ensure_ascii=False))
            return 0

        if a.model:
            r = set_model(d, win, a.model)
            print(json.dumps(r, ensure_ascii=False))
            if not r.get("ok"):
                return 1
        if a.effort:
            r = set_effort(d, win, a.effort)
            print(json.dumps(r, ensure_ascii=False))
            if not r.get("ok"):
                return 1
        if a.prompt:
            r = ask(d, win, a.prompt, a.timeout)
            print(json.dumps(r, ensure_ascii=False))
            if not r.get("ok"):
                return 1
    except RuntimeError as e:
        print(json.dumps({"ok": False, "stage": "aborted", "hint": str(e)},
                         ensure_ascii=False))
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
