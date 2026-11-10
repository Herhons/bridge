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

判断"当前标签是不是目标页"靠窗口标题和 URL（Chrome 的标题 = 当前标签标题）。

2026-11-01 CARD-WEB 修的两条：
  · **新窗口必须提到前台再判页。** Chrome 在后台时页面文档的 URL 读不到
    （见 qi/ui.py 开头），旧代码用 SW_SHOWMINNOACTIVE 开窗后直接轮询 URL，
    永远判"不是目标页"，25 秒后报 no_window 还留一个窗口在桌面。
  · **绑不上就把自己开的窗口关掉**，不留垃圾窗口。
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


def _declare_win32():
    """声明句柄参数类型。

    2026-11-01 CARD-WEB：64 位下句柄可以大于 2^31，ctypes 不声明就按 32 位 int 传，
    抛 `OverflowError: int too long to convert`。枚举窗口时正好会碰到这种句柄
    （实测同一天 opencode_use --list 就是这么崩的）。
    """
    from ctypes import POINTER, c_int, c_uint

    user32.IsWindowVisible.argtypes, user32.IsWindowVisible.restype = [wintypes.HWND], wintypes.BOOL
    user32.IsWindow.argtypes, user32.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.IsIconic.argtypes, user32.IsIconic.restype = [wintypes.HWND], wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, c_int]
    user32.GetWindowTextW.restype = c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.BringWindowToTop.argtypes, user32.BringWindowToTop.restype = [wintypes.HWND], wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, c_int, c_int,
                                    c_int, c_int, c_uint]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, c_uint, ctypes.c_size_t, ctypes.c_size_t]
    user32.PostMessageW.restype = wintypes.BOOL
    shell32.ShellExecuteW.restype = ctypes.c_void_p


_declare_win32()

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SW_SHOWMINNOACTIVE = 7
SW_RESTORE = 9
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
WM_CLOSE = 0x0010

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
    """摆到指定位置和大小。不激活。

    2026-11-02 CARD-06a (c)：`ShowWindow(SW_RESTORE)` 的返回值不可信
    （它返回"调用前是否可见"），所以恢复后要轮询确认窗口真的脱离了最小化，
    再摆位置。恢复不了就不摆——摆一个还在最小化的窗口没有意义。
    """
    from . import window as window_mod

    window_mod.restore(int(hwnd), timeout=4.0)
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


def close(hwnd: int, *, timeout: float = 6.0) -> bool:
    """关掉**脚本自己开的**窗口（用户的窗口不许走这里）。

    新窗口绑定失败后要收尾：巡检 06:0x 那轮留下 2 个 Gemini + 1 个 Grok
    窗口没人关，就是这么堆起来的。返回是否真的关掉了。
    """
    try:
        hwnd = int(hwnd)
        if not user32.IsWindow(hwnd):
            return True
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    except Exception:                       # noqa: BLE001 - 关不掉只是留个窗口，不该抛
        return False
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.2)
        if not user32.IsWindow(hwnd):
            return True
    return not user32.IsWindow(hwnd)


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
    if not _focus(hwnd):
        raise RuntimeError("浏览器窗口无法获得焦点")
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
    rc = shell32.ShellExecuteW(None, "open", CHROME, arg, None, SW_SHOWMINNOACTIVE) or 0
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
        # 可能只是慢半拍：把这段时间里冒出来的新窗口一并关掉，不留垃圾。
        time.sleep(1.0)
        for late in _chrome_hwnds() - before:
            close(late)
        if verbose:
            print("  没等到新窗口")
        return None
    place(hwnd, position, size)
    # 2026-11-01 CARD-WEB：开完必须提到前台。Chrome 后台窗口读不到页面 URL，
    # 不置前就会一直判"不是目标页"（旧代码在这里白等 25 秒然后报 no_window）。
    if not _focus(hwnd) and verbose:
        print(f"  新窗口 hwnd={hwnd} 提不到前台")
    _wait_ready(hwnd)
    if verbose:
        print(f"  新窗口 hwnd={hwnd}")
    return hwnd


