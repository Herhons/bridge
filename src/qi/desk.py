"""桌面端内核：按**进程名**找窗口，启动/复用，置前。

为什么不用窗口标题：
    · codex 桌面端的进程是 `ChatGPT.exe`、窗口标题也是 `ChatGPT`，
      而 Chrome 里开着 chatgpt.com 时窗口标题是 `ChatGPT - Google Chrome`，
      按标题子串匹配会抓错（v0.0.8 的 bench 里 codex 那条基线就是这么废掉的）。
    · dsh 的标题会变成会话名（`List sessions command — DeepSeek Harness`），
      前缀不固定。
所以一律走 GetWindowThreadProcessId + 进程可执行文件名。

四端共同的界面结构（2026-10-29 实测截图确认）：
    左：+ 按钮        中/右：模型芯片 ∨   麦克风   发送圆钮
    发送键颜色：codex 橙 / dsh 蓝 / opencode 灰 / workbuddy 黑
"""

from __future__ import annotations

import ctypes
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

from . import uilock

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SWP_NOACTIVATE = 0x0010
#: 恢复并激活窗口（不改位置/大小）。前台是桌面时，只有它能让 SetForegroundWindow 生效。
SW_RESTORE = 9
SWP_SHOWWINDOW = 0x0040
WM_CLOSE = 0x0010

# 每端：进程可执行文件名 + 启动用的 shell 目标（已安装应用的 AppID）
APPS: dict[str, dict] = {
    "codex": {
        "proc": "ChatGPT.exe",
        "title_hint": "ChatGPT",
        "launch": r"shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App",
    },
    "dsh": {
        "proc": "DeepSeek Harness.exe",
        "title_hint": "DeepSeek Harness",
        "launch": r"shell:AppsFolder\electron.app.DeepSeek Harness",
    },
    "opencode": {
        "proc": "OpenCode.exe",
        "title_hint": "OpenCode",
        "launch": r"shell:AppsFolder\ai.opencode.desktop",
    },
    "workbuddy": {
        "proc": "WorkBuddy.exe",
        "title_hint": "WorkBuddy",
        # 通常已经开着。CARD-09e 第 2 条要"重启一次"，所以这里必须能启动：
        # AppID 来自 `Get-StartApps`（2026-11-02 查得 `WorkBuddy.WorkBuddy`），
        # 和另外三端一样走 shell:AppsFolder；exe 全路径作兜底（restart.py 会用）。
        "launch": r"shell:AppsFolder\WorkBuddy.WorkBuddy",
    },
}


def _pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _proc_name(pid: int) -> str:
    """pid -> 可执行文件名（不含路径）。取不到返回空串。"""
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


def _proc_path(pid: int) -> str:
    """pid -> 可执行文件全路径。取不到返回空串。

    CARD-09e 第 2 条要用它：WorkBuddy 没有 AppID 可走（`APPS["workbuddy"]["launch"]`
    是 None），重启只能按"它自己现在这个 exe 的路径"再拉一次。
    """
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(2048)
        size = wintypes.DWORD(2048)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        kernel32.CloseHandle(h)


def pid_of(hwnd: int) -> int:
    """窗口句柄 -> 进程号。"""
    return _pid_of(hwnd)


def exe_path(hwnd: int) -> str:
    """窗口所属进程的可执行文件全路径（重启时要用）。"""
    return _proc_path(_pid_of(hwnd))


def _visible_windows() -> list[tuple[int, str, tuple[int, int, int, int]]]:
    out: list[tuple[int, str, tuple[int, int, int, int]]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if n > 0:
            b = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, b, n + 1)
            title = b.value
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        out.append((int(hwnd), title,
                    (r.left, r.top, r.right - r.left, r.bottom - r.top)))
        return True

    user32.EnumWindows(cb, 0)
    return out


