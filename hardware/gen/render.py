# Картинки платы (PNG) для проверки и для README: вид сверху, снизу, сборочный чертёж.

import math

from PIL import Image, ImageDraw, ImageFont

from placement import W, H, ANTENNA_KEEPOUT

PX = 20  # пикселей на мм
M = 3    # поле вокруг платы, мм


def _font(size):
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            pass
    return ImageFont.load_default()


def P(x, y):
    return ((x + M) * PX, (y + M) * PX)


def pad_poly(pad, grow=0.0):
    w, h = pad.w + 2 * grow, pad.h + 2 * grow
    if pad.shape in ("circle",):
        return ("ellipse", (pad.x - w / 2, pad.y - h / 2, pad.x + w / 2, pad.y + h / 2))
    if pad.shape == "oval":
        r = min(w, h) / 2
        return ("rrect", (pad.x - w / 2, pad.y - h / 2, pad.x + w / 2, pad.y + h / 2), r)
    if pad.shape == "roundrect":
        r = min(pad.w, pad.h) * pad.rr + grow
        return ("rrect", (pad.x - w / 2, pad.y - h / 2, pad.x + w / 2, pad.y + h / 2), r)
    return ("rect", (pad.x - w / 2, pad.y - h / 2, pad.x + w / 2, pad.y + h / 2))


def draw_pad(d, pad, fill, grow=0.0):
    k = pad_poly(pad, grow)
    if k[0] == "ellipse":
        x1, y1, x2, y2 = k[1]
        d.ellipse([*P(x1, y1), *P(x2, y2)], fill=fill)
    elif k[0] == "rrect":
        x1, y1, x2, y2 = k[1]
        d.rounded_rectangle([*P(x1, y1), *P(x2, y2)], radius=max(k[2] * PX, 0), fill=fill)
    else:
        x1, y1, x2, y2 = k[1]
        d.rectangle([*P(x1, y1), *P(x2, y2)], fill=fill)


def draw_seg(d, x1, y1, x2, y2, w, fill):
    d.line([P(x1, y1), P(x2, y2)], fill=fill, width=max(1, int(round(w * PX))))
    r = w / 2
    for x, y in ((x1, y1), (x2, y2)):
        d.ellipse([*P(x - r, y - r), *P(x + r, y + r)], fill=fill)


def outline(d, fill, width=2):
    from placement import CORNER_R as r
    d.rounded_rectangle([*P(0, 0), *P(W, H)], radius=r * PX, outline=fill, width=width)


def render(parts, tracks, vias, path, side="F", ratsnest=None, pour=None, labels=True, silk_items=None):
    img = Image.new("RGB", (int((W + 2 * M) * PX), int((H + 2 * M) * PX)), (24, 28, 32))
    d = ImageDraw.Draw(img)
    from placement import CORNER_R as r
    d.rounded_rectangle([*P(0, 0), *P(W, H)], radius=r * PX, fill=(18, 70, 40))
    if pour is not None:
        # заливка земли: готовая маска (numpy bool) на сетке PX
        import numpy as np
        mask = Image.fromarray((pour * 255).astype(np.uint8), "L")
        layer = Image.new("RGB", img.size, (40, 120, 60) if side == "F" else (40, 80, 130))
        img.paste(layer, (0, 0), mask)
        d = ImageDraw.Draw(img)
    x1, y1, x2, y2 = ANTENNA_KEEPOUT
    d.rectangle([*P(x1, y1), *P(x2, y2)], outline=(200, 80, 80), width=1)
    other = "B" if side == "F" else "F"
    col = {"F": (200, 60, 50), "B": (60, 110, 220)}
    # дорожки другого слоя — бледно
    for t in tracks:
        if t["layer"] == other:
            draw_seg(d, *t["pts"], t["w"], tuple(int(c * 0.45) for c in col[other]))
    for t in tracks:
        if t["layer"] == side:
            draw_seg(d, *t["pts"], t["w"], col[side])
    for p in parts:
        for pad in p.pads:
            if pad.npth:
                continue
            if side in pad.layers:
                draw_pad(d, pad, (212, 175, 55))
    for v in vias:
        rr = v["d"] / 2
        d.ellipse([*P(v["x"] - rr, v["y"] - rr), *P(v["x"] + rr, v["y"] + rr)], fill=(190, 190, 190))
    for p in parts:
        for pad in p.pads:
            if pad.drill:
                dw, dh = pad.slot or (pad.drill, pad.drill)
                rr = min(dw, dh) / 2
                d.rounded_rectangle([*P(pad.x - dw / 2, pad.y - dh / 2), *P(pad.x + dw / 2, pad.y + dh / 2)],
                                    radius=rr * PX, fill=(10, 10, 10))
            elif pad.npth:
                rr = pad.w / 2
                d.ellipse([*P(pad.x - rr, pad.y - rr), *P(pad.x + rr, pad.y + rr)], fill=(10, 10, 10))
    for v in vias:
        rr = v["drill"] / 2
        d.ellipse([*P(v["x"] - rr, v["y"] - rr), *P(v["x"] + rr, v["y"] + rr)], fill=(10, 10, 10))
    if side == "F":
        for p in parts:
            for it in p.silk:
                if it[0] == "line":
                    d.line([P(it[1], it[2]), P(it[3], it[4])], fill=(235, 235, 235), width=max(1, int(it[5] * PX)))
                else:
                    _, cx, cy, rr, w = it
                    d.ellipse([*P(cx - rr, cy - rr), *P(cx + rr, cy + rr)], outline=(235, 235, 235), width=max(1, int(w * PX)))
        for seg in silk_items or []:
            d.line([P(seg[0], seg[1]), P(seg[2], seg[3])], fill=(235, 235, 235), width=max(1, int(seg[4] * PX)))
    if ratsnest:
        for a, b in ratsnest:
            d.line([P(a.x, a.y), P(b.x, b.y)], fill=(250, 250, 120), width=1)
    if labels:
        f = _font(int(0.9 * PX))
        for p in parts:
            if p.court:
                cx = (p.court[0] + p.court[2]) / 2
                cy = (p.court[1] + p.court[3]) / 2
                d.text(P(cx, cy), p.ref, fill=(120, 220, 255), font=f, anchor="mm")
    outline(d, (230, 210, 60))
    if side == "B":
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    img.save(path)
    return path


