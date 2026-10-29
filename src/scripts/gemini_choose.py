"""gemini 网页版：只做选择，不发提问。

用法：
    python gemini_choose.py --list
    python gemini_choose.py --model flash_lite|flash|pro|argon

实测菜单（深色主题）：
    3.5 Flash-Lite / 极速回答      ← 默认
    3.8 Flash      / 全方位帮助
    3.1 Pro        / 高级推理
    4 Argon        / 复杂任务
    思考等级 ›     （子菜单，本脚本没做）

芯片是右对齐的，宽度随模型名变；所以开菜单靠"点当前芯片"，不按固定偏移点。
"""

from __future__ import annotations

import json
import os
import sys
import time

import cv2

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import Library, Screen  # noqa: E402
from qi import guard  # noqa: E402
from qi.browser import ensure_site  # noqa: E402
from qi.choose import Chooser, cli, open_menu, read_writer  # noqa: E402

CFG = Chooser(
    name="gemini",
    anchor="gem_composer",
    open_offset=(0, 0),
    prefix="gem_",
    options={
        "flash_lite": "3.5 Flash-Lite（极速回答，默认）",
        "flash": "3.8 Flash（全方位帮助）",
        "pro": "3.1 Pro（高级推理）",
        "argon": "4 Argon（复杂任务）",
    },
    has_chip=("flash_lite", "flash", "pro", "argon"),
    writer_tabs=("gem_writer_tabs_dialog", "gem_writer_tabs_task"),
    writers={"dialog": 0, "task": 1},
    open_by_chip=True,
)

# 思考等级（--think）**未实现**，相关代码和模板已删除。
# 实测：子菜单只在鼠标真实悬停时展开，脚本点不开（点一下会把整个菜单关掉）。
# 之前这里留着一整套 _open_think / read_think / set_think，但 --think 分支直接返回
# not_supported，那些函数从来没被调用过，属于死代码，已清掉。


def _win(s):
    """目标窗口矩形。不限定就是在整个虚拟桌面里找，主屏上开着同款页面会先命中那边。"""
    return s.window_rect(CFG.title_hint, on_secondary=CFG.on_secondary)


def _items_visible(s) -> bool:
    return any(s.find(f"gem_item_{k}", threshold=0.85, region=_win(s)) is not None for k in CFG.options)


# 芯片右端的下拉箭头相对**麦克风**的偏移。
# 不能对输入框锚点用固定偏移：芯片左边界随模型名长短变（Flash-Lite 宽、Pro 短），
# 固定偏移在名字短的时候会点到空白，表现为"菜单打不开"。
# 麦克风是右对齐的，位置不变，用它当基准稳。
MIC_TO_CARET = (-68, 0)


def _open_model_menu(s) -> bool:
    """用麦克风（右对齐，位置固定）作基准，点芯片右端的下拉箭头。

    输入框空时右侧是麦克风；有文字时是发送键，两者同槽位，所以依次兜底。
    """
    if _items_visible(s):
        return True
    s.press("esc")
    time.sleep(0.3)
    base = s.find("gem_mic", threshold=0.85, region=_win(s)) or s.find("gem_send_on", threshold=0.85, region=_win(s))
    if base is None:
        return False
    s.click(base, offset=MIC_TO_CARET)
    time.sleep(1.1)
    return _items_visible(s)


def read_model(s) -> dict:
    """读当前模型：看菜单项**左侧**有没有勾。

    实测（2026-10-29 新菜单）：勾在标题左边，采样窗 = (m.x-45, m.y+10, 26, 24)，
    选中 42 个亮像素，未选 0。之前判成"勾位置会变"，是因为整行模板把定位点带偏了；
    现在模板只截标题文字（125x19），定位稳定到 1.000，勾区就跟着稳了。
    """
    if not _open_model_menu(s):
        return {"model": "unknown", "hint": "打不开模型菜单"}
    ink = {}
    for k in CFG.options:
        m = s.find(f"gem_item_{k}", threshold=0.88, region=_win(s))
        if m is not None:
            band = s.grab((m.x - 45, m.y + 10, 26, 24))
            g = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
            ink[k] = int((g > 110).sum())
    s.press("esc")
    time.sleep(0.3)
    if not ink:
        return {"model": "unknown", "hint": "模型项一个都没定位到"}
    best = max(ink, key=ink.get)
    return {"model": best if ink[best] > 5 else "unknown", "ink": ink}


