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
from pathlib import Path
from ctypes import wintypes
from dataclasses import dataclass, field

import uiautomation as auto

from . import safe_ui, ui

user32 = ctypes.windll.user32
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

#: 各端的附件入口。`button` 是工具条上的按钮名（子串匹配）；
#: `menu` 是点开之后菜单里的条目名（没有就是直接弹对话框）。
ATTACH = {
    "chatgpt": {"button": ("添加文件等内容",), "menu": ("添加照片和文件",)},
    "grok": {"button": ("附件",), "menu": ("上传文件",)},   # 附件是 MenuControl，带二级菜单
    "gemini": {"button": ("上传和工具",), "menu": ("上传文件", "从计算机")},
    # deepseek 的附件是**没有名字的图标**（UIA 里是空名 ButtonControl），
    # 只能按"输入框矩形"的相对位置点。比例是 2026-11-12 实测标定的：
    # 输入框 (-1008,102)-(-231,226)，图标中心 (-305,196)。
    "deepseek": {"button": ("附件", "上传"), "menu": (), "point": (0.906, 1.48)},
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


def _find(root, kind, needles) -> object | None:
    kinds = (kind,) if isinstance(kind, str) else tuple(kind)
    for node in root.walk():
        if node.kind not in kinds or node.offscreen:
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


#: 附件 chip 的关闭键前缀。**绝不包含「删除 …」**：
#: 桌面上也有叫「删除」的按钮，2026-11-12 就是这条把点击打到了桌面，
#: 弹出系统的「确实要从桌面上删除这些图标吗?」——差一点删掉用户的东西。
#: 注意「移除此附件」中间没有空格，所以按裸前缀「移除」匹配。
ATTACH_CLOSE_PREFIXES = ("移除", "close ", "remove ")


def _composer_rect(root):
    """输入框矩形（用来算无名图标的相对位置）。找不到返回 None。"""
    for node in root.walk():
        if node.kind == "EditControl" and not node.offscreen:
            rect = node.control.BoundingRectangle
            if rect.right > rect.left and rect.bottom > rect.top:
                return rect
    return None


def clear_attachments(screen, site: str) -> int:
    """移除输入框里已挂的附件。上一轮失败会留下它们，挡住下一次上传。

    安全边界（两条都必须遵守）：
      1. 名字只认「移除/close/remove」，**不碰「删除」**；
      2. 只点**浏览器窗口矩形内**的控件——控件树偶尔会混进桌面项，
         按坐标点击就会点到桌面上。
    """
    try:
        control, _url = ui.document(screen.target_hwnd, site)
        root = ui.tree(control)
        win = auto.ControlFromHandle(screen.target_hwnd).BoundingRectangle
    except Exception:                             # noqa: BLE001
        return 0
    removed = 0
    for node in list(root.walk()):
        if node.kind != "ButtonControl" or node.offscreen:
            continue
        name = (node.name or "").strip()
        if not any(name.startswith(p) for p in ATTACH_CLOSE_PREFIXES):
            continue
        rect = node.control.BoundingRectangle
        cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        if not (win.left <= cx <= win.right and win.top <= cy <= win.bottom):
            continue
        _raw_click(cx, cy)
        time.sleep(0.4)
        removed += 1
    return removed


def _absolute(path) -> str:
    """上传路径一律转绝对路径（文件对话框不认相对路径）。"""
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return str(path)


def upload(screen, site: str, path: str, *, timeout: float = 60.0,
           expect: str | None = None) -> UploadResult:
    """把 `path` 上传到当前页。成功判据：页面上出现该文件名（或 `expect` 子串）。"""
    site = str(site).strip().lower()
    spec = ATTACH.get(site)
    if not spec:
        return UploadResult(False, "site_not_configured", path=path)
    # 文件对话框按**它自己的**当前目录解析路径：传相对路径时永远打不开，
    # 表现为「点了打开、对话框不关」（2026-11-12 实测 dialog_stuck）。
    path = _absolute(path)
    # 路径不存在时对话框同样不关，且会被误记成 dialog_stuck（真的错因是文件没有）。
    # 先在这里拦住：不开对话框、不留残窗。
    if not Path(path).is_file():
        return UploadResult(False, "file_not_found", path=path)
    filename = path.replace("\\", "/").rsplit("/", 1)[-1]
    needle = filename if expect is None else expect
    started = time.time()

    _close_stray_dialogs()
    clear_attachments(screen, site)
    before = top_windows()
    try:
        control, _url = ui.document(screen.target_hwnd, site)
        root = ui.tree(control)
    except Exception as exc:                      # noqa: BLE001
        return UploadResult(False, "page_unreadable", path=path,
                            detail={"error": type(exc).__name__})

    point = spec.get("point")
    if point:
        # 无名图标：按输入框矩形的相对位置点。比例随窗口缩放走，不写死屏幕坐标。
        rect = _composer_rect(root)
        if rect is None:
            return UploadResult(False, "composer_not_found", path=path)
        x = int(rect.left + (rect.right - rect.left) * point[0])
        y = int(rect.top + (rect.bottom - rect.top) * point[1])
        # S1：点位点击必须过归属校验（客户区内 + 命中窗口属于目标进程 + 非桌面类）。
        verdict = safe_ui.safe_click(screen.target_hwnd, x, y)
        if not verdict.ok:
            return UploadResult(False, "unsafe_click", path=path,
                                detail={"reason": verdict.reason, "x": x, "y": y})
    elif spec["menu"]:
        button = _find(root, ("ButtonControl", "MenuControl"), spec["button"])
        if button is None:
            return UploadResult(False, "attach_not_found", path=path,
                                detail={"wanted": list(spec["button"])})
        # 菜单是 toggle 语义：上一次失败可能把它留开着，再点一次反而关掉。
        # 先看菜单项在不在，在就直接点。
        item = _menu_item(screen, site, spec["menu"])
        if item is None:
            if not _click_node(button):
                return UploadResult(False, "attach_unclickable", path=path)
            item = _wait(lambda: _menu_item(screen, site, spec["menu"]), 8.0)
        if item is None:
            # 刚新开会话时页面还在重排，第一点可能打空（或把已经开的菜单点关）。
            # 再点一次、多等一会儿——grok 实测第一点偶发不弹菜单。
            _click_node(button)
            item = _wait(lambda: _menu_item(screen, site, spec["menu"]), 12.0)
        if item is None:
            return UploadResult(False, "menu_item_not_found", path=path,
                                detail={"wanted": list(spec["menu"])})
        # 菜单项优先用 UIA 的 Click：它自己处理展开/滚动，坐标点击在浮层上常打空。
        try:
            getattr(item, "control", item).Click()
        except Exception:                 # noqa: BLE001
            _click_node(item)
    else:
        button = _find(root, "ButtonControl", spec["button"])
        if button is None:
            return UploadResult(False, "attach_not_found", path=path,
                                detail={"wanted": list(spec["button"])})
        if not _click_node(button):
            return UploadResult(False, "attach_unclickable", path=path)

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

    # 有的端会去掉扩展名（gemini 显示 upload_check 而不是 upload_check.md），
    # 所以同时认完整文件名与主干。
    stems = [needle, needle.rsplit(".", 1)[0]]
    landed = _wait(lambda: _attachment_visible(screen, site, stems), timeout)
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
    # 菜单项在不同端叫法不同：chatgpt 是 ButtonControl，gemini 是 MenuItemControl。
    return _find(root, ("ButtonControl", "MenuItemControl"), needles)


def _new_dialog(before):
    for hwnd in top_windows():
        if hwnd in before:
            continue
        if class_name(hwnd) != "#32770" or not user32.IsWindowVisible(hwnd):
            continue
        if ui.title_of(hwnd).strip() in ("打开", "Open"):
            return hwnd
    return None


def _attachment_visible(screen, site, needles) -> bool:
    try:
        control, _url = ui.document(screen.target_hwnd, site)
        root = ui.tree(control)
    except Exception:                             # noqa: BLE001
        return False
    return any(any(n in (node.name or "") for n in needles) for node in root.walk())
