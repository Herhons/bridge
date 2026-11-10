"""Read-only UI structure; omit document queries and long message contents."""
import argparse
import json
from urllib.parse import urlsplit

import uiautomation as auto


def inspect(hwnd):
    rows = []

    def walk(control, parent=-1):
        kind = control.ControlTypeName
        name = control.Name
        row = {"index": len(rows), "parent": parent, "type": kind,
               "name": name if len(name) < 180 else "<message content omitted>",
               "id": control.AutomationId}
        if kind == "DocumentControl":
            pattern = control.GetValuePattern()
            if pattern:
                url = urlsplit(pattern.Value)
                row["url"] = url.scheme + "://" + url.netloc + url.path
        rows.append(row)
        if kind not in ("MenuControl", "MenuBarControl"):
            for child in control.GetChildren():
                walk(child, row["index"])

    walk(auto.ControlFromHandle(hwnd))
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hwnd", type=int)
    parser.add_argument("--tail", type=int, default=140)
    parser.add_argument("--named", action="store_true")
    args = parser.parse_args()
    rows = inspect(args.hwnd)
    if args.named:
        rows = [r for r in rows if r["name"] or r.get("url")]
    print(json.dumps(rows[-args.tail:], ensure_ascii=False, indent=1))
