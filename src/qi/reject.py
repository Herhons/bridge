"""端点"拒绝这一轮"的结构化判据。

**只报看到的事实，不猜原因。**

这里有三个名字，每一个都只描述"我们看见了什么"，不描述"为什么"：

    submit_refused    点了发送，端把这段文字退回输入框，且没出现新的提问气泡
    send_unavailable  发送键存在但一直不可用，一个键都没点
    accepted_silent   端收下了（提问气泡出现），但一直没有生成迹象与回复

为什么不写成 quota / login_required / limit：那些是**原因**，而本模块只拿得到**结构**。
按原因命名却拿不到原因，等于对读代码的人撒谎（仓库里那四个死值就是这么来的）。

原因由 `hint` + `failure` 留下的截图给人看。

## 为什么按端开关

判据的可靠性和端的 UI 强相关。只有拿到过实测结构证据的端才打开对应开关；
表里没有的端 = 全部关闭 = 行为一个字节不变。

    opencode  回填 / 发送键禁用 / 收下无声    ← RUN.txt + card06c 实测
    codex     发送键禁用                      ← docs/完成矩阵.md:15

其余六端零证据，不猜。将来某端拿到实测，改这里一个布尔值即生效。

## 纯函数

本模块**不 import uiautomation、不读屏、不写文件**，所有输入由调用方从 `View` 派生。
这样它可以用假树离线测（照 `replycodes.py` 的形态）。
"""
from dataclasses import dataclass

from . import textcmp

SUBMIT_REFUSED = "submit_refused"
SEND_UNAVAILABLE = "send_unavailable"
ACCEPTED_SILENT = "accepted_silent"

ALL_STAGES = (SUBMIT_REFUSED, SEND_UNAVAILABLE, ACCEPTED_SILENT)

#: 连续几次读到"框里还是我们这段文字"才算数。与 workbuddy 的 STABLE_READS 同口径。
ECHO_READS = 2

#: 收下之后多久没有任何生成迹象就判"端没在干活"。
#: 取 Gemini 实测首段 44.1s 的 2 倍余量；远小于 180s / 900s 预算。
IDLE_SECONDS = 90.0

#: 判据 A 的最短等待。与 `conversation.SUBMIT_RETRY_TOTAL` 对齐：
#: 那段时间里受控重试会再点一次发送（看到的是同一个信号），所以这里必须等它用完。
#: 刻意写成常量而不是 import，避免模块循环依赖。
SUBMIT_SETTLE = 10.0


@dataclass(frozen=True)
class Profile:
    """某一端有哪些结构信号是可信的。默认全关。"""

    echo_back: bool = False
    send_disabled: bool = False
    accepted_idle: bool = False


PROFILES = {
    "opencode": Profile(echo_back=True, send_disabled=True, accepted_idle=True),
    "codex": Profile(echo_back=False, send_disabled=True, accepted_idle=False),
    # B10：workbuddy 是 7 次 ~902s 超时的那个端。它超时那一刻停止键仍可点
    # （`still_generating`），所以"等太久"这条对它无效；但"收下了、90 秒
    # 既没生成迹象也没有新回复"是能判的——那正是端拒绝或卡死的形态。
    "workbuddy": Profile(echo_back=False, send_disabled=False, accepted_idle=True),
}


def signals(facts) -> dict:
    """把事实压成"值得记下来的那几个"，给**未开端**留证据。

    B26：判据没开 ≠ 什么都看不见。把这些量写进结果与 job 证据，攒够现场
    才谈得上给 `PROFILES` 加一端（否则就是猜）。空 dict = 本轮没有任何信号。
    """
    out = {}
    if facts.echo:
        out["echo_back"] = True
    if facts.sends_total and not facts.sends_on:
        out["send_disabled"] = True
    if facts.prompt_here and not facts.busy and not facts.copies:
        out["accepted_no_activity"] = True
    return out

#: 表里没有的端 —— 全部关闭。
DEFAULT = Profile()


def profile(site) -> Profile:
    return PROFILES.get(str(site or "").strip().lower(), DEFAULT)


