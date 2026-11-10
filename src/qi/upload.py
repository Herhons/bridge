"""网页端上传文件：点附件入口 → 系统文件对话框 → 写路径 → 打开 → 等附件落地。

**为什么不能用键盘**：`Screen.press/type_text` 都会先断言前台是目标 Chrome，
而文件对话框抢了前台，必然 `ForegroundLost`。这里改用 UIA 的 ValuePattern
写路径 + 原生鼠标点「打开」，全程不依赖 Chrome 在前台。

**为什么不用 WM_SETTEXT**：Win10/11 的文件对话框是 DirectUI，子控件不是
传统 HWND（`EnumChildWindows` 只看到一个 Button），只能走 UIA。

实测（2026-11-11，chatgpt）：入口是「添加文件等内容」→ 菜单项
「添加照片和文件 从计算机上传」→ 对话框标题「打开」，文件名框 AutomationId=1148，
「打开(O)」按钮 AutomationId=1。上传后页面出现「正在上传文件 <名字>」与「移除 <名字>」。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import uiautomation as auto

from . import ui

user32 = ctypes.windll.user32
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

#: 各端的附件入口。`button` 是工具条上的按钮名（子串匹配）；
#: `menu` 是点开之后菜单里的条目名（没有就是直接弹对话框）。
ATTACH = {
    "chatgpt": {"button": ("添加文件等内容",), "menu": ("添加照片和文件",)},
    "grok": {"button": ("附件",), "menu": ()},
    "gemini": {"button": ("上传和工具",), "menu": ("上传文件", "从计算机")},
    "deepseek": {"button": ("附件", "上传"), "menu": ()},
}

#: 文件对话框里「文件名」输入框与「打开」按钮的稳定 AutomationId。
FILE_EDIT_ID = "1148"
OPEN_BTN_ID = "1"


@dataclass
class UploadResult:
    ok: bool
    reason: str = ""
    path: str = ""
    filename: str = ""
    waited: float = 0.0
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        out = {"ok": self.ok, "reason": self.reason, "path": self.path,
               "filename": self.filename, "waited": round(self.waited, 1)}
        if self.detail:
            out["detail"] = self.detail
        return out


def class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(int(hwnd), buf, 256)
    return buf.value


def top_windows() -> set:
    out = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        out.add(int(hwnd))
        return True

    user32.EnumWindows(callback, 0)
    return out


def _raw_click(x: int, y: int) -> None:
    """原生鼠标点击。刻意不走 `Screen.click_xy`：那会断言 Chrome 在前台。"""
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def _click_node(node) -> bool:
    # 两种入参：页面树的 `ui.Node`（带 .control）与对话框里的原生控件（本身就是控件）
    control = getattr(node, "control", node)
    rect = control.BoundingRectangle
    if rect.right <= rect.left:
        return False
    _raw_click((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    return True


def _find(root, kind: str, needles) -> object | None:
    for node in root.walk():
        if node.kind != kind or node.offscreen:
            continue
        name = (node.name or "").strip()
        if name and any(needle in name for needle in needles):
            return node
    return None


def _wait(predicate, timeout: float, interval: float = 0.25):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _close_stray_dialogs() -> list:
    """清掉残留的文件对话框（上一次失败会留下，挡住后续点击）。"""
    closed = []
    for hwnd in top_windows():
        if class_name(hwnd) != "#32770" or not user32.IsWindowVisible(hwnd):
            continue
        if ui.title_of(hwnd).strip() not in ("打开", "Open"):
            continue
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.1)
        user32.keybd_event(0x1B, 0, 0, 0)
        user32.keybd_event(0x1B, 0, 2, 0)
        time.sleep(0.2)
        closed.append(hwnd)
    return closed


def _dialog_edit(root):
    found = []

    def walk(control, depth=0):
        for child in control.GetChildren():
            if (child.ControlTypeName == "EditControl"
                    and child.AutomationId == FILE_EDIT_ID):
                found.append(child)
            if depth < 7:
                walk(child, depth + 1)

    walk(root)
    return found[0] if found else None


def _dialog_open_button(root):
    found = []

    def walk(control, depth=0):
        for child in control.GetChildren():
            if (child.ControlTypeName == "ButtonControl"
                    and child.AutomationId == OPEN_BTN_ID):
                found.append(child)
            if depth < 7:
                walk(child, depth + 1)

    walk(root)
    return found[0] if found else None


def upload(screen, site: str, path: str, *, timeout: float = 60.0,
           expect: str | None = None) -> UploadResult:
    """把 `path` 上传到当前页。成功判据：页面上出现该文件名（或 `expect` 子串）。"""
    site = str(site).strip().lower()
    spec = ATTACH.get(site)
    if not spec:
        return UploadResult(False, "site_not_configured", path=path)
    filename = path.replace("\\", "/").rsplit("/", 1)[-1]
    needle = filename if expect is None else expect
    started = time.time()

    _close_stray_dialogs()
    before = top_windows()
    try:
        control, _url = ui.document(screen.target_hwnd, site)
        root = ui.tree(control)
    except Exception as exc:                      # noqa: BLE001
        return UploadResult(False, "page_unreadable", path=path,
                            detail={"error": type(exc).__name__})

    button = _find(root, "ButtonControl", spec["button"])
    if button is None:
        return UploadResult(False, "attach_not_found", path=path,
                            detail={"wanted": list(spec["button"])})
    if not _click_node(button):
        return UploadResult(False, "attach_unclickable", path=path)

    if spec["menu"]:
        item = _wait(lambda: _menu_item(screen, site, spec["menu"]), 8.0)
        if item is None:
            return UploadResult(False, "menu_item_not_found", path=path,
                                detail={"wanted": list(spec["menu"])})
        _click_node(item)

    dialog = _wait(lambda: _new_dialog(before), 10.0)
    if dialog is None:
        return UploadResult(False, "dialog_not_found", path=path)

    root = auto.ControlFromHandle(dialog)
    edit = _dialog_edit(root)
    if edit is None:
        _close_stray_dialogs()
        return UploadResult(False, "filename_box_not_found", path=path)

    # 写路径：必须用反斜杠。写成正斜杠时对话框会报「文件名无效」。
    edit.GetValuePattern().SetValue(path.replace("/", "\\"))
    time.sleep(0.3)
    button = _dialog_open_button(root)
    if button is None:
        _close_stray_dialogs()
        return UploadResult(False, "open_button_not_found", path=path)

    user32.SetForegroundWindow(dialog)
    time.sleep(0.4)
    _click_node(button)
    if not _wait(lambda: not user32.IsWindow(dialog), 10.0):
        _close_stray_dialogs()
        return UploadResult(False, "dialog_stuck", path=path)

    landed = _wait(lambda: _attachment_visible(screen, site, needle), timeout)
    return UploadResult(bool(landed), "ok" if landed else "attachment_not_visible",
                        path=path, filename=filename,
                        waited=time.time() - started,
                        detail={"needle": needle})


def _menu_item(screen, site, needles):
    try:
        control, _url = ui.document(screen.target_hwnd, site)
        root = ui.tree(control)
    except Exception:                             # noqa: BLE001
        return None
    return _find(root, "ButtonControl", needles)


def _new_dialog(before):
    for hwnd in top_windows():
        if hwnd in before:
            continue
        if class_name(hwnd) != "#32770" or not user32.IsWindowVisible(hwnd):
            continue
        if ui.title_of(hwnd).strip() in ("打开", "Open"):
            return hwnd
    return None


def _attachment_visible(screen, site, needle) -> bool:
    try:
        control, _url = ui.document(screen.target_hwnd, site)
        root = ui.tree(control)
    except Exception:                             # noqa: BLE001
        return False
    return any(needle in (node.name or "") for node in root.walk())
