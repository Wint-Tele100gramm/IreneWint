#!/usr/bin/env python3
"""Картинка файла Sprint Layout 6 так, как его поймёт Sprint Layout (по разбору формата из KiCad):
медь сверху/снизу с имитацией массива (заливки земли с зазорами объектов), шелкография, контур.

    python3 lay6_view.py ../irene-client/irene-client.lay6 out-top.png out-bottom.png
"""

import sys

from PIL import Image, ImageDraw

import lay6

S = 20          # пикселей на мм


def view(path, side):
    f = lay6.read(open(path, "rb").read())
    b = f["boards"][0]
    w, h = b["size_x"] / 1e4, b["size_y"] / 1e4
    cu = lay6.C1 if side == "F" else lay6.C2
    silk = lay6.S1 if side == "F" else lay6.S2
    pt = (lambda x, y: (x / 1e4 * S, -y / 1e4 * S)) if side == "F" else (lambda x, y: ((w - x / 1e4) * S, -y / 1e4 * S))
    img = Image.new("RGB", (int(w * S), int(h * S)), (20, 60, 20))
    plane = Image.new("L", img.size, 0)
    dp, dk = ImageDraw.Draw(plane), None
    gp = b["ground_plane"][0 if side == "F" else 2]
    if gp:
        dp.rectangle([0, 0, img.size[0], img.size[1]], fill=255)
    objs = b["objects"]

    def on(o):
        return o["layer"] == cu or (o["type"] == lay6.THT_PAD and o["plated"])

    def shape(d, o, grow, fill):
        g = grow * S
        if o["type"] == lay6.LINE:
            pts = [pt(*p) for p in o["points"]]
            lw = o["line_width"] / 1e4 * S + 2 * g
            d.line(pts, fill=fill, width=max(1, int(round(lw))))
            for p in pts:
                d.ellipse([p[0] - lw / 2, p[1] - lw / 2, p[0] + lw / 2, p[1] + lw / 2], fill=fill)
        elif o["type"] == lay6.SMD_PAD:
            xs = [pt(*p)[0] for p in o["points"]]
            ys = [pt(*p)[1] for p in o["points"]]
            d.rectangle([min(xs) - g, min(ys) - g, max(xs) + g, max(ys) + g], fill=fill)
        elif o["type"] == lay6.THT_PAD:
            cx, cy = pt(o["x"], o["y"])
            r = o["outer"] / 1e4 * S + g
            if o["tht_shape"] == 3:
                d.rectangle([cx - r, cy - r, cx + r, cy + r], fill=fill)
            else:
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)
        elif o["type"] == lay6.POLY:
            d.polygon([pt(*p) for p in o["points"]], fill=fill)

    # массив: вырезы вокруг объектов с зазором, запретные зоны
    for o in objs:
        if o["type"] == lay6.POLY and o["keepout"] and o["layer"] == cu:
            shape(dp, o, 0, 0)
        elif on(o) and o["type"] in (lay6.LINE, lay6.SMD_PAD, lay6.THT_PAD) and o["clearance"] > 0:
            shape(dp, o, o["clearance"] / 1e4, 0)
    # контур: массив только внутри платы, 0,4 мм от края (как в Gerber; в Sprint Layout — его настройка)
    img.paste((150, 110, 40), mask=plane)
    d = ImageDraw.Draw(img)
    for o in objs:
        if on(o) and o["type"] in (lay6.LINE, lay6.SMD_PAD, lay6.THT_PAD):
            shape(d, o, 0, (200, 150, 60))
    for o in objs:
        if o["type"] == lay6.THT_PAD:
            cx, cy = pt(o["x"], o["y"])
            r = o["inner"] / 1e4 * S
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 0, 0))
            if o["thermal_width"] and o["clearance"] and on(o) and gp:   # перемычки термобарьера
                R = (o["outer"] + o["clearance"]) / 1e4 * S
                tw = o["rotation"] / 200 * S
                d.line([cx - R, cy, cx + R, cy], fill=(200, 150, 60), width=int(tw))
                d.line([cx, cy - R, cx, cy + R], fill=(200, 150, 60), width=int(tw))
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 0, 0))
    for o in objs:
        lines = []
        if o["layer"] == silk and o["type"] == lay6.LINE:
            lines.append(o)
        if o["type"] == lay6.TEXT and o["layer"] == silk and (o["filled"] or not o["component_id"]):
            lines += o["children"]
        for l in lines:
            pts = [pt(*p) for p in l["points"]]
            d.line(pts, fill=(240, 240, 240), width=max(1, int(l["line_width"] / 1e4 * S)))
        if o["type"] == lay6.CIRCLE and o["layer"] == silk:
            cx, cy = pt(o["x"], o["y"])
            r = (o["outer"] + o["inner"]) / 2 / 1e4 * S
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(240, 240, 240), width=max(1, int((o["outer"] - o["inner"]) / 1e4 * S)))
        if o["type"] == lay6.LINE and o["layer"] == lay6.O:
            d.line([pt(*p) for p in o["points"]], fill=(255, 255, 0), width=2)
    return img


if __name__ == "__main__":
    src = sys.argv[1]
    view(src, "F").save(sys.argv[2])
    view(src, "B").save(sys.argv[3])
