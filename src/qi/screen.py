"""找图内核：截屏 → 找图 → 点/输入/读剪贴板。

一套逻辑，所有端共用。跟 v0.0.8 的区别只有一条：
**不用控件树，只认像素**。屏幕上没有的图就找不到，这是这套方案的边界。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "src" / "assets" / "library"
EVIDENCE = ROOT / "evidence"

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

SRCCOPY = 0x00CC0020
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79

MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x8000, 0x4000
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x0004, 0x0002

VK = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "enter": 0x0D, "esc": 0x1B,
    "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
    "a": 0x41, "c": 0x43, "v": 0x56, "x": 0x58,
}

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _init() -> str:
    try:
        level = "per-monitor-v2" if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)) else "?"
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            level = "per-monitor"
        except Exception:
            user32.SetProcessDPIAware()
            level = "system"

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

    # 剪贴板：句柄是 64 位，不声明 argtypes 会被截断成 0 并 access violation
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    # 提前台用到的窗口 API
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    return level


DPI_LEVEL = _init()


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_void_p)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", ctypes.c_void_p)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(*items: _INPUT) -> None:
    arr = (_INPUT * len(items))(*items)
    if user32.SendInput(len(items), arr, ctypes.sizeof(_INPUT)) != len(items):
        raise RuntimeError("SendInput 被拦截：可能有更高权限的窗口占据前台")


@dataclass
class Match:
    name: str
    score: float
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    def __repr__(self) -> str:
        cx, cy = self.center
        return f"<{self.name} {self.score:.3f} 中心({cx},{cy})>"


class Screen:
    def __init__(self, *, threshold: float = 0.85, library: Path | None = None, verbose: bool = True):
        self.threshold = threshold
        self.library = Path(library) if library else LIBRARY
        self.verbose = verbose
        self._tpl: dict[str, np.ndarray] = {}

    # ---------------------------------------------------------------- 找图
    def bounds(self) -> tuple[int, int, int, int]:
        """虚拟桌面 (x, y, w, h)，含所有显示器。"""
        return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
                user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))

    def bring_to_front(self, title_hint: str) -> bool:
        """把标题含 title_hint 的已有窗口提到前台。

        截图只能看到屏幕上露出来的东西，被别的窗口压住就找不到任何图。
        这里只"提到前台"，不新建、不移动、不改大小。
        """
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if title_hint.lower() in buf.value.lower():
                found.append(int(hwnd))
            return True

        user32.EnumWindows(_cb, 0)
        if not found:
            return False
        hwnd = found[0]
        if user32.GetForegroundWindow() == hwnd:
            return True
        fg = user32.GetForegroundWindow()
        tid_cur = kernel32.GetCurrentThreadId()
        tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        if tid_fg and tid_fg != tid_cur:
            attached = bool(user32.AttachThreadInput(tid_cur, tid_fg, True))
        try:
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(tid_cur, tid_fg, False)
        time.sleep(0.25)
        return user32.GetForegroundWindow() == hwnd

    def template(self, name: str) -> np.ndarray:
        if name in self._tpl:
            return self._tpl[name]
        p = self.library / (name if name.lower().endswith(".png") else f"{name}.png")
        img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(f"资源库缺图：{p.name}（先用 tools/snip.py 截一张）")
        self._tpl[name] = img
        return img

    def grab(self, region: tuple[int, int, int, int] | None = None) -> np.ndarray:
        vx, vy = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw, vh = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        x, y, w, h = (vx, vy, vw, vh) if region is None else region
        hdc = user32.GetDC(0)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        old = gdi32.SelectObject(mdc, bmp)
        try:
            if not gdi32.BitBlt(mdc, 0, 0, w, h, hdc, x, y, SRCCOPY):
                raise RuntimeError("BitBlt 失败")
            bi = _bitmapinfo(w, h)
            buf = ctypes.create_string_buffer(w * h * 4)
            lines = gdi32.GetDIBits(mdc, bmp, 0, h, ctypes.cast(buf, ctypes.c_void_p),
                                    ctypes.cast(ctypes.byref(bi), ctypes.c_void_p), 0)
            if lines != h:
                raise RuntimeError(f"GetDIBits 只拿到 {lines}/{h} 行")
            arr = np.frombuffer(buf, dtype=np.uint8, count=w * h * 4).reshape(h, w, 4)
            return arr[:, :, :3].copy()
        finally:
            gdi32.SelectObject(mdc, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mdc)
            user32.ReleaseDC(0, hdc)

    def find(self, name: str, *, threshold: float | None = None,
             region: tuple[int, int, int, int] | None = None,
             scales: tuple[float, ...] = (1.0,)) -> Match | None:
        th = self.threshold if threshold is None else threshold
        hay = cv2.cvtColor(self.grab(region), cv2.COLOR_BGR2GRAY)
        tpl0 = self.template(name)
        ox, oy = (region[0], region[1]) if region else (
            user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
        best: Match | None = None
        for sc in scales:
            tpl = tpl0 if sc == 1.0 else cv2.resize(
                tpl0, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_CUBIC)
            if tpl.shape[0] > hay.shape[0] or tpl.shape[1] > hay.shape[1]:
                continue
            res = cv2.matchTemplate(hay, tpl, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(res)
            if best is None or mx > best.score:
                best = Match(name, float(mx), ox + loc[0], oy + loc[1], tpl.shape[1], tpl.shape[0])
        if best is not None and best.score >= th:
            if self.verbose:
                print(f"  找到 {best}")
            return best
        if self.verbose:
            print(f"  没找到 {name}（最高 {best.score:.3f} / 阈值 {th}）" if best else f"  没找到 {name}（图比屏幕大）")
        return None

    def region_mean(self, m: Match) -> tuple[float, float, float]:
        """命中框内的平均 BGR。

        模板匹配用的是归一化相关，对整体亮度/对比度不敏感：
        同一个按钮"深蓝可点"和"浅蓝不可点"会给出同样的 1.000。
        所以状态判断必须靠颜色，不能靠分数。
        """
        img = self.grab((m.x, m.y, m.w, m.h))
        return (float(img[:, :, 0].mean()), float(img[:, :, 1].mean()), float(img[:, :, 2].mean()))

    def find_all(self, name: str, *, threshold: float | None = None, limit: int = 10) -> list[Match]:
        th = self.threshold if threshold is None else threshold
        hay = cv2.cvtColor(self.grab(), cv2.COLOR_BGR2GRAY)
        tpl = self.template(name)
        ox, oy = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        res = cv2.matchTemplate(hay, tpl, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= th)
        hits = [Match(name, float(res[y, x]), ox + int(x), oy + int(y), tpl.shape[1], tpl.shape[0])
                for y, x in zip(ys, xs)]
        hits.sort(key=lambda m: m.score, reverse=True)
        kept: list[Match] = []
        for h in hits:
            if all(abs(h.x - k.x) > 4 or abs(h.y - k.y) > 4 for k in kept):
                kept.append(h)
                if len(kept) >= limit:
                    break
        return kept

    def wait(self, name: str, *, timeout: float = 15.0, interval: float = 0.4,
             appear: bool = True, threshold: float | None = None) -> Match | None:
        end = time.time() + timeout
        while time.time() < end:
            m = self.find(name, threshold=threshold)
            if (m is not None) == appear:
                return m
            time.sleep(interval)
        if self.verbose:
            print(f"  等待 {name} {'出现' if appear else '消失'} 超时 {timeout}s")
        return None

    def wait_present(self, name: str, *, timeout: float = 15.0, interval: float = 0.4,
                     threshold: float | None = None) -> bool:
        """等这张图出现。返回 True/False，不返回 Match（避免和"超时"混淆）。"""
        end = time.time() + timeout
        while time.time() < end:
            if self.find(name, threshold=threshold) is not None:
                return True
            time.sleep(interval)
        return False

    def wait_gone(self, name: str, *, timeout: float = 60.0, interval: float = 0.4,
                  threshold: float | None = None) -> bool:
        """等这张图消失。返回 True 表示确实消失了。"""
        end = time.time() + timeout
        while time.time() < end:
            if self.find(name, threshold=threshold) is None:
                return True
            time.sleep(interval)
        return False

    def monitor_rect(self, x: int, y: int) -> tuple[int, int, int, int]:
        """点 (x,y) 所在显示器的矩形。用来把"画面静止"限定在一块屏上。"""

        class _POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class _MI(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                        ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]

        user32.MonitorFromPoint.argtypes = [_POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MI)]
        hmon = user32.MonitorFromPoint(_POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
        mi = _MI()
        mi.cbSize = ctypes.sizeof(_MI)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return self.bounds()
        r = mi.rcMonitor
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)

    def wait_quiet(self, *, timeout: float = 120.0, interval: float = 0.7,
                   tol: float = 0.35, need: int = 2,
                   region: tuple[int, int, int, int] | None = None) -> bool:
        """等画面静止下来。

        用来兜"没抓到停止键"的情况：流式输出时画面一直在变，
        连续 need 帧几乎没变化，就认为生成结束了。
        不依赖任何模板，所以不会因为换主题/换版本失效。

        region 建议传目标窗口所在显示器：整块虚拟桌面里可能有别的屏在放视频。
        """
        prev = self.grab(region)
        stable = 0
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(interval)
            cur = self.grab(region)
            d = float(cv2.absdiff(prev, cur).mean()) if cur.shape == prev.shape else 1.0
            prev = cur
            if d < tol:
                stable += 1
                if stable >= need:
                    return True
            else:
                stable = 0
        return False

    # ---------------------------------------------------------------- 动作
    def move_to(self, x: int, y: int) -> None:
        vx, vy = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw, vh = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        ax = int(round((x - vx) * 65535 / max(1, vw - 1)))
        ay = int(round((y - vy) * 65535 / max(1, vh - 1)))
        _send(_INPUT(type=INPUT_MOUSE,
                     mi=_MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0, None)))

    def click_xy(self, x: int, y: int) -> None:
        self.move_to(x, y)
        time.sleep(0.03)
        _send(_INPUT(type=INPUT_MOUSE, mi=_MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, None)))
        time.sleep(0.03)
        _send(_INPUT(type=INPUT_MOUSE, mi=_MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, None)))

    def click(self, m: Match, *, offset: tuple[int, int] = (0, 0)) -> None:
        cx, cy = m.center
        self.click_xy(cx + offset[0], cy + offset[1])

    def click_image(self, name: str, *, offset: tuple[int, int] = (0, 0),
                    wait: float = 0.0, threshold: float | None = None) -> Match:
        m = self.find(name, threshold=threshold)
        if m is None and wait > 0:
            m = self.wait(name, timeout=wait, threshold=threshold)
        if m is None:
            raise LookupError(f"屏幕上找不到 {name}，没有点击（不猜位置）")
        self.click(m, offset=offset)
        return m

    def type_text(self, text: str, *, interval: float = 0.004) -> None:
        for ch in text:
            if ch == "\n":
                self.press("enter")
                continue
            code = ord(ch)
            if code > 0xFFFF:
                code -= 0x10000
                for part in (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)):
                    _send(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(0, part, KEYEVENTF_UNICODE, 0, None)))
                    _send(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(0, part, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)))
            else:
                _send(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, None)))
                _send(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)))
            if interval:
                time.sleep(interval)

    def press(self, key: str, *, times: int = 1) -> None:
        vk = VK[key.lower()]
        for _ in range(times):
            _send(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(vk, 0, 0, 0, None)))
            time.sleep(0.02)
            _send(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, None)))
            time.sleep(0.02)

    def hotkey(self, *keys: str) -> None:
        codes = [VK[k.lower()] for k in keys]
        for c in codes:
            _send(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(c, 0, 0, 0, None)))
        for c in reversed(codes):
            _send(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(c, 0, KEYEVENTF_KEYUP, 0, None)))

    # ---------------------------------------------------------------- 剪贴板
    def clipboard(self) -> str:
        user32.OpenClipboard(0)
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            p = kernel32.GlobalLock(ctypes.c_void_p(h))
            try:
                return ctypes.wstring_at(p) if p else ""
            finally:
                kernel32.GlobalUnlock(ctypes.c_void_p(h))
        finally:
            user32.CloseClipboard()

    def set_clipboard(self, text: str) -> None:
        data = text.encode("utf-16-le") + b"\x00\x00"
        user32.OpenClipboard(0)
        try:
            user32.EmptyClipboard()
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            p = kernel32.GlobalLock(ctypes.c_void_p(h))
            ctypes.memmove(p, data, len(data))
            kernel32.GlobalUnlock(ctypes.c_void_p(h))
            if not user32.SetClipboardData(CF_UNICODETEXT, ctypes.c_void_p(h)):
                raise RuntimeError("SetClipboardData 失败")
        finally:
            user32.CloseClipboard()

    # ---------------------------------------------------------------- 取证
    def snapshot(self, tag: str = "shot") -> Path:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        p = EVIDENCE / f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        cv2.imwrite(str(p), self.grab())
        return p

    def snapshot_marked(self, m: Match, tag: str = "hit") -> Path:
        img = self.grab()
        vx, vy = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        cv2.rectangle(img, (m.x - vx, m.y - vy), (m.x - vx + m.w, m.y - vy + m.h), (0, 0, 255), 2)
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        p = EVIDENCE / f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        cv2.imwrite(str(p), img)
        return p


def _bitmapinfo(w: int, h: int):
    class _BIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    class _BI(ctypes.Structure):
        _fields_ = [("bmiHeader", _BIH), ("bmiColors", wintypes.DWORD * 3)]

    bi = _BI()
    bi.bmiHeader.biSize = ctypes.sizeof(_BIH)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    return bi
