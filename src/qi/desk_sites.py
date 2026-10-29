"""四个桌面端的配置。所有坐标都是**窗口内 1380x1340 下、相对发送键圆心**的偏移。

窗口尺寸必须固定（desk_flow.open_bind 会强制设成 1380x1340 并摆到副屏），
否则这些偏移全部失效。

## 实测记录（2026-10-29，窗口 1380x1340）

| 端 | 发送键中心 | 尺寸 | BGR | 说明 |
|---|---|---|---|---|
| codex | (1180,1298) | 28x28 | [55,124,238] | 橙，对比度最好 |
| dsh | (1164,1277) | 34x34 | [245,200,179] | 浅蓝 |
| opencode | (1028,1290) | 30x30 | [215,215,215] | 纯灰，靠「三通道接近」认 |
| workbuddy | 未复核 | — | [0,3,3] | 纯黑，但那张截图被别的窗口挡过 |

## 每端的界面差异

codex / dsh / workbuddy：模型和强度**合在一个芯片**里（`GPT-6 Astra Ultra ∨`），
点它打开一个含滑杆的弹层，和网页版 ChatGPT 那套两步结构一样。

opencode：模型和强度是**两个独立芯片**
（`OpenCode / DeepSeek V4.1 Flash Free ∨` 和 `High ∨`），都在输入框下方左侧。
"""

from __future__ import annotations

from .desk_flow import DeskSite

# 发送键颜色判据的写法：直接给 BGR 上下界
# 「三通道接近」这种条件表达不了，所以 opencode 用窄区间模拟（灰 195~235 且互差 <12）
SITES: dict[str, DeskSite] = {
    "codex": DeskSite(
        name="codex",
        proc="ChatGPT.exe",
        title_hint="ChatGPT",
        # 橙：R 高、B 低
        send_b_min=0, send_b_max=110,
        send_g_min=90, send_g_max=170,
        send_r_min=200, send_r_max=255,
        send_size=(24, 34),
        # 输入框空白处：发送键左 480、上 58（实测点这里能让 ProseMirror 聚焦）
        composer=(-480, -58),
        # 芯片「GPT-6 Astra Ultra」文字 x=986..1096，中心 1041 -> 发送键左 139
        chip=(-139, 0),
        newchat=None,          # codex 用 Ctrl+N 或左侧栏 New chat，这里先不点
        copy_tpl="desk_codex_copy",
        notes="发送键橙色，最可靠；输入框是 ProseMirror，没有发送按钮，但右下有橙色圆钮",
    ),
    "dsh": DeskSite(
        name="dsh",
        proc="DeepSeek Harness.exe",
        title_hint="DeepSeek Harness",
        # 浅蓝：B 高、R 略低
        send_b_min=228, send_b_max=255,
        send_g_min=185, send_g_max=215,
        send_r_min=160, send_r_max=200,
        send_size=(30, 40),
        composer=(-480, -58),
        chip=(-139, 0),
        newchat=(-1290, -600),   # 左侧栏顶部「新会话」，待复核
        copy_tpl="desk_dsh_copy",
        notes="蓝色发送键；左侧栏有「新会话」",
    ),
    "opencode": DeskSite(
        name="opencode",
        proc="OpenCode.exe",
        title_hint="OpenCode",
        # 灰 215。区间必须窄：放到 195~235 会把输入框的浅灰边框也圈进来，
        # 整片连成一个 3 万像素的大块，30x30 的发送键就被淹没了（实测返回 None）
        send_b_min=210, send_b_max=222,
        send_g_min=210, send_g_max=222,
        send_r_min=210, send_r_max=222,
        send_size=(26, 36),
        # 实测 (1028,1290) / (1380,1340)
        composer=(-480, -58),
        # 模型芯片在输入框**下方左侧**：实测文字 x=381..486，中心 430 -> 发送键左 598
        chip=(-598, 0),
        # 强度芯片 `High ∨` 在模型芯片右边：约 x=540 -> 发送键左 488
        effort=(-488, 0),
        newchat=None,
        copy_tpl="desk_opencode_copy",
        notes="模型和强度是两个独立芯片，都在输入框下方左侧，不在右下角",
    ),
    "workbuddy": DeskSite(
        name="workbuddy",
        proc="WorkBuddy.exe",
        title_hint="WorkBuddy",
        # 纯黑
        send_b_min=0, send_b_max=60,
        send_g_min=0, send_g_max=60,
        send_r_min=0, send_r_max=60,
        send_size=(24, 36),
        # 窗口 1166x893 时发送键约在 (1073,830)，换算成相对值。
        # **这个数是估的**，那张截图被别的窗口挡过，需要打开确认一次
        composer=(-480, -58),
        chip=(-160, 0),
        newchat=None,
        copy_tpl="desk_wb_copy",
        notes="黑色发送键；这张截图曾被别的窗口挡住，坐标需要复核",
    ),
}
