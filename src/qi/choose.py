"""选择类脚本的通用流程：写入器模式 → 打开菜单 → 点某一项 → 复核。

没有 OCR 就读不出任意文字，所以每个档位靠图代表：
    <prefix>item_<key>.png   菜单里那一行的样子（点它）
    <prefix>chip_<key>.png   选中后芯片的样子（读它）

写入器模式（聊天/工作、对话/任务）单独一套：
    <prefix>writer_tabs_<key>.png  每个模式下"整组按钮"的样子
读模式**不能靠模板相似度**（两态只差胶囊左右，NCC 给 0.98 对 1.00），改用逐像素差。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from . import guard
from .screen import Screen


@dataclass(frozen=True)
class Chooser:
    name: str
    anchor: str                       # 输入框锚点模板（用来定位菜单位置）
    open_offset: tuple[int, int]      # 从锚点点到"模型芯片"的偏移
    prefix: str                       # 模板前缀，如 "grok_"
    options: dict[str, str] = field(default_factory=dict)  # key -> 中文说明
    has_chip: tuple[str, ...] = ()    # 哪些 key 有 chip 图（可读）
    open_by_chip: bool = False        # True=点当前芯片开菜单
    title_hint: str = "Google Chrome"
    on_secondary: bool = True
    writer_tabs: tuple[str, ...] = ()                 # 每个模式一张"整组按钮"图
    writers: dict[str, int] = field(default_factory=dict)  # 模式名 -> 第几格


def _win(s: Screen, cfg: Chooser):
    """目标窗口矩形。所有 find 都要限定在它里面。

    不限定就是在整个虚拟桌面（含主屏）里找：主屏上开着同款页面时会先命中那边，
    然后拿主屏的坐标去点、去读，表现是"点了没反应"或"状态读成 unknown"。
    """
    return s.window_rect(cfg.title_hint, on_secondary=cfg.on_secondary)


# ------------------------------------------------------------------ 写入器模式
def read_writer(s: Screen, cfg: Chooser) -> dict:
    """读当前写入器模式：逐像素差，不靠相似度。

    实测两态相似度 0.980 对 1.000（差在胶囊左右），分不开；
    逐像素差是同态 0.00、异态 29.2，非常干净。
    """
    import cv2
    import numpy as np

    if not cfg.writer_tabs:
        return {"writer": None}
    # 定位用任意一张：两态的整组外观可能差很多（ChatGPT 聊天是灰底、工作是白胶囊+黑边框），
    # 只认第一张会在一态下直接归零（实测：chat 模板在工作态连 0.4 都不到）。
    loc = None
    for nm in cfg.writer_tabs:
        loc = s.find(nm, threshold=0.62, region=_win(s, cfg))
        if loc is not None:
            break
    if loc is None:
        return {"writer": "unknown", "hint": "找不到模式按钮组：可能不在新对话页，或换了主题/缩放"}

    # 多抓一圈，好在 ±4px 内对齐：逐像素差对位置偏移极敏感，差 1~2px 就会误判
    pad = 4
    big = s.grab((loc.x - pad, loc.y - pad, loc.w + 2 * pad, loc.h + 2 * pad))
    best, best_d, scores = None, 1e9, {}
    for attempt in range(3):
        best, best_d, scores = None, 1e9, {}
        for i, name in enumerate(cfg.writer_tabs):
            tpl = cv2.imread(str(s.library / (name if name.endswith(".png") else name + ".png")))
            if tpl is None or tpl.shape[0] > big.shape[0] or tpl.shape[1] > big.shape[1]:
                continue
            d_best = 1e9
            for dy in range(0, big.shape[0] - tpl.shape[0] + 1):
                for dx in range(0, big.shape[1] - tpl.shape[1] + 1):
                    patch = big[dy:dy + tpl.shape[0], dx:dx + tpl.shape[1]]
                    d = float(np.abs(patch.astype(int) - tpl.astype(int)).mean())
                    if d < d_best:
                        d_best = d
            scores[name] = round(d_best, 2)
            if d_best < best_d:
                best, best_d = i, d_best
        if best_d <= 5.0:
            break                       # 够干净，收工
        time.sleep(0.8)                 # 否则可能是切换动画的过渡帧，等一帧再算
        big = s.grab((loc.x - pad, loc.y - pad, loc.w + 2 * pad, loc.h + 2 * pad))
    if best is None or best_d > 12.0:
        return {"writer": "unknown", "diff": scores,
                "hint": "两种模式都差得远：可能换了主题/缩放，需要重截整组图"}
    keys = [k for k, v in cfg.writers.items() if v == best]
    return {"writer": keys[0] if keys else "unknown", "diff": scores,
            "box": [loc.x, loc.y, loc.w, loc.h]}


def choose_writer(s: Screen, cfg: Chooser, key: str) -> dict:
    if key not in cfg.writers:
        return {"ok": False, "stage": "bad_arg", "want": key, "options": list(cfg.writers)}
    cur = read_writer(s, cfg)
    if cur["writer"] == "unknown":
        return {"ok": False, "stage": "unreadable", "hint": cur.get("hint", "")}
    if cur["writer"] == key:
        return {"ok": True, "stage": "already", "before": cur["writer"], "after": cur["writer"]}

    x0, y0, w, h = cur["box"]
    n = len(cfg.writers)
    idx = cfg.writers[key]
    x = x0 + w * (2 * idx + 1) // (2 * n)
    y = y0 + h // 2
    s.click_xy(x, y)

    # 点完不能固定等 1.5s 就读：切换有动画，会读到旧态（实测 work 被读成 chat）。
    # 改成轮询——最多 5s，读到目标值就返回。
    after = cur
    deadline = time.time() + 5.0
    while time.time() < deadline:
        time.sleep(0.6)
        s.move_to(x - 300, max(0, y - 400))
        time.sleep(0.15)
        after = read_writer(s, cfg)
        if after["writer"] == key:
            break
    ok = after["writer"] == key
    return {"ok": ok, "stage": "done" if ok else "unverified",
            "before": cur["writer"], "after": after["writer"]}


# ------------------------------------------------------------------ 模型档位
def _item(cfg: Chooser, key: str) -> str:
    return f"{cfg.prefix}item_{key}"


def _chip(cfg: Chooser, key: str) -> str:
    return f"{cfg.prefix}chip_{key}"


def _calm(s: Screen, cfg: Chooser) -> None:
    """读之前把界面"放平"：关弹层、移开鼠标。

    踩过两次：芯片被鼠标压着会变底色；刚点过的芯片带蓝色聚焦框。
    两种状态下模板都对不上，读出来就是 unknown。
    """
    s.press("esc")
    win = s.window_rect(cfg.title_hint, on_secondary=cfg.on_secondary)
    if win:
        s.move_to(win[0] + 30, win[1] + 120)
    time.sleep(0.35)


def read_current(s: Screen, cfg: Chooser) -> dict:
    _calm(s, cfg)
    for key in cfg.has_chip:
        m = s.find(_chip(cfg, key), region=_win(s, cfg))
        if m is not None:
            return {"state": key, "score": round(m.score, 3)}
    return {"state": "unknown",
            "hint": "当前芯片和任何一张 chip 图都对不上（可能是没截过的档位，或主题/缩放变了）"}


def open_menu(s: Screen, cfg: Chooser) -> bool:
    if cfg.open_by_chip:
        opened = False
        for key in cfg.has_chip:
            m = s.find(_chip(cfg, key), threshold=0.90, region=_win(s, cfg))
            if m is not None:
                s.click(m)
                time.sleep(0.8)
                opened = True
                break
        if not opened:
            return False
    else:
        a = s.find(cfg.anchor, threshold=0.85, region=_win(s, cfg))
        if a is None:
            return False
        s.click(a, offset=cfg.open_offset)
        time.sleep(0.7)
    for key in cfg.options:
        if s.find(_item(cfg, key), threshold=0.88, region=_win(s, cfg)) is not None:
            return True
    return False


def choose(s: Screen, cfg: Chooser, key: str) -> dict:
    if key not in cfg.options:
        return {"ok": False, "stage": "bad_arg", "want": key, "options": list(cfg.options)}
    before = read_current(s, cfg)["state"]
    if not open_menu(s, cfg):
        return {"ok": False, "stage": "menu_not_open", "before": before,
                "hint": "点不开菜单：确认页面在前台、芯片位置没变（换主题/缩放要重截模板）"}
    hit = s.find(_item(cfg, key), threshold=0.88, region=_win(s, cfg))
    if hit is None:
        return {"ok": False, "stage": "item_not_found", "before": before, "want": key,
                "hint": f"菜单里找不到 {_item(cfg, key)}.png"}
    s.click(hit)
    time.sleep(0.9)

    if key in cfg.has_chip:
        after = read_current(s, cfg)
        ok = after["state"] == key
        return {"ok": ok, "stage": "done" if ok else "unverified",
                "before": before, "after": after["state"], "chip_score": after.get("score")}
    win = _win(s, cfg)
    still = any(s.find(_item(cfg, k), threshold=0.90, region=win) is not None for k in cfg.options)
    return {"ok": not still, "stage": "done_no_chip" if not still else "menu_still_open",
            "before": before, "after": key, "verified_by": "menu_closed"}


# ------------------------------------------------------------------ CLI
def cli(cfg: Chooser, argv: list[str]) -> int:
    need, wait, force, argv = guard.parse_guard_args(list(argv))
    name = f"{cfg.name}_choose.py"
    do_list, want, bad, help_, tab, writer = False, None, [], False, None, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-l", "--list"):
            do_list = True
        elif a == "--tab":
            if i + 1 >= len(argv):
                bad.append("--tab 缺值")
            else:
                i += 1
                try:
                    tab = int(argv[i])
                except ValueError:
                    bad.append(f"--tab 要数字，收到 {argv[i]!r}")
        elif a == "--writer":
            if i + 1 >= len(argv):
                bad.append("--writer 缺值")
            else:
                i += 1
                writer = argv[i].strip().lower()
        elif a in ("--model", "--effort", "--mode"):
            if i + 1 >= len(argv):
                bad.append(f"{a} 缺值")
            else:
                i += 1
                want = argv[i].strip().lower()
        elif a in ("-h", "--help"):
            help_ = True
        else:
            bad.append(a)
        i += 1

    if help_ or (not do_list and want is None and writer is None):
        print("用法：")
        print(f"  python {name} --list [--tab N]")
        print(f"  python {name} --model <{'|'.join(cfg.options)}> [--tab N]")
        if cfg.writers:
            print(f"  python {name} --writer <{'|'.join(cfg.writers)}> [--model X]")
            print("  换模式会换掉整套模型列表，所以 --writer 先于 --model 生效")
        print("  --tab N   先按 Ctrl+N 切到第 N 个标签页（左边数）")
        print("  --idle 2 / --wait 120 / --force   你正在用电脑时是否等待")
        for k, desc in cfg.options.items():
            mark = "" if k in cfg.has_chip else "  （只能设，不能读）"
            print(f"    {k:12} {desc}{mark}")
        return 0 if help_ else 2
    if bad:
        print("参数有问题：", bad)
        return 2
    if want is not None and want not in cfg.options:
        print(f"没有这个档位：{want}")
        print("可选：", ", ".join(cfg.options))
        return 2
    if writer is not None and writer not in cfg.writers:
        print(f"没有这个模式：{writer}")
        print("可选：", ", ".join(cfg.writers))
        return 2

    if not guard.guard(need, wait, force):
        return 4
    s = Screen()
    if not s.bring_to_front(cfg.title_hint, on_secondary=cfg.on_secondary):
        print("找不到浏览器窗口")
        return 1
    if tab:
        s.hotkey("ctrl", str(tab))
        time.sleep(1.8)

    if do_list:
        out = dict(read_current(s, cfg))
        if cfg.writers:
            out.update(read_writer(s, cfg))
        print(json.dumps(out, ensure_ascii=False))
        return 0

    rc = 0
    if writer is not None:
        w = choose_writer(s, cfg, writer)
        print(json.dumps(w, ensure_ascii=False))
        if not w.get("ok"):
            rc = 1
    if want is not None:
        r = choose(s, cfg, want)
        print(json.dumps(r, ensure_ascii=False))
        rc = 0 if r.get("ok") else 1
    return rc
