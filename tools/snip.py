"""截模板图：全屏截一张，按区域裁出来存进资源库。

用法：
    python tools/snip.py --shot
        存一张全屏图到 evidence/，用来看坐标（浏览器或看图软件里量一下）

    python tools/snip.py --save ds_composer --region 400,700,300,60
        从当前屏幕现截，裁出这块，存成 src/assets/library/ds_composer.png

    python tools/snip.py --save ds_send_on --from evidence/shot-xxx.png --region ...
        从已有的全屏图里裁（不重新截屏）

裁完会打印这块的尺寸和像素方差：方差接近 0 说明裁到了纯色块，当模板没用。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import Library, Screen  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", action="store_true", help="只截全屏，存 evidence/")
    ap.add_argument("--save", help="存成资源库里这个名字")
    ap.add_argument("--region", help="x,y,w,h（屏幕绝对坐标）")
    ap.add_argument("--from", dest="src", help="从这张已有的全屏图里裁")
    args = ap.parse_args()

    if args.shot and not args.save:
        p = Screen(verbose=False).snapshot("shot")
        print(f"全屏图: {p}")
        return 0

    if not args.save or not args.region:
        ap.print_help()
        return 2

    x, y, w, h = (int(v) for v in args.region.split(","))
    if args.src:
        img = cv2.imread(str(ROOT / args.src))
        if img is None:
            print(f"读不出这张图: {args.src}")
            return 2
    else:
        s = Screen(verbose=False)
        vx, vy, _, _ = s.bounds()
        img = s.grab((x, y, w, h))
        x, y = 0, 0  # 已经按区域截了，不再偏移

    crop = img[y:y + h, x:x + w]
    if crop.size == 0:
        print(f"区域越界: region={args.region} 图={img.shape[1]}x{img.shape[0]}")
        return 2

    lib = Library()
    out = lib.path_of(args.save)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), crop)
    std = float(crop.std())
    print(f"已存 {out.name}  {crop.shape[1]}x{crop.shape[0]}  方差={std:.1f}")
    if std < 2.0:
        print("  ！方差很低，这块几乎是纯色，当模板会到处误命中")
    print(f"资源库现有 {len(lib.names())} 张: {', '.join(lib.names())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
