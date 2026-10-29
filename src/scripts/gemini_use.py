"""gemini 网页版：提问 → 收回答。流程在 qi/flow.py，这里只放本端配置。

用法：
    python gemini_use.py "只回复两个字：收到"
    python gemini_use.py "..." --continue

本端要点（都是实测量出来的）：
- 输入框锚点是左下角固定的「+」，按偏移 (200,0) 点进文字行。
- 空输入时右侧只有麦克风；输入文字后才出现**蓝色圆形发送键**，
  所以"发送键出现了"就代表写进去了。
- 回答动作条是 赞/踩/重新生成/**复制**/更多 —— 复制在**第 4 个**，不在第一个。
  所以不能按"排内最左"点，必须用 gem_copy 图在排内精定位。
- 页面是深色主题，模板也是深色下截的，换主题要重截。
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
    name="gemini",
    title_hint="Google Chrome",
    composer="gem_composer",
    send="gem_send_on",
    actions="gem_actions",
    copy="gem_copy",
    composer_offset=(200, 0),
    newchat="gem_newchat",
    sidebar=None,            # 侧栏是一直展开的，没有折叠态
    send_mode="exists",
    send_threshold=0.88,
)

REQUIRED = ["gem_composer", "gem_send_on", "gem_actions", "gem_copy"]


def main() -> int:
    lib = Library()
    need = list(REQUIRED)
    if "--new" in sys.argv[1:] or not any(a.startswith("--") for a in sys.argv[1:]):
        need.append("gem_newchat")
    print(lib.report(need))
    if lib.missing(need):
        return 3
    return cli(SITE, sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
