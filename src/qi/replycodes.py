"""采集停摆的显式错误码（T3-1）。

**要解决的问题**：`reply_text()` 读不到答案时只返回 `None`，调用方（`wait_reply`）
把它和"页面还在渲染"一视同仁，于是一路静默轮询到 120s / 900s 超时，最后只报一个
笼统的 `timeout`。用户看到的是"等了五分钟然后超时"，而真实原因可能是"结构根本
不可证，再等一万年也等不到"。

本模块把"为什么读不到"翻译成 4 个明确错误码，让调用方能在 3–5 秒内失败。

**这里只有纯函数，不碰 UI、不读文件、不写日志**，所以可以离线穷举测试。

错误码含义：

==================== ========================================== ============
码                    含义                                        还能不能等
==================== ========================================== ============
STREAMING_IN_PROGRESS 页面还在生成（停止键可点）                  **能等**
CONTAINER_BOUNDARY_   有文字但没有可证的消息容器边界               不能等
UNPROVEN
ATTRIBUTION_UNVERIFIED 读到了回复，但证明不了它是本轮答案         不能等
SESSION_ID_MISSING     账本里没有会话 id，永远恢复不了              不能等
==================== ========================================== ============

**只有 STREAMING_IN_PROGRESS 允许继续等**。其余三个必须立刻返回，让面板显示
明确原因——这是施工单 T3-1 第 2 条的硬要求。
"""
from __future__ import annotations

ATTRIBUTION_UNVERIFIED = "ATTRIBUTION_UNVERIFIED"
CONTAINER_BOUNDARY_UNPROVEN = "CONTAINER_BOUNDARY_UNPROVEN"
STREAMING_IN_PROGRESS = "STREAMING_IN_PROGRESS"
SESSION_ID_MISSING = "SESSION_ID_MISSING"
#: B10：收下了、但超过上限仍然一个字符都读不到。
#: 原来 `no_copy/no_text/no_group` 被当成"瞬态、永远不判死"，
#: 于是"从头到尾读不到复制键"的页面会一路等到 900 秒。
REPLY_NEVER_APPEARED = "REPLY_NEVER_APPEARED"

ALL_CODES = (ATTRIBUTION_UNVERIFIED, CONTAINER_BOUNDARY_UNPROVEN,
             STREAMING_IN_PROGRESS, SESSION_ID_MISSING, REPLY_NEVER_APPEARED)

#: 可以继续等的码（其余都是终态）。
WAITABLE = frozenset({STREAMING_IN_PROGRESS})

#: 连续多少次相同原因才判死。3 × POLL(0.4s) ≈ 1.2s，留足了页面重绘的抖动余量，
#: 又远小于 F5 要求的 10s。
STALL_READS = 3

#: `reply_text_detail` 的结构原因 → 边界不可证。
_BOUNDARY_REASONS = frozenset({"loose_text", "multiple_messages", "page_scope"})

#: 这些只说明"这一轮还没读到"，不构成判死理由（可能真的还在渲染）。
_TRANSIENT_REASONS = frozenset({"no_text", "no_copy", "no_group"})

#: 瞬态原因最多容忍这么久。超过还一个字都读不到，就不是"还在渲染"了。
#: 取 120s：远大于正常首段（Gemini 实测 44.1s），又远小于 900s 预算。
TRANSIENT_MAX_SECONDS = 120.0


def classify_stall(reasons, *, busy, sent, has_session_id=True, since_accept=None):
    """根据最近若干次读取的结构原因，判定是否该 fail-fast。

    `reasons`：按时间顺序的 reason 序列（来自 `reply_text_detail`，
    可能含 `unattributed` / `no_session_id`）
    `since_accept`：距"认领到提交"过了多少秒；给了才启用瞬态上限。

    返回错误码；返回 None 表示"继续等"。
    """
    if busy:
        # 停止键还在 = 确实在生成。这个时候判死是错的，必须等。
        return STREAMING_IN_PROGRESS

    tail = [r for r in list(reasons or [])[-STALL_READS:]]
    if len(tail) < STALL_READS:
        return None

    # 连续三次同一类结构问题才算数：单次的裸文本很可能只是渲染中间态。
    if len(set(tail)) == 1:
        only = tail[0]
        if only in _BOUNDARY_REASONS:
            return CONTAINER_BOUNDARY_UNPROVEN
        if only == "unattributed":
            return ATTRIBUTION_UNVERIFIED

    # B10：瞬态原因不再无限容忍。
    if (sent and since_accept is not None and since_accept >= TRANSIENT_MAX_SECONDS
            and set(tail) <= _TRANSIENT_REASONS):
        return REPLY_NEVER_APPEARED

    if not sent:
        # 还没认领到提交，谈不上"答案归属"问题，继续等。
        return None

    if not has_session_id and all(r == "no_session_id" for r in tail):
        return SESSION_ID_MISSING

    return None


def describe(code):
    """给人看的一句话（面板/日志都能用）。"""
    return {
        CONTAINER_BOUNDARY_UNPROVEN:
            "回答的边界无法证明（有文字但没有可证的消息容器）；继续等也不会变好",
        ATTRIBUTION_UNVERIFIED:
            "读到了回复，但证明不了它是本轮答案（会话身份不足）",
        STREAMING_IN_PROGRESS:
            "页面仍在生成中，继续等待",
        SESSION_ID_MISSING:
            "账本里没有会话 id，无法证明恢复目标；这条提交永远无法自动恢复",
        REPLY_NEVER_APPEARED:
            "已经收下了，但超过上限仍然读不到任何回答正文；再等也不会变好",
    }.get(code, "未知状态")
