"""grok 网页版：只做选择，不发提问。

用法：
    python grok_choose.py --list
    python grok_choose.py --model fast|expert|auto|build
    python grok_choose.py --probe          B16：报模板分数 + 菜单开着时截整屏图

实测：菜单有 Auto / Fast / Expert / Build / Heavy。
Heavy 需要 SuperGrok 订阅，点了会跳订阅页，所以不提供。
chip 图只截了 fast / expert 两个，其余档位"能设不能读"。
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import Library, console, uilock  # noqa: E402
from qi.choose import Chooser, cli  # noqa: E402

CFG = Chooser(
    name="grok",
    anchor="grok_composer",
    # B16 实测（2026-11-09 探针）：grok 改成新聊天页后芯片右移了。
    # 锚点 (+) 中心在窗口 (464,580)，"Fast ⌄" 芯片中心 (1048,588)；
    # 旧的 +530 落在 x=994 的空白里，菜单根本没开，才一路报 menu_not_open。
    open_offset=(584, 8),
    prefix="grok_",
    options={
        "fast": "速度优先（默认）",
        "expert": "推理更强",
        "auto": "自动选择",
        "build": "构建模式（Beta）",
    },
    has_chip=("fast", "expert"),
)

REQUIRED = ["grok_composer"] + [f"grok_item_{k}" for k in CFG.options] + [
    f"grok_chip_{k}" for k in CFG.has_chip
]


def probe(argv: list[str]) -> int:
    """只读诊断：菜单开着时在**同一个进程里**截一张整屏图 + 报全部模板分数。

    B16：重截 grok 模板的难点是「一点开菜单，去跑 `tools/snip.py --shot`
    控制台就抢走焦点、菜单立刻关掉，拍到的是没有菜单的桌面」。
    这里用 `Screen.snapshot()` 在开菜单的同一进程内落图，之后再用
    `tools/snip.py --from <这张图> --region ...` 裁模板，坐标从图上量。
    """
    import json
    import time

    from qi import Library, Screen, guard, zoom as zoom_mod, choose as choose_mod
    from qi.browser import ensure_site

    need, wait, force, _rest = guard.parse_guard_args(argv)
    if not guard.guard(need, wait, force):
        return 4
    lib = Library()
    print(lib.report(REQUIRED))
    s = Screen()
    hwnd = ensure_site("grok")
    if hwnd is None:
        print(json.dumps({"ok": False, "stage": "no_window"}, ensure_ascii=False))
        return 1
    s.bind(hwnd)
    if not s.focus():
        print(json.dumps({"ok": False, "stage": "page_not_front"}, ensure_ascii=False))
        return 4
    s.tab_site = "grok"
    time.sleep(1.2)
    out: dict = {"ok": True, "stage": "probe", "hwnd": int(hwnd),
                 "templates": {}, "missing": lib.missing(REQUIRED)}
    try:
        zoom_mod.ensure(s, verbose=True)
        out["zoom"] = {"ok": True}
    except Exception as exc:                # noqa: BLE001 - 诊断不许带崩
        out["zoom"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    win = s.window_rect()
    out["win"] = list(win) if win else None
    names = ["grok_composer", "grok_chip_fast", "grok_chip_expert"] + [
        f"grok_item_{key}" for key in CFG.options]
    for name in names:
        try:
            got = s.find_unique(name, threshold=0.80, region=win,
                                scales=zoom_mod.SCALES)
            out["templates"][name] = {"best": got.get("best"),
                                      "scale": got.get("scale"),
                                      "hit": got["match"] is not None,
                                      "ambiguous": bool(got.get("ambiguous"))}
        except Exception as exc:            # noqa: BLE001
            out["templates"][name] = {"error": f"{type(exc).__name__}: {exc}"}
    opened = choose_mod.open_menu(s, CFG)
    out["menu"] = dict(getattr(choose_mod, "_LAST_OPEN", {}) or {})
    out["menu"]["opened"] = bool(opened)
    try:
        out["snapshot"] = str(s.snapshot("grok-menu").resolve())
    except Exception as exc:                # noqa: BLE001
        out["snapshot_error"] = f"{type(exc).__name__}: {exc}"
    try:
        s.press("esc")
    except Exception:                       # noqa: BLE001
        pass
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if opened else 1


def main() -> int:
    argv = sys.argv[1:]
    if "--probe" in argv:
        return probe([item for item in argv if item != "--probe"])
    lib = Library()
    print(lib.report(REQUIRED))
    if lib.missing(REQUIRED):
        return 3
    return cli(CFG, argv)


if __name__ == "__main__":
    console.setup()          # 入口编码：GBK 控制台不再被 \u200b 打崩（CARD-08 第 1 条）
    raise SystemExit(uilock.entry("grok_choose", main))
