"""气泡文本的认领规则：**一处实现，两端共用**。

2026-11-12（R3-6）：`qi/gemini.py` 和 `src/scripts/grok_use.py` 里各有一份
**逐字相同**的 `_squash` / `prompt_matches` 与同名常量。这正是"第二套栈"的危害形态 ——
改一边忘另一边，就会出现"这个端能认领、那个端认不出"。Round 3 的
`iteration._squash`（验真归一）就是因为只改一处，才让三轮结论全错。

这里只放**纯函数**：不碰 UI、不读环境、不依赖 `conversation`。
两端按需 import，名字保持不变（`prompt_matches` 仍可从各自模块取到）。
"""
from __future__ import annotations

from . import textcmp

#: 折叠气泡的截断标记。`parse` 之后仍要看尾巴，因为控件树里只留开头一段。
TAIL_MARKS = ("…", "...")
#: 短于这个字数的前缀不足以认领一条提问（否则"1."这种前缀会命中一切）。
MIN_PREFIX = 12


def squash(text: str) -> str:
    """整段重复两遍的收紧成一遍。

    真机上用户气泡会同时挂"内层文本"和"外层容器"，两者文本一样、不相邻，
    相邻去重挡不住。这里再兜一次：只有前后半段完全相同时才收。
    """
    half = len(text) // 2
    if len(text) % 2 == 0 and half and text[:half] == text[half:]:
        return text[:half]
    return text


def prompt_matches(text: str, prompt: str) -> bool:
    """控件树里的这段文字，是不是我们发出去的那条提问。

    折叠气泡只留开头一段，所以比的是「前缀」而不是全等：去掉尾部省略号后，
    只要我们这串够长、且提问以它开头，就算同一条。

    比对前走 `textcmp.normalize`：页面会把行首「1.」吃掉，
    用 `ui.canonical` 会永远认领不到。
    """
    got, want = squash(textcmp.normalize(text)), textcmp.normalize(prompt)
    if not got or not want:
        return False
    if got == want:
        return True
    for mark in TAIL_MARKS:
        if got.endswith(mark):
            got = got[: -len(mark)]
            break
    if len(got) < min(MIN_PREFIX, len(want)):
        return False
    return want.startswith(got)


def pick_reply(page, at, baseline):
    """本轮该读哪条回复。

    认领到提问就用它后面那条；认领不到时，只认「比发送前多出来的最新一条」。
    2026-11-01 10:35 那次就是认领一直失败，回复其实早就在树里，脚本却白等到 300s。

    `page` 只要满足 `Scan` 的三个方法（`prompt_at` / `replies` / `reply_after`），
    gemini 与 grok 的 `Scan` 都符合。
    """
    if at is not None:
        # **不兜底**：认领到提问却还没有回复，就是"还没答"。
        # 这里若滑到下面的"最新一条"，会把这之前别的回答当成本轮结果
        # （2026-11-12 收口时差点写出这个回归，原两端的实现都不兜底）。
        return page.reply_after(at)
    replies = page.replies()
    if len(replies) > baseline:
        return replies[-1]
    return None


def already_answered(page, prompt):
    """发送前检查：对话里是否已有本提问之后的回复。有则返回正文，否则 None。

    CARD-09f 第 6 条：中断后重跑、页面其实已答完时，绝不重发，只读已有回复。

    **它只回答"页面上有没有"**。调用方必须先证明这是本事务、同一会话
    （gemini 的 `_prior_answer_proof`），证明不了就不能拿这串文字当本轮答案。
    """
    at = page.prompt_at(prompt)
    if at is None:
        return None
    reply = page.reply_after(at)
    if reply is not None and reply.text.strip():
        return reply.text.strip()
    return None
