#!/usr/bin/env python3
"""Картинка расстановки для проверки: курсы деталей, площадки, «резинки» связей (кроме земли), перекрытия.

    IRENE_PCB_EXT=... python3 plan.py out.png
"""

import math
import sys

from PIL import Image, ImageDraw, ImageFont

import board
from placement import W, H, ANTENNA_KEEPOUT

S = 16


def mst(pads):
    pts = [(p.x, p.y) for p in pads]
    if len(pts) < 2:
        return []
    used, edges = {0}, []
    while len(used) < len(pts):
        best = None
        for i in used:
            for j in range(len(pts)):
                if j in used:
                    continue
                d = math.dist(pts[i], pts[j])
                if best is None or d < best[0]:
                    best = (d, i, j)
        used.add(best[2])
        edges.append((pts[best[1]], pts[best[2]]))
    return edges


def main(out):
    parts = board.load_parts()
    m = 20
    img = Image.new("RGB", (int(W * S) + 2 * m, int(H * S) + 2 * m), "white")
    d = ImageDraw.Draw(img)
    P = lambda x, y: (m + x * S, m + y * S)
    d.rectangle([*P(0, 0), *P(W, H)], outline="black", width=2)
    kx1, ky1, kx2, ky2 = ANTENNA_KEEPOUT
    d.rectangle([*P(kx1, ky1), *P(kx2, ky2)], fill=(255, 230, 230))
    bad = {r for pair in board.check_overlaps(parts) for r in pair}
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 11)
    except OSError:
        font = None
    for p in parts:
        if p.court:
            x1, y1, x2, y2 = p.court
            d.rectangle([*P(x1, y1), *P(x2, y2)], outline="red" if p.ref in bad else (120, 120, 120), width=2 if p.ref in bad else 1)
            if x1 < 0 or y1 < 0 or x2 > W or y2 > H:
                d.rectangle([*P(x1, y1), *P(x2, y2)], outline="orange", width=3)
        elif p.court_circle:
            cx, cy, r = p.court_circle
            d.ellipse([*P(cx - r, cy - r), *P(cx + r, cy + r)], outline="gray")
        for pd in p.pads:
            col = (60, 160, 60) if pd.net == "GND" else ((200, 140, 40) if pd.net else (190, 190, 190))
            d.rectangle([*P(pd.x - pd.w / 2, pd.y - pd.h / 2), *P(pd.x + pd.w / 2, pd.y + pd.h / 2)], fill=col)
        d.text(P(p.x, p.y), p.ref, fill="blue", font=font, anchor="mm")
    for net, pads in board.nets_of(parts).items():
        if net == "GND":
            continue
        uniq = {}
        for pd in pads:
            uniq.setdefault((pd.ref, pd.num), pd)
        for a, b in mst(list(uniq.values())):
            d.line([P(*a), P(*b)], fill=(40, 40, 220), width=1)
    img.save(out)
    print("перекрытия:", board.check_overlaps(parts))


if __name__ == "__main__":
    main(sys.argv[1])
