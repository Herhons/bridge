"""发送前守卫的统一清单（CARD-v010-P0P1 第 3 条 / 第 6 条）。

## 它解决什么

到 v0.0.9 为止，发送前的只读守卫散在四个地方：`sendgate.page_stamp/page_confirm`、
`sendgate.pre_send`（档位回执）、`textcmp.equal` 的读回比对、`submissions.pending`
的账本查询。顺序恰好是"守卫全在 `reserve` 之前"，但**没有一处统一出口**：
以后谁往 `reserve` 后面加一个守卫，就会重新出现"没发出去却消耗了配额"。

这个模块把"这一轮能不能发"收成一个**只读**清单：不写账、不点控件、不改页面。
全部通过返回 `None`；任何一条不过，返回一个固定形状的拒绝结果
（`stage="precheck_refused"`、`delivery_state="PRECHECK_REFUSED"`、
`attempt_consumed=False`），调用方原样返回即可。

## 与既有 stage 的关系

`concurrent_ui` / `tier_unconfirmed` / `user_took_over` 这些 stage 已被既有测试与
调用方依赖，**不在这里改写**。本模块只负责**新增**的两类守卫：

  · `payload_sha`  —— 载荷哈希与实际正文是否一致（`--payload-sha256`）；
  · `user_active`  —— 用户是否正在用键鼠（默认关闭，`--idle-gate` 开启）；
  · `grant_token`  —— 一次性授权票据（`--grant-token`）。

其余检查（`journal_free` / `prompt_match` / `page_stable`）作为可选清单保留，
供 T5 之类的离线用例直接调用，生产链路上仍走各自既有实现。
"""

from __future__ import annotations

import hashlib
import os

from . import guard, submissions, textcmp, tier_guard

#: 结果层投递状态（与 `submissions.delivery_state_of` 的账本层词汇分开）
PRECHECK_REFUSED = "PRECHECK_REFUSED"
STAGED_UNSENT = "STAGED_UNSENT"

#: 环境变量：默认关闭的用户活跃门禁。与 `BRIDGE_JOB_ID` 同一命名族。
ENV_IDLE_GATE = "BRIDGE_IDLE_GATE"
ENV_IDLE_SECONDS = "BRIDGE_IDLE_SECONDS"
ENV_GRANT_TOKEN = "BRIDGE_GRANT_TOKEN"

DEFAULT_IDLE_SECONDS = 2.0

#: `run()` 支持的全部检查名。`require` 用它做白名单校验（拼错就报错，不静默跳过）。
CHECKS = ("payload_sha", "user_active", "grant_token",
          "journal_free", "prompt_match", "session_anchor", "page_stable")


