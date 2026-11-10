"""打开某端的模型弹层，截图，然后 Esc 关掉。不选任何项。

用法：python tools/probe_menu.py codex|dsh|opencode|workbuddy
"""

import sys
import time

import cv2

sys.path.insert(0, "src")
from qi import desk_flow, uilock  # noqa: E402
from qi.desk_sites import SITES  # noqa: E402


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else "codex"
    site = SITES[key]
    s = desk_flow.open_bind(key)
    if s is None:
        print("没窗口")
        return 1
    pos = desk_flow.locate(s, site)
    if pos is None:
        print("找不到发送键")
        return 1
    print("控件:", {k: v for k, v in pos.items() if k != "win"})

    s.click_xy(*pos["chip"])
    time.sleep(1.6)
    img = s.grab(pos["win"])
    out = f"evidence/menu_{key}.png"
    cv2.imwrite(out, img)
    print("截图:", out, img.shape)

    # 只看不选，关掉
    s.press("esc")
    time.sleep(0.6)
    print("已 Esc")
    return 0


if __name__ == "__main__":
    # UI 前台锁（CARD-09f）：真机探针也要全程持有 runs\ui.lock
    sys.exit(uilock.run("probe_menu", main))
