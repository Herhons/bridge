"""Select an existing site's tab and inspect its named controls. Never submits text."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from qi.browser import ensure_site
from qi import ui
from qi.screen import Screen
from qi.conversation import Conversation

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", choices=ui.HOSTS)
    args = parser.parse_args()
    hwnd = ensure_site(args.site, continue_session=True, timeout=10, verbose=False)
    if hwnd is None:
        raise SystemExit("没有唯一的现有目标标签页")
    screen = Screen(verbose=False)
    screen.bind(hwnd)
    view = Conversation(screen, args.site).read()
    nodes = [n for n in view.root.walk() if n.kind in
             ("ButtonControl", "EditControl", "ComboBoxControl", "HyperlinkControl")]
    print(json.dumps({"site": args.site, "editor": view.editor.name,
                      "busy": view.busy, "reply_controls": len(view.copies),
                      "controls": [{"type": n.kind, "name": n.name[:140]}
                                   for n in nodes if not n.control.IsOffscreen]},
                     ensure_ascii=False))