def find_app(name: str, *, min_size: tuple[int, int] = (400, 300)) -> int | None:
    """按进程名找一个该应用的**可见主窗口**，多个时取面积最大的。

    桌面应用常带几个隐藏的辅助窗口（Electron 的 message window 之类），
    所以要求「可见 + 有尺寸 + 面积最大」。

    2026-11-02 CARD-06a (c)：最小化时 GetWindowRect 给的是哨兵矩形
    `(-32000,-32000,160,28)`，宽高过不了这里的门槛，于是整个应用被判成"没开"。
    现在先用 `window.restored_rect()` 试一次——它会恢复最小化窗口并轮询确认；
    恢复不了才跳过。**不拿 ShowWindow 的返回值当结果。**
    """
    from . import window as window_mod

    spec = APPS.get(name)
    if spec is None:
        raise KeyError(f"未知的桌面端: {name}")
    want = spec["proc"].lower()
    best, area = None, 0
    for hwnd, _title, rect in _visible_windows():
        # 先认进程再动尺寸：恢复窗口是有副作用的动作，不能对**别的程序**的
        # 最小化窗口来做（那会把用户自己的窗口弹出来）。
        if _proc_name(_pid_of(hwnd)).lower() != want:
            continue
        w, h = rect[2], rect[3]
        if w < min_size[0] or h < min_size[1]:
            fixed = window_mod.restored_rect(hwnd, timeout=3.0, min_size=min_size)
            if not fixed:
                continue
            rect = fixed
            w, h = rect[2], rect[3]
        a = w * h
        if a > area:
            area, best = a, hwnd
    return best


def running(name: str) -> bool:
    return find_app(name) is not None


def launch(name: str) -> bool:
    """启动应用（用 shell:AppsFolder，MSIX 和普通 exe 都适用）。"""
    spec = APPS[name]
    if not spec.get("launch"):
        return False
    subprocess.Popen(["explorer.exe", spec["launch"]])
    return True


def launch_exe(path: str) -> bool:
    """按可执行文件全路径启动（给没有 AppID 的 WorkBuddy 用，CARD-09e 第 2 条）。"""
    if not path:
        return False
    subprocess.Popen([path], cwd=str(Path(path).parent))
    return True


def close_window(hwnd: int) -> bool:
    """请求"正常关闭"：往主窗口发 WM_CLOSE，返回是否发出去了。

    不杀进程（`TerminateProcess` 会跳过应用的落盘/清理）；关不关得掉由
    `wait_closed` 轮询确认——真正判据是窗口/进程有没有消失，不是这里的返回值。
    """
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    try:
        return bool(user32.PostMessageW(int(hwnd), WM_CLOSE, 0, 0))
    except Exception:                       # noqa: BLE001 - 发不出去按"没关成"处理
        return False


def wait_closed(hwnd: int, *, timeout: float = 20.0, poll: float = 0.4) -> bool:
    """等窗口真的关掉。进程还在但窗口没了也算关掉（托盘程序就是这样）。"""
    end = time.time() + timeout
    while time.time() < end:
        if not user32.IsWindow(int(hwnd)):
            return True
        time.sleep(poll)
    return not user32.IsWindow(int(hwnd))


def wait_window(name: str, *, timeout: float = 30.0, poll: float = 1.0) -> int | None:
    """等应用的可见主窗口出现。"""
    end = time.time() + timeout
    while time.time() < end:
        hwnd = find_app(name)
        if hwnd is not None:
            return hwnd
        time.sleep(poll)
    return find_app(name)


def ensure_app(name: str, *, wait: float = 25.0, verbose: bool = True) -> int | None:
    """没有就启动，然后等窗口出现。返回 hwnd 或 None。"""
    hwnd = find_app(name)
    if hwnd is not None:
        if verbose:
            print(f"  复用已有 {name} 窗口 hwnd={hwnd}")
        return hwnd
    if not launch(name):
        return None
    if verbose:
        print(f"  启动 {name} …")
    end = time.time() + wait
    while time.time() < end:
        time.sleep(1.0)
        hwnd = find_app(name)
        if hwnd is not None:
            if verbose:
                print(f"  起来了 hwnd={hwnd}")
            return hwnd
    return None