def render_assembly(parts, path, side="F"):
    """Сборочный чертёж одной стороны: контуры, обозначения, номиналы, первый вывод. Низ — как видно снизу (зеркально)."""
    global PX
    old = PX
    PX = 40
    mine = [p for p in parts if (p.side if p.smd else side) == side or p.sym == "MH"]
    img = Image.new("RGB", (int((W + 2 * M) * PX), int((H + 2 * M) * PX)), (255, 255, 255))
    d = ImageDraw.Draw(img)
    outline(d, (0, 0, 0), 3)
    for p in mine:
        for pad in p.pads:
            if not pad.npth and side in pad.layers:
                draw_pad(d, pad, (225, 225, 225))
            elif pad.npth:
                rr = pad.w / 2
                d.ellipse([*P(pad.x - rr, pad.y - rr), *P(pad.x + rr, pad.y + rr)], outline=(0, 0, 0), width=2)
        for it in p.fab or p.silk:
            if it[0] == "line":
                d.line([P(it[1], it[2]), P(it[3], it[4])], fill=(90, 90, 90), width=2)
            else:
                _, cx, cy, rr, w = it
                d.ellipse([*P(cx - rr, cy - rr), *P(cx + rr, cy + rr)], outline=(90, 90, 90), width=2)
        p1 = next((pd for pd in p.pads if pd.num == "1"), None)
        if p1 and (len(p.pads) > 2 or p.sym in ("LED", "D", "TVS", "CP")):
            rr = 0.35
            d.ellipse([*P(p1.x - rr, p1.y - rr), *P(p1.x + rr, p1.y + rr)], fill=(220, 0, 0))
    if side == "B":
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
        d = ImageDraw.Draw(img)

    def X(x):
        return W - x if side == "B" else x
    fr = _font(int(0.95 * PX))
    fs = _font(int(0.75 * PX))
    fv = _font(int(0.62 * PX))
    for p in mine:
        if not p.court or p.sym == "MH":
            continue
        cx = X((p.court[0] + p.court[2]) / 2)
        cy = (p.court[1] + p.court[3]) / 2
        small = (p.court[2] - p.court[0]) < 4 or (p.court[3] - p.court[1]) < 2.2
        d.text(P(cx, cy - (0.35 if small else 0.6)), p.ref, fill=(0, 0, 160), font=fs if small else fr, anchor="mm")
        d.text(P(cx, cy + (0.45 if small else 0.7)), p.value, fill=(150, 40, 0), font=fv, anchor="mm")
    d.text(P(W / 2, -1.5), "ВЕРХ (вид сверху)" if side == "F" else "НИЗ (вид снизу, зеркально)", fill=(0, 0, 0),
           font=fr, anchor="mm")
    img.save(path)
    PX = old
    return path
