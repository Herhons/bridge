"""deepseek 网页版：提问 → 收回答。流程在 qi/flow.py，这里只放本端配置。

用法：
    python deepseek_use.py "只回复两个字：收到"
    python deepseek_use.py "..." --continue
    python deepseek_use.py "..." --timeout 300

本端要点：
- 输入框锚点是**麦克风图标**，点它本身会触发语音，所以按偏移 (-300,-65) 点到文字行。
- 发送键深蓝=可点、浅蓝=不可点，但模板匹配分不出来（都是 1.000），
  所以靠颜色判断：B-R > 100 才算可点。
- 回答动作条是整排六连排，全屏唯一，复制键在排内左侧。
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import Library, console, uilock  # noqa: E402
from qi.flow import Site, cli  # noqa: E402

SITE = Site(
    name="deepseek",
    title_hint="Google Chrome",
    composer="ds_composer",
    send="ds_send_on",
    actions="ds_actions",
    copy="ds_copy",
    composer_offset=(-300, -65),
    newchat="ds_newchat",
    stop="ds_stop",
    send_mode="color",
    send_blue_min=100.0,
    # B19：三处只在 deepseek 打开的加固。
    # 1) 粘贴必须确认落地（2450 字时只写进前 3 个字的根因）
    write_verify=True,
    # 2) 输入框里若是本提问的前缀，认成自己上一轮的残草，清掉继续
    adopt_self_draft=True,
    # 3) 判空以输入框文本为准：有草稿时发送键本来就深蓝，旧判据会把
    #    自己的残草当用户草稿，之后每次 draft_present
    empty_by_text=True,
    # 4) 单次粘贴超过 2000 字会被整段吞掉（实测 2000 进、2001 不进，不截断
    #    也不报错）；分 1500 字一块粘，总量可以远超 2000。
    paste_chunk=1500,
)

REQUIRED = ["ds_composer", "ds_send_on", "ds_actions", "ds_copy"]


def main() -> int:
    lib = Library()
    need = list(REQUIRED)
    if "--new" in sys.argv[1:] or not any(a.startswith("--") for a in sys.argv[1:]):
        need.append("ds_newchat")
    print(lib.report(need))
    if lib.missing(need):
        return 3
    return cli(SITE, sys.argv[1:])


if __name__ == "__main__":
    console.setup()          # 入口编码：GBK 控制台不再被 \u200b 打崩（CARD-08 第 1 条）
    # UI 前台锁（CARD-09a 第 6 条）：和服务端抢同一把 runs\ui.lock
    raise SystemExit(uilock.entry("deepseek_use", main))
