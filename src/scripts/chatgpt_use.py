"""chatgpt 网页版：提问 → 收回答。流程在 qi/flow.py，这里只放本端配置。

用法：
    python chatgpt_use.py "只回复两个字：收到"
    python chatgpt_use.py "..." --continue
    python chatgpt_use.py --read          ← 只读最后一条回复，不发送

本端要点：
- 输入框锚点是左下角固定的「+」，按偏移 (240,-5) 点进文字行。
- 空输入时右下角是**橙色语音**按钮（波形图标）；输入文字后变成**橙色箭头**。
  颜色一样，只能靠图标形状区分 —— 所以模板必须截"箭头态"。
- 侧栏「新聊天」在左上角；侧栏收起时先点旁边的面板开关（gpt_sidebar）。

CARD-09e 给本端开的三个开关（都在 `Site` 里，别端默认关）：
  · `composer_name="询问 ChatGPT"` + `clear_draft_on_new`：新聊天会**带着上一条
    草稿**，`--new` 之后一读就是 `draft_present`。按 UIA 名字精确认领输入框
    （地址栏也是 EditControl，用名字把两者分开），有草稿就 Ctrl+A / Delete 清掉，
    读回为空才继续。
  · `paste_plain`：行首 `#`/`* `/`1. ` 会被编辑器当 Markdown 渲染掉标记，
    打字版本核对不上时改用粘贴纯文本再写一次。
  · `uia_fallback`：页面答完了却报超时/被打断时，按「最新一条回复 / 回答已完成」
    标记取最后一条回复正文（`qi/lastreply.py`）。
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
    name="chatgpt",
    title_hint="Google Chrome",
    composer="gpt_composer",
    send="gpt_send_on",
    actions="gpt_actions",
    copy="gpt_copy",
    composer_offset=(240, -5),
    newchat=("gpt_newchat", "gpt_newchat_icon"),   # 展开=整条，收起=铅笔图标
    sidebar="gpt_sidebar",
    send_mode="exists",
    send_threshold=0.88,
    composer_name="询问 ChatGPT",
    clear_draft_on_new=True,
    paste_plain=True,
    uia_fallback=True,
)

REQUIRED = ["gpt_composer", "gpt_send_on", "gpt_actions", "gpt_copy"]


def main() -> int:
    # --read 只读、不发送：不需要输入框/发送键这些模板
    if "--read" in sys.argv[1:]:
        return cli(SITE, sys.argv[1:])
    lib = Library()
    need = list(REQUIRED)
    if "--new" in sys.argv[1:] or not any(a.startswith("--") for a in sys.argv[1:]):
        need.append("gpt_newchat")
    print(lib.report(need))
    if lib.missing(need):
        return 3
    return cli(SITE, sys.argv[1:])


if __name__ == "__main__":
    console.setup()          # 入口编码：GBK 控制台不再被 \u200b 打崩（CARD-08 第 1 条）
    # UI 前台锁（CARD-09a 第 6 条）：和服务端抢同一把 runs\ui.lock
    raise SystemExit(uilock.run("chatgpt_use", main))
