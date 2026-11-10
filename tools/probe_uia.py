"""辅助工具：用 UIA **只量控件位置**，不参与运行时。

用途只有一个——截模板时别再靠肉眼估坐标。流程：
    1. 这个脚本列出目标窗口里的控件（名字 / 类型 / 矩形）
    2. 找到要的那个控件的矩形
    3. 用 tools/snip.py --from <全屏图> --region 把那一块裁出来

产物是**图片**，运行时的 `_use` / `_choose` 完全不 import 这个文件，
也不 import uiautomation。

用法：
    python tools/probe_uia.py --window "Chrome" --depth 30
    python tools/probe_uia.py --window "Chrome" --grep "聊天|工作"
    python tools/probe_uia.py --window "Chrome" --grep "对话|任务" --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# UI 前台锁（CARD-09f）：真机探针也要全程持有 runs\ui.lock
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
from qi import uilock  # noqa: E402

try:
    import uiautomation as auto
except ImportError:
    print("需要 uiautomation（仅本工具用）：pip install uiautomation")
    raise SystemExit(2)


def walk(ctrl, depth=0, max_depth=30, out=None):
    if out is None:
        out = []
    if depth > max_depth:
        return out
    try:
        name = ctrl.Name or ""
        ctype = ctrl.ControlTypeName or ""
        r = ctrl.BoundingRectangle
        rect = (r.left, r.top, r.right - r.left, r.bottom - r.top) if r else None
        if name or ctype in ("ButtonControl", "TextControl", "MenuItemControl"):
            out.append({"depth": depth, "type": ctype, "name": name, "rect": rect})
    except Exception:
        return out
    try:
        children = ctrl.GetChildren()
    except Exception:
        return out
    for c in children:
        walk(c, depth + 1, max_depth, out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="Chrome", help="窗口标题关键词")
    ap.add_argument("--hwnd", type=int, help="直接指定窗口句柄（最准，推荐）")
    ap.add_argument("--depth", type=int, default=30)
    ap.add_argument("--grep", help="只显示名字命中这个正则的控件")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=80)
    args = ap.parse_args()

    auto.SetProcessDpiAwareness(auto.ProcessDpiAwareness.PerMonitorDpiAware)
    if args.hwnd:
        win = auto.ControlFromHandle(args.hwnd)
        if win is None:
            print(f"句柄 {args.hwnd} 取不到控件")
            return 2
    else:
        win = auto.WindowControl(searchDepth=1, SubName=args.window)
        if not win.Exists(2, 0.5):
            print(f"没找到标题含 {args.window!r} 的窗口")
            return 2

    items = walk(win, max_depth=args.depth)
    if args.grep:
        import re
        rx = re.compile(args.grep)
        items = [i for i in items if rx.search(i["name"] or "")]

    if args.json:
        print(json.dumps(items[:args.limit], ensure_ascii=False, indent=1))
        return 0

    for i in items[:args.limit]:
        r = i["rect"]
        rs = f"{r[0]:5d},{r[1]:5d} {r[2]:4d}x{r[3]:4d}" if r else "   -"
        print(f"{'  ' * min(i['depth'], 8)}{i['type']:<22} {rs}  {i['name'][:40]!r}")
    print(f"共 {len(items)} 个控件")
    return 0


if __name__ == "__main__":
    # UI 前台锁（CARD-09f）：真机探针也要全程持有 runs\ui.lock
    sys.exit(uilock.run("probe_uia", main))
