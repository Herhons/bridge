"""Window discovery with watchdog timeout, FindWindow fallback, and app/URL launch helpers."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

#: Primary screen starts at x=0 and grows right; the automation screen is the
#: one left of it (negative x). Never hardcode a positive origin for window
#: moves — that lands on whatever the operator is watching.
PRIMARY_SCREEN_X = 0
#: Measured secondary desktop: 1440x2560 portrait left of the primary screen.
SECONDARY_BOUNDS = (-1440, -653, 0, 1907)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


class FocusTakenError(RuntimeError):
    """Raised by :class:`FocusGuard` when automation stole primary-screen focus."""


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    process_name: str
    pid: int
    class_name: str = ""


def _enum_windows_raw() -> list[WindowInfo]:
    import ctypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    results: list[WindowInfo] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        proc = ""
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if hproc:
            try:
                pbuf = ctypes.create_unicode_buffer(260)
                psize = ctypes.c_ulong(260)
                if kernel32.QueryFullProcessImageNameW(hproc, 0, pbuf, ctypes.byref(psize)):
                    proc = os.path.basename(pbuf.value)
            finally:
                kernel32.CloseHandle(hproc)
        results.append(WindowInfo(
            hwnd=int(hwnd), title=title, process_name=proc,
            pid=int(pid.value), class_name=cls_buf.value,
        ))
        return True

    user32.EnumWindows(_cb, 0)
    return results


def _find_window_fallback(title_hints: list[str] | None = None) -> list[WindowInfo]:
    """Fallback when EnumWindows is blocked by third-party shell hooks (fixes W9)."""
    if os.name != "nt" or not title_hints:
        return []
    import ctypes

    user32 = ctypes.windll.user32
    out: list[WindowInfo] = []
    for title in title_hints:
        hwnd = user32.FindWindowW(None, str(title))
        if hwnd:
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.append(WindowInfo(hwnd=int(hwnd), title=str(title), process_name="", pid=int(pid.value)))
    return out


def list_windows(*, timeout: float = 3.0, fallback_titles: list[str] | None = None) -> list[WindowInfo]:
    """Enumerate visible top-level Windows with a watchdog timeout (prevents 20s+ hang)."""
    if os.name != "nt":
        return []
    box: dict[str, list[WindowInfo]] = {}

    def _worker():
        try:
            box["windows"] = _enum_windows_raw()
        except Exception:
            box["windows"] = []

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if "windows" in box:
        return box["windows"]
    return _find_window_fallback(fallback_titles)


def find_window(
    *,
    process_names: list[str] | None = None,
    title_contains: list[str] | None = None,
    exclude_processes: list[str] | None = None,
    window_class: str | None = None,
    windows: list[WindowInfo] | None = None,
) -> WindowInfo | None:
    """Find the best matching window by process name and/or title keywords."""
    items = windows if windows is not None else list_windows(fallback_titles=title_contains)
    proc_set = {p.lower() for p in (process_names or [])}
    excl_set = {p.lower() for p in (exclude_processes or [])}
    title_keys = [t.lower() for t in (title_contains or [])]
    class_key = (window_class or "").strip().lower()

    candidates: list[WindowInfo] = []
    for win in items:
        pname = win.process_name.lower()
        wtitle = win.title.lower()
        if excl_set and pname in excl_set:
            continue
        if proc_set and pname and pname not in proc_set:
            continue
        if title_keys and not any(k in wtitle for k in title_keys):
            continue
        if class_key and (win.class_name or "").lower() != class_key:
            continue
        candidates.append(win)
    return candidates[0] if candidates else None


def focus_window(hwnd: int) -> bool:
    if os.name != "nt":
        return False
    import ctypes

    SW_RESTORE = 9
    user32 = ctypes.windll.user32
    if user32.IsIconic(int(hwnd)):
        user32.ShowWindow(int(hwnd), SW_RESTORE)
        time.sleep(0.2)
    return bool(user32.SetForegroundWindow(int(hwnd)))


def launch_url_in_chrome(url: str, chrome_path: str) -> bool:
    if os.name != "nt" or not os.path.isfile(chrome_path):
        return False
    subprocess.Popen([chrome_path, "--new-window", "--force-renderer-accessibility", url], close_fds=True)
    return True


def launch_app_id(app_id: str) -> bool:
    """Launch a registered Windows Start App via ``explorer.exe shell:AppsFolder\\<AppID>``."""
    if os.name != "nt" or not app_id:
        return False
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"], close_fds=True)
    return True


# ---------------------------------------------------------------- focus guard
def foreground_hwnd() -> int:
    """Handle of the current foreground window (0 off Windows or on failure)."""
    if os.name != "nt":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """``(left, top, right, bottom)`` of a window, or None when unknown."""
    if os.name != "nt" or not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
            return None
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:
        return None


def is_on_primary_screen(rect: tuple[int, int, int, int] | None) -> bool:
    """True when the window's horizontal centre sits on x >= 0 (primary side)."""
    if not rect:
        return False
    left, _top, right, _bottom = rect
    return ((left + right) / 2.0) >= PRIMARY_SCREEN_X


