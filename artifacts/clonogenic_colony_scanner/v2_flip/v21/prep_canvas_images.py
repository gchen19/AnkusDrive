"""Downsample renders to <=70 KB JPEGs for the design canvas."""
import sys, os
from PIL import Image
src = sys.argv[1]; dst = sys.argv[2]; os.makedirs(dst, exist_ok=True)
for n in ["hero", "front", "exploded", "section", "loading", "flipped", "camera", "inside", "hinge", "rear", "panel", "wiring", "cables"]:
    p = os.path.join(src, n + ".png")
    if not os.path.exists(p): print("missing", n); continue
    im = Image.open(p).convert("RGB"); im.thumbnail((1100, 1100))
    q = 82
    while True:
        out = os.path.join(dst, n + ".jpg"); im.save(out, "JPEG", quality=q, optimize=True, progressive=True)
        if os.path.getsize(out) <= 70000 or q <= 40: break
        q -= 6
    print(n, im.size, os.path.getsize(out) // 1024, "KB", "q", q)