def set_model(s, key: str) -> dict:
    if key not in CFG.options:
        return {"ok": False, "stage": "bad_arg", "want": key, "options": list(CFG.options)}
    cur = read_model(s)["model"]
    if cur == key:
        return {"ok": True, "stage": "already", "before": cur, "after": cur}
    if not _open_model_menu(s):
        return {"ok": False, "stage": "menu_not_open", "before": cur}
    m = s.find(f"gem_item_{key}", threshold=0.88, region=_win(s))
    if m is None:
        s.press("esc")
        return {"ok": False, "stage": "item_not_found", "before": cur, "want": key}
    s.click(m)
    time.sleep(1.2)
    _w = _win(s)
    if _w:
        s.move_to(_w[0] + 60, _w[1] + 60)   # 移开鼠标，别压在菜单/芯片上
    time.sleep(0.4)
    after = read_model(s)["model"]
    return {"ok": after == key, "stage": "done" if after == key else "unverified",
            "before": cur, "after": after}


REQUIRED = ([f"gem_item_{k}" for k in CFG.options]
            + ["gem_mic"]
            + list(CFG.writer_tabs))


def main() -> int:
    need, wait, force, argv = guard.parse_guard_args(list(sys.argv[1:]))
    if not guard.guard(need, wait, force):
        return 4
    # --think 走单独的流程（子菜单维度），其余交给通用 cli
    if "--model" in argv:
        i = argv.index("--model")
        if i + 1 >= len(argv):
            print("--model 缺值；可选：", ", ".join(CFG.options))
            return 2
        key = argv[i + 1].strip().lower()
        rest = argv[:i] + argv[i + 2:]
        sc = Screen()
        hwnd = ensure_site("gemini")
        if hwnd is None:
            print("开不出浏览器窗口"); return 1
        sc.bind(hwnd)
        if not sc.focus():
            print("窗口提不到前台"); return 1
        time.sleep(1.2)
        # --tab 不再生效：ensure_site 保证当前标签就是目标页
        r = set_model(sc, key)
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("ok") else 1

    if "--think" in argv:
        # 实测：思考等级子菜单点一下会把整个菜单关掉，悬停也不触发（鼠标瞬移不产生 hover）。
        # 与其留一个永远失败的开关，不如明确说不支持。
        print(json.dumps({"ok": False, "stage": "not_supported",
                          "hint": "Gemini 思考等级未支持：子菜单只在鼠标真实悬停时展开，脚本点不开。"
                                  "需要的话请在网页上手动选。"}, ensure_ascii=False))
        return 1

    if "--list" in argv:
        sc = Screen()
        hwnd = ensure_site("gemini")
        if hwnd is None:
            print("开不出浏览器窗口"); return 1
        sc.bind(hwnd)
        if not sc.focus():
            print("窗口提不到前台"); return 1
        time.sleep(1.2)
        # --tab 不再生效：ensure_site 保证当前标签就是目标页
        # 读状态不改页面。模式按钮只在新对话页出现，会话页里读不到是正常的。
        w = read_writer(sc, CFG).get("writer")
        out = {"writer": w}
        out.update(read_model(sc))
        if w in (None, "unknown"):
            out["note"] = "模式按钮只在新对话页出现（当前在会话页）。用 --new 先开新对话再读；本命令不替你重置会话。"
        print(json.dumps(out, ensure_ascii=False))
        return 0

    if "--new" in argv:
        # 显式要求：先开新对话，再读/切模式
        sc = Screen()
        hwnd = ensure_site("gemini")
        if hwnd is None:
            print("开不出浏览器窗口"); return 1
        sc.bind(hwnd)
        if not sc.focus():
            print("窗口提不到前台"); return 1
        time.sleep(1.2)
        # --tab 不再生效：ensure_site 保证当前标签就是目标页
        from qi.flow import Site, _open_new_chat
        site = Site(name="gemini", title_hint=CFG.title_hint, composer="gem_composer",
                    send="gem_send_on", actions="gem_actions", copy="gem_copy",
                    composer_offset=(200, 0), newchat="gem_newchat")
        r = _open_new_chat(sc, site)
        time.sleep(1.2)
        rest = [a for a in argv if a != "--new"]
        if not any(a.startswith("--model") or a.startswith("--writer") for a in rest):
            print(json.dumps({"newchat": r, "writer": read_writer(sc, CFG).get("writer")},
                             ensure_ascii=False))
            return 0 if r.get("ok") else 1
        return cli(CFG, rest)

    lib = Library()
    print(lib.report(REQUIRED))
    if lib.missing(REQUIRED):
        return 3
    return cli(CFG, sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
