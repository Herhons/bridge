"""deepseek 网页版：提问 → 收回答。只认图，不认控件树。

用法：
    python deepseek_use.py "只回复两个字：收到"
    python deepseek_use.py "..." --continue      # 不新开对话，直接发在当前会话
    python deepseek_use.py "..." --timeout 300

流程（每一步都是找图，找回找不到就停，不猜位置）：
    点 ds_newchat（--new 时）→ 点 ds_composer → Ctrl+A 全选 → 逐字输入
    → 点 ds_send_on → 等 ds_stop 消失 → 找最靠下的 ds_copy 点它 → 读剪贴板

需要资源库：
    必需  ds_composer / ds_send_on / ds_copy
    可选  ds_newchat（--new 用）/ ds_stop（判生成结束，没有就退回等 ds_copy）

三条不做：
1. 发送键找不到时**不按 Enter 兜底**（会把半截内容发出去，v0.0.8 就栽在这）。
2. 复制读不到时不做文本猜测（答案是长文，猜必错）。
3. 不碰窗口位置、不切标签页、不新开浏览器。
"""

from __future__ import annotations

import json
import os
import sys
import time

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import Library, Screen  # noqa: E402

REQUIRED = ["ds_composer", "ds_send_on", "ds_copy"]
OPTIONAL = ["ds_newchat", "ds_stop"]

# ds_composer 是输入框右侧的麦克风图标（不管开关状态都在），
# 点它本身会触发语音输入，所以要按偏移量点到左边的文字行上。
COMPOSER_OFFSET = (-300, -65)
# 输入框在屏幕上的范围，用来验证"字到底写进去没有"
COMPOSER_REGION = (-977, 603, 776, 124)


def _parse(argv: list[str]) -> tuple[str, str, float, list[str]]:
    mode = "new"
    timeout = 180.0
    words: list[str] = []
    bad: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--new":
            mode = "new"
        elif a == "--continue":
            mode = "continue"
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
    return " ".join(words), mode, timeout, bad


def _send_enabled(s: Screen, m) -> bool:
    """发送键到底可不可点，只能看颜色。

    模板匹配是归一化相关，深蓝（可点）和浅蓝（不可点）都判 1.000，
    但两者 B-R 差得很远：实测可点约 148，不可点约 59。
    """
    b, _g, r = s.region_mean(m)
    return (b - r) > 100


def _is_action_row(s: Screen, m) -> bool:
    """判断这个复制键是不是回答动作条里的那一个。

    依据：回答动作条右边还有 重新生成/点赞/点踩/朗读/分享，一排五六个；
    提问气泡上的复制键是孤立的。所以"右侧还有没有同伴图标"能直接分开两者的结构。
    """
    import cv2
    import numpy as np

    x0, y0 = m.x + m.w + 4, m.y - 3
    strip = s.grab((x0, y0, 190, m.h + 6))
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


def _pick_answer_copy(hits: list, s: Screen) -> tuple[object, dict]:
    """从所有复制键里挑回答那一个。

    两道判据叠加：
    1. 结构：右侧有同伴图标的才算动作条（提问上的那个是孤立的）。
    2. 兜底：都没有同伴时退回"最左 + 最下"——提问靠右、回答靠左。
    """
    rows = [h for h in hits if _is_action_row(s, h)]
    if rows:
        target = max(rows, key=lambda m: m.y)
        return target, {"rule": "action_row", "candidates": len(rows), "total": len(hits)}

    left = min(h.x for h in hits)
    lefts = [h for h in hits if h.x <= left + 40]
    target = max(lefts, key=lambda m: m.y)
    return target, {"rule": "leftmost", "leftmost_x": left, "left_count": len(lefts), "total": len(hits)}


def _hover_hunt_copy(s: Screen) -> tuple[list, str]:
    """找回答的复制键。它常常只在鼠标悬停时才出现，所以分几档高度扫一下。"""
    hits = s.find_all("ds_copy")
    if hits:
        return hits, "direct"
    vx, vy, vw, vh = s.bounds()
    for frac in (0.55, 0.65, 0.75, 0.85):
        s.move_to(vx + int(vw * 0.62), vy + int(vh * frac))
        time.sleep(0.3)
        hits = s.find_all("ds_copy")
        if hits:
            return hits, f"hover@{frac}"
    return [], "none"


