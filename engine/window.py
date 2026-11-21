"""Window discovery with watchdog timeout, FindWindow fallback, and app/URL launch helpers."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass


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
