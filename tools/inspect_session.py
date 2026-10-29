"""Read-only composer/reply check. Does not activate windows or print message content."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from qi.screen import Screen
from qi.conversation import Conversation
from qi import ui

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site")
    parser.add_argument("hwnd", type=int)
    args = parser.parse_args()
    screen = Screen(verbose=False)
    screen.bind(args.hwnd)
    view = Conversation(screen, args.site).read()
    print(json.dumps({"site": args.site, "editor": view.editor.name,
                      "draft_length": len(ui.canonical(view.value)), "busy": view.busy,
                      "reply_controls": len(view.copies)}, ensure_ascii=False))