def ask(prompt: str, *, mode: str = "new", timeout: float = 180.0) -> dict:
    t0 = time.time()
    lib = Library()
    miss = lib.missing(REQUIRED)
    if miss:
        return {"ok": False, "stage": "no_template", "missing": miss,
                "hint": "先截这几张：python tools/snip.py --save <名字> --region x,y,w,h"}
    if mode == "new" and not lib.has("ds_newchat"):
        return {"ok": False, "stage": "no_template", "missing": ["ds_newchat"],
                "hint": "缺 ds_newchat；想发在当前会话就用 --continue"}

    s = Screen()
    # 截图看不到被遮挡的部分，先把 DeepSeek 窗口提到前台（只提前台，不新建不移动）
    front = s.bring_to_front("DeepSeek")
    if not front:
        return {"ok": False, "stage": "page_not_front", "elapsed": round(time.time() - t0, 1),
                "hint": "没找到标题含 DeepSeek 的窗口；先把页面打开"}

    # 1. 新对话
    if mode == "new":
        s.click_image("ds_newchat", wait=3.0)
        time.sleep(0.8)

    # 2. 写入（判"写进去没有"用发送键是否变可用，比像素差分稳）
    composer = s.find("ds_composer", threshold=0.80)
    if composer is None:
        return {"ok": False, "stage": "composer_not_found", "elapsed": round(time.time() - t0, 1),
                "hint": "页面上没有找到输入框锚点图；确认 DeepSeek 页在前台且没被遮挡"}

    send = None
    for attempt in range(2):
        s.click(composer, offset=COMPOSER_OFFSET)
        time.sleep(0.25)
        if attempt == 0:
            # 首次点击常常只用于激活窗口，补一次
            s.click(composer, offset=COMPOSER_OFFSET)
            time.sleep(0.2)
        s.hotkey("ctrl", "a")
        time.sleep(0.1)
        s.type_text(prompt)
        time.sleep(0.4)
        cand = s.find("ds_send_on", threshold=0.92)
        if cand is not None and _send_enabled(s, cand):
            send = cand
            break
    if send is None:
        return {"ok": False, "stage": "write_not_visible", "elapsed": round(time.time() - t0, 1),
                "hint": "输入后发送键没变成可用态：焦点没进输入框，或键被拦"}

    # 3. 提交：只点发送键，不按 Enter
    s.click(send)
    t_sent = time.time()

    # 4. 等生成结束（停止键出现 → 消失）。没有 ds_stop 就退回"等复制键"。
    generating_seen = False
    if lib.has("ds_stop"):
        generating_seen = s.wait_present("ds_stop", timeout=12.0)
        if not generating_seen:
            time.sleep(1.5)  # 极短回答可能没赶上停止键，继续往下走
        elif not s.wait_gone("ds_stop", timeout=timeout):
            return {"ok": False, "stage": "timeout", "elapsed": round(time.time() - t0, 1),
                    "hint": "停止键一直没消失，生成超时"}

    # 5. 复制回答
    hits, how = [], "none"
    deadline = time.time() + timeout
    while time.time() < deadline:
        hits, how = _hover_hunt_copy(s)
        if hits and (generating_seen or time.time() - t_sent > 2.0):
            break
        time.sleep(1.0)
    if not hits:
        return {"ok": False, "stage": "copy_not_found", "elapsed": round(time.time() - t0, 1),
                "hint": "没找到回答的复制键：确认 ds_copy.png 是回答动作条里那一个，而不是提问上的"}

    target, pick = _pick_answer_copy(hits, s)
    # 候选顺序：按左右挑出来的那个优先，其余按 y 从下往上兜底
    ordered = [target] + [h for h in sorted(hits, key=lambda m: m.y, reverse=True) if h is not target]

    reply, tried = "", []
    sentinel = f"__QI_EMPTY_{int(time.time() * 1000)}__"
    for cand in ordered[:3]:
        s.set_clipboard(sentinel)
        s.click(cand)
        got = ""
        for _ in range(20):
            time.sleep(0.2)
            got = s.clipboard()
            if got and got != sentinel:
                break
        tried.append({"x": cand.x, "y": cand.y, "chars": len(got) if got != sentinel else 0})
        if got and got != sentinel and got.strip() != prompt.strip():
            reply, target = got, cand
            break

    if not reply:
        # 三种可能：剪贴板没变、只拿到提问本身
        copied_prompt = any(t["chars"] > 0 for t in tried)
        return {"ok": False,
                "stage": "copied_prompt" if copied_prompt else "copy_failed",
                "elapsed": round(time.time() - t0, 1),
                "copies": len(hits), "how": how, "pick": pick, "tried": tried,
                "hint": "所有候选复制键拿到的都是提问内容或空。回答的动作条可能还没渲染，或被顶出视口"}

    return {
        "ok": True,
        "stage": "done",
        "reply": reply,
        "elapsed": round(time.time() - t0, 1),
        "basis": "copied",
        "copies": len(hits),
        "how": how,
        "pick": pick,
        "score": round(target.score, 3),
        "at": [target.center[0], target.center[1]],
        "mode": mode,
        "prompt_len": len(prompt),
        "generating_seen": generating_seen,
    }


def main() -> int:
    name = os.path.basename(__file__)
    prompt, mode, timeout, bad = _parse(sys.argv[1:])
    if bad:
        print("参数有问题：", bad)
        print(f"用法: python {name} \"你的提问\" [--new|--continue] [--timeout 180]")
        return 2
    if not prompt.strip():
        print(f"用法: python {name} \"你的提问\" [--new|--continue] [--timeout 180]")
        print("  --new       先点新对话再发（默认）")
        print("  --continue  直接发在当前对话里")
        return 2

    lib = Library()
    print(lib.report(REQUIRED))
    if lib.missing(REQUIRED):
        return 3

    result = ask(prompt, mode=mode, timeout=timeout)
    if result.get("ok"):
        print("=== 回答 ===")
        print(result.pop("reply"))
        print("=== 元数据 ===")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print("失败:", json.dumps(result, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
