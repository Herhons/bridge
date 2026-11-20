"""Windows UIA-first snapshot & ref engine for Bridge v0.2.2.

Fixes from v0.2.1 live 6-endpoint test report:
- BUG-01: Resolve ``_AutomationClient`` from ``uiautomation.uiautomation._AutomationClient``
  (required by uiautomation 2.0.29) and surface ``degraded`` / ``degraded_reason`` instead
  of silently swallowing exceptions.
- BUG-02/08: Support ``exclude_names`` in ``Snapshot.find()`` so Chrome address bars
  ("地址和搜索栏") and sidebar search boxes ("搜索会话") are never selected as composers.
- Raise snapshot diagnostic serialization cap and preserve full internal node/ref maps.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

INTERACTIVE_ROLES = frozenset({
    "ButtonControl",
    "EditControl",
    "DocumentControl",
    "ComboBoxControl",
    "MenuItemControl",
    "RadioButtonControl",
    "CheckBoxControl",
    "ListItemControl",
    "TabItemControl",
    "HyperlinkControl",
    "SliderControl",
})

REF_PATTERN = re.compile(r"^@([a-z0-9]+):e(\d+)$")


class UIAError(RuntimeError):
    """Structured UIA error with machine-readable code."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class UINode:
    """Single element in a UIA accessibility snapshot."""

    role: str
    name: str = ""
    automation_id: str = ""
    value: str = ""
    enabled: bool = True
    offscreen: bool = False
    focusable: bool = False
    has_text_pattern: bool = False
    rect: tuple[int, int, int, int] | None = None  # (left, top, width, height)
    ref_id: str | None = None
    control: Any = field(default=None, repr=False, compare=False)
    children: list["UINode"] = field(default_factory=list)

    @property
    def center(self) -> tuple[int, int] | None:
        if not self.rect:
            return None
        left, top, width, height = self.rect
        if width <= 0 or height <= 0:
            return None
        return (left + width // 2, top + height // 2)

    def is_actionable(self, *, include_focusable_text: bool = True) -> bool:
        if self.role in INTERACTIVE_ROLES:
            return True
        if include_focusable_text and self.focusable and self.has_text_pattern and self.role in ("GroupControl", "PaneControl", "CustomControl"):
            return True
        return False

    def to_dict(self, *, compact: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role}
        if self.ref_id:
            out["ref"] = self.ref_id
        if self.name:
            out["name"] = self.name
        if self.automation_id:
            out["id"] = self.automation_id
        if self.value:
            out["value"] = self.value[:200] if compact else self.value
        if not self.enabled:
            out["enabled"] = False
        if self.offscreen:
            out["offscreen"] = True
        if self.rect:
            out["rect"] = list(self.rect)
        if self.children:
            out["children"] = [c.to_dict(compact=compact) for c in self.children]
        return out


@dataclass
class Snapshot:
    """Point-in-time UIA accessibility tree with scoped element refs."""

    snapshot_id: str
    hwnd: int
    title: str
    url: str
    nodes: list[UINode]
    refs: dict[str, UINode]
    window_rect: tuple[int, int, int, int] | None = None
    degraded: bool = False
    degraded_reason: str = ""
    created_at: float = field(default_factory=time.monotonic)

    def walk(self) -> Iterable[UINode]:
        stack = list(reversed(self.nodes))
        while stack:
            node = stack.pop()
            yield node
            if node.children:
                stack.extend(reversed(node.children))

    def _effective_win_rect(self) -> tuple[int, int, int, int] | None:
        if self.window_rect and self.window_rect[2] > 0 and self.window_rect[3] > 0:
            return self.window_rect
        if self.nodes and self.nodes[0].rect:
            return self.nodes[0].rect
        return None

    def node_in_ratio_box(self, node: UINode, ratio_box: Iterable[float] | None) -> bool:
        if not ratio_box:
            return True
        win = self._effective_win_rect()
        center = node.center
        if win is None or center is None:
            return True
        wx, wy, ww, wh = win
        if ww <= 0 or wh <= 0:
            return True
        x0, y0, x1, y1 = tuple(ratio_box)
        rx = (center[0] - wx) / float(ww)
        ry = (center[1] - wy) / float(wh)
        return (x0 <= rx <= x1) and (y0 <= ry <= y1)

    def text_lines(self, *, min_rel_x: float = 0.0, max_rel_x: float = 1.0) -> list[str]:
        win = self._effective_win_rect()
        lines: list[str] = []
        for node in self.walk():
            if node.role != "TextControl":
                continue
            if node.children:
                own = (node.name or "").strip()
                if not own or any((c.name or "").strip() == own for c in node.children):
                    continue
            if (min_rel_x > 0.0 or max_rel_x < 1.0) and win is not None and node.rect is not None and win[2] > 0:
                rel_x = (node.rect[0] - win[0]) / float(win[2])
                if rel_x < min_rel_x or rel_x > max_rel_x:
                    continue
            txt = (node.name or "").strip()
            if not txt:
                continue
            lines.append(txt)
        return lines

    def text_digest(self, *, min_rel_x: float = 0.0, max_rel_x: float = 1.0) -> str:
        joined = "\n".join(self.text_lines(min_rel_x=min_rel_x, max_rel_x=max_rel_x))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    def find(
        self,
        *,
        roles: Iterable[str] | None = None,
        names: Iterable[str] | None = None,
        name_contains: Iterable[str] | None = None,
        exclude_names: Iterable[str] | None = None,
        automation_ids: Iterable[str] | None = None,
        ratio_box: Iterable[float] | None = None,
        enabled_only: bool = True,
        visible_only: bool = False,
        actionable_only: bool = False,
    ) -> list[UINode]:
        role_set = set(roles) if roles else None
        exact_names = {n.strip().lower() for n in names} if names else None
        sub_names = [n.strip().lower() for n in name_contains] if name_contains else None
        excl_sub = [n.strip().lower() for n in exclude_names] if exclude_names else None
        id_set = {i.strip().lower() for i in automation_ids} if automation_ids else None

        hits: list[UINode] = []
        for node in self.walk():
            if enabled_only and not node.enabled:
                continue
            if visible_only and node.offscreen:
                continue
            if actionable_only and not node.ref_id:
                continue
            if role_set and node.role not in role_set:
                continue
            if ratio_box and not self.node_in_ratio_box(node, ratio_box):
                continue
            node_name = (node.name or "").strip().lower()
            node_id = (node.automation_id or "").strip().lower()
            if excl_sub and any(ex in node_name for ex in excl_sub):
                continue
            if exact_names is not None and node_name not in exact_names:
                continue
            if sub_names is not None and not any(sub in node_name for sub in sub_names):
                continue
            if id_set is not None and node_id not in id_set:
                continue
            hits.append(node)
        return hits

    def to_dict(self, *, interactive_only: bool = True, limit: int = 300) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for node in self.walk():
            if interactive_only and not node.ref_id:
                continue
            items.append({
                "ref": node.ref_id,
                "role": node.role,
                "name": node.name,
                "id": node.automation_id,
                "enabled": node.enabled,
                "offscreen": node.offscreen,
                "rect": list(node.rect) if node.rect else None,
            })
            if len(items) >= limit:
                break
        return {
            "snapshot_id": self.snapshot_id,
            "hwnd": self.hwnd,
            "title": self.title,
            "url": self.url,
            "window_rect": list(self.window_rect) if self.window_rect else None,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "ref_count": len(self.refs),
            "digest": self.text_digest(),
            "elements": items,
        }


def snapshot_delta(before: Snapshot, after: Snapshot, *, min_rel_x: float = 0.0, max_rel_x: float = 1.0) -> dict[str, Any]:
    old_lines = before.text_lines(min_rel_x=min_rel_x, max_rel_x=max_rel_x)
    new_lines = after.text_lines(min_rel_x=min_rel_x, max_rel_x=max_rel_x)
    remaining = Counter(old_lines)
    added: list[str] = []
    for line in new_lines:
        if remaining[line] > 0:
            remaining[line] -= 1
        else:
            added.append(line)
    return {
        "changed": before.text_digest(min_rel_x=min_rel_x, max_rel_x=max_rel_x) != after.text_digest(min_rel_x=min_rel_x, max_rel_x=max_rel_x),
        "before_digest": before.text_digest(min_rel_x=min_rel_x, max_rel_x=max_rel_x),
        "after_digest": after.text_digest(min_rel_x=min_rel_x, max_rel_x=max_rel_x),
        "added_lines": added,
        "added_text": "\n".join(added).strip(),
        "ref_delta": len(after.refs) - len(before.refs),
    }


def assign_refs(nodes: list[UINode], snapshot_id: str, *, include_focusable_text: bool = True) -> dict[str, UINode]:
    refs: dict[str, UINode] = {}
    counter = 0

    def _walk(items: list[UINode]) -> None:
        nonlocal counter
        for node in items:
            if node.is_actionable(include_focusable_text=include_focusable_text):
                counter += 1
                ref = f"@{snapshot_id}:e{counter}"
                node.ref_id = ref
                refs[ref] = node
            if node.children:
                _walk(node.children)

    _walk(nodes)
    return refs


def parse_ref(ref_id: str) -> tuple[str, int]:
    m = REF_PATTERN.match((ref_id or "").strip())
    if not m:
        raise UIAError("INVALID_ARGS", f"Ref must be @<snapshot_id>:e<N>, got {ref_id!r}")
    return m.group(1), int(m.group(2))


def resolve_ref(ref_id: str, latest: Snapshot | None) -> UINode:
    snap_id, _index = parse_ref(ref_id)
    if latest is None or latest.snapshot_id != snap_id:
        raise UIAError(
            "STALE_REF",
            f"Ref {ref_id} does not belong to active snapshot {getattr(latest, 'snapshot_id', None)}",
        )
    node = latest.refs.get(ref_id)
    if node is None:
        raise UIAError("STALE_REF", f"Ref {ref_id} not found in snapshot {snap_id}")
    return node


def pick_unique(nodes: list[UINode], label: str) -> UINode:
    if not nodes:
        raise UIAError("TARGET_NOT_FOUND", f"No element matched {label}")
    if len(nodes) > 1:
        raise UIAError(
            "AMBIGUOUS_TARGET",
            f"Matched {len(nodes)} elements for {label}",
            details={"candidates": [n.to_dict() for n in nodes[:6]]},
        )
    return nodes[0]


# ---------------------------------------------------------------------------
# Live Windows UIA backend
# ---------------------------------------------------------------------------

def resolve_automation_client(auto_mod: Any) -> Any:
    """Resolve ``_AutomationClient`` across uiautomation versions (fixes BUG-01).

    In ``uiautomation==2.0.29``, ``_AutomationClient`` lives on the submodule
    ``uiautomation.uiautomation`` rather than the top-level package.
    """
    client_cls = getattr(auto_mod, "_AutomationClient", None)
    if client_cls is None:
        sub = getattr(auto_mod, "uiautomation", None)
        client_cls = getattr(sub, "_AutomationClient", None) if sub is not None else None
    if client_cls is None:
        raise UIAError("UIA_CLIENT_MISSING", "Could not resolve _AutomationClient from uiautomation package")
    return client_cls.instance().IUIAutomation


def _configure_uia_no_auto_focus(uia_client: Any = None) -> None:
    if os.name != "nt":
        return
    try:
        if uia_client is None:
            import uiautomation as auto

            uia_client = resolve_automation_client(auto)
        if uia_client is not None and hasattr(uia_client, "AutoSetFocus"):
            uia_client.AutoSetFocus = False
    except Exception:
        pass


def _read_rect_from_bounds(rect: Any) -> tuple[int, int, int, int] | None:
    try:
        w = int(rect.right - rect.left)
        h = int(rect.bottom - rect.top)
        if w <= 0 or h <= 0:
            return None
        return (int(rect.left), int(rect.top), w, h)
    except Exception:
        return None


def _read_rect(control: Any) -> tuple[int, int, int, int] | None:
    try:
        return _read_rect_from_bounds(control.BoundingRectangle)
    except Exception:
        return None


def _read_value(control: Any) -> str:
    try:
        pat = control.GetValuePattern()
        if pat is not None:
            return str(pat.Value or "")
    except Exception:
        pass
    return ""


def _find_inner_document(root_ctrl: Any, *, url_host: str | None = None) -> tuple[Any, str, str]:
    """Locate the inner web/Electron ``DocumentControl``. Returns ``(ctrl, url, error_reason)``."""
    try:
        import uiautomation as auto

        client = resolve_automation_client(auto)
        cond = client.CreatePropertyCondition(
            auto.PropertyId.ControlTypeProperty,
            auto.ControlType.DocumentControl,
        )
        found = root_ctrl.Element.FindAll(4, cond)  # TreeScope_Descendants = 4
        if not found or found.Length == 0:
            return root_ctrl, "", "no_document_control"

        best_ctrl = None
        best_url = ""
        for i in range(found.Length):
            el = found.GetElement(i)
            doc_ctrl = auto.DocumentControl(element=el)
            url = _read_value(doc_ctrl)
            if url_host and url_host.lower() in url.lower():
                return doc_ctrl, url, ""
            if best_ctrl is None or (url and not best_url):
                best_ctrl = doc_ctrl
                best_url = url
        return (best_ctrl or root_ctrl), best_url, ""
    except Exception as exc:
        return root_ctrl, "", f"inner_doc_error:{type(exc).__name__}:{exc}"


def _build_cached_tree(target_ctrl: Any) -> UINode | None:
    """Single-IPC COM ``CacheRequest`` subtree fetch (uses fixed ``resolve_automation_client``)."""
    import uiautomation as auto

    client = resolve_automation_client(auto)
    req = client.CreateCacheRequest()
    req.TreeScope = 7  # TreeScope_Subtree
    req.TreeFilter = client.RawViewCondition
    for prop in (
        auto.PropertyId.ControlTypeProperty,
        auto.PropertyId.NameProperty,
        auto.PropertyId.AutomationIdProperty,
        auto.PropertyId.IsEnabledProperty,
        auto.PropertyId.IsOffscreenProperty,
        auto.PropertyId.IsKeyboardFocusableProperty,
        auto.PropertyId.BoundingRectangleProperty,
    ):
        req.AddProperty(prop)

    cached_root = target_ctrl.Element.BuildUpdatedCache(req)

    def _walk(element: Any) -> UINode:
        role = auto.ControlTypeNames.get(element.CachedControlType, "Unknown")
        name = str(element.CachedName or "")
        aid = str(element.CachedAutomationId or "")
        enabled = bool(element.CachedIsEnabled)
        offscreen = bool(element.CachedIsOffscreen)
        focusable = bool(element.CachedIsKeyboardFocusable)
        rect = _read_rect_from_bounds(element.CachedBoundingRectangle)
        ctrl_cls = getattr(auto, role, auto.Control)
        ctrl = ctrl_cls(element=element)
        has_tp = role in ("GroupControl", "PaneControl", "CustomControl") and focusable
        node = UINode(
            role=role,
            name=name,
            automation_id=aid,
            enabled=enabled,
            offscreen=offscreen,
            focusable=focusable,
            has_text_pattern=has_tp,
            rect=rect,
            control=ctrl,
        )
        if role != "MenuBarControl":
            kids = element.GetCachedChildren()
            if kids:
                for i in range(kids.Length):
                    node.children.append(_walk(kids.GetElement(i)))
        return node

    return _walk(cached_root)


def take_snapshot(
    hwnd: int,
    *,
    url_host: str | None = None,
    prefer_document: bool = True,
    include_focusable_text: bool = True,
) -> Snapshot:
    """Capture a Windows UIA snapshot of *hwnd* with scoped ``@<snap>:eN`` refs."""
    if os.name != "nt":
        raise UIAError("PLATFORM_UNSUPPORTED", "Live UIA snapshots require Windows")

    import uiautomation as auto

    _configure_uia_no_auto_focus()
    root_ctrl = auto.ControlFromHandle(int(hwnd))
    if root_ctrl is None:
        raise UIAError("NO_WINDOW", f"No UIA control for hwnd={hwnd}")

    title = str(getattr(root_ctrl, "Name", "") or "")
    win_rect = _read_rect(root_ctrl)

    target_ctrl = root_ctrl
    url = ""
    degraded = False
    degraded_reasons: list[str] = []

    if prefer_document:
        target_ctrl, url, doc_err = _find_inner_document(root_ctrl, url_host=url_host)
        if doc_err:
            degraded = True
            degraded_reasons.append(doc_err)

    try:
        root_node = _build_cached_tree(target_ctrl)
    except Exception as exc:
        degraded = True
        degraded_reasons.append(f"cache_fallback:{type(exc).__name__}:{exc}")
        root_node = _build_fallback_tree(target_ctrl)

    nodes = [root_node] if root_node else []
    snap_id = uuid.uuid4().hex[:8]
    refs = assign_refs(nodes, snap_id, include_focusable_text=include_focusable_text)
    return Snapshot(
        snapshot_id=snap_id,
        hwnd=int(hwnd),
        title=title,
        url=url,
        nodes=nodes,
        refs=refs,
        window_rect=win_rect,
        degraded=degraded,
        degraded_reason="; ".join(degraded_reasons),
    )


def _build_fallback_tree(root_ctrl: Any, *, max_depth: int = 24, max_nodes: int = 4000) -> UINode | None:
    count = 0

    def _build(ctrl: Any, depth: int) -> UINode | None:
        nonlocal count
        if depth > max_depth or count >= max_nodes:
            return None
        count += 1
        try:
            role = str(getattr(ctrl, "ControlTypeName", "") or "Unknown")
            name = str(getattr(ctrl, "Name", "") or "")
            aid = str(getattr(ctrl, "AutomationId", "") or "")
            enabled = bool(getattr(ctrl, "IsEnabled", True))
            offscreen = bool(getattr(ctrl, "IsOffscreen", False))
            focusable = bool(getattr(ctrl, "IsKeyboardFocusable", False))
        except Exception:
            return None
        node = UINode(
            role=role,
            name=name,
            automation_id=aid,
            enabled=enabled,
            offscreen=offscreen,
            focusable=focusable,
            has_text_pattern=role in ("GroupControl", "PaneControl", "CustomControl") and focusable,
            rect=_read_rect(ctrl),
            control=ctrl,
        )
        if role != "MenuBarControl":
            try:
                kids = ctrl.GetChildren() or []
            except Exception:
                kids = []
            for child in kids:
                built = _build(child, depth + 1)
                if built is not None:
                    node.children.append(built)
        return node

    return _build(root_ctrl, 0)


def invoke_node(node: UINode, *, headed: bool = False, click_fn: Callable[[int, int], None] | None = None) -> str:
    ctrl = node.control
    if ctrl is not None:
        try:
            inv = ctrl.GetInvokePattern()
            if inv is not None:
                inv.Invoke()
                return "uia:invoke"
        except Exception:
            pass
        try:
            sel = ctrl.GetSelectionItemPattern()
            if sel is not None:
                sel.Select()
                return "uia:select"
        except Exception:
            pass
    if not headed:
        raise UIAError("POLICY_DENIED", f"Headless invoke unavailable for {node.role}:{node.name!r}; retry with headed=True")
    center = node.center
    if center is None:
        raise UIAError("NO_GEOMETRY", f"Element {node.ref_id} has no visible bounding box")
    if click_fn is not None:
        click_fn(*center)
        return "headed:click_fn"
    _physical_click(*center)
    return "headed:mouse"


def set_node_text(
    node: UINode,
    text: str,
    *,
    headed: bool = False,
    chunk_size: int = 0,
    prefer_paste: bool = False,
    paste_fn: Callable[[UINode, str], None] | None = None,
) -> str:
    ctrl = node.control
    if not prefer_paste and ctrl is not None and chunk_size <= 0:
        try:
            pat = ctrl.GetValuePattern()
            if pat is not None and not getattr(pat, "IsReadOnly", False):
                pat.SetValue(text)
                if str(pat.Value or "") == text:
                    return "uia:set_value"
        except Exception:
            pass

    if paste_fn is not None:
        chunks = _split_chunks(text, chunk_size)
        for chunk in chunks:
            paste_fn(node, chunk)
        return "custom:paste_chunked" if len(chunks) > 1 else "custom:paste"

    if not headed:
        raise UIAError("POLICY_DENIED", "Headless text write unavailable; retry with headed=True")

    _headed_paste_text(node, text, chunk_size=chunk_size)
    return "headed:paste_chunked" if chunk_size > 0 and len(text) > chunk_size else "headed:paste"


def scroll_into_view(node: UINode) -> bool:
    ctrl = node.control
    if ctrl is None:
        return False
    try:
        pat = ctrl.GetScrollItemPattern()
        if pat is not None:
            pat.ScrollIntoView()
            return True
    except Exception:
        pass
    return False


def scroll_window_bottom(window_rect: tuple[int, int, int, int] | None, *, steps: int = 8) -> bool:
    """Scroll the main content area toward the bottom (cross-screen long-reply helper)."""
    if os.name != "nt" or window_rect is None:
        return False
    wx, wy, ww, wh = window_rect
    if ww <= 0 or wh <= 0:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        cx = int(wx + ww * 0.55)
        cy = int(wy + wh * 0.50)
        user32.SetCursorPos(cx, cy)
        time.sleep(0.05)
        MOUSEEVENTF_WHEEL = 0x0800
        for _ in range(max(1, steps)):
            user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, -600, 0)
            time.sleep(0.04)
        return True
    except Exception:
        return False


