"""grok 网页版：提问 → 收回答。流程在 qi/flow.py，这里只放本端配置。

用法：
    python grok_use.py "只回复两个字：收到"
    python grok_use.py "..." --continue

本端要点：
- 占位文案是**轮播**的（"将文件夹拖放到聊天中" / "切换到构建模式以创建应用" …），
  不能当锚点。锚点用左下角固定的「+」，按偏移 (250,-27) 点进文字行。
- 空输入时右下角是**语音**按钮（浅蓝波形）；输入文字后才变成蓝色发送箭头。
  所以判"可发送"用"出现了发送键"就够，不需要比颜色。
- 侧栏「聊天」＝新对话；侧栏收起时先点右上角的侧栏开关。
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import Library  # noqa: E402
from qi.flow import Site, cli  # noqa: E402

SITE = Site(
    name="grok",
    title_hint="Google Chrome",
    composer="grok_composer",
    send="grok_send_on",
    actions="grok_actions",
    copy="grok_copy",
    composer_offset=(250, -27),
    newchat="grok_newchat",
    sidebar="grok_sidebar",
    send_mode="exists",
    screen_reply=True,   # Grok 的回答动作条不进控件树（实测 209 个节点里没有复制键）
    send_threshold=0.90,
)

REQUIRED = ["grok_composer", "grok_send_on", "grok_actions", "grok_copy"]


def main() -> int:
    lib = Library()
    need = list(REQUIRED)
    if "--new" in sys.argv[1:] or not any(a.startswith("--") for a in sys.argv[1:]):
        need.append("grok_newchat")
    print(lib.report(need))
    if lib.missing(need):
        return 3
    return cli(SITE, sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
