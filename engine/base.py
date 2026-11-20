"""Unified base driver for all 10 Bridge v0.2.4 endpoints.

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
import json
import os
import re
import sys
import time
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
    scroll_into_view,
    scroll_window_bottom,
    send_hotkey,
    set_node_text,
    snapshot_delta,
    take_snapshot,
    upload_via_file_dialog,
)
from engine.vision import capture_window, match_template
from engine.window import WindowInfo, find_window, focus_window, launch_app_id, launch_url_in_chrome

EXIT_CODES = {
    "ok": 0,
    "error": 1,
    "missing_resource": 3,
    "cancelled_by_user": 4,
    "tier_unconfirmed": 5,
    "duplicate": 6,
}

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
    """UIA-first, screenshot-fallback driver shared by all 10 endpoints."""

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

    def locate_window(self, *, auto_open: bool = False) -> WindowInfo | None:
        if self._window_fn is not None:
            return self._window_fn()
        win = find_window(
            process_names=self.cfg.get("process_names"),
            title_contains=self.cfg.get("title_contains"),
            exclude_processes=self.cfg.get("exclude_processes"),
        )
        if win is None and auto_open:
            launched = False
            if self.cfg.get("kind") == "web" and self.cfg.get("url"):
                launched = launch_url_in_chrome(self.cfg["url"], self.bridge_cfg.get("chrome_path", ""))
            elif self.cfg.get("launch_app_id"):
                launched = launch_app_id(str(self.cfg["launch_app_id"]))
            if launched:
                self._sleep(2.5)
                win = find_window(
                    process_names=self.cfg.get("process_names"),
                    title_contains=self.cfg.get("title_contains"),
                    exclude_processes=self.cfg.get("exclude_processes"),
                )
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
        hits = snap.find(
            roles=roles,
            names=sel.get("names") or None,
            name_contains=sel.get("name_contains") or None,
            exclude_names=exclude,
            automation_ids=sel.get("automation_ids") or None,
            ratio_box=ratio_box,
            enabled_only=True,
            actionable_only=True,
        )
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
            self._click_or_fallback(
                selector_node,
                win.hwnd,
                self.cfg.get("templates", {}).get("model_selector"),
                headed=headed,
            )
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

    def _attach_files(self, win: WindowInfo, snap: Snapshot, paths: list[str], *, headed: bool) -> list[str]:
        methods: list[str] = []
        menu_cfg = self.cfg.get("selectors", {}).get("attach_menu_item") or {}
        ratio_fallback = self.cfg.get("fallback_attach_ratio")
        wait_after = float(self.cfg.get("attach_wait_seconds", 1.0))
        for fp in paths:
            btn = self._find_attach_control(snap, "attach_button")
            if btn is not None:
                self._click_or_fallback(btn, win.hwnd, self.cfg.get("templates", {}).get("attach_button"), headed=headed)
            elif ratio_fallback and headed and snap.window_rect is not None:
                click_ratio(snap.window_rect, tuple(ratio_fallback))
            else:
                raise UIAError("TARGET_NOT_FOUND", f"No attach_button found for {self.name}")
            self._sleep(0.35)
            if menu_cfg:
                snap_m = self.take_snap(win.hwnd)
                item = self._find_attach_control(snap_m, "attach_menu_item")
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
            if headed:
                focus_window(win.hwnd)
                self._sleep(0.15)
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
        min_rel_x = float(self.cfg.get("content_min_rel_x", 0.0))
        max_rel_x = float(self.cfg.get("content_max_rel_x", 1.0))
        prefer_paste = bool(self.cfg.get("prefer_paste", False))
        require_receipt = bool(self.cfg.get("require_send_receipt", False))

        # Resolve --profile into effective model + free_only guard
        effective_model = model
        effective_free_only = bool(free_only)
        if profile:
            try:
                prof_model, prof_free = resolve_profile(self.name, profile, policy=self.model_policy)
            except ValueError as exc:
                return {"ok": False, "endpoint": self.name, "error": "invalid_profile", "message": str(exc), "reply": ""}
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
                return {"ok": False, "endpoint": self.name, "error": "missing_resource", "message": f"Attachment not found: {ap}", "reply": ""}

        clean_prompt = self.prepare_prompt(prompt)
        if not clean_prompt.strip():
            return {"ok": False, "endpoint": self.name, "error": "empty_prompt", "reply": ""}

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
                }

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
                focus_window(win.hwnd)
                self._sleep(0.2)

            before_meta = self.pre_send_meta(win)
            before_snap: Snapshot | None = None
            try:
                before_snap = self.take_snap(win.hwnd)
                if before_snap.degraded:
                    warnings.append(f"snapshot_degraded:{before_snap.degraded_reason}")

                if new_chat:
                    nc = self._find_new_chat_button(before_snap)
                    if nc is not None:
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
                        }
                    if model_switch_info.get("switched"):
                        before_snap = self.take_snap(win.hwnd)
                    elif model_switch_info.get("attempted"):
                        warnings.append(f"model_switch_failed:{model_switch_info.get('reason')}")

                if attach_paths:
                    attach_methods = self._attach_files(win, before_snap, attach_paths, headed=use_headed)
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
                    }

                chunk_size = int(self.cfg.get("paste_chunk", 0))
                if composer is None and fallback_ratio:
                    if confirm and not self._confirm_fn(self.name, clean_prompt, effective_model):
                        self.ledger.mark(clean_prompt, self.name, "failed_retryable", note="cancelled_by_user")
                        return {"ok": False, "endpoint": self.name, "error": "cancelled_by_user", "reply": ""}
                    self.ledger.mark(clean_prompt, self.name, "submitting")
                    wrect = before_snap.window_rect
                    if wrect is None:
                        raise UIAError("NO_GEOMETRY", "Window geometry unavailable for ratio fallback")
                    focus_window(win.hwnd)
                    click_ratio_and_paste(wrect, tuple(fallback_ratio), clean_prompt, chunk_size=chunk_size)
                    write_method = "ratio:paste"
                    submit_method = "ratio:enter"
                else:
                    if self._write_fn is not None:
                        write_method = self._write_fn(composer, clean_prompt, use_headed, chunk_size)
                    else:
                        try:
                            write_method = set_node_text(
                                composer,
                                clean_prompt,
                                headed=use_headed,
                                chunk_size=chunk_size,
                                prefer_paste=prefer_paste,
                            )
                        except UIAError:
                            focus_window(win.hwnd)
                            write_method = set_node_text(
                                composer,
                                clean_prompt,
                                headed=True,
                                chunk_size=chunk_size,
                                prefer_paste=True,
                            )
                            use_headed = True

                    if confirm:
                        if not self._confirm_fn(self.name, clean_prompt, effective_model):
                            self.ledger.mark(clean_prompt, self.name, "failed_retryable", note="cancelled_by_user")
                            return {
                                "ok": False,
                                "endpoint": self.name,
                                "error": "cancelled_by_user",
                                "reply": "",
                            }

                    self.ledger.mark(clean_prompt, self.name, "submitting")
                    self._sleep(0.3)
                    post_write_snap = self.take_snap(win.hwnd)
                    send_btn = self._find_send_button(post_write_snap)
                    send_tpl = self.cfg.get("templates", {}).get("send_button")
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
                }

        # Wait for reply convergence
        deadline = started + budget
        last_text = ""
        stable_since: float | None = None
        last_snap: Snapshot | None = before_snap
        receipt_seen = False
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

            if not receipt_seen and self._has_send_receipt(before_snap, cur_snap, clean_prompt, min_rel_x=min_rel_x, max_rel_x=max_rel_x):
                receipt_seen = True

            generating = self._is_generating(cur_snap, min_rel_x=min_rel_x, max_rel_x=max_rel_x)
            delta = snapshot_delta(before_snap, cur_snap, min_rel_x=min_rel_x, max_rel_x=max_rel_x)
            filtered = self._extract_reply_lines(delta["added_lines"], prompt=clean_prompt)
            cur_text = "\n".join(filtered).strip()
            last_diag = {
                "receipt_seen": receipt_seen,
                "generating": generating,
                "delta_added_count": len(delta["added_lines"]),
                "filtered_count": len(filtered),
                "copy_button_count": len(self._copy_buttons(cur_snap)),
                "last_text_preview": cur_text[:160],
            }

            if generating or not cur_text or (require_receipt and not receipt_seen):
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

        if last_text and (not require_receipt or receipt_seen):
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