def _split_chunks(text: str, chunk_size: int) -> list[str]:
    if chunk_size <= 0 or len(text) <= chunk_size:
        return [text]
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def _physical_click(x: int, y: int) -> None:
    if os.name != "nt":
        raise UIAError("PLATFORM_UNSUPPORTED", "Physical click requires Windows")
    import ctypes

    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.03)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def _bind_clipboard_ctypes():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.GetClipboardSequenceNumber.argtypes = []
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    return user32, kernel32


def get_clipboard_sequence() -> int:
    if os.name != "nt":
        return 0
    user32, _ = _bind_clipboard_ctypes()
    return int(user32.GetClipboardSequenceNumber())


def set_clipboard(text: str) -> None:
    if os.name != "nt":
        raise UIAError("PLATFORM_UNSUPPORTED", "Clipboard requires Windows")
    import ctypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    user32, kernel32 = _bind_clipboard_ctypes()
    data = (text + "\x00").encode("utf-16-le")
    for _ in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise UIAError("CLIPBOARD_BUSY", "Could not open Windows clipboard")
    try:
        user32.EmptyClipboard()
        hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not hmem:
            raise UIAError("CLIPBOARD_ALLOC_FAILED", "GlobalAlloc returned NULL")
        ptr = kernel32.GlobalLock(hmem)
        if not ptr:
            kernel32.GlobalFree(hmem)
            raise UIAError("CLIPBOARD_LOCK_FAILED", "GlobalLock returned NULL")
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            kernel32.GlobalUnlock(hmem)
        if not user32.SetClipboardData(CF_UNICODETEXT, hmem):
            kernel32.GlobalFree(hmem)
            raise UIAError("CLIPBOARD_SET_FAILED", "SetClipboardData failed")
    finally:
        user32.CloseClipboard()


