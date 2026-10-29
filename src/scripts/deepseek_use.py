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

    # 1. 新对话
    if mode == "new":
        s.click_image("ds_newchat", wait=3.0)
        time.sleep(0.8)

    # 2. 写入
    composer = s.find("ds_composer", threshold=0.80)
    if composer is None:
        return {"ok": False, "stage": "composer_not_found", "elapsed": round(time.time() - t0, 1),
                "hint": "页面上没有找到输入框锚点图；确认 DeepSeek 页在前台且没被遮挡"}
    s.click(composer)
    time.sleep(0.2)
    s.hotkey("ctrl", "a")
    time.sleep(0.1)
    s.type_text(prompt)
    time.sleep(0.2)

    # 3. 提交：只点发送键，不按 Enter
    send = s.find("ds_send_on", threshold=0.85)
    if send is None:
        return {"ok": False, "stage": "send_not_available", "elapsed": round(time.time() - t0, 1),
                "hint": "发送键不可用：可能没写进输入框，或该端发送键是灰态（另存一张 ds_send_on 的可用态图）"}
    s.click(send)

    # 4. 等生成结束
    if lib.has("ds_stop"):
        if s.wait("ds_stop", timeout=12.0, appear=True) is None:
            return {"ok": False, "stage": "no_generating_signal", "elapsed": round(time.time() - t0, 1),
                    "hint": "点了发送但没看到生成中的停止键：可能没发出去"}
        if s.wait("ds_stop", timeout=timeout, appear=False) is None:
            return {"ok": False, "stage": "timeout", "elapsed": round(time.time() - t0, 1)}

    # 5. 复制回答（取最靠下的那个复制键：回答在提问下面）
    hits, how = [], "none"
    deadline = time.time() + timeout
    while time.time() < deadline:
        hits, how = _hover_hunt_copy(s)
        if hits:
            break
        time.sleep(1.0)
    if not hits:
        return {"ok": False, "stage": "copy_not_found", "elapsed": round(time.time() - t0, 1),
                "hint": "没找到回答的复制键：确认 ds_copy.png 是回答动作条里那一个，而不是提问上的"}
    target = max(hits, key=lambda m: m.y)

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
                "copies": len(hits), "how": how,
                "hint": "点了复制键但剪贴板没变：可能点到了别的按钮，或该键需要更长响应"}

    return {
        "ok": True,
        "stage": "done",
        "reply": reply,
        "elapsed": round(time.time() - t0, 1),
        "basis": "copied",
        "copies": len(hits),
        "hover": how,
        "score": round(target.score, 3),
        "at": [target.center[0], target.center[1]],
        "mode": mode,
        "prompt_len": len(prompt),
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
