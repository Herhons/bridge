"""chatgpt 网页版：提问 → 收回答。流程在 qi/flow.py，这里只放本端配置。

用法：
    python chatgpt_use.py "只回复两个字：收到"
    python chatgpt_use.py "..." --continue

本端要点：
- 输入框锚点是左下角固定的「+」，按偏移 (240,-5) 点进文字行。
- 空输入时右下角是**橙色语音**按钮（波形图标）；输入文字后变成**橙色箭头**。
  颜色一样，只能靠图标形状区分 —— 所以模板必须截"箭头态"。
- 侧栏「新聊天」在左上角；侧栏收起时先点旁边的面板开关（gpt_sidebar）。
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
    name="chatgpt",
    title_hint="Google Chrome",
    composer="gpt_composer",
    send="gpt_send_on",
    actions="gpt_actions",
    copy="gpt_copy",
    composer_offset=(240, -5),
    newchat="gpt_newchat",
    sidebar="gpt_sidebar",
    send_mode="exists",
    send_threshold=0.88,
)

REQUIRED = ["gpt_composer", "gpt_send_on", "gpt_actions", "gpt_copy"]


def main() -> int:
    lib = Library()
    need = list(REQUIRED)
    if "--new" in sys.argv[1:] or not any(a.startswith("--") for a in sys.argv[1:]):
        need.append("gpt_newchat")
    print(lib.report(need))
    if lib.missing(need):
        return 3
    return cli(SITE, sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