def activate(hwnd: int, *, tries: int = 3) -> bool:
    """把窗口提到前台，返回是否成功。

    并发模式（`BRIDGE_UI_CONCURRENT=1`）下这一步也要进锁：抢前台是**共享资源**，
    两个席位同时 SetForegroundWindow 会互相把对方挤下去，谁的点按都抛
    ForegroundLost（2026-11-08 真机：opencode 的 choose 被 codex 抢走前台）。
    """
    if uilock.concurrent_mode():
        with uilock.section("desk:activate", timeout=uilock.SECTION_TIMEOUT):
            return _activate_locked(hwnd, tries)
    return _activate_locked(hwnd, tries)


def _activate_locked(hwnd: int, tries: int = 3) -> bool:
    """把窗口提到前台，返回是否成功。

    SetForegroundWindow 经常被系统拒绝（前台锁：只有「当前前台进程」或
    「刚收到用户输入的进程」才能改前台）。实测复用时 100% 失败——
    窗口明明活着，但前台一直是 WorkBuddy，之后每一次点击都会被
    _assert_foreground 拦掉。

    三个办法按顺序试：
      1. 直接 SetForegroundWindow；
      2. 轻敲一下 Alt 再 SetForegroundWindow —— Alt 会解除前台锁，
         这是不依赖 AttachThreadInput 的常用做法；
      3. topmost 闪一下（HWND_TOPMOST 再 HWND_NOTOPMOST）。
    """
    # 必须先 SW_RESTORE：2026-11-08 实测，前台是桌面（WorkerW）时，
    # 即使 IsIconic 为假，只调 SetForegroundWindow 也一律失败。
    user32.ShowWindow(int(hwnd), SW_RESTORE)
    for i in range(tries):
        if user32.GetForegroundWindow() == int(hwnd):
            return True
        user32.SetForegroundWindow(int(hwnd))
        time.sleep(0.25)
        if user32.GetForegroundWindow() == int(hwnd):
            return True
        if i == 0:
            # 轻敲 Alt 解除前台锁
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            time.sleep(0.05)
        elif i == 1:
            user32.SetWindowPos(int(hwnd), -1, 0, 0, 0, 0, 0x0001 | 0x0002)
            time.sleep(0.2)
            user32.SetWindowPos(int(hwnd), -2, 0, 0, 0, 0, 0x0001 | 0x0002)
            time.sleep(0.2)
    return user32.GetForegroundWindow() == int(hwnd)


def ensure_foreground(hwnd: int, *, tries: int = 5, gap: float = 0.2) -> bool:
    """并发下把某个端抢到前台，失败重试。

    为什么需要：同一时刻只有一个前台，4 席会互相把对方挤下去。
    `activate()` 单次成功不代表稳定（抢完可能立刻被另一个席抢走），
    所以这里"进 section → 抢 → 复核 → 重试"。
    """
    with uilock.section(f"bring:{int(hwnd)}", timeout=uilock.SECTION_TIMEOUT):
        for _ in range(max(1, tries)):
            if _activate_locked(hwnd, tries=1):
                return True
            time.sleep(gap)
        return user32.GetForegroundWindow() == int(hwnd)


def place(hwnd: int, pos: tuple[int, int], size: tuple[int, int]) -> None:
    """挪窗口到指定位置和尺寸（不激活）。"""
    user32.SetWindowPos(int(hwnd), 0, int(pos[0]), int(pos[1]),
                        int(size[0]), int(size[1]),
                        SWP_NOACTIVATE | SWP_SHOWWINDOW)


def rect_of(hwnd: int) -> tuple[int, int, int, int] | None:
    r = wintypes.RECT()
    if not user32.GetWindowRect(int(hwnd), ctypes.byref(r)):
        return None
    if r.right - r.left <= 0:
        return None
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)
