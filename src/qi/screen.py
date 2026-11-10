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

from . import uilock

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "src" / "assets" / "library"
EVIDENCE = ROOT / "evidence"

#: 恢复并激活窗口（不改位置/大小）。前台是桌面时，只有它能让 SetForegroundWindow 生效。
SW_RESTORE = 9

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
    **{str(d): 0x30 + d for d in range(10)},
}
# 补齐 a-z：快捷键经常用到（Ctrl+Shift+O 之类），缺一个就 KeyError
VK.update({chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)})

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
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
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
    scale: float = 1.0        # 命中所用的模板尺度（CARD-06b 第 6 条要记录它）

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    def __repr__(self) -> str:
        cx, cy = self.center
        tail = f" 尺度{self.scale:g}" if self.scale != 1.0 else ""
        return f"<{self.name} {self.score:.3f} 中心({cx},{cy}){tail}>"


class ForegroundLost(RuntimeError):
    """目标窗口不在前台：用户切走了，或者被别的窗口盖住了。

    每次点击/输入前都校验一次。这样"用户在用电脑"从"点歪了还不知道"
    变成"立刻停下并报错"——2026-10-29 有一次就是这样把点击打在了微信上。
    """


class Screen:
    def __init__(self, *, threshold: float = 0.85, library: Path | None = None, verbose: bool = True):
        self.threshold = threshold
        self.library = Path(library) if library else LIBRARY
        self.verbose = verbose
        self._tpl: dict[str, np.ndarray] = {}
        self.target_hwnd: int | None = None
        # 最近一次 find/find_all 的最高分（不管过没过阈值）。模板失配的报错要带它：
        # 「没找到」和「差 0.03 分」是两件事，现场看日志时差别很大。
        self.last_best: float | None = None
        # 点击前是否要求目标窗口在前台。默认要：点到别的窗口上比不点更糟。
        self.require_foreground = True
        # B15：tabs 模式下"我这一页是哪个网站的标签"。
        # 设了它，grab/_assert_foreground 会先确认活动标签是自己的（只读也要确认——
        # 轮询段每轮放锁，别的席位会把标签切走）。桌面端 / per_site 模式保持 None。
        self.tab_site: str | None = None

    def _ensure_tab(self) -> None:
        """tabs 模式下把活动标签切回自己。非该模式 / 没设 site 时是空操作。

        B16：切标签本身要进临界区——标签条是四个席位共用的，不在锁里切会出现
        "A 刚切过去、B 立刻切走、A 拍到 B 的页面"。与 `browser.ensure_site` /
        `ui.document` 用同一把 `runs/ui.lock`（`section` 只是 owner 标签不同），
        同线程重入由 `hold` 按 canonical path 放行，不会自锁。
        """
        site = self.tab_site
        if not site or self.target_hwnd is None:
            return
        from . import browser
        if browser.window_mode() != "tabs":
            return
        with uilock.section(f"tab:{site}", timeout=uilock.SECTION_TIMEOUT):
            browser.acquire_tab(site, self.target_hwnd)

    # ---------------------------------------------------------------- 找图
    def bounds(self) -> tuple[int, int, int, int]:
        """虚拟桌面 (x, y, w, h)，含所有显示器。"""
        return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
                user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))

    def window_rect(self, title_hint: str | None = None, *,
                    on_secondary: bool = False) -> tuple[int, int, int, int] | None:
        """取窗口矩形。

        **bind() 过就优先用绑定句柄，不按标题搜。** 副屏同时开了好几个 Chrome 时，
        按标题搜到哪个取决于 EnumWindows 顺序，等于随机选（2026-10-29 因此把按键
        打到了错误的窗口上）。没绑定才退回按标题找。

        按标题找的用途是把"找图/找色"限定在目标窗口内——整块虚拟桌面里别的屏
        有视频、图标也可能是同色，很容易抢先命中。

        2026-11-02 CARD-06a (c)：绑定的窗口**最小化**时不能再把哨兵矩形
        `(-32000,-32000,160,28)` 当矩形返回——`grok_choose --list` 就是这么
        在 `move_to(-31970,-31880)` 上抛 RuntimeError 的。现在先恢复再取，
        恢复不了抛 `WindowMinimized`（退出码 7），绝不拿哨兵坐标去点。
        """
        if self.target_hwnd and user32.IsWindow(self.target_hwnd):
            return self.bound_rect()
        if title_hint is None:
            return None
        pw, ph = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
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
            if title_hint.lower() not in buf.value.lower():
                return True
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            if on_secondary and (0 <= cx < pw and 0 <= cy < ph):
                return True
            if r.right - r.left < 200 or r.bottom - r.top < 200:
                return True
            found.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
            return True

        user32.EnumWindows(_cb, 0)
        return found[0] if found else None

    def bound_rect(self) -> tuple[int, int, int, int]:
        """绑定窗口的可用矩形；最小化就先恢复，恢复不了抛 `WindowMinimized`。

        返回值保证不是哨兵矩形、宽高都过阈值——所以拿它算出来的点击点
        一定是真实屏幕坐标。恢复成功的那一下已经轮询确认过，不看
        `ShowWindow` 的返回值（它返回"调用前是否可见"，不是"恢复成功"）。
        """
        from . import window as window_mod
        facts = window_mod.restore(self.target_hwnd)
        if not facts["ok"]:
            raise window_mod.WindowMinimized(
                f"目标窗口 hwnd={self.target_hwnd} 处于最小化且没能恢复"
                f"（reason={facts['reason']}，rect={facts['rect']}，"
                f"等了 {facts['waited']}s）；不拿哨兵坐标去点。", facts)
        x, y, w, h = facts["rect"]
        return (x, y, w, h)

    def bind(self, hwnd: int) -> bool:
        """绑定一个窗口句柄。之后 window_rect / 前台校验都认它，不再按标题搜。

        2026-11-02 CARD-06a (c)：顺手把最小化的窗口恢复掉（轮询确认，失败也不抛——
        真正的判定留给 `window_rect`）。放在 bind 里而不是各脚本里，是因为
        "绑定一个还在最小化的窗口"在每个调用方都是同一个错误，没必要抄 8 遍。

        B16 注意：**不要给 bind 加 site 参数**。网页端改成在 bind/focus 成功之后
        直接写 `screen.tab_site = <站点名>`（见各 choose / use 入口）——仓库里
        大量测试桩的 `bind(self, hwnd)` 只收一个参数，改签名会把它们全打红。
        """
        if not hwnd or not user32.IsWindow(int(hwnd)):
            return False
        self.target_hwnd = int(hwnd)
        from . import window as window_mod
        try:
            window_mod.restore(self.target_hwnd)
        except Exception:                   # noqa: BLE001 - 恢复失败由 window_rect 报
            pass
        return True

    def focus(self) -> bool:
        """把自己**绑定的**窗口提到前台。没绑定就返回 False（不猜）。

        并发（`BRIDGE_UI_SECTIONED=1`）时"抢前台"本身也要进锁：否则 A 刚把
        自己的窗口提上来、B 立刻提走，A 的点击就抛 `ForegroundLost`
        （2026-11-08 真机，codex 与 workbuddy 互抢）。单跑不加锁，行为不变。
        """
        hwnd = self.target_hwnd
        if not hwnd:
            return False
        if uilock.sectioned():
            with uilock.section("focus", timeout=uilock.SECTION_TIMEOUT):
                return self._focus_locked(hwnd)
        return self._focus_locked(hwnd)

    def _focus_locked(self, hwnd) -> bool:
        if user32.GetForegroundWindow() == hwnd:
            return True
        # **必须先 SW_RESTORE**：2026-11-08 实测，前台是桌面（WorkerW）时，
        # 即使 `IsIconic` 为假，直接 SetForegroundWindow 也一律失败；
        # ShowWindow(SW_RESTORE) 之后再 SetForegroundWindow 就能成功。
        # 它只做"恢复/激活"，不改窗口位置与大小。
        user32.ShowWindow(hwnd, SW_RESTORE)
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
        if user32.GetForegroundWindow() == hwnd:
            return True
        # Windows 的前台锁：只有"当前前台进程"或"刚收到用户输入的进程"能改前台。
        # AttachThreadInput 在并发互抢时会失效（2026-11-08 真机：codex 与 workbuddy
        # 互相把对方挤下去，谁的点按都抛 ForegroundLost）。兜底用 desk 那套三段策略：
        # 轻敲 Alt 解除前台锁 / topmost 闪一下；**并且重试**——单次失败往往只是
        # 对方正在打字，200ms 后就能抢到。
        from . import desk
        for _ in range(3):
            if desk.activate(hwnd, tries=1):
                return True
            time.sleep(0.2)
        return user32.GetForegroundWindow() == hwnd

    def bring_to_front(self, title_hint: str, *, on_secondary: bool = False) -> bool:
        """把标题含 title_hint 的已有窗口提到前台。

        截图只能看到屏幕上露出来的东西，被别的窗口压住就找不到任何图。
        这里只"提到前台"，不新建、不移动、不改大小。

        为什么需要 on_secondary：浏览器标签标题会随会话名变
        （ChatGPT 开个会话后窗口标题就变成"回复收到"，不再是"ChatGPT"）。
        所以对网页端只能按"浏览器 + 在哪块屏"来认，不能按站点名认。
        """
        pw = user32.GetSystemMetrics(0)
        ph = user32.GetSystemMetrics(1)

        def _on_secondary_center(hwnd: int) -> bool:
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            # 主屏原点固定 (0,0)，用屏幕尺寸判断中心是否落在主屏上
            return not (0 <= cx < pw and 0 <= cy < ph)

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
                if on_secondary and not _on_secondary_center(hwnd):
                    return True
                found.append(int(hwnd))
            return True

        user32.EnumWindows(_cb, 0)
        if not found:
            return False
        hwnd = found[0]
        self.target_hwnd = hwnd          # 即使已经在前台也要记下来，供点击前校验
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
        # 截图读的是屏幕合成结果：被遮挡/标签不是自己 → 读到的是别人的画面。
        self._ensure_tab()
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
                best = Match(name, float(mx), ox + loc[0], oy + loc[1],
                             tpl.shape[1], tpl.shape[0], float(sc))
        self.last_best = best.score if best is not None else None
        if best is not None and best.score >= th:
            if self.verbose:
                print(f"  找到 {best}")
            return best
        if self.verbose:
            # 分数之外还要带上「在哪块屏、什么缩放」：本机副屏是竖屏，
            # 窗口坐标是负的，模板又可能是在另一个页面缩放下截的，只看分数判不出原因。
            tried = (f"；模板缩放 {'/'.join(f'{sc * 100:.0f}%' for sc in scales)}"
                     if len(scales) > 1 else "")
            print(f"  没找到 {name}（最高 {best.score:.3f} / 阈值 {th}{tried}；{self.describe(region)}）"
                  if best else f"  没找到 {name}（图比屏幕大；{self.describe(region)}）")
        return None

    def find_unique(self, name: str, *, threshold: float | None = None,
                    region: tuple[int, int, int, int] | None = None,
                    scales: tuple[float, ...] = (1.0,), limit: int = 10,
                    gap: int = 8) -> dict:
        """多尺度 + **唯一性**匹配（CARD-06b 第 6 条）。

        `find()` 只给最高分，多尺度时"同一张图在两处命中"看不出来。档位芯片
        必须唯一：芯片和菜单项长得像，取第一个就可能读到菜单里那一行。

        返回：{"match": Match|None, "ambiguous": bool, "best": 最高分,
               "scale": 最高分所在尺度, "hits": 该尺度下的命中}
        · 只要**任一**尺度上同一模板有 ≥2 处命中 → ambiguous=True，match=None；
        · 最高分没过阈值 → match=None，best 仍然带出来（报错要用它）。
        """
        th = self.threshold if threshold is None else threshold
        hay = cv2.cvtColor(self.grab(region), cv2.COLOR_BGR2GRAY)
        tpl0 = self.template(name)
        ox, oy = (region[0], region[1]) if region else (
            user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
        best_score, best_scale, best_match = -1.0, 1.0, None
        ambiguous = False

        def keep(hits):
            hits.sort(key=lambda m: m.score, reverse=True)
            kept = []
            for hit in hits:
                if all(abs(hit.x - old.x) > gap or abs(hit.y - old.y) > gap for old in kept):
                    kept.append(hit)
                    if len(kept) >= limit:
                        break
            return kept

        for sc in scales:
            tpl = tpl0 if sc == 1.0 else cv2.resize(
                tpl0, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_CUBIC)
            if tpl.shape[0] > hay.shape[0] or tpl.shape[1] > hay.shape[1]:
                continue
            res = cv2.matchTemplate(hay, tpl, cv2.TM_CCOEFF_NORMED)
            if not res.size:
                continue
            peak = float(res.max())
            if peak > best_score:
                py, px = np.unravel_index(int(res.argmax()), res.shape)
                best_score = peak
                best_scale = float(sc)
                best_match = Match(name, peak, ox + int(px), oy + int(py),
                                   tpl.shape[1], tpl.shape[0], float(sc))
            ys, xs = np.where(res >= th)
            if len(xs) > 1:
                same = keep([Match(name, float(res[y, x]), ox + int(x), oy + int(y),
                                   tpl.shape[1], tpl.shape[0], float(sc))
                             for y, x in zip(ys, xs)])
                if len(same) > 1:
                    ambiguous = True
        self.last_best = None if best_score < 0 else best_score
        if ambiguous:
            return {"match": None, "ambiguous": True, "best": self.last_best,
                    "scale": best_scale, "hits": []}
        if best_match is not None and best_match.score >= th:
            return {"match": best_match, "ambiguous": False, "best": self.last_best,
                    "scale": best_scale, "hits": [best_match]}
        return {"match": None, "ambiguous": False, "best": self.last_best,
                "scale": best_scale, "hits": []}

    def dpi_scale(self) -> float | None:
        """目标窗口所在显示器的缩放倍率（1.0 = 100%）。读不到返回 None。"""
        try:
            user32.GetDpiForWindow.argtypes = [wintypes.HWND]
            user32.GetDpiForWindow.restype = wintypes.UINT
            user32.GetDpiForSystem.argtypes = []
            user32.GetDpiForSystem.restype = wintypes.UINT
            if self.target_hwnd and user32.IsWindow(int(self.target_hwnd)):
                return user32.GetDpiForWindow(int(self.target_hwnd)) / 96.0
            return user32.GetDpiForSystem() / 96.0
        except Exception:                   # noqa: BLE001 - 诊断信息不许把主流程带崩
            return None

    def describe(self, region: tuple[int, int, int, int] | None = None) -> str:
        """一行说清「找图找在哪块屏、什么缩放」。只给报错/日志用，不参与判定。"""
        parts = []
        try:
            r = region or self.window_rect()
        except Exception:                   # noqa: BLE001
            r = None
        if r:
            parts.append(f"区域 {r[0]},{r[1]} {r[2]}x{r[3]}")
            try:
                m = self.monitor_rect(r[0] + r[2] // 2, r[1] + r[3] // 2)
                parts.append(f"显示器 {m[0]},{m[1]} {m[2]}x{m[3]}")
            except Exception:               # noqa: BLE001
                pass
        scale = self.dpi_scale()
        if scale:
            parts.append(f"显示器缩放 {round(scale * 100)}%")
        return "；".join(parts) if parts else "区域未知"

    def region_mean(self, m: Match) -> tuple[float, float, float]:
        """命中框内的平均 BGR。

        模板匹配用的是归一化相关，对整体亮度/对比度不敏感：
        同一个按钮"深蓝可点"和"浅蓝不可点"会给出同样的 1.000。
        所以状态判断必须靠颜色，不能靠分数。
        """
        img = self.grab((m.x, m.y, m.w, m.h))
        return (float(img[:, :, 0].mean()), float(img[:, :, 1].mean()), float(img[:, :, 2].mean()))

    def find_all(self, name: str, *, threshold: float | None = None, limit: int = 10,
                 region: tuple[int, int, int, int] | None = None) -> list[Match]:
        """和 find 一样，但返回所有命中（按分数降序，去重）。

        region 一定要传：不传就是在整个虚拟桌面（含主屏）里找，
        主屏上开着同款页面/同款图标时会先命中那边，然后拿着主屏的坐标去点。
        """
        th = self.threshold if threshold is None else threshold
        hay = cv2.cvtColor(self.grab(region), cv2.COLOR_BGR2GRAY)
        tpl = self.template(name)
        ox, oy = (region[0], region[1]) if region else (
            user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
        res = cv2.matchTemplate(hay, tpl, cv2.TM_CCOEFF_NORMED)
        self.last_best = float(res.max()) if res.size else None
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
             appear: bool = True, threshold: float | None = None,
             region: tuple[int, int, int, int] | None = None) -> Match | None:
        end = time.time() + timeout
        while time.time() < end:
            m = self.find(name, threshold=threshold, region=region)
            if (m is not None) == appear:
                return m
            time.sleep(interval)
        if self.verbose:
            print(f"  等待 {name} {'出现' if appear else '消失'} 超时 {timeout}s")
        return None

    def wait_present(self, name: str, *, timeout: float = 15.0, interval: float = 0.4,
                     threshold: float | None = None,
                     region: tuple[int, int, int, int] | None = None) -> bool:
        """等这张图出现。返回 True/False，不返回 Match（避免和"超时"混淆）。"""
        end = time.time() + timeout
        while time.time() < end:
            if self.find(name, threshold=threshold, region=region) is not None:
                return True
            time.sleep(interval)
        return False

    def wait_gone(self, name: str, *, timeout: float = 60.0, interval: float = 0.4,
                  threshold: float | None = None,
                  region: tuple[int, int, int, int] | None = None) -> bool:
        """等这张图消失。返回 True 表示确实消失了。"""
        end = time.time() + timeout
        while time.time() < end:
            if self.find(name, threshold=threshold, region=region) is None:
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

    # ---------------------------------------------------------------- 动作
    def _mark_input(self) -> None:
        """记一笔「最后这次键鼠输入是我合成的」（CARD-09e 第 7 条）。

        下一个脚本启动时的空闲闸会读这个 tick：如果系统最后的输入就是这一笔，
        说明用户从那以后没动过机器，那句「你正在用电脑」是我们自己造成的。
        纯提示，写失败一律吞掉，不许影响动作本身。
        """
        try:
            from . import guard
            guard.mark_synthetic("screen")
        except Exception:                   # noqa: BLE001
            pass

    def move_to(self, x: int, y: int) -> None:
        """把光标移到屏幕坐标 (x, y)。

        以前用 mouse_event + MOUSEEVENTF_ABSOLUTE 归一化到 65535。这个接口在
        多显示器/混合 DPI 下**有时被系统忽略**：调用返回后光标仍在原处，
        于是后续 click 点在"上一次的位置"——这正是误触录音键的根因
        （实测请求 (-495,243)，光标实际停在 (-442,288)）。
        SetCursorPos 是直接定位，实测偏差 0，改用它。
        移动后再读一次确认，不达标就重试一次。
        """
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetCursorPos.restype = wintypes.BOOL
        for _ in range(2):
            user32.SetCursorPos(int(x), int(y))
            time.sleep(0.02)
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            if abs(pt.x - int(x)) <= 1 and abs(pt.y - int(y)) <= 1:
                return
        # 两次都没到位：至少别让调用方以为点到了
        raise RuntimeError(f"光标没能移到 ({x},{y})，实际停在 ({pt.x},{pt.y})")

    def _assert_foreground(self) -> None:
        """点击/输入前校验：前台还是不是当初那个窗口。

        没调用过 bring_to_front（target_hwnd 为空）时直接放行，
        这样只读场景（截图、找图）不受影响。
        """
        if not self.require_foreground or self.target_hwnd is None:
            return
        self._ensure_tab()
        fg = user32.GetForegroundWindow()
        if fg != self.target_hwnd:
            raise ForegroundLost(
                f"目标窗口不在前台（前台={fg}，目标={self.target_hwnd}）。"
                "你可能切到了别的窗口，已中止，不猜位置。")

    def click_xy(self, x: int, y: int) -> None:
        # 并发模式（`BRIDGE_UI_CONCURRENT=1`）下"前台"是共享资源：别的席位新建窗口
        # （比如网页端开自己的 Chrome 窗）会把前台抢走，这不是"用户接管"。
        # 所以点之前先把自己的窗口抢回来；单跑模式保持原样——用户抢走就中止。
        if uilock.concurrent_mode() and self.target_hwnd is not None:
            if user32.GetForegroundWindow() != self.target_hwnd:
                self.focus()
        self._assert_foreground()
        self.move_to(x, y)
        time.sleep(0.03)
        _send(_INPUT(type=INPUT_MOUSE, mi=_MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, None)))
        time.sleep(0.03)
        _send(_INPUT(type=INPUT_MOUSE, mi=_MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, None)))
        self._mark_input()

    def click(self, m: Match, *, offset: tuple[int, int] = (0, 0)) -> None:
        cx, cy = m.center
        self.click_xy(cx + offset[0], cy + offset[1])

    def drag(self, x1: int, y1: int, x2: int, y2: int, *, steps: int = 8) -> None:
        """按住从 (x1,y1) 拖到 (x2,y2)。

        滑杆这类控件只点一下改不动（点了也弹回原位），必须按住拖。

        每一步都用 SetCursorPos 而不是 SendInput 的绝对坐标：这个项目里
        已经证实绝对坐标在这台机器上不可靠（move_to 曾经用 mouse_event
        挪不动光标，改成 SetCursorPos 才好）。拖动更怕这个——坐标一偏，
        按下和抬起就不在同一个控件上，表现是"拖了一下就失灵"。
        """
        self._assert_foreground()
        user32.SetCursorPos(int(x1), int(y1))
        time.sleep(0.06)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        if abs(pt.x - int(x1)) > 2 or abs(pt.y - int(y1)) > 2:
            raise RuntimeError(f"光标没能移到拖动起点 ({x1},{y1})，实际 ({pt.x},{pt.y})")
        user32.mouse_event(0x0002, 0, 0, 0, 0)      # LEFTDOWN
        time.sleep(0.10)
        for i in range(1, steps + 1):
            x = x1 + (x2 - x1) * i // steps
            y = y1 + (y2 - y1) * i // steps
            user32.SetCursorPos(int(x), int(y))
            time.sleep(0.04)
        # 终点要"停住"：网页的 mousemove 处理有节流，松手前不多停一会儿，
        # 控件会停在半路（实测拖 60px 只走了 33px）。这里在原位重复发同一坐标，
        # 相当于按住不动等它跟上，再松手。
        for _ in range(6):
            user32.SetCursorPos(int(x2), int(y2))
            time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)      # LEFTUP
        self._mark_input()

    def click_image(self, name: str, *, offset: tuple[int, int] = (0, 0),
                    wait: float = 0.0, threshold: float | None = None,
                    region: tuple[int, int, int, int] | None = None) -> Match:
        m = self.find(name, threshold=threshold, region=region)
        if m is None and wait > 0:
            m = self.wait(name, timeout=wait, threshold=threshold, region=region)
        if m is None:
            raise LookupError(f"屏幕上找不到 {name}，没有点击（不猜位置）")
        self.click(m, offset=offset)
        return m

    def scroll(self, notches: int, *, at: tuple[int, int] | None = None) -> None:
        """滚轮。正数向上、负数向下，一格 = 120。

        at 给了就先移过去再滚——滚轮事件发给**鼠标当前位置下面的窗口**，
        不移过去就是在别处滚（之前 _find_item 把鼠标放在窗口中心，那是页面
        正中，根本不在模型列表上，所以怎么滚都找不到最后一项）。
        """
        self._assert_foreground()
        if at is not None:
            self.move_to(int(at[0]), int(at[1]))
            time.sleep(0.05)
        user32.mouse_event(0x0800, 0, 0, int(notches) * 120, 0)
        self._mark_input()

    def type_text(self, text: str, *, interval: float = 0.004) -> None:
        for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
            self._assert_foreground()
            if ch == "\n":
                self.hotkey("shift", "enter")
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
        self._mark_input()

    def _ensure_foreground(self) -> None:
        """并发模式下把前台抢回来；单跑模式什么都不做（保持"用户抢走就中止"）。

        键盘输入也是发给**前台窗口**的：抢不回来就会把内容打进别人的窗口。
        """
        if uilock.concurrent_mode() and self.target_hwnd is not None:
            if user32.GetForegroundWindow() != self.target_hwnd:
                self.focus()

    def press(self, key: str, *, times: int = 1) -> None:
        self._ensure_foreground()
        self._assert_foreground()
        vk = VK[key.lower()]
        for _ in range(times):
            _send(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(vk, 0, 0, 0, None)))
            time.sleep(0.02)
            _send(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, None)))
            time.sleep(0.02)
        self._mark_input()

    def hotkey(self, *keys: str) -> None:
        self._ensure_foreground()
        self._assert_foreground()
        codes = [VK[k.lower()] for k in keys]
        for c in codes:
            _send(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(c, 0, 0, 0, None)))
        for c in reversed(codes):
            _send(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(c, 0, KEYEVENTF_KEYUP, 0, None)))
        self._mark_input()

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

    def clipboard_seq(self) -> int | None:
        """剪贴板序列号：有人写一次剪贴板，Windows 就把它 +1。读不到返回 None。

        CARD-09f 第 5 条：写剪贴板后用「序列号确实变了」确认内容真的更新了，
        而不是靠「读回来的文本对不对」（两个不同文本在极端情况下也可能被判成
        没变）。纯诊断，读不到不猜。
        """
        try:
            return int(user32.GetClipboardSequenceNumber())
        except Exception:                    # noqa: BLE001 - 诊断信息，不许带崩主流程
            return None

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