def get_clipboard() -> str:
    if os.name != "nt":
        return ""
    import ctypes

    CF_UNICODETEXT = 13
    user32, kernel32 = _bind_clipboard_ctypes()
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def send_hotkey(*keys: str) -> None:
    if os.name != "nt":
        raise UIAError("PLATFORM_UNSUPPORTED", "Hotkeys require Windows")
    import ctypes

    VK = {
        "ctrl": 0x11,
        "alt": 0x12,
        "shift": 0x10,
        "enter": 0x0D,
        "esc": 0x1B,
        "tab": 0x09,
        "delete": 0x2E,
        "a": 0x41,
        "c": 0x43,
        "v": 0x56,
        "pagedown": 0x22,
        "end": 0x23,
        "home": 0x24,
    }
    user32 = ctypes.windll.user32
    codes = [VK[k.lower()] for k in keys]
    for code in codes:
        user32.keybd_event(code, 0, 0, 0)
        time.sleep(0.02)
    for code in reversed(codes):
        user32.keybd_event(code, 0, 2, 0)
        time.sleep(0.02)


def _headed_paste_text(node: UINode, text: str, *, chunk_size: int = 0) -> None:
    center = node.center
    if center is not None:
        _physical_click(*center)
        time.sleep(0.15)
    elif node.control is not None:
        try:
            node.control.SetFocus()
        except Exception:
            pass
    send_hotkey("ctrl", "a")
    time.sleep(0.05)
    send_hotkey("delete")
    time.sleep(0.05)
    for chunk in _split_chunks(text, chunk_size):
        set_clipboard(chunk)
        time.sleep(0.05)
        send_hotkey("ctrl", "v")
        time.sleep(0.25)


