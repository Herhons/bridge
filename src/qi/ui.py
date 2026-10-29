"""Named UI controls for binding, input and reply ownership.

Coordinates come only from the currently bound control. No image/position fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from urllib.parse import urlsplit

import uiautomation as auto
from uiautomation.uiautomation import _AutomationClient


@dataclass(eq=False)
class Node:
    kind: str
    name: str
    control: object = None
    parent: Node | None = field(default=None, repr=False)
    children: list[Node] = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    @cached_property
    def key(self):
        return tuple(self.control.GetRuntimeId())

    @cached_property
    def enabled(self):
        return bool(self.control.IsEnabled)

    @cached_property
    def offscreen(self):
        return bool(self.control.IsOffscreen)


def tree(control):
    """Fetch one UIA snapshot instead of dereferencing old nodes after a render."""
    client = _AutomationClient.instance().IUIAutomation
    request = client.CreateCacheRequest()
    request.TreeScope = 7  # TreeScope_Subtree
    request.TreeFilter = client.RawViewCondition
    for prop in (auto.PropertyId.ControlTypeProperty, auto.PropertyId.NameProperty,
                 auto.PropertyId.RuntimeIdProperty, auto.PropertyId.IsEnabledProperty,
                 auto.PropertyId.IsOffscreenProperty):
        request.AddProperty(prop)

    def build(element, parent=None):
        kind = auto.ControlTypeNames[element.CachedControlType]
        node = Node(kind, element.CachedName, getattr(auto, kind)(element=element), parent)
        node.key = tuple(element.GetCachedPropertyValue(auto.PropertyId.RuntimeIdProperty))
        node.enabled = bool(element.CachedIsEnabled)
        node.offscreen = bool(element.CachedIsOffscreen)
        children = element.GetCachedChildren()
        if children and node.kind != "MenuBarControl":
            node.children = [build(children.GetElement(i), node) for i in range(children.Length)]
        return node

    return build(control.Element.BuildUpdatedCache(request))


def documents(hwnd):
    def visit(control):
        if control.ControlTypeName == "DocumentControl":
            pattern = control.GetValuePattern()
            if pattern and pattern.Value:
                yield control, pattern.Value
                return
        for child in control.GetChildren():
            yield from visit(child)
    return list(visit(auto.ControlFromHandle(hwnd)))


HOSTS = {"chatgpt": "chatgpt.com", "deepseek": "chat.deepseek.com",
         "gemini": "gemini.google.com", "grok": "grok.com"}


def document(hwnd, site):
    docs = documents(hwnd)
    if site in HOSTS:
        docs = [(ctrl, url) for ctrl, url in docs if urlsplit(url).hostname == HOSTS[site]]
    else:
        docs = [(ctrl, url) for ctrl, url in docs if urlsplit(url).scheme in
                ("file", "oc", "dsh-app", "app")]
    if len(docs) != 1:
        raise RuntimeError(f"{site}: 目标文档数量为 {len(docs)}，无法绑定")
    return docs[0]


def text_of(node):
    """Leaf text only: UIA often repeats a heading's text in its child."""
    if node.kind in ("ButtonControl", "EditControl", "ComboBoxControl", "MenuControl"):
        return ""
    if not node.children:
        return node.name if node.kind in ("TextControl", "HyperlinkControl") else ""
    return "\n".join(part for c in node.children if (part := text_of(c)))


def canonical(text):
    return "".join(text.replace("\u200b", "").replace("\ufeff", "").split())


def edit_value(control):
    value = control.GetPattern(auto.PatternId.ValuePattern)
    if value:
        return value.Value
    text = control.GetPattern(auto.PatternId.TextPattern)
    if text:
        return text.DocumentRange.GetText(-1)
    raise RuntimeError("输入控件没有可读的文本模式")


def unique(nodes, description):
    nodes = list(nodes)
    if len(nodes) != 1:
        raise RuntimeError(f"{description}: 找到 {len(nodes)} 个控件")
    return nodes[0]


def click(screen, node):
    rect = node.control.BoundingRectangle
    screen.click_xy((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)


def tabs(hwnd):
    root = tree(auto.ControlFromHandle(hwnd))
    groups = [n for n in root.walk() if n.kind == "TabControl"
              and any(c.kind == "TabItemControl" for c in n.children)]
    return max(([c for c in g.children if c.kind == "TabItemControl"] for g in groups),
               key=len, default=[])
