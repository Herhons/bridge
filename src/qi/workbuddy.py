"""WorkBuddy controls identified by their accessible names."""
from __future__ import annotations

import argparse
import json
import time

from . import desk, guard, ui
from .conversation import Conversation, ask
from .screen import Screen


def open_app():
    hwnd = desk.ensure_app("workbuddy")
    if hwnd is None:
        raise RuntimeError("WorkBuddy 没有可操作的窗口")
    screen = Screen(verbose=False)
    screen.bind(hwnd)
    if not screen.focus():
        raise RuntimeError("WorkBuddy 窗口无法获得焦点")
    return screen


def controls(screen):
    doc, _ = ui.document(screen.target_hwnd, "workbuddy")
    return list(ui.tree(doc).walk())


def model_control(nodes):
    return ui.unique((n for n in nodes if n.kind == "ComboBoxControl"
                      and n.name.startswith("Select model:")), "WorkBuddy 模型控件")


def current_model(nodes):
    return model_control(nodes).name.removeprefix("Select model:").strip()


def select_model(screen, wanted):
    before = current_model(controls(screen))
    if before == wanted:
        return {"ok": True, "before": before, "after": before, "verified": True}
    ui.click(screen, model_control(controls(screen)))
    time.sleep(0.3)
    items = [n for n in controls(screen) if n.name == wanted
             and n.kind in ("MenuItemControl", "ListItemControl", "TextControl")
             and not n.control.IsOffscreen]
    leaves = [n for n in items if not any(c in items for c in n.children)]
    ui.click(screen, ui.unique(leaves, "目标模型标签"))
    time.sleep(0.4)
    after = current_model(controls(screen))
    return {"ok": after == wanted, "verified": after == wanted,
            "before": before, "after": after, "want": wanted}


def main(argv=None, *, sending=False):
    parser = argparse.ArgumentParser(description="WorkBuddy 模型选择 / 提问并读取本轮回复")
    if sending:
        parser.add_argument("prompt")
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--new", action="store_true")
        group.add_argument("--continue", dest="continuing", action="store_true")
        parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--idle", type=float, default=2)
    parser.add_argument("--wait", type=float, default=120)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.list and (args.model or sending):
        parser.error("--list 不能与模型修改或提问同用")
    if not sending and not (args.list or args.model):
        parser.print_help()
        return 2
    if not guard.guard(args.idle, args.wait, args.force):
        return 4
    screen = open_app()
    if sending and not args.continuing:
        new_chat(screen, "workbuddy")
    if args.list:
        print(json.dumps({"ok": True, "model": current_model(controls(screen))}, ensure_ascii=False))
        return 0
    if args.model:
        result = select_model(screen, args.model)
        print(json.dumps(result, ensure_ascii=False))
        if not result["ok"]:
            return 1
    if sending:
        result = ask(Conversation(screen, "workbuddy"), args.prompt, args.timeout)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    return 0


def new_chat(screen, site):
    labels = {"workbuddy": {"新建任务"}, "opencode": {"新建会话", "New session"},
              "dsh": {"新建会话", "New session"}, "codex": {"New chat", "新聊天"}}
    doc, _ = ui.document(screen.target_hwnd, site)
    nodes = [n for n in ui.tree(doc).walk() if n.name in labels[site]
             and n.kind in ("ButtonControl", "TabItemControl") and not n.control.IsOffscreen]
    # Some clients expose the same action in both sidebar and header.
    if not nodes:
        raise RuntimeError(f"{site}: 找不到新对话控件")
    ui.click(screen, nodes[0])
    time.sleep(0.5)
