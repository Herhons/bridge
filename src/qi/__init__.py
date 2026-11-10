"""v0.0.9 内核：按图找图的 AI 客户端驱动。

    from qi import Screen, Library
    s = Screen()
    s.click_image("ds_composer")
    s.type_text("你好")
    s.click_image("ds_send_on")
"""

from .library import Library
from .screen import DPI_LEVEL, LIBRARY, ForegroundLost, Match, Screen

__all__ = ["Screen", "Match", "Library", "ForegroundLost", "DPI_LEVEL", "LIBRARY"]
