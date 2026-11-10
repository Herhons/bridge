"""选择类脚本的通用流程：写入器模式 → 打开菜单 → 点某一项 → 复核。

没有 OCR 就读不出任意文字，所以每个档位靠图代表：
    <prefix>item_<key>.png   菜单里那一行的样子（点它）
    <prefix>chip_<key>.png   选中后芯片的样子（读它）

写入器模式（聊天/工作、对话/任务）单独一套：
    <prefix>writer_tabs_<key>.png  每个模式下"整组按钮"的样子
读模式**不能靠模板相似度**（两态只差胶囊左右，NCC 给 0.98 对 1.00），改用逐像素差。
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field

from . import guard, tier_guard, zoom as zoom_mod
from .browser import ensure_site
from .screen import ForegroundLost, Screen
from .window import WindowMinimized


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
# 写入器按钮组的判据（CARD-09e 第 6 条）
#
# 两态**只差胶囊在左还是在右**：模板相似度实测 0.994（对话态）/ 0.971（另一态），
# 都在 0.95 以上 —— 所以 `find` 的分数只用来"找框"，绝不能拿来判态。
# 判态靠逐像素差：同态 9.15、异态 32.24（2026-11-02 真机，
# `evidence\card09e_probe_writer.py` 量出来的）。
WRITER_LOCATE = 0.62        # 找框用的低阈值（框对不对由逐像素差兜底）
WRITER_DIFF_MAX = 12.0      # 同态上限；超过就看"是不是两态都高"（悬停/动画）
WRITER_DIFF_LOOSE = 30.0    # 放宽后的上限：只在与次优拉开倍数时才认
WRITER_RATIO = 2.0          # 与次优的倍数关系
WRITER_TEMPLATE_HIT = 0.97  # 逐像素差彻底失灵时的兜底：模板命中到这个分数
WRITER_TEMPLATE_MARGIN = 0.02   # 且必须比另一态高这么多（实测 0.994/0.971，差 0.023）
WRITER_PARK = (30, 120)     # 测量前把鼠标挪到窗口左上角的偏移（避开悬停高亮）

# 分段控件（Angular Material segmented button）的类名特征。Gemini 的「对话 / 任务」
# 就是它：两个按钮 class 完全一样，**不暴露 Toggle/Selection**，只有那层滑动胶囊
# （`...row-slider`，无名 GroupControl）的位置能说明谁被选中。
SEGMENT_CLASS = "segmented-button-action"
SLIDER_CLASS = "segmented-button-row-slider"


def _class_of(node) -> str:
    try:
        return str(node.control.ClassName or "")
    except Exception:                   # noqa: BLE001 - 读不到就当没有
        return ""


def _rect_of(node):
    try:
        r = node.control.BoundingRectangle
    except Exception:                   # noqa: BLE001
        return None
    if r.right - r.left <= 0 or r.bottom - r.top <= 0:
        return None
    return (r.left, r.top, r.right, r.bottom)


def _overlap(a, b) -> float:
    """两个矩形的交叠面积占较小者的比例（0~1）。"""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return (w * h) / small if small else 0.0


def _writer_from_uia(s: Screen, cfg: Chooser) -> dict | None:
    """从控件树读写入器：看分段控件里那层"选中胶囊"压在哪个按钮上。

    为什么优先走这里（CARD-09e 第 6 条）：Gemini 的 `gem_writer_tabs_*` 两张模板是
    早先截的，2026-11-02 实测**任务态下"对话"那张反而更像我**（21.38 对 26.29），
    逐像素差怎么调都会指错。而控件树里 `gem-segmented-button-row-slider` 的矩形
    精确压在被选中的那个按钮上——这才是页面自己给的答案。

    读不到（不在新对话页 / 结构变了 / 不是这种分段控件）返回 None，调用方回退到
    逐像素差那套，行为与改前一致。
    """
    from . import ui

    hwnd = getattr(s, "target_hwnd", None)
    if not hwnd:
        return None
    try:
        control, _url = ui.document(hwnd, cfg.name)
        root = ui.tree(control)
    except Exception:                   # noqa: BLE001 - 读不到就走模板那条路
        return None

    groups: dict[int, list] = {}
    parents: dict[int, object] = {}
    for node in root.walk():
        if node.kind != "ButtonControl" or SEGMENT_CLASS not in _class_of(node):
            continue
        parent = node.parent
        if parent is None:
            continue
        groups.setdefault(id(parent), []).append(node)
        parents[id(parent)] = parent
    for key, buttons in groups.items():
        if len(buttons) < 2:
            continue
        group = parents[key]
        sliders = [c for c in group.children if SLIDER_CLASS in _class_of(c)]
        if not sliders:
            continue
        box = _rect_of(sliders[0])
        if box is None:
            continue
        scored = [(node, _overlap(box, _rect_of(node) or (0, 0, 0, 0))) for node in buttons]
        best = max(scored, key=lambda item: item[1])
        if best[1] < 0.5:               # 胶囊没压在任何按钮上：不敢认
            continue
        index = buttons.index(best[0])
        keys = [k for k, v in cfg.writers.items() if v == index]
        if not keys:
            continue
        group_box = _rect_of(group) or _union([_rect_of(n) for n in buttons])
        out = {"writer": keys[0], "basis": "uia_slider", "index": index,
               "labels": [n.name for n in buttons], "got": best[0].name,
               "overlap": round(best[1], 2)}
        if group_box:
            out["box"] = [group_box[0], group_box[1],
                          group_box[2] - group_box[0], group_box[3] - group_box[1]]
        return out
    return None


def _union(rects) -> tuple | None:
    """几个矩形的外接矩形；一个都没有就返回 None。"""
    rects = [r for r in rects if r]
    if not rects:
        return None
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def _park_mouse(s: Screen, cfg: Chooser) -> None:
    """测量前把光标挪出按钮组。

    2026-11-02 的现场：按钮组被鼠标压着时会多一层底色，两态的逐像素差
    **同时**抬高，于是"两种模式都差得远"。这和 `_calm` 给芯片挪鼠标是同一条经验。
    """
    win = _win(s, cfg)
    if not win:
        return
    try:
        s.move_to(win[0] + WRITER_PARK[0], win[1] + WRITER_PARK[1])
        time.sleep(0.2)
    except RuntimeError:                # 光标挪不动不影响测量，只是少一层保险
        pass


def _writer_locate(s: Screen, cfg: Chooser) -> tuple[dict, object]:
    """两张模板各找一次：返回 ({模板名: 最高分}, 第一个命中的 Match)。

    分数只用来定位和兜底，**不判态**（见上面的常量说明）。
    """
    scores, first, win = {}, None, _win(s, cfg)
    for name in cfg.writer_tabs:
        match = s.find(name, threshold=WRITER_LOCATE, region=win)
        best = getattr(s, "last_best", None)
        scores[name] = None if best is None else round(float(best), 3)
        if match is not None and first is None:
            first = match
    return scores, first


def _writer_diffs(s: Screen, box, cfg: Chooser) -> dict:
    """在 box ±4px 内算每张模板的最小逐像素平均绝对差。"""
    import cv2
    import numpy as np

    pad = 4
    big = s.grab((box.x - pad, box.y - pad, box.w + 2 * pad, box.h + 2 * pad))
    out = {}
    for name in cfg.writer_tabs:
        tpl = cv2.imread(str(s.library / (name if name.endswith(".png") else name + ".png")))
        if tpl is None or tpl.shape[0] > big.shape[0] or tpl.shape[1] > big.shape[1]:
            continue
        best = 1e9
        for dy in range(0, big.shape[0] - tpl.shape[0] + 1):
            for dx in range(0, big.shape[1] - tpl.shape[1] + 1):
                patch = big[dy:dy + tpl.shape[0], dx:dx + tpl.shape[1]]
                best = min(best, float(np.abs(patch.astype(int) - tpl.astype(int)).mean()))
        out[name] = round(best, 2)
    return out


def _writer_confident(best: str, diffs: dict) -> bool:
    """逐像素差能不能定案：同态直接过；放宽区间要求"比次优差一倍以上"。"""
    if best not in diffs:
        return False
    mine = diffs[best]
    others = [v for k, v in diffs.items() if k != best]
    other = min(others) if others else 1e9
    if mine <= WRITER_DIFF_MAX:
        return True
    return mine <= WRITER_DIFF_LOOSE and other >= mine * WRITER_RATIO


def read_writer(s: Screen, cfg: Chooser) -> dict:
    """读当前写入器模式。

    顺序（CARD-09e 第 6 条）：
    1. **控件树优先**：分段控件里那层选中胶囊压在谁身上（`_writer_from_uia`）；
    2. 读不到再走逐像素差：先 `_writer_locate` 找框，**把鼠标挪开**再截图，
       同态 12 以内直接过，12~30 只在与次优拉开 2 倍时才认；
    3. 还定不了案 → `writer=unknown`，把 `locate` 分数带出去给调用方兜底。
    """
    if not cfg.writer_tabs:
        return {"writer": None}
    from_uia = _writer_from_uia(s, cfg)
    if from_uia is not None:
        return from_uia
    locate, box = _writer_locate(s, cfg)
    if box is None:
        return {"writer": "unknown", "locate": locate,
                "hint": "找不到模式按钮组：可能不在新对话页，或换了主题/缩放"}

    diffs: dict = {}
    for attempt in range(3):
        _park_mouse(s, cfg)
        diffs = _writer_diffs(s, box, cfg)
        best = min(diffs, key=diffs.get) if diffs else None
        if best is not None and _writer_confident(best, diffs):
            keys = [k for k, v in cfg.writers.items() if v == cfg.writer_tabs.index(best)]
            return {"writer": keys[0] if keys else "unknown", "diff": diffs,
                    "locate": locate, "basis": "pixel_diff", "reads": attempt + 1,
                    "box": [box.x, box.y, box.w, box.h]}
        time.sleep(0.8)                 # 可能是切换动画的过渡帧，等一帧再算
        locate, again = _writer_locate(s, cfg)
        box = again or box
    return {"writer": "unknown", "diff": diffs, "locate": locate,
            "hint": "两种模式都差得远：可能换了主题/缩放，需要重截整组图"}


def assume_writer(cur: dict, cfg: Chooser, key: str) -> dict | None:
    """像素差读不出态时的兜底：**按模板相似度认**（CARD-09e 第 6 条卡面原文）。

    只在三条同时成立时才认，认出来也标 `degraded`，不许冒充"逐像素确认"：
      · 该模式的模板分数 ≥ `WRITER_TEMPLATE_HIT`（0.97）；
      · 比**另一态**高 ≥ `WRITER_TEMPLATE_MARGIN`（0.02，实测两态 0.994 / 0.971）；
      · 调用方明确要的就是这一态（key 由 `choose_writer` 传入）。

    2026-11-02 真机反例：任务态下两张模板都掉到 0.95 / 0.937，差 0.013。
    这种"谁都不像"的时候必须拒绝——按 0.95 认成 dialog 会让脚本在任务模式里
    静默发消息（实测量到过）。仍读不回来时由调用方报 unreadable：宁可失败，不猜。
    """
    locate = (cur or {}).get("locate") or {}
    name = cfg.writer_tabs[cfg.writers[key]]
    score = locate.get(name)
    if score is None or score < WRITER_TEMPLATE_HIT:
        return None
    others = [v for k, v in locate.items() if k != name and v is not None]
    if others and score - max(others) < WRITER_TEMPLATE_MARGIN:
        return None
    return {"ok": True, "stage": "assumed", "before": key, "after": key,
            "basis": "template_hit", "degraded": True, "locate": locate,
            "hint": (f"逐像素差读不出模式（{cur.get('diff') or '无数据'}），按模板相似度 "
                     f"{score:.3f} 认定已是 {key}；这只是兜底，不是逐像素确认")}


def choose_writer(s: Screen, cfg: Chooser, key: str) -> dict:
    if key not in cfg.writers:
        return {"ok": False, "stage": "bad_arg", "want": key, "options": list(cfg.writers)}
    cur = read_writer(s, cfg)
    if cur["writer"] == "unknown":
        assumed = assume_writer(cur, cfg, key)
        if assumed is not None:
            return assumed
        return {"ok": False, "stage": "unreadable", "hint": cur.get("hint", ""),
                "diff": cur.get("diff"), "locate": cur.get("locate")}
    if cur["writer"] == key:
        return {"ok": True, "stage": "already", "before": cur["writer"], "after": cur["writer"],
                "basis": cur.get("basis"), "diff": cur.get("diff")}

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
            "before": cur["writer"], "after": after["writer"],
            "basis": after.get("basis"), "diff": after.get("diff"),
            "locate": after.get("locate"),
            "hint": None if ok else "点完之后读不回目标模式：不认成功，请人工看一眼页面"}


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
    """读当前芯片。**多尺度 + 唯一性**（CARD-06b 第 6 条）。

    旧实现按 `has_chip` 顺序取第一个过阈值的模板：菜单项和芯片长得像时，
    "第一个命中"可能就是菜单里那一行；两个芯片都命中时也分不出是谁。
    现在：
      · 每个 chip 模板都走 `find_unique`（尺度 0.8–1.25）；
      · 同一模板多处命中 → `reason=ambiguous`，state=unknown（宁可读不到，不猜）；
      · 两个不同 chip 同时命中 → 同样 unknown。
    返回值多带 template/score/threshold/scale/zoom 相关的字段，失败留证要用。
    """
    _calm(s, cfg)
    region = _win(s, cfg)
    hits, best = [], None
    for key in cfg.has_chip:
        name = _chip(cfg, key)
        got = s.find_unique(name, region=region, scales=zoom_mod.SCALES)
        score = got.get("best")
        info = {"name": name, "score": None if score is None else round(score, 3),
                "threshold": s.threshold, "scale": got.get("scale"),
                "hit": got.get("match") is not None}
        if best is None or (info["score"] or 0) > (best["score"] or 0):
            best = info
        if got.get("ambiguous"):
            return {"state": "unknown", "reason": "ambiguous", "template": info,
                    "score": info["score"], "scale": info["scale"],
                    "hint": f"{name} 在同一画面上命中多处：可能是菜单项和芯片同时匹上，"
                            "不取第一个（多尺度不能替代唯一性校验）"}
        if got.get("match") is not None:
            hits.append((key, got["match"], info))
    if len(hits) == 1:
        key, match, info = hits[0]
        return {"state": key, "score": round(match.score, 3),
                "scale": getattr(match, "scale", 1.0), "template": info,
                "threshold": s.threshold}
    if len(hits) > 1:
        return {"state": "unknown", "reason": "multiple_chips",
                "template": best, "score": best["score"] if best else None,
                "hint": "同时命中多个芯片模板（" + "、".join(k for k, _m, _i in hits) +
                        "）：认不出选中哪一个"}
    return {"state": "unknown", "reason": "template_miss", "template": best,
            "score": best["score"] if best else None,
            "hint": "当前芯片和任何一张 chip 图都对不上（可能是没截过的档位，或主题/缩放变了）"}


def _unique(s: Screen, name: str, *, threshold: float, region) -> dict:
    """多尺度 + 唯一性匹配。尺度限制在 0.8–1.25（CARD-06b 第 6 条）。"""
    return s.find_unique(name, threshold=threshold, region=region,
                         scales=zoom_mod.SCALES)


#: 最近一次 `open_menu` 的分步证据。B16：grok 的 `menu_not_open` 以前只有一个
#: 布尔值，anchor 找不着和菜单开了但一行都匹不上完全分不清。
#: 仍然**只返 bool**（`choose()` 与测试桩按这个契约打桩），诊断走这个旁路。
_LAST_OPEN: dict = {}


def open_menu(s: Screen, cfg: Chooser) -> bool:
    global _LAST_OPEN
    win = _win(s, cfg)
    info: dict = {"anchor": None, "items": {}, "why": "unknown",
                  "win": list(win) if win else None}
    _LAST_OPEN = info
    if cfg.open_by_chip:
        opened = False
        for key in cfg.has_chip:
            got = _unique(s, _chip(cfg, key), threshold=0.90, region=win)
            info[f"chip_{key}"] = {"best": got.get("best"),
                                   "hit": got["match"] is not None}
            if got["match"] is not None:
                s.click(got["match"])
                time.sleep(0.8)
                opened = True
                break
        if not opened:
            info["why"] = "chip_not_found"
            return False
    else:
        a = s.find(cfg.anchor, threshold=0.85, region=win)
        info["anchor"] = {"hit": a is not None,
                          "best": getattr(s, "last_best", None),
                          "threshold": 0.85, "offset": list(cfg.open_offset)}
        if a is None:
            info["why"] = "anchor_not_found"
            return False
        s.click(a, offset=cfg.open_offset)
        time.sleep(0.7)
    for key in cfg.options:
        got = _unique(s, _item(cfg, key), threshold=0.88, region=win)
        info["items"][key] = {"best": got.get("best"),
                              "hit": got["match"] is not None,
                              "ambiguous": bool(got.get("ambiguous"))}
        if got["match"] is not None:
            info["why"] = "opened"
            return True
    info["why"] = "menu_empty"
    return False


def choose(s: Screen, cfg: Chooser, key: str) -> dict:
    if key not in cfg.options:
        return {"ok": False, "stage": "bad_arg", "want": key, "options": list(cfg.options)}
    before = read_current(s, cfg)
    if before["state"] == key:
        # 已经是目标档：不要为了"走一遍流程"去点开菜单——多一次点击就多一次
        # 出错机会，而且菜单模板本身也可能失配（2026-11-02 实测 grok 如此）。
        return {"ok": True, "stage": "already", "before": key, "after": key,
                "chip_score": before.get("score"), "scale": before.get("scale"),
                "template": before.get("template")}
    if not open_menu(s, cfg):
        # `globals().get` 是刻意的：`choose` 会被测试按函数名抠出来 exec，
        # 那份命名空间里没有 `_LAST_OPEN`，不能因为加诊断就把它们打红。
        detail = dict(globals().get("_LAST_OPEN") or {})
        why = detail.get("why") or "unknown"
        return {"ok": False, "stage": "menu_not_open", "why": why,
                "before": before["state"],
                "template": before.get("template"), "score": before.get("score"),
                "probe": detail,
                "hint": ("点不开菜单（" + why + "）：" +
                         {"anchor_not_found": "输入框锚点模板没匹上，重截 grok_composer",
                          "menu_empty": "菜单开了但没有一行 item 模板匹上，重截 grok_item_*",
                          "chip_not_found": "当前档位芯片没匹上，重截 grok_chip_*"}.get(
                              why, "确认页面在前台、缩放 100%（换主题/缩放要重截模板）"))}
    got = _unique(s, _item(cfg, key), threshold=0.88, region=_win(s, cfg))
    hit = got["match"]
    if hit is None:
        return {"ok": False, "stage": "item_not_found", "before": before["state"], "want": key,
                "template": {"name": _item(cfg, key), "score": got.get("best"),
                             "threshold": 0.88, "scale": got.get("scale"),
                             "hit": False},
                "reason": "ambiguous" if got.get("ambiguous") else "template_miss",
                "hint": (f"菜单里找不到 {_item(cfg, key)}.png"
                         + ("（同一模板命中多处，判为不可信）" if got.get("ambiguous") else ""))}
    s.click(hit)
    time.sleep(0.9)

    if key in cfg.has_chip:
        after = read_current(s, cfg)
        ok = after["state"] == key
        return {"ok": ok, "stage": "done" if ok else "unverified",
                "before": before["state"], "after": after["state"],
                "chip_score": after.get("score"), "scale": after.get("scale"),
                "template": after.get("template"),
                "reason": "" if ok else after.get("reason", "readback_mismatch")}
    win = _win(s, cfg)
    still = any(_unique(s, _item(cfg, k), threshold=0.90, region=win)["match"] is not None
                for k in cfg.options)
    return {"ok": not still, "stage": "done_no_chip" if not still else "menu_still_open",
            "before": before["state"], "after": key, "verified_by": "menu_closed"}


# ------------------------------------------------------------------ CLI
def cli(cfg: Chooser, argv: list[str]) -> int:
    need, wait, force, argv = guard.parse_guard_args(list(argv))
    parser = argparse.ArgumentParser(prog=f"{cfg.name}_choose.py",
                                     description="按写入器、模型的顺序选择并复核")
    parser.add_argument("--list", "-l", action="store_true")
    parser.add_argument("--writer", choices=cfg.writers, type=str.lower)
    parser.add_argument("--model", "--effort", "--mode", dest="model",
                        choices=cfg.options, type=str.lower)
    parser.add_argument("--tab", type=int, choices=range(1, 10))
    args = parser.parse_args(argv)
    do_list, want, writer = args.list, args.model, args.writer
    if do_list and (want is not None or writer is not None):
        parser.error("--list 不能与选择模式或模型的参数同用")
    if not (do_list or want or writer):
        parser.print_help()
        return 2

    if not guard.guard(need, wait, force):
        return 4
    s = Screen()
    hwnd = ensure_site(cfg.name, tab=args.tab)
    if hwnd is None:
        print("开不出浏览器窗口（检查 Chrome 路径）")
        return 1
    s.bind(hwnd)
    if not s.focus():
        print("窗口提不到前台")
        return 1
    s.tab_site = cfg.name
    time.sleep(1.2)
    try:
        # 页面缩放预检（CARD-06b 第 6 条）：非 100% 先按 Ctrl+0，仍不是就退出。
        # 只对浏览器窗口发快捷键；桌面端/读不到进程名的窗口直接跳过。
        zoom_mod.ensure(s, verbose=True)
        if do_list:
            out = dict(read_current(s, cfg))
            if cfg.writers:
                out.update(read_writer(s, cfg))
            print(json.dumps(out, ensure_ascii=False))
            # 任何一个读不出来的字段都算失败：返回 0 会让验证器把"没读到"记成通过
            unknown = out["state"] == "unknown" or out.get("writer") == "unknown"
            return 1 if unknown else 0
        if writer is not None:
            w = choose_writer(s, cfg, writer)
            print(json.dumps(w, ensure_ascii=False))
            if not w["ok"]:
                return _fail(cfg.name, s, hwnd, w, 1)
        if want is not None:
            r = choose(s, cfg, want)
            print(json.dumps(r, ensure_ascii=False))
            if not r["ok"]:
                return _fail(cfg.name, s, hwnd, r, 1)
            # 读回确认了才写档位回执（CARD-06a (a)）。没 chip 可读、只靠"菜单关了"
            # 验证的档位（grok auto/build）把 readback 记成空——use 会因为
            # readback_missing 拒发，那是对的：它确实没被读回来过。
            #
            # 用 globals() 取而不是直接引用：test_web_cli 会把 cli 这一个函数
            # 从源码里抠出来 exec（没有包上下文），取不到就跳过，不让测试失败
            # 变成"回执功能没做"的假象。
            ledger = globals().get("tier_guard")
            if ledger is not None:
                readback = None if r.get("verified_by") == "menu_closed" else r.get("after")
                ledger.record_choice(cfg.name, target=want, readback=readback,
                                     dimension="model", hwnd=hwnd, tab=args.tab, raw=r)
    except ForegroundLost as e:
        print("失败:", json.dumps({"ok": False, "stage": "user_took_over",
                                  "hint": str(e)}, ensure_ascii=False))
        return 4
    except WindowMinimized as e:
        # 最小化且恢复失败：退出码 7，不抛 traceback（CARD-06a (c)）
        print("失败:", json.dumps({"ok": False, "stage": "window_minimized",
                                  "hint": str(e), **e.facts}, ensure_ascii=False))
        return e.code
    except zoom_mod.ZoomNot100 as e:
        result = {**e.as_result(), "ok": False}
        _evidence(cfg.name, s, hwnd, result)
        print("失败:", json.dumps(result, ensure_ascii=False))
        return e.code
    return 0


def _evidence(endpoint, screen, hwnd, result) -> None:
    """失败留证（CARD-06b 第 9 条）。取不到 screen 就交给 failure 自己截图。"""
    module = None
    try:                                  # 单文件测试会把这个函数抠出来跑，取不到就跳过
        from . import failure as module     # noqa: F811 - 允许 ImportError 时保持 None
    except Exception:                     # noqa: BLE001
        module = None
    if module is not None:
        module.record(endpoint, result, screen=screen, hwnd=hwnd)


def _fail(endpoint, screen, hwnd, result, code) -> int:
    _evidence(endpoint, screen, hwnd, result)
    return code