def idle_gate_enabled() -> bool:
    raw = (os.environ.get(ENV_IDLE_GATE) or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def idle_threshold() -> float:
    try:
        return max(0.0, float(os.environ.get(ENV_IDLE_SECONDS) or DEFAULT_IDLE_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_IDLE_SECONDS


def grant_token() -> str | None:
    value = (os.environ.get(ENV_GRANT_TOKEN) or "").strip()
    return value or None


def digest(prompt) -> str:
    """正文字节的 SHA-256（hex）。

    签发、校验、核销三处必须同源：票据绑定的是"这段正文"，不是"某个进程环境变量"。
    读 `BRIDGE_PAYLOAD_SHA256` 会把上一次调用的哈希绑到这一次，所以一律现算。
    """
    return hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()


def _refusal(reason: str, hint: str, checks: list) -> dict:
    return precheck_refused(reason=reason, hint=hint, checks=checks)


def precheck_refused(*, reason: str, hint: str = "", checks: list | None = None) -> dict:
    """前置守卫拒绝的固定形状。**没有任何"可能已发"的余地**。"""
    return {
        "ok": False,
        "stage": "precheck_refused",
        "delivery_state": PRECHECK_REFUSED,
        "attempt_consumed": False,
        "sent": False,
        "submission": "not_sent",
        "reason": str(reason),
        "hint": str(hint or reason),
        "checks": list(checks or []),
    }


def staged_unsent(*, reason: str, hint: str = "", compare: dict | None = None,
                  draft_cleaned: bool = False, evidence_path: str | None = None,
                  checks: list | None = None) -> dict:
    """写入成功但**没有点击**的固定形状（与 `gemini.StagedUnsent` 同构）。"""
    compare = dict(compare or {})
    return {
        "ok": False,
        "stage": "staged_unsent",
        "delivery_state": STAGED_UNSENT,
        "attempt_consumed": False,
        "sent": False,
        "submission": "not_sent",
        "reason": str(reason),
        "hint": str(hint or reason),
        "draft_cleaned": bool(draft_cleaned),
        "evidence_path": evidence_path,
        "link_target_unverifiable": bool(compare.get("link_target_unverifiable")),
        "checks": list(checks or []),
    }


def run(*, endpoint: str, site: str, prompt: str, payload_sha256: str | None = None,
        view=None, screen=None, stamp: dict | None = None, send_sid: str | None = None,
        root=None, require: tuple[str, ...] = (), user_active_probe=None,
        expected_sha256: str | None = None) -> dict | None:
    """跑一遍只读守卫。全部通过返回 `None`，否则返回拒绝结果。

    `require` 里放**额外**要求跑的检查名（默认只跑新增的三项）。
    拼错的名字直接抛 `ValueError` —— 静默跳过等于给自己开后门。
    """
    wanted = list(require)
    for name in wanted:
        if name not in CHECKS:
            raise ValueError(f"未知的 precheck 检查项：{name!r}（可选 {', '.join(CHECKS)}）")

    if expected_sha256 is None:
        # 显式参数优先；没给就一律看环境变量。
        # CARD-v010-FINAL-POLISH：旧条件还要求 `BRIDGE_PAYLOAD_FILE` 存在，于是
        # 「只给 --payload-sha256、不给 --payload-file」时这一整段被跳过 —— 而
        # `payload.preflight` 恰恰只写 SHA 不写 FILE，结果用户显式要求的哈希校验
        # 被静默丢掉，二道闸空转（八端全部走这条 env 分支）。
        # 反过来 FILE 在而 SHA 缺时仍是 None，行为不变。
        # 残留 env 的代价是 fail-closed 拒绝，比"以为校验过其实没校验"安全。
        expected_sha256 = (os.environ.get("BRIDGE_PAYLOAD_SHA256") or "").strip() or None

    checks: list[dict] = []

    def add(name: str, ok: bool, reason: str = "", **detail) -> bool:
        item = {"name": name, "ok": bool(ok), "reason": reason or None}
        if detail:
            item["detail"] = detail
        checks.append(item)
        return bool(ok)

    # ---- 1) 载荷哈希（给了才查；CLI 层已经查过一次，这里是二道闸）
    if expected_sha256 is not None:
        got = digest(prompt)
        ok = got.lower() == str(expected_sha256).strip().lower()
        if not add("payload_sha", ok, "" if ok else "payload_sha256_mismatch"):
            return _refusal("payload_sha256_mismatch",
                            "载荷哈希与正文不一致：拒绝进入任何 UI 操作", checks)

    # ---- 2) 一次性授权票据（存在就校验，过期/已用/不匹配一律拒）
    token = grant_token()
    if token:
        verdict = tier_guard.check_grant(
            token, target=endpoint, action="use",
            payload_sha256=expected_sha256 or payload_sha256, root=root)
        if not add("grant_token", verdict.get("ok"),
                   "" if verdict.get("ok") else str(verdict.get("reason") or ""),
                   grant_id=token):
            # CARD-v010-P4B：`check_grant` 的 8 个失败分支**全部已带 `grant_` 前缀**，
            # 这里再拼一次会产出 `grant_grant_expired` 这种双前缀。原样透传即可，
            # 上面 `add()` 记进 checks 的也是同一个值，两处必须一致。
            return _refusal(str(verdict.get("reason") or "grant_invalid"),
                            "授权票据不成立：" + str(verdict.get("hint") or ""), checks)
    else:
        add("grant_token", True, "absent")

    # ---- 3) 用户活跃（默认关闭；开启后无票据即拒）
    if idle_gate_enabled():
        probe = user_active_probe or (lambda: guard.user_active(idle_threshold()))
        active = bool(probe())
        if active and not token:
            add("user_active", False, "user_active",
                threshold=idle_threshold(), idle=guard.idle_seconds())
            return _refusal("user_active",
                            "你正在用电脑，且没有一次性授权票据；本轮不发送", checks)
        add("user_active", True, "granted" if active and token else "idle")

    # ---- 4) 可选清单
    if "journal_free" in wanted:
        prior = submissions.pending(site, prompt, root=root)
        if not add("journal_free", prior is None,
                   "" if prior is None else "submission_pending",
                   state=(submissions.delivery_state_of(prior) if prior else None)):
            return _refusal("submission_pending",
                            "这条提问已有提交记录，按 fail-closed 不重发", checks)

    if "prompt_match" in wanted:
        value = getattr(view, "value", "") if view is not None else None
        if value is None:
            add("prompt_match", True, "unchecked")
        else:
            verdict = textcmp.compare(prompt, value)
            if not add("prompt_match", verdict.get("ok"),
                       "" if verdict.get("ok") else str(verdict.get("reason") or ""),
                       stats=verdict.get("stats")):
                return _refusal(f"prompt_{verdict.get('reason') or 'mismatch'}",
                                verdict.get("hint") or "读回与提问不一致", checks)

    if "session_anchor" in wanted:
        from .conversation import path_of, session_id_of

        url = getattr(view, "url", None) if view is not None else None
        now_sid = session_id_of(url)
        if now_sid is None and send_sid is None:
            ok = bool(url) and send_sid is None
            add("session_anchor", ok, "" if ok else "session_missing",
                url=url)
            if not ok:
                return _refusal("session_missing", "读不到会话地址，拒绝发送", checks)
        else:
            ok = now_sid == send_sid
            if not add("session_anchor", ok, "" if ok else "session_drift",
                       now_sid=now_sid, send_sid=send_sid):
                return _refusal("session_drift", "会话锚点漂移，拒绝发送", checks)

    if "page_stable" in wanted:
        if stamp is None or screen is None:
            add("page_stable", True, "unchecked")
        else:
            from . import sendgate

            verdict = sendgate.page_confirm(screen, site, stamp)
            if not add("page_stable", verdict.get("ok"),
                       "" if verdict.get("ok") else "page_changed",
                       changed=verdict.get("changed")):
                return _refusal("page_changed", "页面在写入后被换走，拒绝发送", checks)

    return None


def consume_grant_token(*, root=None, target: str = "", action: str = "use",
                        payload_sha256: str | None = None) -> dict:
    """点击已经发出之后核销票据。**单次即焚**。

    放在"点击之后"而不是"校验之后"：校验通过但随后被抢走的情况仍然存在，
    那一次不该烧掉用户的授权。
    """
    token = grant_token()
    if not token:
        return {"ok": True, "reason": "no_token"}
    return tier_guard.consume_grant(token, target=target or "", action=action,
                                    payload_sha256=payload_sha256, root=root)
