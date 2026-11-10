"""grok 网页版：只做选择，不发提问。

用法：
    python grok_choose.py --list
    python grok_choose.py --model fast|expert|auto|build

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
    open_offset=(530, 0),
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


def main() -> int:
    lib = Library()
    print(lib.report(REQUIRED))
    if lib.missing(REQUIRED):
        return 3
    return cli(CFG, sys.argv[1:])


if __name__ == "__main__":
    console.setup()          # 入口编码：GBK 控制台不再被 \u200b 打崩（CARD-08 第 1 条）
    raise SystemExit(uilock.run("grok_choose", main))
