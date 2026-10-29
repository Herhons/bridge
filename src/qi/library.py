"""资源库：只管"有哪些图、缺哪些图"。

按键精灵的资源库就是一堆图片。这里额外做一件事：
**开跑前先报缺图**，而不是跑到一半才 FileNotFoundError。
"""

from __future__ import annotations

from pathlib import Path

from .screen import LIBRARY


class Library:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else LIBRARY

    def names(self) -> list[str]:
        if not self.path.exists():
            return []
        return sorted(p.stem for p in self.path.glob("*.png"))

    def has(self, name: str) -> bool:
        return (self.path / (name if name.endswith(".png") else f"{name}.png")).exists()

    def path_of(self, name: str) -> Path:
        return self.path / (name if name.endswith(".png") else f"{name}.png")

    def missing(self, required: list[str]) -> list[str]:
        return [n for n in required if not self.has(n)]

    def report(self, required: list[str]) -> str:
        miss = self.missing(required)
        if not miss:
            return f"资源库就绪：{len(required)}/{len(required)} 张"
        lines = [f"资源库缺 {len(miss)}/{len(required)} 张："]
        lines += [f"  - {n}.png" for n in miss]
        lines.append("截法：python tools/snip.py --save <名字> --region x,y,w,h")
        return "\n".join(lines)