def click_ratio_and_paste(window_rect: tuple[int, int, int, int], ratio_xy: tuple[float, float], text: str, *, chunk_size: int = 0) -> None:
    wx, wy, ww, wh = window_rect
    cx = int(wx + ww * ratio_xy[0])
    cy = int(wy + wh * ratio_xy[1])
    _physical_click(cx, cy)
    time.sleep(0.15)
    send_hotkey("ctrl", "a")
    time.sleep(0.05)
    send_hotkey("delete")
    time.sleep(0.05)
    for chunk in _split_chunks(text, chunk_size):
        set_clipboard(chunk)
        time.sleep(0.05)
        send_hotkey("ctrl", "v")
        time.sleep(0.25)
    send_hotkey("enter")


def upload_via_file_dialog(file_path: str, *, timeout: float = 8.0) -> str:
    """Fill a native Windows Open/打开 file dialog with *file_path* and press Enter."""
    if os.name != "nt":
        raise UIAError("PLATFORM_UNSUPPORTED", "Native file dialog requires Windows")
    import uiautomation as auto

    abs_path = str(os.path.abspath(file_path))
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        for title in ("打开", "Open", "选择文件", "Choose File"):
            dlg = auto.WindowControl(searchDepth=2, Name=title)
            if dlg.Exists(0.15, 0.05):
                edit = dlg.EditControl(Name="文件名(N):")
                if not edit.Exists(0.15, 0.05):
                    edit = dlg.EditControl()
                if edit.Exists(0.15, 0.05):
                    edit.SetFocus()
                    time.sleep(0.1)
                    set_clipboard(abs_path)
                    time.sleep(0.05)
                    send_hotkey("ctrl", "a")
                    time.sleep(0.05)
                    send_hotkey("ctrl", "v")
                    time.sleep(0.15)
                    send_hotkey("enter")
                    return "uia:file_dialog"
        time.sleep(0.2)
    raise UIAError("DIALOG_NOT_FOUND", f"File dialog did not appear within {timeout:.1f}s")
