"""Unified base driver for all 11 Bridge v0.2.5 endpoints.

Fixes & capabilities in v0.2.3:
- BUG-08 (Hermes): GBK-safe stdio output (``configure_stdio_utf8`` / ``safe_print_json``)
  eliminating Windows ``UnicodeEncodeError`` on emojis like ``✅``; preserves composer
  focus before hotkey submit; supports configurable ``submit_hotkey`` and exact Hermes
  composer coordinates ``[0.446, 0.946]``.
- BUG-09 (DeepSeek): Fixes ``_extract_reply_lines`` so a bottom composer prompt echo
  never wipes out a valid reply; tightens ``_is_generating`` to avoid static-label
  false positives; adds scroll-to-bottom before ``copy_button`` for cross-screen replies;
  records rich timeout diagnostics in ``runs/fail/``.
- BUG-06 (GrokBot): Strips standalone timestamps (``04:18``, ``昨天 19:30``) and UI chrome
  labels while keeping prompt-anchored reply slicing.
- Model Policy & Cost Guard: Supports ``--profile test|routine|council``, ``--free-only``,
  dynamic ``0.00x`` / ``免费`` option matching on WorkBuddy, Fail-Closed ``tier_unconfirmed``
  (exit code 5) when a paid multiplier is detected or free tier cannot be verified, and
  ``models(live=True)`` live dropdown probing.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from engine import __version__
from engine.config import (
    RUNS_DIR,
    classify_model_label,
    get_endpoint_config,
    load_bridge_config,
    load_model_policy,
    resolve_profile,
)
from engine.file_dialog import (  # noqa: F401 - compatibility re-exports
    BM_CLICK,
    FILE_DIALOG_CLASS,
    FILE_NAME_CONTROL_ID,
    OPEN_BUTTON_CONTROL_ID,
    WM_SETTEXT,
    _dialog_edit_hwnd,
    _win32_user32,
    _window_pid,
    fill_file_dialog,
    fill_windows_file_dialog as _fill_windows_file_dialog,
    find_existing_file_dialog,
    find_file_dialog_window,
    list_file_dialogs,
)
from engine.ledger import SubmissionLedger
from engine.lock import hold_ui_lock
from engine.uia import (
    Snapshot,
    UIAError,
    UINode,
    click_ratio,
    click_ratio_and_paste,
    get_clipboard,
    get_clipboard_sequence,
    invoke_node,
    preserved_clipboard,
    scroll_into_view,
    scroll_window_bottom,
    send_hotkey,
    set_node_text,
    snapshot_delta,
    take_snapshot,
    upload_via_file_dialog,
)
from engine.vision import capture_window, match_template
from engine import window as window_mod  # mockable probe surface for focus checks
from engine.window import (
    FOCUS_GUARD_STRICT_DEFAULT,
    FocusGuard,
    WindowInfo,
    find_window,
    focus_window,
    launch_app_id,
    launch_url_in_chrome,
    preserved_focus,
)

EXIT_CODES = {
    "ok": 0,
    "error": 1,
    "missing_resource": 3,
    "cancelled_by_user": 4,
    "tier_unconfirmed": 5,
    "duplicate": 6,
}

#: UI text that means "this window will never answer without a human". Matched
#: case-insensitively against every visible node of a snapshot, and only while
#: waiting for a reply, so an ordinary answer that happens to quote these words
#: is still accepted. Endpoints may extend the list with their own
#: ``blocking_popup_patterns`` entries in ``config/endpoints.json``.
#:
#: ``Allow`` on its own is intentionally *not* a pattern: Antigravity's
#: permission dialog always carries ``Waiting for user input`` or
#: ``Yes, allow this time``, while "Allow me to explain" is a normal sentence.
DEFAULT_BLOCKING_POPUP_PATTERNS: tuple[str, ...] = (
    # Gemini paywall / upgrade promotion.
    "解锁 4 carbon",
    "unlock 4 carbon",
    "us$99.99",
    "升级到",
    # Antigravity / web permission confirmation. "Request Review" is
    # deliberately absent: it is a permanently visible button in the lower-right
    # corner, so matching it fired on every normal conversation.
    "waiting for user input",
    "yes, allow this time",
    "allow check ",
    # Generic paid-wall wording.
    "upgrade to continue",
    "升级以继续",
)


def snapshot_text(snap: "Snapshot | None") -> str:
    """All visible node labels of a snapshot, newline-joined and lowercased."""
    if snap is None:
        return ""
    nodes = getattr(snap, "nodes", None) or ()
    return "\n".join(
        str(getattr(node, "name", "") or "") for node in nodes
    ).lower()


def fill_windows_file_dialog(
    paths: list[str],
    *,
    owner_pid: int,
    timeout: float = 10.0,
    poll: float = 0.15,
    user32=None,
) -> str:
    """Compatibility shim around :func:`engine.file_dialog.fill_windows_file_dialog`.

    Only difference: the marshaller is this module's ``ctypes`` binding, so the
    long-standing ``mock.patch.object(base, "ctypes", ...)`` usage keeps working.
    """
    return _fill_windows_file_dialog(
        paths,
        owner_pid=owner_pid,
        timeout=timeout,
        poll=poll,
        user32=user32,
        ctypes_module=ctypes,
    )


DEFAULT_EXCLUDE_COMPOSER_NAMES = (
    "地址和搜索栏",
    "Address and search bar",
    "搜索会话",
    "Search",
)

DEFAULT_BUSY_TEXTS = (
    "正在工作",
    "正在思考",
    "正在生成",
    "thinking...",
    "generating...",
    "助手 正在工作",
)

DEFAULT_NOISE_CONTAINS = (
    "内容由 AI 生成",
    "创建拆分视图",
    "今天日程安排是什么",
    "Chooses Fast or Expert",
    "chat.deepseek.com",
    "grok.com",
)

DEFAULT_NOISE_REGEXES = (
    r"^已思考（用时\s*\d+\s*秒）$",
    r"^\d{1,2}:\d{2}(?::\d{2})?$",
    r"^(?:上午|下午|今天|昨天|前天)\s*\d{1,2}:\d{2}$",
    r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?$",
)

#: Focus option A (M4-4): ON unless an endpoint/bridge config turns it off.
#: Chrome only exposes a clickable accessibility tree while its window is
#: foreground, so automation borrows the foreground for one action and hands it
#: back in ``preserved_focus``' ``finally``.
FOCUS_BORROW_RESTORE_DEFAULT = True

#: A ``DocumentControl`` covering this much of the window is the *whole page*
#: container Chrome exposes for a backgrounded tab, never the composer.
FULLPAGE_DOC_MIN_WIDTH_RATIO = 0.85
FULLPAGE_DOC_MIN_HEIGHT_RATIO = 0.70


def is_fullpage_document_node(
    node: "UINode | None",
    window_rect: tuple[int, int, int, int] | None,
) -> bool:
    """True for the whole-page ``DocumentControl`` of a sleeping Chrome tab.

    ``bridge.py status`` used to report ``ready=true`` for a backgrounded
    ``deepseek`` / ``grok`` tab: the page container is a ``DocumentControl`` and
    it was being accepted as the composer. A container that spans >=85% of the
    window width *and* >=70% of its height is the page, not an input box — real
    composers are a shallow strip at the bottom.
    """
    if node is None:
        return False
    if str(getattr(node, "role", "") or "") != "DocumentControl":
        return False
    rect = getattr(node, "rect", None)
    if not rect or not window_rect:
        return False
    try:
        _nl, _nt, nw, nh = (int(v) for v in rect)
        _wl, _wt, ww, wh = (int(v) for v in window_rect)
    except (TypeError, ValueError):
        return False
    if nw <= 0 or nh <= 0 or ww <= 0 or wh <= 0:
        return False
    return (
        nw >= ww * FULLPAGE_DOC_MIN_WIDTH_RATIO
        and nh >= wh * FULLPAGE_DOC_MIN_HEIGHT_RATIO
    )


def configure_stdio_utf8() -> None:
    """Best-effort UTF-8 stdio reconfiguration for Windows GBK consoles (fixes BUG-08)."""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass


def safe_print(text: str, *, stream: Any = None, flush: bool = False) -> None:
    """Print *text* without crashing on Windows GBK consoles."""
    target = sys.stdout if stream is None else stream
    try:
        print(text, file=target, flush=flush)
    except UnicodeEncodeError:
        enc = getattr(target, "encoding", None) or "ascii"
        fallback = text.encode(enc, errors="replace").decode(enc, errors="replace")
        print(fallback, file=target, flush=flush)


def safe_print_json(payload: Any, *, indent: int | None = 2, stream: Any = None) -> None:
    """Serialize *payload* as JSON and print safely even on GBK consoles."""
    target = sys.stdout if stream is None else stream
    text = json.dumps(payload, ensure_ascii=False, indent=indent)
    try:
        print(text, file=target, flush=True)
    except UnicodeEncodeError:
        print(json.dumps(payload, ensure_ascii=True, indent=indent), file=target, flush=True)


def exit_code_for(result: dict[str, Any]) -> int:
    if result.get("ok"):
        return 0
    err = str(result.get("error") or "")
    if err in EXIT_CODES:
        return EXIT_CODES[err]
    if err in ("window_not_found", "composer_not_found", "page_not_ready", "TARGET_NOT_FOUND"):
        return EXIT_CODES["missing_resource"]
    return EXIT_CODES["error"]


def _default_confirm(endpoint: str, prompt: str, model: str | None) -> bool:
    safe_print(f"Endpoint: {endpoint}")
    if model:
        safe_print(f"Model: {model}")
    preview = prompt[:200] + ("..." if len(prompt) > 200 else "")
    safe_print(f"Prompt: {preview}")
    try:
        ans = input("Send? [Y/n] ").strip().lower()
    except EOFError:
        return False
    return ans in ("", "y", "yes")


class EndpointDriver:
    """UIA-first, screenshot-fallback driver shared by all 11 endpoints."""

    name: str = ""

    def __init__(
        self,
        name: str | None = None,
        *,
        config: dict[str, Any] | None = None,
        bridge_config: dict[str, Any] | None = None,
        model_policy: dict[str, Any] | None = None,
        snapshot_fn: Callable[[int], Snapshot] | None = None,
        window_fn: Callable[[], WindowInfo | None] | None = None,
        invoke_fn: Callable[[UINode, bool], str] | None = None,
        write_fn: Callable[[UINode, str, bool, int], str] | None = None,
        confirm_fn: Callable[[str, str, str | None], bool] | None = None,
        hotkey_fn: Callable[..., None] | None = None,
        scroll_fn: Callable[[tuple[int, int, int, int] | None], bool] | None = None,
        dialog_fn: Callable[[str], str] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        lock_path: Path | None = None,
        ledger_path: Path | None = None,
        evidence_dir: Path | None = None,
    ):
        self.name = name or self.name
        if not self.name:
            raise ValueError("EndpointDriver requires a name")
        self.cfg = dict(config) if config is not None else get_endpoint_config(self.name)
        self.bridge_cfg = dict(bridge_config) if bridge_config is not None else load_bridge_config()
        self.model_policy = dict(model_policy) if model_policy is not None else load_model_policy()
        self._snapshot_fn = snapshot_fn
        self._window_fn = window_fn
        self._invoke_fn = invoke_fn
        self._write_fn = write_fn
        self._confirm_fn = confirm_fn or _default_confirm
        self._hotkey_fn = hotkey_fn
        self._scroll_fn = scroll_fn
        self._dialog_fn = dialog_fn
        self._sleep = sleep_fn
        self._lock_path = lock_path
        self._evidence_dir = evidence_dir or (RUNS_DIR / "fail")
        self.ledger = SubmissionLedger(ledger_path) if ledger_path is not None else SubmissionLedger()
        #: Last ``focus_guard:*`` diagnostic; "" when focus stayed put.
        self.last_focus_event = ""

    # ----------------------------------------------------------------- helpers
    def _send_hotkey_safe(self, *keys: str) -> None:
        if self._hotkey_fn is not None:
            self._hotkey_fn(*keys)
        elif os.name == "nt":
            try:
                send_hotkey(*keys)
            except Exception:
                pass

    def _send_esc(self) -> None:
        self._send_hotkey_safe("esc")

    def _scroll_bottom(self, snap: Snapshot | None) -> bool:
        wrect = snap.window_rect if snap is not None else None
        if self._scroll_fn is not None:
            return bool(self._scroll_fn(wrect))
        return scroll_window_bottom(wrect)

    def _record_failure_evidence(self, error: str, snap: Snapshot | None = None, extra: dict[str, Any] | None = None) -> str | None:
        try:
            self._evidence_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = self._evidence_dir / f"{self.name}_{stamp}_{error}.json"
            payload: dict[str, Any] = {
                "endpoint": self.name,
                "error": error,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                **(extra or {}),
            }
            if snap is not None:
                payload["snapshot"] = snap.to_dict(interactive_only=False, limit=300)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return str(path)
        except Exception:
            return None

    def _find_target_window(self) -> WindowInfo | None:
        """Process + title + WNDCLASS lookup, with a title-agnostic retry.

        Electron apps rename their window after the active document: Antigravity
        turns "Antigravity" into the conversation title as soon as a chat starts
        (measured 2026-11-21), which used to make status()/ask() lose a window
        that was plainly on screen. The retry drops only the title filter and
        only for endpoints that pin a ``window_class`` -- process name alone is
        not selective enough for shared ``chrome.exe`` web endpoints.
        """
        window_class = self.cfg.get("window_class")
        title_contains = self.cfg.get("title_contains")
        win = find_window(
            process_names=self.cfg.get("process_names"),
            title_contains=title_contains,
            exclude_processes=self.cfg.get("exclude_processes"),
            window_class=window_class,
        )
        if win is None and title_contains and window_class:
            win = find_window(
                process_names=self.cfg.get("process_names"),
                title_contains=None,
                exclude_processes=self.cfg.get("exclude_processes"),
                window_class=window_class,
            )
            if win is not None:
                safe_print(
                    "[%s] window title no longer matches %r; matched by "
                    "process+window_class instead"
                    % (self.name, title_contains),
                    stream=sys.stderr,
                    flush=True,
                )
        return win

    def locate_window(self, *, auto_open: bool = False) -> WindowInfo | None:
        if self._window_fn is not None:
            return self._window_fn()
        win = self._find_target_window()
        if win is None and auto_open:
            launched = False
            if self.cfg.get("kind") == "web" and self.cfg.get("url"):
                launched = launch_url_in_chrome(self.cfg["url"], self.bridge_cfg.get("chrome_path", ""))
            elif self.cfg.get("launch_app_id"):
                launched = launch_app_id(str(self.cfg["launch_app_id"]))
            if launched:
                self._sleep(2.5)
                win = self._find_target_window()
        return win

    def take_snap(self, hwnd: int) -> Snapshot:
        if self._snapshot_fn is not None:
            return self._snapshot_fn(hwnd)
        return take_snapshot(
            hwnd,
            url_host=self.cfg.get("url_host"),
            prefer_document=True,
            include_focusable_text=True,
        )

    def _find_composer(self, snap: Snapshot) -> UINode | None:
        sel = self.cfg.get("selectors", {}).get("composer", {})
        ratio_box = sel.get("anchor_box_ratio")
        exclude = list(DEFAULT_EXCLUDE_COMPOSER_NAMES) + list(sel.get("exclude_names") or [])
        roles = sel.get("roles") or ["EditControl", "DocumentControl", "GroupControl"]
        win_rect = snap.window_rect
        hits = [
            node
            for node in snap.find(
                roles=roles,
                names=sel.get("names") or None,
                name_contains=sel.get("name_contains") or None,
                exclude_names=exclude,
                automation_ids=sel.get("automation_ids") or None,
                ratio_box=ratio_box,
                enabled_only=True,
                actionable_only=True,
            )
            # A sleeping Chrome tab exposes one whole-page ``DocumentControl``;
            # accepting it made ``status`` claim the endpoint was ready.
            if not is_fullpage_document_node(node, win_rect)
        ]
        if hits:
            return hits[-1]
        edits = snap.find(
            roles=["EditControl"],
            exclude_names=exclude,
            ratio_box=ratio_box,
            enabled_only=True,
        )
        if edits:
            return edits[-1]
        if not ratio_box:
            any_edits = snap.find(roles=["EditControl"], exclude_names=exclude, enabled_only=True)
            return any_edits[-1] if any_edits else None
        return None

    def _find_send_button(self, snap: Snapshot) -> UINode | None:
        sel = self.cfg.get("selectors", {}).get("send_button", {})
        hits = snap.find(
            roles=sel.get("roles") or ["ButtonControl"],
            names=sel.get("names") or ["发送", "提交", "Send", "Submit"],
            name_contains=sel.get("name_contains") or None,
            automation_ids=sel.get("automation_ids") or None,
            ratio_box=sel.get("anchor_box_ratio"),
            enabled_only=True,
        )
        return hits[-1] if hits else None

    def _copy_buttons(self, snap: Snapshot) -> list[UINode]:
        sel = self.cfg.get("selectors", {}).get("copy_button", {})
        names = sel.get("names")
        if not names:
            return []
        return snap.find(roles=["ButtonControl"], names=names, enabled_only=True)

    def _try_copy_button_reply(self, snap: Snapshot, win: WindowInfo, *, headed: bool) -> str | None:
        if self._invoke_fn is not None:
            return None
        btns = self._copy_buttons(snap)
        if not btns and headed and self.cfg.get("scroll_before_copy", False):
            # Cross-screen replies may have scrolled the latest copy button out of the viewport.
            if self._scroll_bottom(snap):
                self._sleep(0.3)
                try:
                    snap = self.take_snap(win.hwnd)
                    btns = self._copy_buttons(snap)
                except UIAError:
                    return None
        if not btns:
            return None
        btn = btns[-1]
        scroll_into_view(btn)
        seq_before = get_clipboard_sequence()
        # Copying the reply is a clipboard write like any other paste: measured
        # 2026-11-21, a Grok run left the operator's 3840-char clipboard replaced
        # by the 2-char reply. Read the payload inside the guard so the original
        # text is back by the time ask() returns. The click itself borrows the
        # foreground too: leaving it on the automation window after the reply is
        # the same red-line breach as stealing it before the submit.
        with self._borrowed_focus(win, allow=headed):
            with preserved_clipboard():
                try:
                    self._click_or_fallback(btn, win.hwnd, None, headed=headed)
                except UIAError:
                    return None
                for _ in range(10):
                    if get_clipboard_sequence() != seq_before:
                        txt = get_clipboard().strip()
                        return txt or None
                    self._sleep(0.1)
        return None

    def _find_model_selector(self, snap: Snapshot) -> UINode | None:
        sel = self.cfg.get("selectors", {}).get("model_selector") or {}
        if not sel:
            return None
        roles = sel.get("roles") or ["ComboBoxControl", "ButtonControl"]
        aids = sel.get("automation_ids") or None
        if sel.get("names"):
            hits = snap.find(roles=roles, names=sel["names"], automation_ids=aids, enabled_only=True)
            if hits:
                return hits[0]
        if sel.get("name_contains"):
            hits = snap.find(roles=roles, name_contains=sel["name_contains"], automation_ids=aids, enabled_only=True)
            if hits:
                return hits[0]
        if sel.get("names") or sel.get("name_contains"):
            # A name filter that matched nothing means "this page has no such
            # control" (DeepSeek's 深度思考 is a chip, not a dropdown). Returning
            # the first ButtonControl instead picked a nameless 16x17 glyph and
            # made ``models --live`` click it — the 2026-11-21 13:44 accidental
            # activation. Only an unfiltered selector may fall back.
            return None
        hits = snap.find(roles=roles, automation_ids=aids, enabled_only=True)
        return hits[0] if hits else None

    def _list_model_options(self, snap: Snapshot, *, exclude_node: UINode | None = None) -> list[UINode]:
        sel = self.cfg.get("selectors", {}).get("model_option") or {}
        roles = sel.get("roles") or ["ListItemControl", "MenuItemControl", "RadioButtonControl"]
        hits = snap.find(roles=roles, enabled_only=True)
        out: list[UINode] = []
        seen: set[tuple[str, str]] = set()
        for h in hits:
            if exclude_node is not None and h.name == exclude_node.name and h.role == exclude_node.role and h.rect == exclude_node.rect:
                continue
            label = (h.name or "").strip()
            if not label:
                continue
            key = (h.role, label)
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
        return out

    def _find_model_option(
        self,
        snap: Snapshot,
        model: str,
        *,
        exclude_node: UINode | None = None,
        free_only: bool = False,
    ) -> UINode | None:
        options = self._list_model_options(snap, exclude_node=exclude_node)
        if not options:
            return None

        aliases = [str(a).strip().lower() for a in ((self.cfg.get("model_aliases") or {}).get(model) or [model]) if str(a).strip()]

        # 1. Exact/substring alias match (respecting free_only cost guard)
        for opt in options:
            low = opt.name.strip().lower()
            if any(al in low for al in aliases):
                if free_only and classify_model_label(opt.name, policy=self.model_policy) == "paid":
                    continue
                return opt

        # 2. Dynamic free-tier discovery (e.g., WorkBuddy '0.00x' / '限时免费' when free model changes)
        if free_only or model.strip().lower() == "free":
            for opt in options:
                if classify_model_label(opt.name, policy=self.model_policy) == "free":
                    return opt

        return None

    # ------------------------------------------------------- focus / write guard
    def focus_guard_strict(self) -> bool:
        """Whether losing primary-screen focus must abort instead of just warn.

        ON by default since M4-4: a silent takeover (``--headed`` dragging the
        operator's 斗鱼 client off the primary screen) is worse than a failed
        send. Only an explicit ``false`` in the endpoint config or in
        ``config/bridge.json`` relaxes it back to warn-only.
        """
        if "focus_guard_strict" in self.cfg:
            return bool(self.cfg.get("focus_guard_strict"))
        return bool(
            self.bridge_cfg.get("focus_guard_strict", FOCUS_GUARD_STRICT_DEFAULT)
        )

    def _primary_foreground_hwnd(self) -> int:
        """Foreground handle when it belongs to the primary screen, else ``0``.

        Used to decide whether activating our secondary-screen window would
        steal focus from the operator. Probes go through :mod:`engine.window`
        so tests can patch them exactly like :class:`FocusGuard` does.
        """
        try:
            hwnd = int(window_mod.foreground_hwnd() or 0)
        except Exception:  # noqa: BLE001 - a probe must never break a send
            return 0
        if not hwnd:
            return 0
        try:
            rect = window_mod.window_rect(hwnd)
        except Exception:  # noqa: BLE001
            return 0
        return hwnd if window_mod.is_on_primary_screen(rect) else 0

    def _focus_taken_payload(
        self,
        clean_prompt: str,
        warnings,
        where: str,
        *,
        submitted: bool = False,
    ) -> dict[str, Any]:
        """Structured block for a strict-mode focus takeover.

        ``submitted=False`` means nothing left the composer, so the ledger stays
        ``failed_retryable``; ``submitted=True`` means the submit had already
        fired when the restore failed, so the ledger goes ``unknown`` instead —
        re-sending automatically could double-post.
        """
        if submitted:
            self.ledger.mark(
                clean_prompt, self.name, "unknown", note="focus_taken_after_submit"
            )
            message = (
                "primary-screen focus was not restored after %s; strict focus "
                "guard aborted the run (the submit had already been issued)" % where
            )
        else:
            self.ledger.mark(
                clean_prompt, self.name, "failed_retryable", note="focus_taken"
            )
            message = (
                "primary-screen focus was taken (%s); strict focus guard "
                "blocked the submit" % where
            )
        return {
            "ok": False,
            "endpoint": self.name,
            "error": "focus_taken",
            "message": message,
            "submitted": bool(submitted),
            "focus_event": self.last_focus_event,
            "warnings": list(warnings or []),
            "reply": "",
        }

    def focus_borrow_restore(self) -> bool:
        """Whether Chrome may borrow the foreground for one click (option A).

        ON by default: a Chrome accessibility tree only becomes clickable once
        its window owns the foreground, and refusing to activate at all is what
        blocked M4-4's live round. Every borrow is paired with
        :func:`engine.window.preserved_focus`, which puts the operator's
        primary-screen window back in ``finally`` — so the trade is "focus for
        one click" instead of "focus stolen until the operator notices".
        """
        if "focus_borrow_restore" in self.cfg:
            return bool(self.cfg.get("focus_borrow_restore"))
        return bool(
            self.bridge_cfg.get("focus_borrow_restore", FOCUS_BORROW_RESTORE_DEFAULT)
        )

    def _borrow_foreground(self, hwnd: int, warnings: list[str] | None = None) -> bool:
        """Wake *hwnd* for one action; True when Windows really gave it foreground.

        ``SetForegroundWindow`` is routinely denied for a background process
        (measured 2026-11-21: overlapping Gemini/Grok windows on the automation
        screen). A denied transfer used to be invisible, so the physical click
        landed on whichever window happened to be on top. Here a denial raises
        the target without activating it and leaves a diagnostic behind.
        """
        target = int(hwnd or 0)
        if target <= 0:
            return False
        try:
            focus_window(target)
        except Exception:  # noqa: BLE001 - a failed wake is not fatal
            pass
        self._sleep(0.12)
        try:
            focused = window_mod.foreground_hwnd() == target
        except Exception:  # noqa: BLE001
            focused = False
        if not focused:
            try:
                window_mod.raise_window(target)
            except Exception:  # noqa: BLE001
                pass
            if warnings is not None:
                warnings.append("focus_guard:borrow_unconfirmed:0x%x" % target)
            self._sleep(0.05)
        return focused

    @contextmanager
    def _borrowed_focus(
        self,
        win: "WindowInfo | None",
        *,
        warnings: list[str] | None = None,
        allow: bool = True,
    ):
        """Wake *win* for one action, then hand the primary screen back.

        Restoration happens on the way out (also on exception), so a click that
        raises still leaves the operator's window foreground. A restore that
        does not stick is remembered as ``focus_guard:restore_failed:0x..`` so
        the caller's ``focus_guard_strict`` check can turn it into
        ``error="focus_taken"``.
        """
        hwnd = int(getattr(win, "hwnd", 0) or 0)
        borrow = bool(allow) and hwnd > 0 and self.focus_borrow_restore()
        guard = FocusGuard(strict=False, restore_on_exit=True)
        try:
            with guard:
                if borrow:
                    self._borrow_foreground(hwnd, warnings)
                yield guard
        finally:
            if warnings is not None:
                warnings.extend(guard.warnings())
                if guard.restore_failed and not guard.taken:
                    warnings.append(
                        "focus_guard:restore_failed:0x%x" % int(guard.before or 0)
                    )
            if guard.taken:
                self.last_focus_event = guard.diagnostics[-1]
            elif guard.restore_failed:
                self.last_focus_event = (
                    "focus_guard:restore_failed:0x%x" % int(guard.before or 0)
                )

    def guarded_write(self, fn, *args, warnings=None, borrow_hwnd: int = 0, **kwargs):
        """Run one window/paste action under a :class:`FocusGuard`.

        The guard is always diagnostic: a takeover is appended to ``warnings``
        as ``focus_guard:primary_lost:0x..->0x..`` and remembered on the driver
        as ``self.last_focus_event``. Blocking is decided by the caller with
        :meth:`focus_guard_strict`, so a single focus blip cannot silently kill
        an otherwise valid send while a strict run still refuses to submit.

        ``borrow_hwnd`` (focus option A) activates that window for the duration
        of *fn* and restores the previous primary-screen foreground on exit; a
        restore that does not stick is recorded as ``focus_guard:restore_failed``.
        """
        guard = FocusGuard(strict=False, restore_on_exit=True)
        borrow = int(borrow_hwnd or 0) > 0 and self.focus_borrow_restore()
        with guard:
            if borrow:
                self._borrow_foreground(int(borrow_hwnd), warnings)
            result = fn(*args, **kwargs)
        if warnings is not None:
            warnings.extend(guard.warnings())
            if guard.restore_failed and not guard.taken:
                warnings.append(
                    "focus_guard:restore_failed:0x%x" % int(guard.before or 0)
                )
        if guard.taken:
            self.last_focus_event = guard.diagnostics[-1]
        elif guard.restore_failed:
            self.last_focus_event = (
                "focus_guard:restore_failed:0x%x" % int(guard.before or 0)
            )
        return result

    def blocking_popup_patterns(self) -> tuple[str, ...]:
        """Default paywall/permission wording plus this endpoint's own entries.

        ``config/endpoints.json`` may add ``blocking_popup_patterns`` per
        endpoint; entries are appended (never replace the defaults) so an
        endpoint can only make the detector stricter.
        """
        merged: list[str] = list(DEFAULT_BLOCKING_POPUP_PATTERNS)
        for extra in self.cfg.get("blocking_popup_patterns") or ():
            text = str(extra).strip().lower()
            if text and text not in merged:
                merged.append(text)
        return tuple(merged)

    def detect_blocking_popup(self, snap: "Snapshot | None") -> str:
        """Return the matched blocking pattern, or ``""`` when the UI looks sane."""
        blob = snapshot_text(snap)
        if not blob:
            return ""
        for pattern in self.blocking_popup_patterns():
            if pattern in blob:
                return pattern
        return ""

    def _blocking_popup_payload(
        self,
        matched: str,
        snap: "Snapshot | None",
        clean_prompt: str,
        started: float,
        warnings: list[str],
    ) -> dict[str, Any]:
        """Structured fail-fast result for a popup no automation can dismiss.

        The ledger goes ``failed_retryable`` on purpose: nothing was submitted,
        so once a human clears the dialog the same prompt must be sendable again
        without ``--force`` or a ledger wipe.
        """
        self.ledger.mark(
            clean_prompt, self.name, "failed_retryable", note="blocking_popup"
        )
        ev = self._record_failure_evidence(
            "blocking_popup", snap, {"matched": matched}
        )
        return {
            "ok": False,
            "endpoint": self.name,
            "error": "blocking_popup",
            "matched": matched,
            "message": (
                "Blocking popup detected on %s (matched=%r); a human has to clear "
                "it, so the wait was aborted instead of burning the timeout"
                % (self.name, matched)
            ),
            "evidence": ev,
            "warnings": list(warnings or []),
            "reply": "",
            "elapsed": round(time.monotonic() - started, 2),
        }

    def switch_model(
        self,
        win: WindowInfo,
        snap: Snapshot,
        model: str,
        *,
        headed: bool,
        free_only: bool = False,
    ) -> dict[str, Any]:
        """Switch model safely with optional Fail-Closed free-tier cost guard."""
        req_low = model.strip().lower()
        enforce_free = bool(self.cfg.get("enforce_free_switch", False)) and (free_only or req_low == "free")

        if req_low == "default" and self.cfg.get("skip_default_model_switch", False) and not enforce_free:
            return {"attempted": False, "switched": False, "reason": "default_skipped"}

        sel_cfg = self.cfg.get("selectors", {}).get("model_selector") or {}
        if not sel_cfg:
            return {"attempted": False, "switched": False, "reason": "not_supported"}

        selector_node = self._find_model_selector(snap)
        if selector_node is None:
            return {
                "attempted": True,
                "switched": False,
                "reason": "selector_not_found",
                "blocked_paid": enforce_free,
            }

        try:
            with self._borrowed_focus(win, allow=headed):
                self._click_or_fallback(
                    selector_node,
                    win.hwnd,
                    self.cfg.get("templates", {}).get("model_selector"),
                    headed=headed,
                )
                self._sleep(0.4)
                snap2 = self.take_snap(win.hwnd)
                all_options = self._list_model_options(snap2, exclude_node=selector_node)
                option_node = self._find_model_option(snap2, model, exclude_node=selector_node, free_only=free_only)
                if option_node is None:
                    self._send_esc()
                    self._sleep(0.25)
                    return {
                        "attempted": True,
                        "switched": False,
                        "reason": "free_option_not_found" if (free_only or req_low == "free") else "option_not_found",
                        "blocked_paid": enforce_free or (free_only and classify_model_label(selector_node.name, policy=self.model_policy) == "paid"),
                        "available_options": [
                            {"name": o.name, "tier": classify_model_label(o.name, policy=self.model_policy)}
                            for o in all_options
                        ],
                    }

                tier = classify_model_label(option_node.name, policy=self.model_policy)
                if free_only and tier == "paid":
                    self._send_esc()
                    self._sleep(0.25)
                    return {
                        "attempted": True,
                        "switched": False,
                        "reason": "paid_tier_blocked",
                        "target": option_node.name,
                        "blocked_paid": True,
                        "available_options": [
                            {"name": o.name, "tier": classify_model_label(o.name, policy=self.model_policy)}
                            for o in all_options
                        ],
                    }

                self._click_or_fallback(option_node, win.hwnd, None, headed=headed)
                self._sleep(0.4)
        except UIAError as exc:
            if exc.code == "POLICY_DENIED":
                return {
                    "attempted": True,
                    "switched": False,
                    "reason": "headless_click_denied",
                    "hint": "Pass --headed to click UI-only model selector",
                    "blocked_paid": enforce_free,
                }
            raise

        return {
            "attempted": True,
            "switched": True,
            "target": option_node.name,
            "tier": tier,
            "mode": "dropdown",
        }

    def _is_generating(self, snap: Snapshot, *, min_rel_x: float = 0.0, max_rel_x: float = 1.0) -> bool:
        sel = self.cfg.get("selectors", {}).get("stop_button", {})
        stop_names = sel.get("names") or ["停止", "停止生成", "Stop", "Stop generating"]
        if snap.find(roles=["ButtonControl"], names=stop_names, enabled_only=True):
            return True
        busy_words = list(DEFAULT_BUSY_TEXTS) + list(self.cfg.get("busy_text_patterns") or [])
        for line in snap.text_lines(min_rel_x=min_rel_x, max_rel_x=max_rel_x)[-6:]:
            low = line.strip().lower()
            if not low or len(low) > 60:
                continue
            if any(bw.lower() in low for bw in busy_words):
                return True
        return False

    def _find_attach_control(self, snap: Snapshot, key: str) -> UINode | None:
        sel = self.cfg.get("selectors", {}).get(key) or {}
        if not sel:
            return None
        hits = snap.find(
            roles=sel.get("roles") or ["ButtonControl", "MenuItemControl", "GroupControl"],
            names=sel.get("names") or None,
            name_contains=sel.get("name_contains") or None,
            automation_ids=sel.get("automation_ids") or None,
            ratio_box=sel.get("anchor_box_ratio"),
            enabled_only=True,
        )
        return hits[-1] if hits else None

    def _wait_attach_menu(self, win: WindowInfo) -> tuple[UINode | None, Snapshot]:
        """Poll for the attach menu item instead of trusting a fixed sleep.

        Measured 2026-11-21 on Gemini: the 上传和工具 menu takes ~0.4s to reach
        the accessibility tree, so the old ``sleep(0.35)`` + single snapshot
        reported "No attach_menu_item found" for a menu that was about to open.
        """
        budget = float(self.cfg.get("attach_menu_timeout", 3.0))
        poll = float(self.bridge_cfg.get("attach_menu_poll", 0.2)) or 0.2
        deadline = time.monotonic() + max(0.0, budget)
        snap = self.take_snap(win.hwnd)
        while True:
            item = self._find_attach_control(snap, "attach_menu_item")
            if item is not None or time.monotonic() >= deadline:
                return item, snap
            self._sleep(poll)
            snap = self.take_snap(win.hwnd)

    def _attach_files(self, win: WindowInfo, snap: Snapshot, paths: list[str], *, headed: bool) -> list[str]:
        methods: list[str] = []
        menu_cfg = self.cfg.get("selectors", {}).get("attach_menu_item") or {}
        ratio_fallback = self.cfg.get("fallback_attach_ratio")
        wait_after = float(self.cfg.get("attach_wait_seconds", 1.0))
        for fp in paths:
            btn = self._find_attach_control(snap, "attach_button")
            with self._borrowed_focus(win, allow=headed):
                if btn is not None:
                    self._click_or_fallback(btn, win.hwnd, self.cfg.get("templates", {}).get("attach_button"), headed=headed)
                elif ratio_fallback and headed and snap.window_rect is not None:
                    click_ratio(snap.window_rect, tuple(ratio_fallback))
                else:
                    raise UIAError("TARGET_NOT_FOUND", f"No attach_button found for {self.name}")
                self._sleep(0.35)
                if menu_cfg:
                    item, snap_m = self._wait_attach_menu(win)
                    if item is None:
                        self._send_esc()
                        raise UIAError("TARGET_NOT_FOUND", f"No attach_menu_item found for {self.name}")
                    self._click_or_fallback(item, win.hwnd, None, headed=headed)
                    self._sleep(0.35)
            m = self._dialog_fn(fp) if self._dialog_fn is not None else upload_via_file_dialog(fp)
            methods.append(m)
            self._sleep(wait_after)
            snap = self.take_snap(win.hwnd)
        return methods

    def _ensure_composer_ready(
        self,
        win: WindowInfo,
        snap: Snapshot,
        *,
        warnings: list[str],
        attempts: int = 3,
        delay: float = 0.25,
    ) -> tuple[Snapshot, dict[str, Any] | None]:
        """Guarantee composer + attach_button are visible before ``--attach``.

        A chat that is still loading renders its composer a moment late, and the
        old ``--attach`` path clicked the attach button against a snapshot with
        no composer at all — the state that produced the empty ``#32770`` and the
        "message box never appears" dead end. Scroll to the bottom and
        re-snapshot a few times; if it is still missing, fail with a structured
        payload instead of driving the native dialog blind.
        """
        selectors = self.cfg.get("selectors") or {}
        needs_attach = bool(selectors.get("attach_button"))
        rounds = max(1, int(attempts))
        missing = ""
        for index in range(rounds):
            composer = self._find_composer(snap)
            attach_btn = (
                self._find_attach_control(snap, "attach_button")
                if needs_attach
                else None
            )
            if composer is not None and (not needs_attach or attach_btn is not None):
                if index:
                    warnings.append("composer_ready_after_retry:%d" % index)
                return snap, None
            missing = "composer" if composer is None else "attach_button"
            if index + 1 >= rounds:
                break
            self._scroll_bottom(snap)
            self._sleep(delay)
            snap = self.take_snap(win.hwnd)

        evidence = self._record_failure_evidence(
            "composer_not_ready", snap, {"missing": missing, "attempts": rounds}
        )
        return snap, {
            "ok": False,
            "endpoint": self.name,
            "error": "composer_not_ready",
            "message": (
                "%s never rendered %s after %d scroll/re-snapshot attempt(s); "
                "refusing to drive the file dialog blind" % (self.name, missing, rounds)
            ),
            "missing": missing,
            "attempts": rounds,
            "evidence": evidence,
            "reply": "",
        }

    def attach_files(
        self,
        win: WindowInfo,
        snap: Snapshot,
        paths: list[str],
        *,
        headed: bool,
        warnings: list[str] | None = None,
    ) -> list[str]:
        """Public attachment hook grouped around one native file dialog.

        Endpoints without an ``attach_button`` selector fail fast with a clear
        NotImplementedError. Stubbed dialog_fn keeps the legacy per-file path
        for offline tests; production uses the headless #32770 primitive once
        for the whole quoted multi-file argument.
        """
        if not paths:
            return []
        selectors = self.cfg.get("selectors") or {}
        if not selectors.get("attach_button"):
            raise NotImplementedError(
                "endpoint %s has no attach_button selector; --attach is not supported"
                % self.name
            )
        if self._dialog_fn is not None:
            return self._attach_files(win, snap, paths, headed=headed)

        btn = self._find_attach_control(snap, "attach_button")
        if btn is None:
            raise UIAError("TARGET_NOT_FOUND", f"No attach_button found for {self.name}")
        # Which #32770 already exist: the picker we raise has to be a *new* one.
        known_dialogs = {hwnd for hwnd, _pid in list_file_dialogs()}
        # A UIA Invoke on a Chrome button can still raise the window (measured
        # 2026-11-21 on DeepSeek), so the click itself is guarded: a takeover is
        # recorded and a strict run refuses to submit afterwards. Focus option A
        # borrows the foreground for the two clicks and hands it straight back.
        with self._borrowed_focus(win, warnings=warnings, allow=headed):
            self.guarded_write(
                self._click_or_fallback,
                btn,
                win.hwnd,
                self.cfg.get("templates", {}).get("attach_button"),
                headed=headed,
                warnings=warnings,
            )
            self._sleep(0.35)

            menu_cfg = selectors.get("attach_menu_item") or {}
            dialog_already_open = False
            if menu_cfg:
                try:
                    dialog_already_open = bool(
                        find_existing_file_dialog(int(getattr(win, "pid", 0) or 0))
                    )
                except UIAError:
                    dialog_already_open = False
            if menu_cfg and not dialog_already_open:
                item, snap_m = self._wait_attach_menu(win)
                if item is None:
                    self._send_esc()
                    raise UIAError("TARGET_NOT_FOUND", f"No attach_menu_item found for {self.name}")
                self.guarded_write(
                    self._click_or_fallback,
                    item,
                    win.hwnd,
                    None,
                    headed=headed,
                    warnings=warnings,
                )
                self._sleep(0.35)

        method = self._fill_raised_file_dialog(win, paths, known_dialogs, warnings)
        self._sleep(float(self.cfg.get("attach_wait_seconds", 1.0)))

        ready_patterns = self.cfg.get("attach_ready_patterns") or []
        if ready_patterns:
            deadline = time.monotonic() + float(self.cfg.get("attach_ready_timeout", 10.0))
            poll = float(self.bridge_cfg.get("poll_interval", 0.25))
            while time.monotonic() < deadline:
                if self.take_snap(win.hwnd).find(name_contains=ready_patterns):
                    break
                self._sleep(poll)
            else:
                raise UIAError(
                    "ATTACH_NOT_READY",
                    "Attachment chip did not appear on %s after upload" % self.name,
                )
        return [method]

    def _new_file_dialog(
        self,
        win: WindowInfo,
        known: set[int],
        warnings: list[str] | None = None,
        *,
        timeout: float | None = None,
    ) -> int:
        """First visible #32770 that did not exist before the click, or ``0``.

        Ownership is relaxed to "same executable": measured 2026-11-21, Gemini's
        picker is a ``#32770`` owned by a *different* ``chrome.exe`` pid than the
        window, so a pid-only lookup spun out its whole budget and reported
        ``DIALOG_NOT_FOUND`` for a dialog that was plainly on screen. Dialogs of
        other applications are still refused.
        """
        owner_pid = int(getattr(win, "pid", 0) or 0)
        budget = float(
            timeout if timeout is not None
            else self.cfg.get("attach_dialog_scan_timeout", 2.0)
        )
        poll = float(self.bridge_cfg.get("attach_dialog_poll", 0.2)) or 0.2
        wanted = {
            str(p).strip().lower()
            for p in (self.cfg.get("process_names") or [])
            if str(p).strip()
        }
        deadline = time.monotonic() + max(0.0, budget)
        while True:
            for hwnd, pid in list_file_dialogs():
                if hwnd in known:
                    continue
                if pid != owner_pid:
                    image = window_mod.process_image_name(pid).strip().lower()
                    if wanted and image not in wanted:
                        continue
                    if warnings is not None:
                        warnings.append("attach_dialog_foreign_pid:%d" % pid)
                return hwnd
            if time.monotonic() >= deadline:
                return 0
            self._sleep(poll)

    def _fill_raised_file_dialog(
        self,
        win: WindowInfo,
        paths: list[str],
        known: set[int],
        warnings: list[str] | None = None,
    ) -> str:
        """Drive the #32770 the attach click just raised and fill it.

        The pid-matched lookup stays first because it is the common case and the
        long-standing seam; only when it times out do we accept a dialog raised
        by a sibling process of the same executable.
        """
        owner_pid = int(getattr(win, "pid", 0) or 0)
        timeout = float(self.cfg.get("attach_dialog_timeout", 10.0))
        try:
            return fill_windows_file_dialog(
                paths, owner_pid=owner_pid, timeout=timeout
            )
        except UIAError as exc:
            if exc.code != "DIALOG_NOT_FOUND":
                raise
        dialog_hwnd = self._new_file_dialog(win, known, warnings)
        if dialog_hwnd:
            return fill_file_dialog(
                dialog_hwnd, paths, timeout=timeout, ctypes_module=ctypes
            )
        raise UIAError(
            "DIALOG_NOT_FOUND",
            "#32770 dialog for pid %s did not appear within %.1fs"
            % (owner_pid, timeout),
        )

    def _find_new_chat_button(self, snap: Snapshot) -> UINode | None:
        sel = self.cfg.get("selectors", {}).get("new_chat", {})
        names = sel.get("names")
        if not names:
            return None
        hits = snap.find(roles=["ButtonControl", "HyperlinkControl", "TabItemControl"], names=names, enabled_only=True)
        return hits[0] if hits else None

    def _filter_noise_lines(self, lines: list[str], *, prompt_squash: str = "") -> list[str]:
        placeholders = {p.strip().lower() for p in (self.cfg.get("placeholders") or [])}
        noise_exact = {n.strip().lower() for n in (self.cfg.get("noise_lines") or [])}
        noise_sub = [s.lower() for s in list(DEFAULT_NOISE_CONTAINS) + list(self.cfg.get("noise_contains") or [])]
        regexes = [re.compile(pat) for pat in list(DEFAULT_NOISE_REGEXES) + list(self.cfg.get("noise_regexes") or [])]

        cleaned: list[str] = []
        for line in lines:
            s = line.strip()
            low = s.lower()
            if not s or low in placeholders or low in noise_exact:
                continue
            if prompt_squash and "".join(s.split()) == prompt_squash:
                continue
            if any(rx.match(s) for rx in regexes):
                continue
            if any(sub in low for sub in noise_sub):
                continue
            cleaned.append(s)
        return cleaned

    def _extract_reply_lines(self, lines: list[str], prompt: str = "") -> list[str]:
        """Anchor after the current prompt echo safely (never wiping reply if composer sits at bottom)."""
        prompt_squash = "".join(prompt.split())
        anchor_key = prompt_squash[:32] if len(prompt_squash) >= 8 else prompt_squash

        if anchor_key:
            match_indices = [
                idx
                for idx, line in enumerate(lines)
                if (ls := "".join(line.split())) and (ls == prompt_squash or (len(anchor_key) >= 12 and anchor_key in ls))
            ]
            # Try from the last prompt occurrence backward; only accept a slice that leaves real reply lines.
            for idx in reversed(match_indices):
                candidate = self._filter_noise_lines(lines[idx + 1 :], prompt_squash=prompt_squash)
                if candidate:
                    return candidate

        return self._filter_noise_lines(lines, prompt_squash=prompt_squash)

    def _prompt_receipt_needles(self, prompt: str) -> list[str]:
        squash = "".join(prompt.split())
        if not squash:
            return []
        needles = [squash[:24] if len(squash) >= 8 else squash]
        for sep in ("：", ":"):
            if sep in prompt:
                tail = "".join(prompt.rsplit(sep, 1)[-1].split())
                if len(tail) >= 6 and tail not in needles:
                    needles.append(tail[:24])
        if len(squash) >= 12 and squash[-16:] not in needles:
            needles.append(squash[-16:])
        return needles

    def _has_send_receipt(
        self,
        before_snap: Snapshot,
        cur_snap: Snapshot,
        prompt: str,
        *,
        min_rel_x: float = 0.0,
        max_rel_x: float = 1.0,
    ) -> bool:
        """Check whether the prompt landed (head or suffix token) or a new copy button appeared."""
        if len(self._copy_buttons(cur_snap)) > len(self._copy_buttons(before_snap)):
            return True
        needles = self._prompt_receipt_needles(prompt)
        if not needles:
            return False
        delta = snapshot_delta(before_snap, cur_snap, min_rel_x=min_rel_x, max_rel_x=max_rel_x)
        search_pool = delta["added_lines"] or cur_snap.text_lines(min_rel_x=min_rel_x, max_rel_x=max_rel_x)
        for line in search_pool:
            ls = "".join(line.split())
            if any(n in ls for n in needles):
                return True
        return False

    def prepare_prompt(self, prompt: str) -> str:
        if self.cfg.get("single_line_paste"):
            return " ".join(line.strip() for line in prompt.splitlines() if line.strip())
        return prompt

    def post_read_fallback(self, win: WindowInfo, before_meta: dict[str, Any]) -> str | None:
        return None

    def pre_send_meta(self, win: WindowInfo) -> dict[str, Any]:
        return {}

    def _click_or_fallback(self, node: UINode | None, hwnd: int, tpl_name: str | None, *, headed: bool) -> str:
        if node is not None:
            if self._invoke_fn is not None:
                return self._invoke_fn(node, headed)
            try:
                return invoke_node(node, headed=False)
            except UIAError:
                if headed:
                    return invoke_node(node, headed=True)
                raise
        if tpl_name and headed:
            cap = capture_window(hwnd)
            if cap is not None:
                img, rect = cap
                m = match_template(img, tpl_name, origin=(rect[0], rect[1]))
                if m is not None:
                    from engine.uia import _physical_click

                    _physical_click(*m.center)
                    return f"vision:template:{tpl_name}"
        raise UIAError("TARGET_NOT_FOUND", f"Could not locate control (template={tpl_name!r})")

    # -------------------------------------------------------------- public API
    def status(self) -> dict[str, Any]:
        win = self.locate_window(auto_open=False)
        ready = False
        degraded = False
        degraded_reason = ""
        if win is not None:
            try:
                snap = self.take_snap(win.hwnd)
                degraded = snap.degraded
                degraded_reason = snap.degraded_reason
                ready = self._find_composer(snap) is not None or bool(self.cfg.get("fallback_composer_ratio"))
            except UIAError:
                ready = False
        return {
            "ok": win is not None,
            "version": __version__,
            "endpoint": self.name,
            "kind": self.cfg.get("kind", "unknown"),
            "window_found": win is not None,
            "ready": ready,
            "snapshot_degraded": degraded,
            "degraded_reason": degraded_reason,
            "hwnd": win.hwnd if win else None,
            "title": win.title if win else None,
            "process": win.process_name if win else None,
            "models": list(self.cfg.get("models") or []),
        }

    def models(self, *, live: bool = False, headed: bool = True) -> dict[str, Any]:
        ep_policy = (self.model_policy.get("endpoints") or {}).get(self.name) or {}
        base_out: dict[str, Any] = {
            "ok": True,
            "endpoint": self.name,
            "models": list(self.cfg.get("models") or []),
            "aliases": dict(self.cfg.get("model_aliases") or {}),
            "policy_profiles": {
                k: ep_policy.get(k) for k in ("test", "routine", "council") if k in ep_policy
            },
            "verified_models": list(ep_policy.get("verified_models") or []),
            "note": self.cfg.get("models_note", ""),
        }
        if not live:
            return base_out

        win = self.locate_window(auto_open=False)
        if win is None:
            return {**base_out, "ok": False, "error": "window_not_found"}
        try:
            # Probing a dropdown needs the same foreground the click needs;
            # focus option A borrows it and hands the primary screen back.
            with self._borrowed_focus(win, allow=headed):
                snap = self.take_snap(win.hwnd)
                sel_node = self._find_model_selector(snap)
                if sel_node is None:
                    return {**base_out, "live_options": [], "live_note": "selector_not_found"}
                self._click_or_fallback(sel_node, win.hwnd, self.cfg.get("templates", {}).get("model_selector"), headed=headed)
                self._sleep(0.4)
                snap2 = self.take_snap(win.hwnd)
                opts = self._list_model_options(snap2, exclude_node=sel_node)
                self._send_esc()
                self._sleep(0.2)
            base_out["current_selector"] = sel_node.name
            base_out["live_options"] = [
                {
                    "role": o.role,
                    "name": o.name,
                    "tier": classify_model_label(o.name, policy=self.model_policy),
                }
                for o in opts
            ]
            return base_out
        except UIAError as exc:
            self._send_esc()
            return {**base_out, "ok": False, "error": exc.code, "message": exc.message}

    def snapshot(self, *, interactive_only: bool = True, limit: int = 300) -> dict[str, Any]:
        win = self.locate_window(auto_open=False)
        if win is None:
            return {"ok": False, "endpoint": self.name, "error": "window_not_found"}
        try:
            snap = self.take_snap(win.hwnd)
        except UIAError as exc:
            return {"ok": False, "endpoint": self.name, "error": exc.code, "message": exc.message}
        return {"ok": True, "endpoint": self.name, **snap.to_dict(interactive_only=interactive_only, limit=limit)}

    def read(self) -> dict[str, Any]:
        win = self.locate_window(auto_open=False)
        if win is None:
            return {"ok": False, "endpoint": self.name, "error": "window_not_found", "reply": ""}
        meta = dict(self.pre_send_meta(win))
        meta["after_seq"] = 0
        fallback = self.post_read_fallback(win, meta)
        if fallback:
            return {"ok": True, "endpoint": self.name, "source": "fallback", "reply": fallback}
        try:
            snap = self.take_snap(win.hwnd)
        except UIAError as exc:
            return {"ok": False, "endpoint": self.name, "error": exc.code, "message": exc.message, "reply": ""}
        min_rel_x = float(self.cfg.get("content_min_rel_x", 0.0))
        max_rel_x = float(self.cfg.get("content_max_rel_x", 1.0))
        lines = self._extract_reply_lines(snap.text_lines(min_rel_x=min_rel_x, max_rel_x=max_rel_x))
        reply = "\n".join(lines).strip()
        return {
            "ok": bool(reply),
            "endpoint": self.name,
            "source": "uia",
            "snapshot_degraded": snap.degraded,
            "reply": reply,
            "error": None if reply else "no_reply",
        }

    def ask(
        self,
        prompt: str,
        *,
        timeout: float | None = None,
        new_chat: bool = False,
        headed: bool | None = None,
        model: str | None = None,
        profile: str | None = None,
        free_only: bool = False,
        confirm: bool = False,
        force: bool = False,
        attach: list[str] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        budget = float(timeout or self.cfg.get("timeout") or self.bridge_cfg.get("default_timeout", 180.0))
        poll = float(self.bridge_cfg.get("poll_interval", 0.5))
        stable_need = float(self.cfg.get("stable_seconds", self.bridge_cfg.get("stable_seconds", 2.5)))
        use_headed = (not self.bridge_cfg.get("headless_default", True)) if headed is None else bool(headed)
        prepared_error, prepared = self._prepare_ask_inputs(
            prompt,
            profile=profile,
            model=model,
            free_only=free_only,
            attach=attach,
            force=force,
        )
        if prepared_error is not None:
            return prepared_error
        clean_prompt = prepared["clean_prompt"]
        effective_model = prepared["effective_model"]
        effective_free_only = prepared["effective_free_only"]
        attach_paths = prepared["attach_paths"]

        model_switch_info: dict[str, Any] | None = None
        attach_methods: list[str] = []
        warnings: list[str] = []
        lock_timeout = float(self.bridge_cfg.get("lock_timeout", 120.0))
        with hold_ui_lock(f"ask:{self.name}", timeout=lock_timeout, lock_path=self._lock_path):
            win = self.locate_window(auto_open=True)
            if win is None:
                return {
                    "ok": False,
                    "endpoint": self.name,
                    "error": "window_not_found",
                    "message": f"Target window for {self.name} is not open",
                    "reply": "",
                }

            if use_headed:
                # Never steal the operator's primary-screen foreground. When a
                # window on x >= 0 owns focus (斗鱼 client, editor, terminal…)
                # the interaction has to go through non-activating UIA instead
                # of calling SetForegroundWindow on our secondary-screen window.
                primary_fg = self._primary_foreground_hwnd()
                if primary_fg:
                    warnings.append(
                        "focus_guard:skipped_activation:0x%x" % primary_fg
                    )
                else:
                    # Focus activation is itself a key action: snapshot it so a
                    # strict run can refuse to submit after stealing the primary
                    # screen, and a relaxed run records it in ``warnings``.
                    self.guarded_write(focus_window, win.hwnd, warnings=warnings)
                    self._sleep(0.2)
                    if self.focus_guard_strict() and self.last_focus_event:
                        return self._focus_taken_payload(
                            clean_prompt, warnings, "headed focus"
                        )

            before_meta = self.pre_send_meta(win)
            submitted_error, submitted = self._submit_prompt_to_window(
                win,
                None,
                clean_prompt,
                use_headed=use_headed,
                effective_model=effective_model,
                confirm=confirm,
                warnings=warnings,
                profile=profile,
                started=started,
                new_chat=new_chat,
                effective_free_only=effective_free_only,
                attach_paths=attach_paths,
                attach_methods=attach_methods,
                model_switch_info=model_switch_info,
            )
            if submitted_error is not None:
                return submitted_error
            before_snap = submitted["before_snap"]
            write_method = submitted["write_method"]
            submit_method = submitted["submit_method"]
            use_headed = submitted["use_headed"]
            model_switch_info = submitted["model_switch_info"]
            attach_methods = submitted["attach_methods"]

        return self._await_reply_convergence(
            win,
            before_snap,
            before_meta,
            clean_prompt,
            started=started,
            budget=budget,
            poll=poll,
            stable_need=stable_need,
            use_headed=use_headed,
            write_method=write_method,
            submit_method=submit_method,
            effective_model=effective_model,
            profile=profile,
            model_switch_info=model_switch_info,
            attach_paths=attach_paths,
            attach_methods=attach_methods,
            warnings=warnings,
        )

    # -- ask() phases (M4-10) -------------------------------------------
    def _prepare_ask_inputs(
        self,
        prompt: str,
        *,
        profile: str | None,
        model: str | None,
        free_only: bool,
        attach: list[str] | None,
        force: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Stage 1 — resolve ``--profile``/attach and gate the ledger.

        Returns ``(error_payload, inputs)``: a non-None payload is a *final*
        ``ask()`` result (invalid profile, missing attachment, empty prompt,
        duplicate submission) and the caller must return it verbatim. Inputs
        hold ``clean_prompt`` / ``effective_model`` / ``effective_free_only`` /
        ``attach_paths``. The unsupported-attach case still raises, exactly as
        before: it is a programming error, not an agent-level failure.
        """
        # Resolve --profile into effective model + free_only guard
        effective_model = model
        effective_free_only = bool(free_only)
        if profile:
            try:
                prof_model, prof_free = resolve_profile(self.name, profile, policy=self.model_policy)
            except ValueError as exc:
                return {"ok": False, "endpoint": self.name, "error": "invalid_profile", "message": str(exc), "reply": ""}, {}
            if not effective_model:
                effective_model = prof_model
            if prof_free:
                effective_free_only = True

        raw_attach = [attach] if isinstance(attach, str) else list(attach or [])
        attach_paths: list[str] = []
        for entry in raw_attach:
            for part in str(entry).split(","):
                if part.strip():
                    attach_paths.append(str(Path(part.strip())))
        for ap in attach_paths:
            if not Path(ap).is_file():
                return {"ok": False, "endpoint": self.name, "error": "missing_resource", "message": f"Attachment not found: {ap}", "reply": ""}, {}
        if attach_paths and not ((self.cfg.get("selectors") or {}).get("attach_button")):
            raise NotImplementedError(
                "endpoint %s has no attach_button selector; --attach is not supported"
                % self.name
            )

        clean_prompt = self.prepare_prompt(prompt)
        if not clean_prompt.strip():
            return {"ok": False, "endpoint": self.name, "error": "empty_prompt", "reply": ""}, {}

        if not force:
            allowed, prev_state = self.ledger.check(clean_prompt, self.name)
            if not allowed:
                return {
                    "ok": False,
                    "endpoint": self.name,
                    "error": "duplicate",
                    "previous_state": prev_state,
                    "ledger_key": self.ledger.key(clean_prompt, self.name),
                    "message": f"Duplicate prompt blocked (state={prev_state}); pass --force or clear ledger to resend",
                    "reply": "",
                }, {}

        return None, {
            "clean_prompt": clean_prompt,
            "effective_model": effective_model,
            "effective_free_only": effective_free_only,
            "attach_paths": attach_paths,
        }

    def _submit_prompt_to_window(
        self,
        win: "WindowInfo",
        before_snap: "Snapshot | None",
        clean_prompt: str,
        *,
        use_headed: bool,
        effective_model: str | None,
        confirm: bool,
        warnings: list[str],
        profile: str | None,
        started: float,
        new_chat: bool = False,
        effective_free_only: bool = False,
        attach_paths: list[str] | None = None,
        attach_methods: list[str] | None = None,
        model_switch_info: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Stage 2 — write the prompt into the window and press send.

        Returns ``(error_payload, state)``. ``state`` carries everything the
        wait phase needs (``before_snap`` is the *post*-switch/attach snapshot,
        ``use_headed`` may have been promoted by the paste fallback), and is
        empty on error because the caller returns immediately.

        Besides the parameters named above, the body needs ``profile`` /
        ``started`` (payloads) and the attach/model-switch results, so they are
        passed in rather than recomputed.
        """
        attach_paths = list(attach_paths or [])
        attach_methods = list(attach_methods or [])
        prefer_paste = bool(self.cfg.get("prefer_paste", False))
        try:
            if before_snap is None:
                before_snap = self.take_snap(win.hwnd)
            if before_snap.degraded:
                warnings.append(f"snapshot_degraded:{before_snap.degraded_reason}")

            if new_chat:
                nc = self._find_new_chat_button(before_snap)
                if nc is not None:
                    with self._borrowed_focus(win, warnings=warnings, allow=use_headed):
                        self._click_or_fallback(nc, win.hwnd, self.cfg.get("templates", {}).get("new_chat"), headed=use_headed)
                    self._sleep(0.8)
                    before_snap = self.take_snap(win.hwnd)

            if effective_model or (effective_free_only and self.cfg.get("enforce_free_switch", False)):
                target_model = effective_model or "free"
                model_switch_info = self.switch_model(
                    win,
                    before_snap,
                    target_model,
                    headed=use_headed,
                    free_only=effective_free_only,
                )
                if model_switch_info.get("blocked_paid"):
                    return {
                        "ok": False,
                        "endpoint": self.name,
                        "error": "tier_unconfirmed",
                        "message": f"Free-only guard blocked sending on {self.name} (reason={model_switch_info.get('reason')})",
                        "profile": profile,
                        "model_requested": target_model,
                        "model_switch": model_switch_info,
                        "reply": "",
                    }, {}
                if model_switch_info.get("switched"):
                    before_snap = self.take_snap(win.hwnd)
                elif model_switch_info.get("attempted"):
                    warnings.append(f"model_switch_failed:{model_switch_info.get('reason')}")

            # A paywall or permission dialog can appear right after a model
            # switch; abort before writing anything rather than paste into a
            # window that will never answer.
            blocking = self.detect_blocking_popup(before_snap)
            if blocking:
                return self._blocking_popup_payload(
                    blocking, before_snap, clean_prompt, started, warnings
                ), {}

            if attach_paths:
                before_snap, not_ready = self._ensure_composer_ready(
                    win, before_snap, warnings=warnings
                )
                if not_ready is not None:
                    self.ledger.mark(
                        clean_prompt,
                        self.name,
                        "failed_retryable",
                        note="composer_not_ready",
                    )
                    return not_ready, {}
                attach_methods = self.attach_files(
                    win, before_snap, attach_paths,
                    headed=use_headed, warnings=warnings,
                )
                if self.focus_guard_strict() and self.last_focus_event:
                    # The upload may be complete, but nothing has been
                    # submitted yet: abort here rather than type into a
                    # window we stole from the operator.
                    return self._focus_taken_payload(
                        clean_prompt, warnings, "attach click"
                    ), {}
                before_snap = self.take_snap(win.hwnd)

            composer = self._find_composer(before_snap)
            fallback_ratio = self.cfg.get("fallback_composer_ratio")

            if composer is None and not fallback_ratio:
                ev = self._record_failure_evidence("composer_not_found", before_snap)
                self.ledger.mark(clean_prompt, self.name, "failed_retryable", note="composer_not_found")
                return {
                    "ok": False,
                    "endpoint": self.name,
                    "error": "composer_not_found",
                    "message": f"No input composer found in {self.name}; page may be on login/loading screen",
                    "evidence": ev,
                    "reply": "",
                }, {}

            chunk_size = int(self.cfg.get("paste_chunk", 0))
            if composer is None and fallback_ratio:
                if confirm and not self._confirm_fn(self.name, clean_prompt, effective_model):
                    self.ledger.mark(clean_prompt, self.name, "failed_retryable", note="cancelled_by_user")
                    return {"ok": False, "endpoint": self.name, "error": "cancelled_by_user", "reply": ""}, {}
                self.ledger.mark(clean_prompt, self.name, "submitting")
                wrect = before_snap.window_rect
                if wrect is None:
                    raise UIAError("NO_GEOMETRY", "Window geometry unavailable for ratio fallback")
                # Same rule as the pre-interaction probe: never pull the
                # operator's primary-screen window off its screen for a
                # ratio click, and let the guard decide on strictness.
                if not self._primary_foreground_hwnd():
                    self.guarded_write(focus_window, win.hwnd, warnings=warnings)
                self.guarded_write(
                    click_ratio_and_paste,
                    wrect, tuple(fallback_ratio), clean_prompt,
                    chunk_size=chunk_size,
                    warnings=warnings,
                    borrow_hwnd=win.hwnd if use_headed else 0,
                )
                write_method = "ratio:paste"
                submit_method = "ratio:enter"
                if self.focus_guard_strict() and self.last_focus_event:
                    return self._focus_taken_payload(
                        clean_prompt, warnings, "ratio paste"
                    ), {}
            else:
                if self._write_fn is not None:
                    write_method = self._write_fn(composer, clean_prompt, use_headed, chunk_size)
                else:
                    try:
                        # Guarded so a paste that activates the window is
                        # detected: strict mode must be able to refuse the
                        # submit instead of silently stealing focus. Option A
                        # borrows the foreground for the paste only.
                        write_method = self.guarded_write(
                            set_node_text,
                            composer,
                            clean_prompt,
                            headed=use_headed,
                            chunk_size=chunk_size,
                            prefer_paste=prefer_paste,
                            icebreaker=bool(self.cfg.get("icebreaker_before_paste", False)),
                            warnings=warnings,
                            borrow_hwnd=win.hwnd if use_headed else 0,
                        )
                    except UIAError:
                        if not self._primary_foreground_hwnd():
                            self.guarded_write(focus_window, win.hwnd, warnings=warnings)
                        write_method = self.guarded_write(
                            set_node_text,
                            composer,
                            clean_prompt,
                            headed=True,
                            chunk_size=chunk_size,
                            prefer_paste=True,
                            icebreaker=bool(self.cfg.get("icebreaker_before_paste", False)),
                            warnings=warnings,
                            borrow_hwnd=win.hwnd,
                        )
                        use_headed = True

                if self.focus_guard_strict() and self.last_focus_event:
                    return self._focus_taken_payload(
                        clean_prompt, warnings, "composer write"
                    ), {}

                if confirm:
                    if not self._confirm_fn(self.name, clean_prompt, effective_model):
                        self.ledger.mark(clean_prompt, self.name, "failed_retryable", note="cancelled_by_user")
                        return {
                            "ok": False,
                            "endpoint": self.name,
                            "error": "cancelled_by_user",
                            "reply": "",
                        }, {}

                self.ledger.mark(clean_prompt, self.name, "submitting")
                self._sleep(0.3)
                post_write_snap = self.take_snap(win.hwnd)
                send_btn = self._find_send_button(post_write_snap)
                send_tpl = self.cfg.get("templates", {}).get("send_button")
                # The submit click and the Enter hotkey both land on whatever
                # is foreground, so they run inside one borrow: activate, act,
                # restore. Losing the operator's window here cannot un-send the
                # message, so the abort below is marked ``submitted``.
                with self._borrowed_focus(win, warnings=warnings, allow=use_headed):
                    if send_btn is not None:
                        submit_method = self._click_or_fallback(send_btn, win.hwnd, send_tpl, headed=use_headed)
                    else:
                        submit_method = ""
                        if send_tpl and use_headed and self._invoke_fn is None:
                            try:
                                submit_method = self._click_or_fallback(None, win.hwnd, send_tpl, headed=True)
                            except UIAError:
                                submit_method = ""
                        if not submit_method:
                            submit_keys = tuple(str(self.cfg.get("submit_hotkey", "enter")).lower().split("+"))
                            if self._invoke_fn is not None:
                                submit_method = f"{'+'.join(submit_keys)}:stub"
                            else:
                                if self.cfg.get("submit_focus_composer", False) and composer.control is not None:
                                    try:
                                        composer.control.SetFocus()
                                        self._sleep(0.08)
                                    except Exception:
                                        pass
                                elif not use_headed:
                                    focus_window(win.hwnd)
                                self._send_hotkey_safe(*submit_keys)
                                submit_method = f"headed:{'+'.join(submit_keys)}"

                if self.focus_guard_strict() and self.last_focus_event:
                    # The click is already out; aborting the wait is the only
                    # honest option when the operator's focus never came back.
                    return self._focus_taken_payload(
                        clean_prompt, warnings, "submit", submitted=True
                    ), {}
        except UIAError as exc:
            ev = self._record_failure_evidence(exc.code, before_snap, {"message": exc.message})
            self.ledger.mark(clean_prompt, self.name, "failed_retryable", note=exc.code)
            return {
                "ok": False,
                "endpoint": self.name,
                "error": exc.code,
                "message": exc.message,
                "evidence": ev,
                "reply": "",
            }, {}

        return None, {
            "before_snap": before_snap,
            "write_method": write_method,
            "submit_method": submit_method,
            "use_headed": use_headed,
            "model_switch_info": model_switch_info,
            "attach_methods": attach_methods,
        }

    def _await_reply_convergence(
        self,
        win: "WindowInfo",
        before_snap: "Snapshot | None",
        before_meta: dict[str, Any],
        clean_prompt: str,
        *,
        started: float,
        budget: float,
        poll: float,
        stable_need: float,
        use_headed: bool,
        write_method: str,
        submit_method: str,
        effective_model: str | None,
        profile: str | None,
        model_switch_info: dict[str, Any] | None,
        attach_paths: list[str],
        attach_methods: list[str],
        warnings: list[str],
    ) -> dict[str, Any]:
        """Stage 3 — poll until the reply settles, then return the final payload.

        Owns every terminal outcome of a send that already left the window:
        fallback read, copy-button reply, ``returned_at_deadline`` and the
        timeout / ``submit_not_verified`` diagnostics (ledger + evidence file).
        The four gate settings are re-read from config here because they are
        pure ``self.cfg`` reads — same values ``ask()`` used before the split.
        """
        min_rel_x = float(self.cfg.get("content_min_rel_x", 0.0))
        max_rel_x = float(self.cfg.get("content_max_rel_x", 1.0))
        require_receipt = bool(self.cfg.get("require_send_receipt", False))
        require_new_copy = bool(self.cfg.get("require_new_copy_button", False))
        deadline = started + budget
        last_text = ""
        stable_since: float | None = None
        last_snap: Snapshot | None = before_snap
        receipt_seen = False
        before_copy_count = len(self._copy_buttons(before_snap)) if before_snap else 0
        # The new-copy gate is only meaningful when this UI actually exposes a
        # copy button. Fixtures and UI variants without one must not wait for a
        # control that can never appear (otherwise every ask() ends in timeout).
        copy_button_seen = before_copy_count > 0
        last_diag: dict[str, Any] = {}
        while time.monotonic() < deadline:
            fb = self.post_read_fallback(win, before_meta)
            if fb:
                self.ledger.mark(clean_prompt, self.name, "accepted")
                return self._ok_payload(
                    fb, "fallback", write_method, submit_method, effective_model, profile, model_switch_info, started, warnings, before_snap
                )

            try:
                cur_snap = self.take_snap(win.hwnd)
                last_snap = cur_snap
            except UIAError:
                self._sleep(poll)
                continue

            blocking = self.detect_blocking_popup(cur_snap)
            if blocking:
                return self._blocking_popup_payload(
                    blocking, cur_snap, clean_prompt, started, warnings
                )

            if not receipt_seen and self._has_send_receipt(before_snap, cur_snap, clean_prompt, min_rel_x=min_rel_x, max_rel_x=max_rel_x):
                receipt_seen = True

            generating = self._is_generating(cur_snap, min_rel_x=min_rel_x, max_rel_x=max_rel_x)
            delta = snapshot_delta(before_snap, cur_snap, min_rel_x=min_rel_x, max_rel_x=max_rel_x)
            filtered = self._extract_reply_lines(delta["added_lines"], prompt=clean_prompt)
            cur_text = "\n".join(filtered).strip()
            copy_count = len(self._copy_buttons(cur_snap))
            if copy_count:
                copy_button_seen = True
            new_copy_seen = copy_count > before_copy_count
            copy_gate_active = require_new_copy and copy_button_seen
            last_diag = {
                "receipt_seen": receipt_seen,
                "generating": generating,
                "delta_added_count": len(delta["added_lines"]),
                "filtered_count": len(filtered),
                "copy_button_count": copy_count,
                "copy_gate_active": copy_gate_active,
                "last_text_preview": cur_text[:160],
            }

            if generating or not cur_text or (require_receipt and not receipt_seen) or (copy_gate_active and not new_copy_seen):
                last_text = cur_text if (not require_receipt or receipt_seen) else ""
                stable_since = None
                self._sleep(poll)
                continue

            if cur_text != last_text:
                last_text = cur_text
                stable_since = time.monotonic()
                self._sleep(poll)
                continue

            if stable_since is None:
                stable_since = time.monotonic()
            if time.monotonic() - stable_since >= stable_need:
                copied = self._try_copy_button_reply(cur_snap, win, headed=use_headed)
                final_reply = copied or cur_text
                self.ledger.mark(clean_prompt, self.name, "accepted")
                return self._ok_payload(
                    final_reply,
                    "copy_button" if copied else "uia_delta",
                    write_method,
                    submit_method,
                    effective_model,
                    profile,
                    model_switch_info,
                    started,
                    warnings,
                    cur_snap,
                    attached_files=attach_paths,
                    attach_methods=attach_methods,
                )
            self._sleep(poll)

        deadline_copy_ok = not (require_new_copy and copy_button_seen) or (
            last_snap is not None and len(self._copy_buttons(last_snap)) > before_copy_count
        )
        if last_text and (not require_receipt or receipt_seen) and deadline_copy_ok:
            warnings.append("returned_at_deadline")
            self.ledger.mark(clean_prompt, self.name, "accepted", note="returned_at_deadline")
            return self._ok_payload(
                last_text, "uia_final", write_method, submit_method, effective_model, profile, model_switch_info, started, warnings, last_snap
            )

        reason = "submit_not_verified" if (require_receipt and not receipt_seen) else "timeout"
        ev = self._record_failure_evidence(reason, last_snap, last_diag or {"receipt_seen": receipt_seen})
        self.ledger.mark(clean_prompt, self.name, "unknown", note=reason)
        return {
            "ok": False,
            "endpoint": self.name,
            "error": "timeout",
            "reason": reason,
            "message": f"No verified reply received within {budget:.0f}s (reason={reason})",
            "diag": last_diag,
            "evidence": ev,
            "warnings": warnings,
            "reply": "",
            "elapsed": round(time.monotonic() - started, 2),
        }

    def _ok_payload(
        self,
        reply: str,
        source: str,
        write_method: str,
        submit_method: str,
        model: str | None,
        profile: str | None,
        model_switch_info: dict[str, Any] | None,
        started: float,
        warnings: list[str],
        snap: Snapshot | None,
        attached_files: list[str] | None = None,
        attach_methods: list[str] | None = None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": True,
            "endpoint": self.name,
            "reply": reply,
            "source": source,
            "write_method": write_method,
            "submit_method": submit_method,
            "profile": profile,
            "model_requested": model,
            "model_switch": model_switch_info,
            "snapshot_degraded": bool(snap.degraded) if snap else False,
            "elapsed": round(time.monotonic() - started, 2),
        }
        if attached_files:
            out["attached_files"] = list(attached_files)
            out["attach_methods"] = list(attach_methods or [])
        if warnings:
            out["warnings"] = warnings
        return out


def save_reply_file(path: str | Path, text: str) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return str(target)


def run_endpoint_cli(driver_cls: type[EndpointDriver], argv: list[str] | None = None) -> int:
    configure_stdio_utf8()
    parser = argparse.ArgumentParser(
        prog=f"{driver_cls.name}.py",
        description=f"Bridge v{__version__} endpoint script for {driver_cls.name}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ask = sub.add_parser("ask", help="Send a prompt and print the full reply")
    p_ask.add_argument("prompt", nargs="?", default="", help="Prompt text")
    p_ask.add_argument("--file", "-f", dest="prompt_file", help="Read prompt from UTF-8 file")
    p_ask.add_argument("--attach", "-a", action="append", default=None, help="Attach file path(s) before sending")
    p_ask.add_argument("--save", "-o", dest="save_path", help="Save full reply text to UTF-8 file")
    p_ask.add_argument("--new", action="store_true", help="Start a new chat before sending")
    p_ask.add_argument("--model", default=None, help="Switch to this model before sending")
    p_ask.add_argument("--profile", choices=("test", "routine", "council"), default=None, help="Scene strength profile")
    p_ask.add_argument("--free-only", action="store_true", help="Refuse sending if paid tier detected")
    p_ask.add_argument("--confirm", action="store_true", help="Wait for user confirmation before sending")
    p_ask.add_argument("--force", action="store_true", help="Bypass duplicate ledger check")
    p_ask.add_argument("--timeout", type=float, default=None, help="Wait timeout in seconds")
    p_ask.add_argument("--headed", action="store_true", help="Allow physical mouse/keyboard fallback immediately")
    p_ask.add_argument("--json", action="store_true", dest="as_json", help="Emit full structured JSON")

    p_read = sub.add_parser("read", help="Read latest reply without sending")
    p_read.add_argument("--save", "-o", dest="save_path", help="Save read reply to UTF-8 file")
    p_read.add_argument("--json", action="store_true", dest="as_json")

    p_models = sub.add_parser("models", help="List configured or live-probed models")
    p_models.add_argument("--live", action="store_true", help="Probe live UIA model dropdown")
    p_models.add_argument("--json", action="store_true", dest="as_json")

    p_status = sub.add_parser("status", help="Check window and readiness status")
    p_status.add_argument("--json", action="store_true", dest="as_json")

    p_snap = sub.add_parser("snapshot", help="Inspect live UIA accessibility tree")
    p_snap.add_argument("--all", action="store_true", help="Include non-interactive nodes")
    p_snap.add_argument("--limit", type=int, default=300)

    args = parser.parse_args(argv)
    driver = driver_cls()

    if args.command == "ask":
        prompt = args.prompt
        if args.prompt_file:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8")
        res = driver.ask(
            prompt,
            timeout=args.timeout,
            new_chat=args.new,
            headed=True if args.headed else None,
            model=args.model,
            profile=args.profile,
            free_only=args.free_only,
            confirm=args.confirm,
            force=args.force,
            attach=args.attach,
        )
        if res.get("ok") and args.save_path:
            res["saved_to"] = save_reply_file(args.save_path, res["reply"])
        if args.as_json:
            safe_print_json(res)
        elif res.get("ok"):
            safe_print(res["reply"])
        else:
            safe_print_json(res, indent=None, stream=sys.stderr)
        return exit_code_for(res)

    if args.command == "read":
        res = driver.read()
        if res.get("ok") and args.save_path:
            res["saved_to"] = save_reply_file(args.save_path, res["reply"])
        if args.as_json:
            safe_print_json(res)
        elif res.get("ok"):
            safe_print(res["reply"])
        else:
            safe_print_json(res, indent=None, stream=sys.stderr)
        return exit_code_for(res)

    if args.command == "models":
        res = driver.models(live=args.live, headed=True)
        safe_print_json(res)
        return exit_code_for(res)

    if args.command == "status":
        res = driver.status()
        safe_print_json(res)
        return exit_code_for(res)

    if args.command == "snapshot":
        res = driver.snapshot(interactive_only=not args.all, limit=args.limit)
        safe_print_json(res)
        return exit_code_for(res)

    return 2
