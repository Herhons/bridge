"""人机验证 / 未知错误检测（CARD-06b 第 8 条）。

## 判据从哪来

2026-10-29 ~ 11-02 的巡检里，网页端偶尔会停在两种"不是脚本能处理"的页面上：

1. **Cloudflare 人机验证**：一个勾选框 + 「Verify you are human」/「确认您是真人」。
   脚本点它 → 可能被判风控、越点越严；所以 `*_use` **不点、不重试**，
   直接 `stage=human_check` 返回，由巡检层像正常用户那样点一次。
2. **「Unknown error」**：页面自己的错误横幅，重试只会重复同一个错误。
   单独 `reason=unknown_error`，**不归到人机验证里**（三家会审都提了这条）。

## 只认"短句 + 精确匹配"，不做全文包含

会审页面/聊天记录里就可能出现 "Cloudflare" 这个词（本仓的评审记录里就有）。
所以：
  · 只认一小撮**完整短语**（去掉首尾空白后整串相等），
  · 节点必须可见、名字不超过 60 字（横幅/勾选框标签都是短句），
  · 标题也算一条证据（挑战页标题就是 "Just a moment..."）。
这样宁可漏判（漏判时后面的找图会失败并留证），也不把正常页面误判成人机验证。
"""

from __future__ import annotations

from . import exit_codes as ec

# 完整短语（小写比对）。加新词只加"整句"，不要加单词。
CLOUDFLARE = {
    "verify you are human", "verify you are human.", "确认您是真人", "确认你是真人",
    "验证您是人类", "验证你是人类", "验证您是人类。", "请验证您是真人",
    "i'm not a robot", "我不是机器人", "人机验证", "安全验证",
    "just a moment...", "just a moment", "checking your browser",
    "checking your browser before accessing", "请稍候…", "请稍候...",
    "cf-challenge", "turnstile",
}

UNKNOWN_ERROR = {
    "unknown error", "unknown error.", "unknown error occurred",
    "something went wrong", "something went wrong.", "出错了", "出错了。",
    "发生错误", "发生错误。", "未知错误", "未知错误。",
}

MAX_LEN = 60          # 超过这个长度的节点不当横幅看（聊天正文常常很长）


class HumanCheck(RuntimeError):
    """页面停在人机验证/未知错误上。CLI 层转成 stage=human_check。"""

    stage = "human_check"
    code = ec.FAIL

    def __init__(self, kind: str, matches: list | None = None, facts: dict | None = None):
        self.kind = kind
        self.reason = "unknown_error" if kind == "unknown_error" else "cloudflare"
        self.matches = list(matches or [])
        self.facts = dict(facts or {})
        super().__init__(f"{self.reason}: {self.matches[:3]}")

    def as_result(self) -> dict:
        return {"ok": False, "stage": self.stage, "reason": self.reason,
                "code": self.code, "matches": self.matches[:8],
                "hint": ("页面停在「Unknown error」：先人工看一眼，别重试"
                         if self.reason == "unknown_error" else
                         "页面停在人机验证：脚本不点、不重试；由巡检层像正常用户点一次")}


def _norm(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def detect(root, *, title: str = "") -> dict:
    """扫一遍控件树 + 窗口标题。返回 {"hit","kind","matches"}。

    只看可见节点；不可见的分支（折叠的菜单、离屏列表）里的旧文案不算。
    """
    matches: list[str] = []
    kind = None
    title_key = _norm(title)
    for phrase in CLOUDFLARE:
        if title_key == phrase or title_key.startswith(phrase + " "):
            matches.append(f"title:{title}")
            kind = "cloudflare"
            break
    for phrase in UNKNOWN_ERROR:
        if title_key == phrase:
            matches.append(f"title:{title}")
            kind = "unknown_error"
            break

    if root is not None:
        for node in root.walk():
            name = _norm(getattr(node, "name", ""))
            if not name or len(name) > MAX_LEN:
                continue
            try:
                if node.control is not None and node.control.IsOffscreen:
                    continue
            except Exception:               # noqa: BLE001 - 元素失效：当作不可见
                continue
            if name in CLOUDFLARE:
                matches.append(node.name)
                kind = kind or "cloudflare"
            elif name in UNKNOWN_ERROR:
                matches.append(node.name)
                kind = "unknown_error" if kind != "cloudflare" else kind
    return {"hit": bool(matches), "kind": kind, "matches": matches}


def check(root=None, *, title: str = "") -> dict:
    """detect 的别名；调用方读起来更像一道门。"""
    return detect(root, title=title)


def raise_if_hit(root=None, *, title: str = "") -> dict:
    """命中就抛 `HumanCheck`，否则返回空 dict。"""
    found = detect(root, title=title)
    if found["hit"]:
        raise HumanCheck(found["kind"], found["matches"],
                         {"title": title, "count": len(found["matches"])})
    return {}
