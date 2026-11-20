"""Screenshot template matching fallback for Bridge v0.2.1.

Strictly secondary to UIA: missing template PNGs never abort startup or fail
a run — they simply return ``None`` so UIA remains the primary path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from engine.config import TEMPLATES_DIR


@dataclass
class Match:
    template: str
    x: int
    y: int
    w: int
    h: int
    score: float
    scale: float = 1.0

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)


def capture_window(hwnd: int):
    """Capture window image via PrintWindow (works even when covered on Windows)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        import numpy as np

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32),
                ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]

        rect = RECT()
        if not user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
            return None
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None

        hwnd_dc = user32.GetWindowDC(int(hwnd))
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        old = gdi32.SelectObject(mem_dc, bmp)
        try:
            # PW_RENDERFULLCONTENT = 2 captures Chromium/Electron hardware surfaces
            user32.PrintWindow(int(hwnd), mem_dc, 2)
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h  # top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
            arr = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4))
            return arr[:, :, :3].copy(), (rect.left, rect.top, w, h)
        finally:
            gdi32.SelectObject(mem_dc, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(int(hwnd), hwnd_dc)
    except Exception:
        return None


def match_template(
    image,
    template_name: str,
    *,
    threshold: float = 0.85,
    scales: tuple[float, ...] = (1.0, 0.8, 1.25),
    origin: tuple[int, int] = (0, 0),
    templates_dir: Path | None = None,
) -> Match | None:
    """Match a template PNG inside *image*. Returns ``None`` if template is absent or below threshold."""
    if image is None or not template_name:
        return None
    tdir = templates_dir or TEMPLATES_DIR
    tpl_path = Path(template_name)
    if not tpl_path.is_file():
        tpl_path = tdir / (template_name if template_name.endswith(".png") else f"{template_name}.png")
    if not tpl_path.is_file():
        return None

    try:
        import cv2
    except ImportError:
        return None

    tpl = cv2.imread(str(tpl_path), cv2.IMREAD_COLOR)
    if tpl is None:
        return None

    best: Match | None = None
    ih, iw = image.shape[:2]
    for scale in scales:
        tw = max(1, int(round(tpl.shape[1] * scale)))
        th = max(1, int(round(tpl.shape[0] * scale)))
        if tw >= iw or th >= ih:
            continue
        scaled = tpl if scale == 1.0 else cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(image, scaled, cv2.TM_CCOEFF_NORMED)
        _min_v, max_v, _min_loc, max_loc = cv2.minMaxLoc(res)
        score = float(max_v)
        if score >= threshold and (best is None or score > best.score):
            best = Match(
                template=template_name,
                x=int(origin[0] + max_loc[0]),
                y=int(origin[1] + max_loc[1]),
                w=tw,
                h=th,
                score=round(score, 4),
                scale=scale,
            )
    return best
