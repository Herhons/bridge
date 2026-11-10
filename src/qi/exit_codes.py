"""统一退出码与 stage 映射（CARD-06a (d)）。

## 为什么要有这个文件

到 2026-11-02 为止，各脚本的退出码是**各写各的**：同是"失败"，
网页端返回 1、桌面端也返回 1，但"窗口提不到前台"有人返回 1、有人返回 4。
新卡要加 5/6/7 三个码，如果不先把现有的抄下来核对，
迟早会和 argparse 的 2、资源库的 3、占用保护的 4 撞车。

## 现有退出码审计（2026-11-02 逐脚本 grep 的结果）

| 码 | 含义 | 出现位置 |
|----|------|----------|
| 0  | 正常：done / already / done_no_chip / list / probe / noop | 全部脚本 |
| 1  | 通用失败：no_window、controls_not_found、unverified、读不出来 | 全部脚本 |
| 2  | 参数错 | argparse 默认（全部脚本）；各 cli 的"没给任何动作" |
| 3  | 资源库缺模板（`Library.missing`） | 各 choose/use 的入口 |
| 4  | 让位：「你在用电脑」user_busy / user_took_over / not_foreground | guard、各 cli |
| 5  | **新增**：档位回执缺失或不符 → stage=tier_unconfirmed | 本卡（tier_guard） |
| 6  | **新增**：UIA 读不到文档 → stage=uia_docs_lost | 本卡（ui.candidates） |
| 7  | **新增**：窗口最小化且恢复失败 → stage=window_minimized | 本卡（window.restore） |

**冲突核对**：`src/` 里原先没有任何 `return 5/6/7`（已 grep 全目录确认），
argparse 自己只会用 2（参数错）和 0（--help），3/4 已分别被"缺模板""让位"占住。
唯一的异类在 `src/scripts/workflow_use.py`：它**自己的文档串**里 3 = 「advance 后需要监工」。
那是工作流状态机，不操作任何窗口、也不发消息，与 5/6/7 无关；
为避免将来混淆，那里保留原有语义，并在 `docs/exit-codes.md` 单独列一行说明。

## 用法

    from . import exit_codes as ec
    return ec.TIER_UNCONFIRMED

脚本里已有的裸数字**不强制改写**（改 8 个脚本的每一个 return 风险大于收益），
但新代码一律用这里的常量；`docs/exit-codes.md` 是给人看的同一张表。
"""

from __future__ import annotations

OK = 0
FAIL = 1
USAGE = 2
MISSING_ASSETS = 3
BUSY = 4
TIER_UNCONFIRMED = 5
UIA_DOCS_LOST = 6
WINDOW_MINIMIZED = 7
UI_LOCK_BUSY = 8      # CARD-09f：runs\ui.lock 被另一个自动化占着，等超时
CONCURRENT_UI = 9     # CARD-09f：发送中途前台标签/文档被换走，已清草稿退出
#: B15：tabs 模式下认不出"哪个标签是我的"。**fail-closed**，不猜一个就往上写字。
TAB_UNBOUND = 10

# stage -> 退出码。只收「卡面上写死了的」和「新增的」，
# 收得太全反而会变成第二份真相（各脚本的 stage 名本来就不统一）。
STAGE_CODES: dict[str, int] = {
    # B15：标签
    "tab_unbound": TAB_UNBOUND,
    "tab_ambiguous": TAB_UNBOUND,
    "tab_strip_missing": TAB_UNBOUND,
    # 正常
    "done": OK,
    "already": OK,
    "done_no_chip": OK,
    "list": OK,
    "probe": OK,
    "noop": OK,
    "skipped": OK,
    "answered": OK,
    "read": OK,
    "already_answered": OK,
    # 参数
    "bad_arg": USAGE,
    "bad_args": USAGE,
    "usage": USAGE,
    # 资源
    "missing_assets": MISSING_ASSETS,
    # 让位
    "user_busy": BUSY,
    "user_took_over": BUSY,
    "not_foreground": BUSY,
    "aborted": BUSY,
    # 本卡新增
    "tier_unconfirmed": TIER_UNCONFIRMED,
    "uia_docs_lost": UIA_DOCS_LOST,
    "window_minimized": WINDOW_MINIMIZED,
    # CARD-06b 返工：--new 之后按回执重选模型失败，和档位未确认同属"档位不可信"，
    # 退出码必须是 5（以前这条分支只返回裸数字，没登记过这个 stage）。
    "model_reset_after_new": TIER_UNCONFIRMED,
    # CARD-09f 新增：UI 前台锁被占 / 发送中途前台被换走
    "ui_busy": UI_LOCK_BUSY,
    "concurrent_ui": CONCURRENT_UI,
    # CARD-06b 新增的两个 stage：都不单独占退出码（按通用失败 1 算），
    # 但必须显式登记——否则 code_for 也会给 1，却看不出"这是登记过的"。
    "zoom_not_100": FAIL,
    "human_check": FAIL,
    # UI-BOOTSTRAP-3：首页/会话列表没有 composer，开消息前就拒（不点、不打字）。
    # 不单独占码（按通用失败 1 算），但显式登记，别让它只靠 default 混过去。
    "session_not_open": FAIL,
    # R11：端点拒绝这一轮的三条**结构**判据（不猜原因）。
    # 同样不单独占码：服务端只认 stage，退出码给 1 就够；新增独立码还要同时改
    # codex/dsh/opencode 三处的硬编码与 docs 码表，收益为 0。
    "submit_refused": FAIL,       # 点了发送，端把这段文字退回输入框
    "send_unavailable": FAIL,     # 发送键存在但全部不可用，一个键都没点
    "accepted_silent": FAIL,      # 端收下了，但一直没有生成迹象与回复
    # B19：把"没写进去"和"没发出去"分开。这两件事以前都并到 input_mismatch /
    # timeout 里，真机上一眼看不出是"字没进去"还是"发出去没落地"。
    "input_not_landed": FAIL,     # 粘贴/键入没落地：编辑器里始终没有这段文字
    "submit_not_landed": FAIL,    # 点了发送，限定时间内没有任何落点证据
    "newchat_not_landed": FAIL,   # 点了「发起新对话」，但页面仍是旧会话
}

# 给 docs 与测试用的一行说明
LABELS: dict[int, str] = {
    OK: "正常",
    FAIL: "通用失败（找不到窗口/控件、读不出、复核不过）",
    USAGE: "参数错（argparse 默认；也是「没给任何动作」）",
    MISSING_ASSETS: "资源库缺模板",
    BUSY: "让位：你在用电脑 / 窗口被切走 / 提不到前台",
    TIER_UNCONFIRMED: "档位回执缺失或不符，先跑对应的 *_choose",
    UIA_DOCS_LOST: "UIA 读不到文档（已唤醒重试仍为 0）",
    WINDOW_MINIMIZED: "窗口最小化且恢复失败",
    UI_LOCK_BUSY: "UI 前台锁被另一个自动化占着（runs\\ui.lock），等超时",
    CONCURRENT_UI: "发送中途前台标签/文档被换走，已清草稿退出",
}


def code_for(stage: str | None, default: int = FAIL) -> int:
    """stage 名 → 退出码。认不出来的按通用失败（1）算，不猜成 0。"""
    if not stage:
        return default
    return STAGE_CODES.get(str(stage), default)


def table() -> list[tuple[int, str]]:
    """[(码, 说明)]，按码排序。docs 与测试共用这一份，避免两处各说各话。"""
    return [(code, LABELS[code]) for code in sorted(LABELS)]
