"""准备一个可操作的 Chrome 窗口。

规则（2026-10-29 用户定的）：
  1. 上次脚本开的窗口还在 → 直接用（重开会在你桌面堆窗口）
  2. 否则看现有 Chrome **有没有在播音频**（放视频几乎一定出声）：
       · 没在放 → 就在那个 Chrome 里开目标网页，再把它摆到副屏
       · 在放   → 新开一个窗口（不打扰你正在看的东西）
  3. 一个 Chrome 都没有 → 新开窗口

**一次只开"当前要操作的那个站"**，不是四个都开。因为复用已有窗口时，
用户原有的标签还在前面，`Ctrl+N` 的编号就不可靠了；只开一个的话，
新标签必然落在最后并且是当前标签，脚本直接就能操作——不依赖任何编号。

判断"当前标签是不是目标页"靠窗口标题（Chrome 的标题 = 当前标签标题）。
"""

from __future__ import annotations

import ctypes
import json
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SW_SHOWMINNOACTIVE = 7
SW_RESTORE = 9
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".chrome-window.json"

# 站点名 -> (URL, 窗口标题里该有的关键词)
SITES = {
    "chatgpt": ("https://chatgpt.com/", "chatgpt"),
    "gemini": ("https://gemini.google.com/app", "gemini"),
    "grok": ("https://grok.com/", "grok"),
    "deepseek": ("https://chat.deepseek.com/", "deepseek"),
}

DEFAULT_POS = (-1440, -560)
DEFAULT_SIZE = (1380, 1340)


# ---------------------------------------------------------------- 窗口枚举
def _title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(int(hwnd))
    if n <= 0:
        return ""
    b = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(int(hwnd), b, n + 1)
    return b.value


def _chrome_hwnds() -> set[int]:
    out: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        t = _title(int(hwnd))
        if "google chrome" not in t.lower():
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.right - r.left < 300 or r.bottom - r.top < 300:
            return True
        out.add(int(hwnd))
        return True

    user32.EnumWindows(cb, 0)
    return out


def alive(hwnd: int) -> bool:
    if not hwnd or not user32.IsWindow(int(hwnd)):
        return False
    return "google chrome" in _title(int(hwnd)).lower()


def _frontmost_chrome() -> int | None:
    h = int(user32.GetForegroundWindow())
    return h if alive(h) else None


def _on_secondary(hwnd: int) -> bool:
    """窗口中心是否在副屏（主屏原点固定 (0,0)）。"""
    pw = user32.GetSystemMetrics(0)
    ph = user32.GetSystemMetrics(1)
    r = wintypes.RECT()
    user32.GetWindowRect(int(hwnd), ctypes.byref(r))
    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    return not (0 <= cx < pw and 0 <= cy < ph)


def _largest_chrome() -> int | None:
    best, area = None, 0
    for h in _chrome_hwnds():
        r = wintypes.RECT()
        user32.GetWindowRect(h, ctypes.byref(r))
        a = (r.right - r.left) * (r.bottom - r.top)
        if a > area:
            best, area = h, a
    return best


# ---------------------------------------------------------------- 摆放与前台
def place(hwnd: int, position=DEFAULT_POS, size=DEFAULT_SIZE) -> None:
    """摆到指定位置和大小。不激活。"""
    user32.ShowWindow(int(hwnd), SW_RESTORE)
    time.sleep(0.2)
    user32.SetWindowPos(int(hwnd), 0, position[0], position[1], size[0], size[1],
                        SWP_NOACTIVATE | SWP_SHOWWINDOW)


def _focus(hwnd: int) -> bool:
    hwnd = int(hwnd)
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
    time.sleep(0.3)
    return user32.GetForegroundWindow() == hwnd


# ---------------------------------------------------------------- 状态
def _load() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(hwnd: int, site: str = "") -> None:
    STATE.write_text(json.dumps({"hwnd": int(hwnd), "site": site}, ensure_ascii=False),
                     encoding="utf-8")