def ensure_site(name: str, *, tab: int | None = None, continue_session: bool = False,
                position=DEFAULT_POS, size=DEFAULT_SIZE,
                timeout: float = 25.0, verbose: bool = True) -> int | None:
    """绑定实际站点 URL。续聊只选择现有标签，绝不打开首页。"""
    from .audio import probe as audio_probe
    from . import ui
    from .screen import Screen
    from urllib.parse import urlsplit

    if name not in SITES:
        raise KeyError(f"未知站点 {name}，可选 {list(SITES)}")
    url, keyword = SITES[name]

    def docs_of(hwnd):
        try:
            return ui.documents(hwnd)
        except Exception as exc:            # noqa: BLE001 - 只用于判断，下面会走"没就绪"
            return exc

    def correct_page(hwnd):
        found = docs_of(hwnd)
        if isinstance(found, Exception):
            return False
        return any(value and urlsplit(value).hostname == ui.HOSTS[name]
                   for _, value in found)

    def title_matches(hwnd):
        """URL 读不到时的退路：Chrome 的标题就是当前标签标题。"""
        return keyword in _title(hwnd).lower()

    def page_ready(hwnd):
        """目标页是否已经在眼前。

        先看 URL；URL 读不到（Chrome 窗口不在前台时的常态）时看标题。
        **不能把"读不到 URL"直接当成"没打开"**——旧代码就是这么误判的。
        """
        found = docs_of(hwnd)
        if isinstance(found, Exception):
            return False
        if any(value and urlsplit(value).hostname == ui.HOSTS[name]
               for _, value in found):
            return True
        if len(found) == 1 and not found[0][1]:
            return title_matches(hwnd)
        return False

    def why(hwnd):
        """失败时一行说清现场，别只留个 no_window。"""
        found = docs_of(hwnd)
        if isinstance(found, Exception):
            docs = f"读取失败 {type(found).__name__}: {found}"
        else:
            docs = "；".join(value or "(URL 未知)" for _, value in found) or "没有文档"
        return f"{ui.window_facts(hwnd)}；文档：{docs}"

    def await_page(hwnd, seconds=12.0):
        """等页面真的变成目标站。

        标签页标题会被会话名替换（ChatGPT 的标题曾经是「回归测试标记」），
        所以「按标题找不到」不能推出「这个站没开着」；新开的标签也不是
        立刻就能读到 URL。这里统一按 URL 轮询，URL 读不到才退回标题。
        """
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if page_ready(hwnd):
                return True
            time.sleep(0.5)
        return False

    def select_existing(hwnd):
        from . import window as window_mod
        # 最小化时 UIA 枚举标签页会读空，先恢复（轮询确认，不看返回值）
        window_mod.restore(int(hwnd), timeout=4.0)
        s = Screen(verbose=verbose)
        s.bind(hwnd)
        if not s.focus():
            return False
        if tab is None and correct_page(hwnd):
            return True
        items = ui.tabs(hwnd)
        if tab is not None:
            if not 1 <= tab <= len(items):
                return False
            target = items[tab - 1]
        else:
            matches = [item for item in items if keyword in item.name.lower()]
            if len(matches) != 1:
                # 标题常常已经变成会话名：挨个试，命中 URL 就算（别去开重复标签）
                others = [k for s2, (_, k) in SITES.items() if s2 != name]
                for item in items:
                    if any(k in item.name.lower() for k in others):
                        continue
                    item.control.GetSelectionItemPattern().Select()
                    if await_page(hwnd, 3.0):
                        return True
                return False
            target = matches[0]
        target.control.GetSelectionItemPattern().Select()
        return await_page(hwnd, timeout)

    old = current()
    if old:
        if select_existing(old):
            if verbose:
                print(f"  复用窗口 hwnd={old}（当前已是 {name}）")
            _save(old, name)
            return old
        if continue_session or tab is not None:
            return None
        if verbose:
            print(f"  复用窗口 hwnd={old}，在里面开 {name}")
        _open_url(old, url)
        if not await_page(old):
            # 这是用户的窗口，即使没打开也不能关；只把现场说清楚。
            if verbose:
                print(f"  在复用窗口里没打开 {name}（{why(old)}），窗口是用户的，不动它")
            return None
        _save(old, name)
        return old

    if continue_session or tab is not None:
        return None

    info = audio_probe()
    playing = info["playing"] if info["available"] else True
    if verbose:
        if info["available"]:
            print(f"  Chrome 在播音频: {playing}")
        else:
            print(f"  音频检测不可用，按「正在播放」处理 → 新开窗口（{info['hint']}）")

    if not playing:
        target = _frontmost_chrome() or _largest_chrome()
        # 只复用**已经在副屏**的窗口。主屏那个是用户自己的，把它挪到副屏等于
        # 改了他的布局——那已经越界了，宁可多开一个窗口。
        if target and _on_secondary(target):
            if verbose:
                print(f"  没在播放 → 复用副屏上已有的 Chrome hwnd={target} 开 {name}")
            place(target, position, size)
            _open_url(target, url)
            if not await_page(target):
                if verbose:
                    print(f"  复用窗口里没打开 {name}（{why(target)}），窗口是用户的，不动它")
                return None
            _save(target, name)
            return target
        if verbose and target:
            print("  现有 Chrome 在主屏，不动它 → 新开窗口")

    hwnd = _spawn(url, position, size, timeout, verbose)
    if hwnd and await_page(hwnd):
        _save(hwnd, name)
        return hwnd
    if hwnd:
        # 自己开的窗口绑不上就收尾，别留在桌面上。
        if verbose:
            print(f"  绑定 {name} 失败，关掉自己开的窗口 hwnd={hwnd}（{why(hwnd)}）")
        close(hwnd)
    return None