@dataclass(frozen=True)
class Facts:
    """一轮读取里能直接看到的结构事实。全部由调用方从 View 派生。"""

    echo: bool = False          # 输入框里正好是本次提问原文
    prompt_here: bool = False   # 我们这条提问已经在树上（= 已被端收下）
    sends_total: int = 0        # 名字命中 SEND 的按钮个数
    sends_on: int = 0           # 其中可用（IsEnabled）的个数
    busy: bool = False          # 有可点的停止键 = 还在生成
    copies: int = 0             # 回复动作条上的复制键总数
    composer_chars: int = 0     # 输入框当前字符数（只用于诊断）


def observe(view, prompt, *, prompt_here=False) -> Facts:
    """从 View 抽出结构事实。**纯**：缺字段一律按"不知道"处理，绝不抛。"""

    value = getattr(view, "value", "") or ""
    sends = getattr(view, "sends", ()) or ()
    copies = getattr(view, "copies", ()) or ()

    echo = False
    if prompt and value:
        try:
            echo = textcmp.equal(prompt, value)
        except Exception:                              # noqa: BLE001 - 归一失败就当不知道
            echo = False

    return Facts(
        echo=bool(echo),
        prompt_here=bool(prompt_here),
        sends_total=len(sends),
        sends_on=sum(1 for on in sends if on),
        busy=bool(getattr(view, "busy", False)),
        copies=len(copies),
        composer_chars=len(value),
    )


def sends_blocked(prof: Profile, facts: Facts) -> bool:
    """发送键存在、但一个都点不了，且当前不在生成。

    **必须与"找不到发送键"分开**：找不到是脚本/UI 问题，不可用是端不接受输入。
    两者混成一句话，就是 `docs/完成矩阵.md:15` 那句"无法区分是脚本问题还是额度问题"。
    """
    return (prof.send_disabled and facts.sends_total > 0
            and facts.sends_on == 0 and not facts.busy)


def new_state() -> dict:
    """调用方维护的跨轮状态。`submit_at` 由调用方在点完发送后填。"""
    return {
        "submit_at": None,      # 首次点击发送的时刻（monotonic）
        "echo_reads": 0,        # 连续命中"框里还是我们这段文字"的次数
        "idle_since": None,     # 收下之后"什么都没发生"的起点
        "accepted": False,      # 端已收下
        "busy_seen": False,     # 收下之后是否见过生成迹象（见过就不再判"无声"）
        "baseline_copies": 0,   # 提交前的复制键条数
    }


def verdict(prof: Profile, facts: Facts, state: dict, now: float) -> dict | None:
    """本轮是否已经可以判定"端拒绝了这一轮"。返回要并进结果里的字段，或 None。

    `state` 会被就地更新（计数 / 起点）。调用方每轮读树之后调一次。
    """
    # ---------------------------------------------------------- 判据 A
    if prof.echo_back and state.get("submit_at") is not None:
        waited = now - state["submit_at"]
        if waited >= SUBMIT_SETTLE and facts.echo and not facts.prompt_here:
            state["echo_reads"] = int(state.get("echo_reads") or 0) + 1
            if state["echo_reads"] >= ECHO_READS:
                return {
                    "ok": False, "stage": SUBMIT_REFUSED, "sent": True,
                    "submission": "unknown",
                    "echo_reads": state["echo_reads"],
                    "composer_chars": facts.composer_chars,
                    "hint": ("点了发送，但这段文字又回到输入框、且没有出现新的提问气泡："
                             "端拒收了这次提交（具体原因不在结构里，看 evidence/fail 的截图）。"
                             "不要重发同一条，先用 tools/submission_clear.py 解锁"),
                }
        elif state.get("echo_reads"):
            state["echo_reads"] = 0

    # ---------------------------------------------------------- 判据 C
    if prof.accepted_idle and state.get("accepted") and not state.get("busy_seen"):
        if facts.busy:
            state["busy_seen"] = True
            state["idle_since"] = None
        elif facts.copies > int(state.get("baseline_copies") or 0):
            # 已经有新回复了，交给正常交卷逻辑
            state["idle_since"] = None
        else:
            if state.get("idle_since") is None:
                state["idle_since"] = now
            elif now - state["idle_since"] >= IDLE_SECONDS:
                return {
                    "ok": False, "stage": ACCEPTED_SILENT, "sent": True,
                    "submission": "accepted",
                    "idle_seconds": round(now - state["idle_since"], 1),
                    "hint": ("端收下了提问，但一直没有生成迹象、也没有回复："
                             "别继续等，看 evidence/fail 的截图确认端的状态"),
                }
    return None
