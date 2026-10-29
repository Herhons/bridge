"""只测模式读写，不发送任何消息。来回切几轮看读值稳不稳。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "scripts"))

from qi import Screen  # noqa: E402
import chatgpt_choose as C  # noqa: E402

s = Screen(verbose=False)
print("窗口:", s.window_rect("Google Chrome", on_secondary=True))
print("初始:", C.read_writer_gpt(s))

for target in ("chat", "work", "chat", "work"):
    r = C.choose_writer_gpt(s, target)
    time.sleep(0.4)
    again = C.read_writer_gpt(s)
    print(f"-> {target}: {r}  |  复读 {again['writer']} ink={again.get('ink')}")
