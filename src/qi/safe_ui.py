"""安全 UI 原语：任何「点一下 / 按一下」都必须先证明目标归属。

为什么单独一个模块：2026-11-12 两次真机事故都是「按屏幕坐标点 + 全局按键」在焦点
不确定时打到了桌面（一次点到「删除」，一次 Ctrl+A/Delete 选中 155 个图标）。
根因不是某个 if 写漏了，而是**默认允许盲点盲按**。这里把判定提成纯函数：

* `decide_point(...)`：点之前，目标点必须落在目标窗口的客户区内、命中的窗口属于
  目标进程、且不是桌面/任务栏那几类窗口。
* `decide_keys(...)`：发键之前，前台必须是目标窗口，且（危险键）焦点必须落在
  可编辑控件上；发完再核一次前台。

纯函数部分不碰 Win32，可在离线单测里直接构造各种「打偏」场景。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

#: 桌面 / 任务栏类窗口：点到它们就是事故。
FORBIDDEN_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                     "SysListView32"}
#: 危险键：会把「全选 + 删除」这类动作发到当前焦点。
DANGEROUS_KEYS = {frozenset({"ctrl", "a"}), frozenset({"ctrl", "x"}),
                  frozenset({"delete"}), frozenset({"shift", "delete"})}


@dataclass(frozen=True)
class PointVerdict:
    ok: bool
    reason: str

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason}


def decide_point(*, x: int, y: int, client_rect, hit_pid, hit_class: str,
                 target_pid: int, hit_top_hwnd=None, target_hwnd=None,
                 expected_rect=None, current_rect=None) -> PointVerdict:
    """点 (x,y) 是否安全。`client_rect` = (left, top, right, bottom)。

    `hit_top_hwnd`/`target_hwnd`：命中窗口的**顶层**必须是目标窗口——
    同进程的弹窗/对话框（同 pid）也能通过 pid 校验，只有比顶层才拦得住。
    `expected_rect`/`current_rect`：布局漂移（窗口移动/重排/DPI 变）时点会打偏，
    两值不等就拒。
    """
    if (expected_rect is not None and current_rect is not None
            and tuple(expected_rect) != tuple(current_rect)):
        return PointVerdict(False, "layout_moved")
    if hit_top_hwnd is not None and target_hwnd is not None and hit_top_hwnd != target_hwnd:
        return PointVerdict(False, "other_top_window")
    left, top, right, bottom = client_rect
    if not (left <= x < right and top <= y < bottom):
        return PointVerdict(False, "outside_client")
    if (hit_class or "") in FORBIDDEN_CLASSES:
        return PointVerdict(False, f"forbidden_window:{hit_class}")
    if hit_pid is None or hit_pid != target_pid:
        return PointVerdict(False, "other_process")
    return PointVerdict(True, "ok")


def is_dangerous(keys: Iterable[str]) -> bool:
    return frozenset(k.lower() for k in keys) in DANGEROUS_KEYS


def decide_keys(*, keys, foreground_hwnd, target_hwnd, focused_role: str | None,
                after_foreground=None) -> PointVerdict:
    """发键是否安全。

    `focused_role` 是 UIA 焦点控件的 ControlTypeName（读不到给 None）。
    `after_foreground` 给了就做「发完再核一次」的判定。
    """
    keys = list(keys)
    if foreground_hwnd != target_hwnd:
        return PointVerdict(False, "foreground_not_target")
    if is_dangerous(keys):
        if focused_role not in ("EditControl", "DocumentControl"):
            return PointVerdict(False, "focused_not_editable")
    if after_foreground is not None and after_foreground != target_hwnd:
        return PointVerdict(False, "focus_moved_after_keys")
    return PointVerdict(True, "ok")


# ---------------------------------------------------------------- Win32 薄包装
def _win32():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    return ctypes, wintypes, user32


def class_of(hwnd) -> str:
    _c, wintypes, user32 = _win32()
    import ctypes

    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def pid_of(hwnd) -> int | None:
    _c, wintypes, user32 = _win32()
    import ctypes

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value) or None


def client_rect(hwnd):
    _c, wintypes, user32 = _win32()
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    return (origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom)


def point_verdict(hwnd, x: int, y: int) -> PointVerdict:
    """真机版：查窗口类、窗口归属进程、客户区。"""
    _c, wintypes, user32 = _win32()

    rect = client_rect(hwnd)
    if rect is None:
        return PointVerdict(False, "no_client_rect")
    hit = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    if not hit:
        return PointVerdict(False, "no_window_at_point")
    top = user32.GetAncestor(hit, 2) or hit          # GA_ROOT=2
    return decide_point(x=int(x), y=int(y), client_rect=rect,
                        hit_pid=pid_of(hit), hit_class=class_of(hit),
                        target_pid=pid_of(hwnd) or -1,
                        hit_top_hwnd=top, target_hwnd=hwnd)


def safe_click(hwnd, x: int, y: int) -> PointVerdict:
    """校验通过才点。返回判定，调用方按 `ok` 决定是否继续。"""
    verdict = point_verdict(hwnd, x, y)
    if not verdict.ok:
        return verdict
    _c, _w, user32 = _win32()
    user32.SetCursorPos(int(x), int(y))
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    return verdict
