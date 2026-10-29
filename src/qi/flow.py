"""通用「提问 → 收回答」流程。四个网页端共用这一份，差异全部压进 Site 配置。

v0.0.8 的教训是同一套流程抄 8 遍，改一处要落 8 次。这里只有一份：
    找新对话（侧栏没开就先开）→ 点输入框 → 全选→输入 → 点发送
    → 等生成结束（停止键/画面静止）→ 找动作条 → 点排内复制 → 读剪贴板
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass

from . import guard
from .screen import Match, Screen


@dataclass(frozen=True)
class Site:
    name: str                  # 端点名（用于日志）
    title_hint: str            # 窗口标题关键词，用来提到前台
    composer: str              # 输入框锚点模板名
    send: str                  # 发送键（可用态）模板名
    actions: str | tuple[str, ...]     # 回答动作条整排模板（对话/任务可能不是同一排，可给多个）
    copy: str                  # 动作条内复制键模板名
    composer_offset: tuple[int, int]   # 从锚点中心到文字区的偏移
    newchat: str | tuple[str, ...] | None = None   # 新对话按钮（可多个候选：整条 / 收起后的图标）
    sidebar: str | None = None         # 侧栏折叠/展开按钮（侧栏没开时先点它）
    stop: str | None = None            # 生成中的停止键
    send_mode: str = "color"           # color=靠颜色判断可用；exists=出现即可用
    send_blue_min: float = 100.0       # B-R 大于它算可点
    send_threshold: float = 0.92
    on_secondary: bool = True          # 只认副屏上那个浏览器窗口


def _win(s: Screen, site: Site):
    """目标窗口矩形。所有 find 都必须限定在它里面。

    不限定就是在整个虚拟桌面（-1440..2560）里找，主屏上开着同款页面时
    会先命中主屏那份，然后拿着主屏的坐标去点——点的是用户的窗口。
    """
    return s.window_rect(site.title_hint, on_secondary=site.on_secondary)


def _send_enabled(s: Screen, site: Site, m: Match) -> bool:
    if site.send_mode == "exists":
        return True
    b, _g, r = s.region_mean(m)
    return (b - r) > site.send_blue_min


def _open_new_chat(s: Screen, site: Site) -> dict:
    """点新对话。

    侧栏有两种形态：展开是一整条「新聊天」，收起只剩一个铅笔图标。
    所以这里按候选列表找：整条 → 图标 → 都不是就先点侧栏开关再找一遍。
    """
    if not site.newchat:
        return {"ok": True, "stage": "skipped"}
    names = (site.newchat,) if isinstance(site.newchat, str) else tuple(site.newchat)

    win = _win(s, site)

    def _try() -> object:
        for nm in names:
            hit = s.find(nm, threshold=0.85, region=win)
            if hit is not None:
                return hit
        return None

    hit = _try()
    if hit is None and site.sidebar:
        sb = s.find(site.sidebar, region=win)
        if sb is not None:
            # 必须把 find 的结果存下来再点。原来是 if s.find(...) 然后又 s.find(...) 一次，
            # 第二次可能返回 None（页面正在变），s.click(None) 直接 AttributeError。
            s.click(sb)
            time.sleep(0.7)
            hit = _try()
    if hit is None:
        return {"ok": False, "stage": "newchat_not_found",
                "tried": list(names),
                "hint": "找不到新对话按钮：补一张收起状态的图标图，或一张侧栏开关图"}
    s.click(hit)
    time.sleep(0.9)
    return {"ok": True, "stage": "clicked", "via": hit.name}


def _is_action_row(s: Screen, m: Match) -> bool:
    """复制键右侧还有没有同伴图标。回答动作条是一排，提问上那个是孤立的。"""
    import cv2
    import numpy as np

    strip = s.grab((m.x + m.w + 4, m.y - 3, 190, m.h + 6))
    if strip.size == 0:
        return False
    g = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
    mask = (g < 210).astype(np.uint8)
    n, _lab, st, _c = cv2.connectedComponentsWithStats(mask, 8)
    peers = 0
    for i in range(1, n):
        _x, _y, w, h, area = st[i]
        if 6 <= w <= 26 and 6 <= h <= 26 and 30 <= area <= 400:
            peers += 1
    return peers >= 2


def _pick_copy_target(s: Screen, site: Site) -> tuple[Match | None, dict]:
    names = (site.actions,) if isinstance(site.actions, str) else tuple(site.actions)
    win = _win(s, site)
    rows: list[Match] = []
    for nm in names:
        rows += s.find_all(nm, threshold=0.88, region=win)
    if not rows:
        time.sleep(0.5)                      # 动作条可能还在渐入
        for nm in names:
            rows += s.find_all(nm, threshold=0.88, region=win)
    if rows:
        row = max(rows, key=lambda m: m.y)      # 多轮会话取最靠下那排＝最新回答
        inner = s.find(site.copy, threshold=0.85, region=(row.x, row.y, row.w, row.h))
        if inner is not None:
            return inner, {"rule": "actions_row", "row": [row.x, row.y, row.w, row.h],
                           "tpl": row.name.split("@")[0]}
        return Match(site.copy + "@row_offset", 1.0, row.x + 6, row.y + 6, 22, 22), {
            "rule": "actions_row_offset", "row": [row.x, row.y, row.w, row.h]}

    hits = s.find_all(site.copy, threshold=0.85, region=win)
    if not hits:
        hits = _hover_hunt(s, site)
    if not hits:
        return None, {"rule": "none"}
    rows2 = [h for h in hits if _is_action_row(s, h)]
    if rows2:
        return max(rows2, key=lambda m: m.y), {"rule": "peer", "candidates": len(rows2)}
    left = min(h.x for h in hits)
    lefts = [h for h in hits if h.x <= left + 40]
    return max(lefts, key=lambda m: m.y), {"rule": "leftmost", "total": len(hits)}


def _hover_hunt(s: Screen, site: Site) -> list[Match]:
    """找不到动作条时，把鼠标在页面里挪几个位置，让它 hover 出来。

    **必须限定在目标窗口里**。原来用的是 s.bounds()（整个虚拟桌面 4000x2560），
    x 算出来是 1040 —— 那是**主屏**，用户正在那儿看视频。鼠标会跑到主屏上晃，
    正是"不许抢"明确禁止的行为。
    """
    win = s.window_rect(site.title_hint, on_secondary=site.on_secondary)
    if win is None:
        return []
    x, y, w, h = win
    for frac in (0.55, 0.65, 0.75, 0.85):
        s.move_to(x + int(w * 0.62), y + int(h * frac))
        time.sleep(0.3)
        hits = s.find_all(site.copy, threshold=0.85, region=win)
        if hits:
            return hits
    return []


def ask(site: Site, prompt: str, *, mode: str = "new", timeout: float = 180.0,
        verbose: bool = True, tab: int | None = None, prepare=None) -> dict:
    """prepare(s) -> dict：新对话之后、写输入之前要做的准备（比如切任务模式）。

    顺序不能反：Gemini 点「新对话」会重置模式，先切后点等于白切。
    """
    t0 = time.time()
    s = Screen(verbose=verbose)

    if not s.bring_to_front(site.title_hint, on_secondary=site.on_secondary):
        return {"ok": False, "stage": "page_not_front", "elapsed": round(time.time() - t0, 1),
                "hint": f"没找到标题含 {site.title_hint!r} 的窗口"}
    if tab:
        s.hotkey("ctrl", str(tab))
        time.sleep(1.8)

    # 1. 新对话
    if mode == "new":
        r = _open_new_chat(s, site)
        if not r["ok"]:
            return {"ok": False, "stage": r["stage"], "elapsed": round(time.time() - t0, 1),
                    "hint": r.get("hint", "")}

    # 1b. 准备（切模式等）。必须在点完新对话之后——否则会被重置。
    if prepare is not None:
        pr = prepare(s)
        if not pr.get("ok"):
            return {"ok": False, "stage": "prepare_failed", "elapsed": round(time.time() - t0, 1),
                    "prepare": pr,
                    "hint": pr.get("hint") or "准备步骤失败（切换模式/档位时复核没过）"}

    # 2. 写入
    win = _win(s, site)
    anchor = s.find(site.composer, threshold=0.80, region=win)
    if anchor is None:
        return {"ok": False, "stage": "composer_not_found", "elapsed": round(time.time() - t0, 1),
                "hint": "找不到输入框锚点；确认页面在前台、没被遮挡、缩放没变"}
    send = None
    for attempt in range(2):
        s.click(anchor, offset=site.composer_offset)
        time.sleep(0.25)
        if attempt == 0:
            s.click(anchor, offset=site.composer_offset)   # 首次点击常只用于激活窗口
            time.sleep(0.2)
        s.hotkey("ctrl", "a")
        time.sleep(0.1)
        s.type_text(prompt)
        time.sleep(0.4)
        cand = s.find(site.send, threshold=site.send_threshold, region=win)
        if cand is not None and _send_enabled(s, site, cand):
            send = cand
            break
    if send is None:
        return {"ok": False, "stage": "write_not_visible", "elapsed": round(time.time() - t0, 1),
                "hint": "输入后发送键没变成可用态：焦点没进输入框，或键被拦"}

    # 3. 提交前再确认一次：写字的这几秒里你可能开始用电脑了
    if guard.user_active(0.6):
        return {"ok": False, "stage": "user_took_over", "elapsed": round(time.time() - t0, 1),
                "hint": "检测到你在用电脑，已中止（没点发送）。稍后重跑即可。"}
    s.click(send)

    # 4. 等生成结束
    generating_seen = False
    if site.stop:
        generating_seen = s.wait_present(site.stop, timeout=12.0, region=win)
        if generating_seen and not s.wait_gone(site.stop, timeout=timeout, region=win):
            return {"ok": False, "stage": "timeout", "elapsed": round(time.time() - t0, 1),
                    "hint": "停止键一直没消失，生成超时"}
    if not generating_seen:
        page = s.monitor_rect(*anchor.center)
        if not s.wait_quiet(timeout=timeout, region=page):
            return {"ok": False, "stage": "timeout", "elapsed": round(time.time() - t0, 1),
                    "hint": "等画面静止超时，生成可能还没结束"}

    # 5. 复制
    target, pick = None, {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        target, pick = _pick_copy_target(s, site)
        if target is not None:
            break
        time.sleep(1.0)
    if target is None:
        return {"ok": False, "stage": "copy_not_found", "elapsed": round(time.time() - t0, 1),
                "pick": pick,
                "hint": f"没找到动作条：确认 {site.actions}.png 是整排，且页面没被遮挡"}

    sentinel = f"__QI_EMPTY_{int(time.time() * 1000)}__"
    s.set_clipboard(sentinel)
    s.click(target)
    reply = ""
    for _ in range(20):
        time.sleep(0.2)
        reply = s.clipboard()
        if reply and reply != sentinel:
            break
    if not reply or reply == sentinel:
        return {"ok": False, "stage": "copy_failed", "elapsed": round(time.time() - t0, 1),
                "pick": pick, "hint": "点了复制键但剪贴板没变"}
    if reply.strip() == prompt.strip():
        return {"ok": False, "stage": "copied_prompt", "elapsed": round(time.time() - t0, 1),
                "pick": pick, "hint": "复制到的是提问本身，动作条可能还没渲染"}

    return {"ok": True, "stage": "done", "reply": reply, "elapsed": round(time.time() - t0, 1),
            "basis": "copied", "pick": pick, "score": round(target.score, 3),
            "at": [target.center[0], target.center[1]], "mode": mode,
            "prompt_len": len(prompt), "generating_seen": generating_seen}


def cli(site: Site, argv: list[str], *, prepare=None, extra_help: str = "") -> int:
    need, wait, force, argv = guard.parse_guard_args(list(argv))
    mode, timeout, words, bad, tab = "new", 180.0, [], [], None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--new":
            mode = "new"
        elif a == "--continue":
            mode = "continue"
        elif a == "--tab":
            if i + 1 < len(argv):
                i += 1
                try:
                    tab = int(argv[i])
                except ValueError:
                    bad.append(f"--tab 要数字，收到 {argv[i]!r}")
            else:
                bad.append("--tab 缺值")
        elif a == "--timeout":
            if i + 1 < len(argv):
                i += 1
                timeout = float(argv[i])
            else:
                bad.append("--timeout 缺值")
        elif a.startswith("-"):
            bad.append(a)
        else:
            words.append(a)
        i += 1
    prompt = " ".join(words)
    name = f"{site.name}_use.py"

    if bad:
        print("参数有问题：", bad)
        print(f'用法: python {name} "你的提问" [--new|--continue] [--timeout 180] [--tab N]')
        if extra_help:
            print(extra_help)
        return 2
    if not prompt.strip():
        print(f'用法: python {name} "你的提问" [--new|--continue] [--timeout 180] [--tab N]')
        print("  --new       先点新对话再发（默认）")
        print("  --continue  直接发在当前对话里")
        print("  --idle 2    你连续 2 秒没动键鼠才开跑（默认 2）")
        print("  --wait 120  最多等你 120 秒（默认 120），等不到就放弃")
        print("  --force     不等，立刻跑（会抢焦点）")
        if extra_help:
            print(extra_help)
        return 2

    if not guard.guard(need, wait, force):
        return 4
    result = ask(site, prompt, mode=mode, timeout=timeout, tab=tab, prepare=prepare)
    if result.get("ok"):
        print("=== 回答 ===")
        print(result.pop("reply"))
        print("=== 元数据 ===")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print("失败:", json.dumps(result, ensure_ascii=False))
    return 1
