"""会话锚点：判断「现在还在不在本轮那个对话」，以及不在时怎么办。

为什么需要：`start_new_chat()` 只验「地址里没有会话 id」，它证明不了"这是本轮
那个会话"；而超时重试路径又会再调它，于是同一提问进了两个会话（2026-11-12 实测）。

这里把决策提成纯函数 `resolve()`：
  · URL 与锚点一致          → stay
  · 不一致、锚点有 URL、未 accepted → navigate（回跳锚点 URL）
  · 不一致且已 accepted      → session_lost（**绝不新建**，保留账本交人工）
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class SessionAnchor:
    site: str
    round_name: str
    url: str                     # 本轮会话的规范 URL（带 session id）
    prompt_hash: str             # 本轮提问的短哈希，用于核对"这条回复是不是这题的"
    accepted: bool = False       # 本轮提问是否已被端接收

    def matches(self, url: str) -> bool:
        return bool(url) and bool(self.url) and url.split("?")[0] == self.url.split("?")[0]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SessionAnchor":
        return cls(site=data.get("site", ""), round_name=data.get("round_name", ""),
                   url=data.get("url", ""), prompt_hash=data.get("prompt_hash", ""),
                   accepted=bool(data.get("accepted")))


def resolve(anchor: SessionAnchor | None, current_url: str) -> dict:
    """返回 {action: stay|navigate|new_allowed|session_lost, url, reason}。"""
    if anchor is None:
        return {"action": "new_allowed", "url": current_url, "reason": "no_anchor"}
    if anchor.matches(current_url):
        return {"action": "stay", "url": current_url, "reason": "url_matches"}
    if anchor.accepted:
        return {"action": "session_lost", "url": current_url,
                "reason": "drifted_after_accepted"}
    if anchor.url:
        return {"action": "navigate", "url": anchor.url, "reason": "drifted"}
    return {"action": "new_allowed", "url": current_url, "reason": "no_anchor_url"}
