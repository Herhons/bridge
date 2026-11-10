"""窗口最小化 → 恢复，并且**用实测结果**确认真的恢复了（CARD-06a (c)）。

## 为什么单独一个模块

最小化窗口的 `GetWindowRect` 返回的是哨兵矩形 `(-32000, -32000, 160, 28)`。
旧代码各写各的：`Screen.window_rect` 直接把它当矩形用，于是
`grok_choose --list` 在 `move_to(win[0] + 30, win[1] + 120)` 上抛
`光标没能移到 (-31970,-31880)`；`desk.find_app` 按"宽高 ≥ 400×300"过滤，
最小化的窗口直接被判成"这个应用没开"。

## 两条硬规则

1. **不看 `ShowWindow` 的返回值。** 它返回的是"调用前是否可见"，不是"恢复成功"。
   唯一可信的判据是轮询到 `IsIconic == False` **且**矩形不再是哨兵、宽高大于阈值。
2. **失败不抛异常到用户面前。** `restore()` 自己只返回 facts，调用方按
   `ok=False` 走 `stage=window_minimized`（退出码 7）。
   `ensure()` 是给"必须拿到矩形才能继续"的老代码用的，它会抛
   `WindowMinimized`——但那个异常带着 stage/code，CLI 层统一转成 7。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.windll.user32

SW_RESTORE = 9
SENTINEL = -32000                 # 最小化时 GetWindowRect 的哨兵坐标
MIN_W, MIN_H = 200, 120           # 恢复后至少要有这么大，否则不算恢复


def _declare() -> None:
    user32.IsWindow.argtypes, user32.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.IsIconic.argtypes, user32.IsIconic.restype = [wintypes.HWND], wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL


_declare()


class WindowMinimized(RuntimeError):
    """窗口最小化且没恢复过来。CLI 层转成 `stage=window_minimized` / 退出码 7。"""

    stage = "window_minimized"
    code = 7

    def __init__(self, message: str, facts: dict | None = None):
        super().__init__(message)
        self.facts = dict(facts or {})


class _Win32:
    """默认实现。测试可以换掉它，从而离线跑"恢复失败 / 第二次才成功"。"""

    def is_window(self, hwnd) -> bool:
        return bool(user32.IsWindow(int(hwnd)))

    def is_iconic(self, hwnd) -> bool:
        return bool(user32.IsIconic(int(hwnd)))

    def show(self, hwnd, cmd: int) -> bool:
        return bool(user32.ShowWindow(int(hwnd), int(cmd)))

    def rect(self, hwnd):
        r = wintypes.RECT()
        if not user32.GetWindowRect(int(hwnd), ctypes.byref(r)):
            return None
        return (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))


_API = _Win32()


def raw_rect(hwnd, api=None) -> tuple[int, int, int, int] | None:
    """原样返回 GetWindowRect（可能是哨兵矩形）。窗口没了返回 None。"""
    api = api or _API
    try:
        return api.rect(int(hwnd))
    except Exception:                       # noqa: BLE001 - 句柄失效只算读不到
        return None


def is_minimized(hwnd, api=None) -> bool:
    api = api or _API
    try:
        return bool(api.is_iconic(int(hwnd)))
    except Exception:                       # noqa: BLE001
        return False


def usable(rect, *, min_w: int = MIN_W, min_h: int = MIN_H) -> bool:
    """矩形能不能用来找图/点：不是哨兵、宽高过阈值。

    只判 `w > 0` 不够：最小化时 Chrome 给的是 160x28 这种"小但正"的尺寸。
    """
    if not rect:
        return False
    x, y, w, h = rect
    if x <= SENTINEL + 1 or y <= SENTINEL + 1:
        return False
    return w >= min_w and h >= min_h


def restore(hwnd, *, timeout: float = 5.0, interval: float = 0.15,
            min_size: tuple[int, int] = (MIN_W, MIN_H), api=None) -> dict:
    """恢复最小化窗口并**轮询确认**。绝不抛异常，结果全在 facts 里。

    返回 facts：
        hwnd / was_iconic / show_called / show_return / ok / rect / waited / reason
    `reason`：ok=True 时为 "already"（本来就没最小化）或 "restored"；
              ok=False 时为 "gone"（窗口没了）/"iconic"（还是最小化）/
              "sentinel"（矩形仍是哨兵）/"too_small"（宽高不够）。
    """
    api = api or _API
    hwnd = int(hwnd)
    facts = {"hwnd": hwnd, "was_iconic": False, "show_called": False,
             "show_return": None, "ok": False, "rect": None, "waited": 0.0,
             "reason": ""}
    started = time.monotonic()

    if not api.is_window(hwnd):
        facts["reason"] = "gone"
        return facts

    iconic = api.is_iconic(hwnd)
    facts["was_iconic"] = bool(iconic)
    if iconic:
        facts["show_called"] = True
        try:
            facts["show_return"] = bool(api.show(hwnd, SW_RESTORE))
        except Exception:                   # noqa: BLE001 - 返回值不可信，失败也不抛
            facts["show_return"] = None

    while True:
        rect = raw_rect(hwnd, api)
        still_iconic = api.is_iconic(hwnd)
        facts["rect"] = list(rect) if rect else None
        facts["waited"] = round(time.monotonic() - started, 2)
        if not still_iconic and usable(rect, min_w=min_size[0], min_h=min_size[1]):
            facts["ok"] = True
            facts["reason"] = "restored" if facts["was_iconic"] else "already"
            return facts
        if time.monotonic() - started >= timeout:
            facts["ok"] = False
            if not rect:
                facts["reason"] = "gone"
            elif still_iconic:
                facts["reason"] = "iconic"
            elif rect[0] <= SENTINEL + 1 or rect[1] <= SENTINEL + 1:
                facts["reason"] = "sentinel"
            else:
                facts["reason"] = "too_small"
            return facts
        time.sleep(interval)


def ensure(hwnd, *, timeout: float = 5.0, interval: float = 0.15,
           min_size: tuple[int, int] = (MIN_W, MIN_H), api=None) -> dict:
    """同 `restore`，但失败时抛 `WindowMinimized`（带 facts）。

    给"没有矩形就没法继续"的地方用。CLI 层捕获后返回退出码 7，
    不会把 traceback 甩到用户脸上。
    """
    facts = restore(hwnd, timeout=timeout, interval=interval, min_size=min_size, api=api)
    if not facts["ok"]:
        raise WindowMinimized(
            f"窗口 hwnd={hwnd} 最小化且没能恢复（reason={facts['reason']}，"
            f"rect={facts['rect']}，等了 {facts['waited']}s）", facts)
    return facts


def restored_rect(hwnd, *, timeout: float = 5.0, min_size: tuple[int, int] = (MIN_W, MIN_H),
                  api=None):
    """取矩形；最小化就先恢复再取。拿不到可用矩形返回 None（不抛）。

    各脚本的 `find_window()` 都按"宽高 ≥ 400×300"过滤候选窗口，
    直接把 `rect_of(hwnd)` 换成这个函数，最小化的窗口就不会再被判成"没开"。
    """
    rect = raw_rect(hwnd, api)
    if usable(rect, min_w=min_size[0], min_h=min_size[1]):
        return rect
    if is_minimized(hwnd, api) or (rect and (rect[0] <= SENTINEL + 1)):
        facts = restore(hwnd, timeout=timeout, min_size=min_size, api=api)
        if facts["ok"]:
            return tuple(facts["rect"])          # type: ignore[arg-type]
    return None