class FocusGuard:
    """Snapshots the foreground window around an action and flags theft.

    The red line is "never take focus away from the operator's primary screen".
    Automation legitimately works on the negative-x screen, so the guard only
    complains when the *before* window belonged to the primary side and the
    *after* window belongs somewhere else. ``strict=True`` turns the diagnostic
    into a hard :class:`FocusTakenError`; otherwise the event is recorded and
    the caller carries on (default, so a legit window switch is not fatal).

    ``fg_fn`` / ``rect_fn`` are injectable for offline tests.
    """

    def __init__(
        self,
        *,
        strict: bool = False,
        fg_fn: Callable[[], int] | None = None,
        rect_fn: Callable[[int], tuple[int, int, int, int] | None] | None = None,
    ):
        self.strict = bool(strict)
        self._fg = fg_fn or foreground_hwnd
        self._rect = rect_fn or window_rect
        self.before = 0
        self.after = 0
        self.before_on_primary = False
        self.taken = False
        self.diagnostics: list[str] = []

    def __enter__(self) -> "FocusGuard":
        try:
            self.before = int(self._fg() or 0)
        except Exception:
            self.before = 0
        self.before_on_primary = (
            is_on_primary_screen(self._rect(self.before)) if self.before else False
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            return False  # never swallow the caller's exception
        try:
            self.after = int(self._fg() or 0)
        except Exception:
            self.after = 0
        if (
            self.before
            and self.before_on_primary
            and self.after
            and self.after != self.before
            and not is_on_primary_screen(self._rect(self.after))
        ):
            self.taken = True
            self.diagnostics.append(
                "focus_guard:primary_lost:0x%x->0x%x" % (self.before, self.after)
            )
            if self.strict:
                raise FocusTakenError(
                    "primary-screen focus was taken by automation "
                    "(0x%x -> 0x%x)" % (self.before, self.after)
                )
        return False

    def warnings(self) -> list[str]:
        return list(self.diagnostics)


# ---------------------------------------------------------------- secondary tiling
def secondary_screen_bounds() -> tuple[int, int, int, int] | None:
    """Bounds of the left-of-primary monitor, or None when it is not present."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        monitors: list[tuple[int, int, int, int]] = []
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(wintypes.RECT), ctypes.c_double,
        )

        def _cb(hmon, hdc, lprc, data):
            r = lprc.contents
            monitors.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(_cb), 0)
        for left, top, right, bottom in monitors:
            if right <= PRIMARY_SCREEN_X:
                return (left, top, right, bottom)
    except Exception:
        return None
    return None


def _set_window_pos(hwnd: int, x: int, y: int, width: int, height: int, flags: int) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(
            ctypes.windll.user32.SetWindowPos(
                int(hwnd), 0, int(x), int(y), int(width), int(height), int(flags)
            )
        )
    except Exception:
        return False


def tile_windows_on_secondary_screen(
    hwnds: list[int],
    bounds: tuple[int, int, int, int] | None = None,
    *,
    set_pos_fn: Callable[[int, int, int, int, int, int], bool] | None = None,
) -> list[dict[str, Any]]:
    """Tile windows top-to-bottom on the automation screen, never activating them.

    ``SWP_NOACTIVATE`` is mandatory (plus ``SWP_NOZORDER`` so the stack order is
    untouched): moving a window must never pull focus off the primary screen.
    ``bounds`` defaults to the live monitor left of x=0, then to the measured
    ``SECONDARY_BOUNDS``. Coordinates are computed from the bounds, so nothing is
    ever placed at a positive-x origin.
    """
    left, top, right, bottom = bounds or secondary_screen_bounds() or SECONDARY_BOUNDS
    width = max(1, int(right) - int(left))
    height = max(1, int(bottom) - int(top))
    handles = [int(h) for h in (hwnds or []) if int(h) > 0]
    if not handles:
        return []
    position = set_pos_fn or _set_window_pos
    flags = SWP_NOACTIVATE | SWP_NOZORDER
    step = height / float(len(handles))
    results: list[dict[str, Any]] = []
    for index, hwnd in enumerate(handles):
        y = int(round(top + step * index))
        h = int(round(top + step * (index + 1))) - y
        ok = bool(position(hwnd, left, y, width, max(1, h), flags))
        results.append({
            "hwnd": hwnd,
            "x": left,
            "y": y,
            "width": width,
            "height": max(1, h),
            "flags": flags,
            "ok": ok,
        })
    return results
