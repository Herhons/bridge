"""gemini 网页版：提问 → 收回答。判据在 qi/gemini.py，这里只放本端配置。

用法：
    python gemini_use.py "只回复两个字：收到"
    python gemini_use.py "帮我把这批文件按日期归类" --task      ← 任务（计划）模式
    python gemini_use.py "..." --continue
    python gemini_use.py --read                                 ← 只读最后一条回复，不发送

本端要点（都是实测量出来的）：
- 输入框锚点是左下角固定的「+」，按偏移 (200,0) 点进文字行。
- 空输入时右侧只有麦克风；输入文字后才出现**紫色圆形发送键**，
  所以"发送键出现了"就代表写进去了。
- 回答动作条是 赞/踩/重新生成/**复制**/更多 —— 复制在**第 4 个**，不在第一个。
- 页面是深色主题，模板也是深色下截的，换主题要重截。
- **点「发起新对话」会把模式重置回对话**，所以要用任务模式必须"先新对话、再切任务"。
  顺序反了等于白切（这是之前的 bug）。

2026-11-01 修（CARD-GEM-fix-reply-detect）：判据从「整串精确相等 + 模板」改成
「控件树切块 + 动作条 + 文本稳定 3 秒」，发送键模板降级为兜底。原因见 qi/gemini.py
开头：11:4x 那次提问气泡是折叠的（尾部「…」+ 前缀「你说」），旧判据永远匹配不上，
240s 白等到超时，而回复其实早就写完了。
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import Library, console, gemini, uilock  # noqa: E402
from qi.choose import Chooser, choose_writer  # noqa: E402
from qi.flow import Site  # noqa: E402

SITE = Site(
    name="gemini",
    title_hint="Google Chrome",
    composer="gem_composer",
    send="gem_send_on",
    actions=("gem_actions", "gem_actions_task"),   # 对话 6 个图标 / 任务 4 个图标，两排模板
    copy="gem_copy",
    composer_offset=(200, 0),
    newchat="gem_newchat",
    sidebar=None,            # 侧栏是一直展开的，没有折叠态
    send_mode="exists",
    send_threshold=0.88,
    # B19：gemini 的占位文字不在公共 KNOWN_PLACEHOLDERS 里，`flow.clear_composer`
    # 会把它当成"还有 10 个字没清掉"（真机报 draft_clear_failed）。这里补上。
    placeholders=("问问 Gemini", "Ask Gemini"),
)

# 模式切换用（对话 / 任务）。模板是"整组按钮"各一张，读法见 qi/choose.py。
WRITER = Chooser(
    name="gemini",
    anchor="gem_composer",
    open_offset=(0, 0),
    prefix="gem_",
    writer_tabs=("gem_writer_tabs_dialog", "gem_writer_tabs_task"),
    writers={"dialog": 0, "task": 1},
)

REQUIRED = ["gem_composer", "gem_send_on", "gem_actions", "gem_actions_task", "gem_copy"]


def _mk_prepare(want: str):
    def fn(s):
        return choose_writer(s, WRITER, want)
    return fn


def main() -> int:
    argv = list(sys.argv[1:])
    reading = "--read" in argv          # 只读模式：不发送，也不需要模板
    want = None if (reading or "--continue" in argv) else "dialog"
    if not reading:
        if "--task" in argv and "--chat" in argv:
            print("--task 和 --chat 不能同用")
            return 2
        if "--task" in argv:
            argv.remove("--task")
            want = "task"
        elif "--chat" in argv:
            argv.remove("--chat")
            want = "dialog"

        # 模式按钮只在新对话页出现，所以用 --task/--chat 时强制先回新对话页
        if want is not None and "--continue" in argv:
            print("当前会话不能切换写入器；使用 --new --task 或 --new --chat")
            return 2

        lib = Library()
        need = list(REQUIRED) + list(WRITER.writer_tabs)
        if "--new" in argv or not any(a.startswith("--") for a in argv) or want:
            need.append("gem_newchat")
        print(lib.report(need))
        if lib.missing(need):
            return 3

    prepare = _mk_prepare(want) if want else None
    return gemini.cli(
        SITE, argv,
        prepare=prepare,
        extra_help="  --task      发到「任务（计划）」模式：先新对话，再切任务，然后提交\n"
                   "  --chat      发到「对话」模式（默认）\n"
                   "  --read      只读当前标签页最后一条回复，不发送",
    )


if __name__ == "__main__":
    console.setup()          # 入口编码：GBK 控制台不再被 \u200b 打崩（CARD-08 第 1 条）
    # UI 前台锁（CARD-09a 第 6 条）：和服务端抢同一把 runs\ui.lock
    raise SystemExit(uilock.entry("gemini_use", main))
