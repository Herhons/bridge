"""桌面端公用流程：提问 → 收回答。

和网页端 `flow.py` 的关系：
    共用 `Screen`（截图/模板匹配/点击/输入/剪贴板）和 `guard`（别抢用户鼠标），
    但**找窗口的方式不同**——桌面端按进程名找（`desk.find_app`），
    网页端按标题找（`Screen.bring_to_front`）。

四端的界面是同一套骨架（2026-10-29 逐端截图确认）：

    输入框（占位文字）
    ┌──────────────────────────────────┐
    │ +  ...           芯片 ∨  麦克风  (发送) │
    └──────────────────────────────────┘

所以流程可以共用，差异全部收敛到 `DeskSite`：
    · 进程名、窗口标题
    · 发送键怎么认（颜色 / 相对窗口右下角的位置）
    · 各控件相对发送键的偏移

## 为什么用「发送键」当锚点

它是输入框右下角那个圆钮，四端都有，而且位置几乎贴着窗口右下角。
输入框本身是纯色圆角块、没有可匹配的纹理；`+` 按钮在浅色主题下对比度极低
（实测灰度 >200，和背景差不到 15）。发送键是唯一「形状 + 颜色都明显」的东西。

## 硬约束

窗口尺寸必须先归一到 `WIN_SIZE`。所有偏移都是在这个尺寸下量的，
换尺寸就得重量——这和网页端同一个道理。
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import desk, guard
from .screen import Screen


@dataclass
class DeskSite:
    """一个桌面端的坐标与判据。所有偏移都是**相对发送键圆心**的窗口内坐标。"""
    name: str
    proc: str
    title_hint: str

    # 发送键怎么认：颜色条件（BGR 三个通道各给一个范围）或位置
    send_b_min: int = 0
    send_b_max: int = 255
    send_g_min: int = 0
    send_g_max: int = 255
    send_r_min: int = 0
    send_r_max: int = 255
    send_size: tuple[int, int] = (22, 40)     # 允许的边长范围
    # 颜色认不出来时的兜底：发送键在窗口内的**相对位置**（0~1）。
    # 窗口尺寸是固定的（open_bind 会强制 1380x1340），所以这等价于固定坐标。
    # opencode / workbuddy 的发送键一个是灰环白图标、一个是纯黑，
    # 用颜色筛都会和周围连成一片（实测命中 3 万像素、找不到 30x30 的独立块）。

    # 相对发送键圆心（窗口内坐标）的偏移
    composer: tuple[int, int] = (-480, -58)   # 点这里让输入框获得焦点
    chip: tuple[int, int] = (-139, 0)         # 模型下拉
    effort: tuple[int, int] | None = None     # 强度下拉（只有 opencode 有独立芯片）
    newchat: tuple[int, int] | None = None    # 新对话按钮

    # 复制按钮：回答动作条里那个，用模板匹配
    copy_tpl: str = ""

    notes: str = ""
    extra: dict = field(default_factory=dict)


def _bgr(img):
    return img[:, :, 0].astype(int), img[:, :, 1].astype(int), img[:, :, 2].astype(int)


def find_send(s: Screen, site: DeskSite, win):
    """找发送键圆心（窗口内坐标）。按颜色筛，取最靠右下那个。

    不要求「圆」——低分辨率下圆和圆角方块分不开，而且 opencode / workbuddy
    的发送键是低饱和的（灰 / 黑），形状判据比颜色更不靠谱。
    所以这里只做颜色 + 尺寸 + 「在窗口右下四分之一」三件事。
    """
    img = s.grab(win)
    if img.size == 0:
        return None
    b, g, r = _bgr(img)
    mask = ((b >= site.send_b_min) & (b <= site.send_b_max) &
            (g >= site.send_g_min) & (g <= site.send_g_max) &
            (r >= site.send_r_min) & (r <= site.send_r_max)).astype(np.uint8)
    h, w = mask.shape
    mask[: int(h * 0.6), :] = 0                     # 只要下半部分
    mask[:, : int(w * 0.5)] = 0                     # 只要右半部分
    n, _lb, st, _c = cv2.connectedComponentsWithStats(mask, 8)
    lo, hi = site.send_size
    cands = [(int(st[i][0]), int(st[i][1]), int(st[i][2]), int(st[i][3]), int(st[i][4]))
             for i in range(1, n)
             if lo <= st[i][2] <= hi and lo <= st[i][3] <= hi and st[i][4] > 120]
    if not cands:
        return None
    x, y, cw, ch, _a = max(cands, key=lambda t: t[0] + t[1])
    return (x + cw // 2, y + ch // 2)


def locate(s: Screen, site: DeskSite):
    """返回该端各控件的**屏幕坐标**。找不到发送键就返回 None。

    全部相对发送键算，所以窗口挪了、换了分辨率也不用改配置——
    只要窗口尺寸是 WIN_SIZE。
    """
    win = s.window_rect()
    if win is None:
        return None
    send = find_send(s, site, win)
    if send is None:
        return None
    sx, sy = send

    def abs_of(off):
        if off is None:
            return None
        return (win[0] + sx + off[0], win[1] + sy + off[1])

    return {
        "win": win,
        "send": (win[0] + sx, win[1] + sy),
        "composer": abs_of(site.composer),
        "chip": abs_of(site.chip),
        "effort": abs_of(site.effort),
        "newchat": abs_of(site.newchat),
    }


def open_bind(name: str, *, position=(-1440, -560), size=(1380, 1340),
              wait: float = 30.0, verbose: bool = True) -> Screen | None:
    """启动/复用应用，把窗口摆到固定位置和尺寸，返回已 bind 的 Screen。

    尺寸必须固定：所有偏移都在这个尺寸下量的。网页端也是这么做的。
    """
    hwnd = desk.ensure_app(name, wait=wait, verbose=verbose)
    if hwnd is None:
        return None
    desk.place(hwnd, position, size)
    time.sleep(0.6)
    if not desk.activate(hwnd):
        if verbose:
            print("  警告：没能把窗口提到前台，后续点击会被前台检查拦掉")
    time.sleep(1.0)
    s = Screen(verbose=verbose)
    s.bind(hwnd)
    # bind 之后把 target_hwnd 显式设上，_assert_foreground 才会拿它比
    s.target_hwnd = hwnd
    return s


def ask(s: Screen, site: DeskSite, prompt: str, *, timeout: float = 180.0,
        prepare=None, verbose: bool = True) -> dict:
    from .conversation import Conversation, ask as send_and_receive
    if prepare is not None:
        prepared = prepare(s, site, locate(s, site))
        if not prepared["ok"]:
            return {"ok": False, "stage": "prepare_failed", "submission": "not_sent"}
    return send_and_receive(Conversation(s, site.name), prompt, timeout)


from . import runlog  # noqa: E402


def cli(site: DeskSite, argv: list[str] | None = None) -> int:
    """`*_use.py` 的统一入口。"""
    import argparse
    argv = list(sys.argv[1:] if argv is None else argv)
    # 第7项：和网页端一样用 argparse，参数顺序随意，--timeout 写错直接报错
    idle_need, idle_wait, force, rest = guard.parse_guard_args(argv)
    parser = argparse.ArgumentParser(prog=f"{site.name}_use.py")
    parser.add_argument("prompt", help="完整提问")
    parser.add_argument("--timeout", type=float, default=180.0)
    try:
        args = parser.parse_args(rest)
        if not args.prompt.strip() or args.timeout <= 0:
            parser.error("提问不能为空，--timeout 必须大于 0")
    except SystemExit as exc:
        return 2 if exc.code else 0
    prompt, timeout = args.prompt, args.timeout
    # 和网页端一致：默认等你空闲 2 秒才动手，最多等 120 秒
    if not guard.guard(idle_need, idle_wait, force):
        return 4
    s = open_bind(site.name)
    if s is None:
        r = {"ok": False, "stage": "no_window"}
        runlog.record(site.name, r)
        print(json.dumps(r, ensure_ascii=False))
        return 1
    started = time.monotonic()
    try:
        r = ask(s, site, prompt, timeout=timeout)
    except Exception as e:                       # noqa: BLE001
        r = {"ok": False, "stage": "exception", "error": str(e)}
        runlog.record(site.name, r)
        print(json.dumps(r, ensure_ascii=False))
        return 1
    r.setdefault("elapsed", round(time.monotonic() - started, 1))
    runlog.record(site.name, r)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("ok") else 1
