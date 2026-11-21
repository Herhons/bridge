"""Win32 ``#32770`` (native Open) file-dialog primitives.

Split out of ``engine/base.py`` so the fragile Win32 message plumbing has one
home; ``engine/base.py`` re-exports every symbol here for backwards
compatibility.

Design rules (unchanged from v0.2.4):
- No clipboard, no ``SetForegroundWindow``, no physical keyboard input.
- Only targeted ``WM_SETTEXT`` / ``BM_CLICK`` messages, so an unattended run
  never steals focus from the main screen.

v0.2.5 fix: on Windows 10 ``GetDlgItem(dialog, 1148)`` returns the *outer*
``ComboBoxEx32`` wrapper, so ``WM_SETTEXT`` landed on the wrong control and the
file name stayed empty (observed live: the Open dialog sat there with no text
and the attach flow timed out). The real editable control is the inner
``Edit`` reached via ``ComboBoxEx32 -> ComboBox -> Edit``.
"""
from __future__ import annotations

import ctypes
import os
import time

from engine.uia import UIAError

FILE_DIALOG_CLASS = "#32770"
FILE_NAME_CONTROL_ID = 1148
OPEN_BUTTON_CONTROL_ID = 1
WM_SETTEXT = 0x000C
BM_CLICK = 0x00F5

COMBO_EX_CLASS = "ComboBoxEx32"
COMBO_CLASS = "ComboBox"
EDIT_CLASS = "Edit"


def _win32_user32():
    """Return user32 with fixed argtypes; never activates or focuses a window."""
    if os.name != "nt":
        raise UIAError("PLATFORM_UNSUPPORTED", "Native file dialog requires Windows")
    u32 = ctypes.windll.user32
    try:
        u32.FindWindowExW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_wchar_p, ctypes.c_wchar_p,
        ]
        u32.FindWindowExW.restype = ctypes.c_void_p
        u32.GetDlgItem.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u32.GetDlgItem.restype = ctypes.c_void_p
        u32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
        ]
        u32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        u32.IsWindow.argtypes = [ctypes.c_void_p]
        u32.IsWindow.restype = ctypes.c_int
        # 64-bit safe: without explicit argtypes ctypes truncates the HWND
        # and the LPARAM to 32 bits, so WM_SETTEXT targets garbage.
        u32.SendMessageW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
        ]
        u32.SendMessageW.restype = ctypes.c_ssize_t
    except AttributeError:
        pass
    return u32


def _window_pid(user32, hwnd: int) -> int:
    # A c_ulong instance is auto-byref'd for a POINTER(c_ulong) argtype, which
    # also keeps the offline fake user32 (it writes ``.value``) working.
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(int(hwnd), pid)
    return int(pid.value)


def find_existing_file_dialog(owner_pid: int, *, user32=None) -> int:
    """Single read-only pass for an already-open #32770 owned by *owner_pid*."""
    u32 = user32 or _win32_user32()
    hwnd = 0
    while True:
        hwnd = u32.FindWindowExW(None, hwnd, FILE_DIALOG_CLASS, None)
        if not hwnd:
            return 0
        if int(owner_pid) <= 0 or _window_pid(u32, hwnd) == int(owner_pid):
            return int(hwnd)


def find_file_dialog_window(
    owner_pid: int,
    *,
    timeout: float = 10.0,
    poll: float = 0.15,
    user32=None,
) -> int:
    """Find a top-level #32770 window owned by *owner_pid* without focusing it."""
    u32 = user32 or _win32_user32()
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        hwnd = find_existing_file_dialog(owner_pid, user32=u32)
        if hwnd:
            return hwnd
        time.sleep(poll)
    raise UIAError(
        "DIALOG_NOT_FOUND",
        "#32770 dialog for pid %s did not appear within %.1fs" % (owner_pid, timeout),
    )


def _dialog_edit_hwnd(user32, dialog_hwnd: int) -> int:
    """Handle of the control that really accepts ``WM_SETTEXT``.

    Walk ``ComboBoxEx32 -> ComboBox -> Edit`` first: on Windows 10
    ``GetDlgItem(dialog_hwnd, 1148)`` hands back the outer ComboBoxEx32, and
    text sent there is silently dropped. ``GetDlgItem`` remains as a fallback
    for dialogs that do not use the combo-box layout.
    """
    combo_ex = user32.FindWindowExW(dialog_hwnd, 0, COMBO_EX_CLASS, None)
    combo = user32.FindWindowExW(combo_ex or dialog_hwnd, 0, COMBO_CLASS, None)
    edit = user32.FindWindowExW(combo or combo_ex or dialog_hwnd, 0, EDIT_CLASS, None)
    if edit:
        return int(edit)
    direct = user32.GetDlgItem(dialog_hwnd, FILE_NAME_CONTROL_ID)
    return int(direct) if direct else 0


def fill_windows_file_dialog(
    paths: list[str],
    *,
    owner_pid: int,
    timeout: float = 10.0,
    poll: float = 0.15,
    user32=None,
    ctypes_module=None,
) -> str:
    """Fill a native #32770 Open dialog and press its Open button, headless.

    Uses targeted WM_SETTEXT / BM_CLICK messages only: no global clipboard,
    no SetForegroundWindow, no physical keyboard, no focus stealing.

    ``ctypes_module`` only exists so ``engine/base.py`` can keep marshalling
    through its own ``ctypes`` binding (offline tests monkeypatch it).
    """
    marshaller = ctypes_module or ctypes
    u32 = user32 or _win32_user32()
    dialog_hwnd = find_file_dialog_window(
        owner_pid, timeout=timeout, poll=poll, user32=u32
    )
    absolute = [os.path.abspath(os.path.expanduser(str(p))) for p in paths]
    filename_text = " ".join('"%s"' % p for p in absolute)

    edit_hwnd = _dialog_edit_hwnd(u32, dialog_hwnd)
    if not edit_hwnd:
        raise UIAError(
            "TARGET_NOT_FOUND",
            "File name edit control (ID %d or ComboBoxEx32 chain) not found"
            % FILE_NAME_CONTROL_ID,
        )
    u32.SendMessageW(edit_hwnd, WM_SETTEXT, 0, marshaller.c_wchar_p(filename_text))

    button_hwnd = u32.GetDlgItem(dialog_hwnd, OPEN_BUTTON_CONTROL_ID)
    if not button_hwnd:
        raise UIAError("TARGET_NOT_FOUND", "Open button (ID 1) not found")
    u32.SendMessageW(int(button_hwnd), BM_CLICK, 0, 0)

    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline and u32.IsWindow(dialog_hwnd):
        time.sleep(poll)
    return "#32770:file_dialog"


# Re-exported for callers that inject a custom marshaller (see engine/base.py).
__all__ = [
    "FILE_DIALOG_CLASS",
    "FILE_NAME_CONTROL_ID",
    "OPEN_BUTTON_CONTROL_ID",
    "WM_SETTEXT",
    "BM_CLICK",
    "_win32_user32",
    "_window_pid",
    "_dialog_edit_hwnd",
    "find_existing_file_dialog",
    "find_file_dialog_window",
    "fill_windows_file_dialog",
]