def current() -> int | None:
    h = _load().get("hwnd")
    return int(h) if h and alive(int(h)) else None


def forget() -> None:
    if STATE.exists():
        STATE.unlink()


# ---------------------------------------------------------------- 主流程
def _wait_ready(hwnd: int, timeout: float = 15.0) -> bool:
    """等页面加载完：窗口标题连续两次相同就算稳。

    Chrome 加载中标题会变（先域名、再页面标题、再带未读数），稳定下来基本就是好了。
    不这么等的话，`ensure_site` 返回时 DOM 还没渲染，脚本上去找不到控件，报一堆
    "unreadable / 打不开菜单"——实测新标签打开后只等 1.2s 就是这个结果。
    """
    prev = None
    end = time.time() + timeout
    while time.time() < end:
        t = _title(hwnd)
        if prev is not None and t == prev and "google chrome" in t.lower():
            return True
        prev = t
        time.sleep(0.8)
    return False


def _open_url(hwnd: int, url: str, wait: float = 1.2) -> None:
    """在已有窗口里开一个标签。Chrome 会把它开在最后并切过去。"""
    _focus(hwnd)
    shell32.ShellExecuteW(None, "open", CHROME, url, None, SW_SHOWMINNOACTIVE)
    time.sleep(wait)
    _wait_ready(hwnd)


def _spawn(url: str, position, size, timeout: float, verbose: bool) -> int | None:
    before = _chrome_hwnds()
    arg = " ".join([
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-position={position[0]},{position[1]}",
        f"--window-size={size[0]},{size[1]}",
        url,
    ])
    rc = shell32.ShellExecuteW(None, "open", CHROME, arg, None, SW_SHOWMINNOACTIVE)
    if rc <= 32:
        if verbose:
            print(f"  启动失败 rc={rc}")
        return None

    hwnd = None
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.4)
        new = _chrome_hwnds() - before
        if new:
            hwnd = max(new)
            break
    if hwnd is None:
        if verbose:
            print("  没等到新窗口")
        return None
    place(hwnd, position, size)
    _wait_ready(hwnd)
    if verbose:
        print(f"  新窗口 hwnd={hwnd}")
    return hwnd


def ensure_site(name: str, *, position=DEFAULT_POS, size=DEFAULT_SIZE,
                timeout: float = 25.0, verbose: bool = True) -> int | None:
    """确保有一个窗口，且当前标签是 name 对应的站点。返回窗口句柄。"""
    from .audio import chrome_playing_audio

    if name not in SITES:
        raise KeyError(f"未知站点 {name}，可选 {list(SITES)}")
    url, keyword = SITES[name]

    old = current()
    if old:
        place(old, position, size)
        if keyword in _title(old).lower():
            if verbose:
                print(f"  复用窗口 hwnd={old}（当前已是 {name}）")
            _save(old, name)
            return old
        if verbose:
            print(f"  复用窗口 hwnd={old}，在里面开 {name}")
        _open_url(old, url)
        _save(old, name)
        return old

    playing = chrome_playing_audio()
    if verbose:
        print(f"  Chrome 在播音频: {playing}")

    if not playing:
        target = _frontmost_chrome() or _largest_chrome()
        # 只复用**已经在副屏**的窗口。主屏那个是用户自己的，把它挪到副屏等于
        # 改了他的布局——那已经越界了，宁可多开一个窗口。
        if target and _on_secondary(target):
            if verbose:
                print(f"  没在播放 → 复用副屏上已有的 Chrome hwnd={target} 开 {name}")
            place(target, position, size)
            _open_url(target, url)
            _save(target, name)
            return target
        if verbose and target:
            print("  现有 Chrome 在主屏，不动它 → 新开窗口")

    hwnd = _spawn(url, position, size, timeout, verbose)
    if hwnd:
        _save(hwnd, name)
    return hwnd
