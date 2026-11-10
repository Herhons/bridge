"""Read-only UIA probe: list Chrome documents and optionally dump matching text nodes.

No clicks, no focus changes, no input injection, no window mutation — only reads the
accessibility tree and window titles.

    python tools/probe_uia_readonly.py                 # just list windows/docs
    python tools/probe_uia_readonly.py --dump 模型      # dump text nodes containing 模型
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
from ctypes import wintypes

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

user32 = ctypes.windll.user32


def titled_windows():
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            size = user32.GetWindowTextLengthW(hwnd)
            if size > 0:
                buf = ctypes.create_unicode_buffer(size + 1)
                user32.GetWindowTextW(hwnd, buf, size + 1)
                out.append((int(hwnd), buf.value))
        return True

    user32.EnumWindows(_cb, 0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", default=None, help="dump text nodes whose name contains this")
    ap.add_argument("--title", default="chrome", help="window title substring to inspect")
    args = ap.parse_args()

    from qi import ui

    for hwnd, title in titled_windows():
        if args.title and args.title.lower() not in title.lower():
            continue
        print(f"[win] hwnd={hwnd} title={title!r}")
        try:
            docs = ui.documents(hwnd)
        except Exception as exc:                # noqa: BLE001
            print(f"  documents failed: {type(exc).__name__}: {exc}")
            continue
        print(f"  docs={len(docs)}")
        for control, url in docs:
            print(f"   - url={url!r}")
            if args.dump is None:
                continue
            try:
                root = ui.tree(control)
            except Exception as exc:            # noqa: BLE001
                print(f"     tree failed: {exc}")
                continue
            hits = 0
            for node in root.walk():
                name = (node.name or "").strip()
                if name and args.dump.lower() in name.lower():
                    hits += 1
                    if hits <= 40:
                        print(f"     [{node.kind}] {name[:140]!r}")
            print(f"     hits={hits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
