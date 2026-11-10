"""页面缩放（CARD-06b 第 6 条）：非 100% 就先按 Ctrl+0，仍不是 100% 就退出。

## 为什么单独一个模块

模板是**在某个缩放下截的**。页面缩放一变，同一个按钮的像素尺寸就跟着变，
于是"图明明在屏幕上却匹不上"：2026-11-01 `grok_send_on` 最高 0.490、当时
Chrome 是 125%；2026-11-02 CARD-06b 开工时实测同一窗口又是 **125%**，
`grok_chip_*` 全在 0.35~0.5（芯片模板是在 100% 下截的）。

## 三条硬规则（卡面 1/6）

1. **只对浏览器窗口发 Ctrl+0。** 独立桌面端（Codex / dsh / OpenCode）没有
   "页面缩放"，对它们发 Ctrl+0 是往编辑器里打字符——绝对不做。
2. **发完必须重新检测**，不看"我按过了"就当真。仍非 100% → `ZoomNot100`
   （stage=zoom_not_100），退出并留证，不硬着头皮找图。
3. **多尺度只是兜底**：限定 0.8–1.25，且不能替代唯一性校验
   （同一模板多处命中仍算失败，见 `qi/screen.py::find_unique`）。
"""

from __future__ import annotations

import ctypes
import time

from . import exit_codes as ec

# 多尺度兜底的允许范围（卡面写死 0.8–1.25）。超出范围的重采样既慢又容易误配。
MIN_SCALE = 0.8
MAX_SCALE = 1.25
SCALES = (1.0, 0.9, 0.8, 1.1, 1.25)

# Chrome / Edge / Brave / Chromium 的进程名。只有它们才有"页面缩放"。
BROWSER_PROCS = ("chrome.exe", "msedge.exe", "brave.exe", "chromium.exe",
                 "chrome_proxy.exe")

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class ZoomNot100(RuntimeError):
    """页面缩放不是 100% 且没能用 Ctrl+0 修正。CLI 层转成 stage=zoom_not_100。"""

    stage = "zoom_not_100"
    code = ec.FAIL

    def __init__(self, message: str, facts: dict | None = None):
        super().__init__(message)
        self.facts = dict(facts or {})

    def as_result(self) -> dict:
        return {"ok": False, "stage": self.stage, "code": self.code,
                "hint": str(self), "zoom": self.facts}


def clamp_scales(scales=None) -> tuple[float, ...]:
    """把调用方给的尺度收进 0.8–1.25；1.0 永远在第一位。"""
    out = [1.0]
    for value in scales if scales is not None else SCALES:
        try:
            scale = float(value)
        except (TypeError, ValueError):
            continue
        if MIN_SCALE - 1e-9 <= scale <= MAX_SCALE + 1e-9 and all(
                abs(scale - seen) > 1e-9 for seen in out):
            out.append(scale)
    return tuple(out)


def _proc_name(pid: int) -> str:
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint(1024)
            if not kernel32.QueryFullProcessImageNameW(
                    ctypes.c_void_p(handle), 0, buf, ctypes.byref(size)):
                return ""
            return buf.value.rsplit("\\", 1)[-1].lower()
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:                       # noqa: BLE001 - 诊断信息不许带崩主流程
        return ""


def pid_of(hwnd) -> int | None:
    try:
        from ctypes import wintypes

        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        return int(pid.value) or None
    except Exception:                       # noqa: BLE001
        return None


def is_browser(hwnd) -> bool | None:
    """窗口是不是浏览器。读不到进程名返回 None（不猜成 False 也不猜成 True）。"""
    pid = pid_of(hwnd)
    if not pid:
        return None
    name = _proc_name(pid)
    if not name:
        return None
    return name in BROWSER_PROCS


def percent(hwnd) -> int | None:
    """读页面缩放（百分数）。读不到返回 None。

    来源是 Chrome 工具栏那个按钮的可访问名「缩放比例：125%」——这是
    **UIA 的真实读数**，不是从 DPI 推的。
    """
    if not hwnd:
        return None
    from . import ui

    try:
        text = ui.page_zoom(hwnd)
    except Exception:                       # noqa: BLE001
        return None
    if not text:
        return None
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    try:
        return int(digits) if digits else None
    except ValueError:
        return None


def ensure(screen, *, verbose: bool = False, settle: float = 0.8,
           attempts: int = 2) -> dict:
    """确保页面缩放是 100%。返回 facts；不满足时抛 `ZoomNot100`。

    facts: {"ok", "before", "after", "pressed", "browser", "skipped"}
    · 非浏览器/读不到窗口 → skipped（桌面端不该被这一条拦住）；
    · 浏览器但读不到缩放 → 拒（fail-closed，不假装 100%）；
    · 不是 100% → 只对浏览器窗口发 Ctrl+0 → 重新读 → 仍不是 100% → 抛。
    """
    hwnd = getattr(screen, "target_hwnd", None)
    facts = {"ok": True, "before": None, "after": None, "pressed": False,
             "browser": None, "skipped": ""}
    if not hwnd:
        facts["skipped"] = "no_window"
        return facts
    browser = is_browser(hwnd)
    facts["browser"] = browser
    if browser is False:
        facts["skipped"] = "not_browser"      # 桌面端没有页面缩放，绝不按 Ctrl+0
        return facts

    before = percent(hwnd)
    facts["before"] = before
    facts["after"] = before
    if before == 100:
        return facts
    if before is None:
        raise ZoomNot100(f"读不到页面缩放（hwnd={hwnd} 是浏览器，但没有「缩放比例」按钮）；"
                         "不假装 100%", facts)
    if browser is None:                       # 读不到进程名：不冒险发快捷键
        raise ZoomNot100(f"页面缩放 {before}%，但读不到窗口进程名，不知道该不该发 Ctrl+0",
                         facts)
    after = before
    for attempt in range(max(1, int(attempts))):
        try:
            screen.hotkey("ctrl", "0")       # 只对已绑定的浏览器窗口，只发这一种
        except Exception as exc:              # noqa: BLE001 - 发不出去就是没修好
            facts["error"] = f"{type(exc).__name__}: {exc}"
            raise ZoomNot100(f"页面缩放 {before}%，Ctrl+0 发不出去：{exc}", facts)
        facts["pressed"] = True
        facts["attempts"] = attempt + 1
        time.sleep(settle)
        after = percent(hwnd)
        facts["after"] = after
        if verbose:
            print(f"  页面缩放 {before}% → {after}%（第 {attempt + 1} 次 Ctrl+0）")
        if after == 100:
            return facts
    raise ZoomNot100(f"页面缩放 {before}%，按 Ctrl+0 {facts.get('attempts', 1)} 次后"
                     f"仍是 {after}%；不硬找图（模板是在 100% 下截的）", facts)
