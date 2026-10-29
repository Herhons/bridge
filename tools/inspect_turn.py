"""Inspect only the current regression turn. Never enters or submits a prompt."""
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from qi import ui
from qi.browser import ensure_site
from qi.conversation import Conversation, prompt_nodes
from qi.screen import Screen

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("site")
parser.add_argument("--hwnd", type=int)
parser.add_argument("--token", required=True)
args = parser.parse_args()
hwnd = args.hwnd or ensure_site(args.site, continue_session=True, verbose=False)
screen = Screen(verbose=False)
screen.bind(hwnd)
view = Conversation(screen, args.site).read()
prompt = ("这是收发回归测试。不要调用工具、执行命令、访问文件或联网。\n"
          "请只回复下面的标记，不加解释：\n" + args.token)
nodes = list(view.root.walk())
matches = prompt_nodes(view, prompt)
url = urlsplit(view.url)
start = min((i for i, n in enumerate(nodes) if args.token in n.name), default=len(nodes)-45)
print(json.dumps({"url": url.scheme + "://" + url.netloc + url.path,
    "editor": view.editor.name, "value": view.value,
    "busy": view.busy, "matches": [{"key": n.key, "kind": n.kind} for n in matches],
    "copies": [{"key": n.key, "name": n.name} for n in view.copies],
    "tail": [{"i": i, "p": nodes.index(n.parent) if n.parent else -1,
              "kind": n.kind, "name": n.name[:170], "key": n.key}
             for i, n in enumerate(nodes) if i >= start-4]}, ensure_ascii=False))
