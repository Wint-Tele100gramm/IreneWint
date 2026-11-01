# Независимая проверка готовых Gerber и сверловки: файлы разбираются заново (не из модели платы),
# медь растеризуется, и проверяется, что каждая цепь связна, а разные цепи не замкнуты.
# Заодно получаются картинки «как будет изготовлено».

import math
import re

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from placement import W, H

RES = 40   # пикселей на мм (0,025 мм) — зазор 0,2 мм = 8 пикселей


def _px(x, y):
    return (x * RES, (H - y) * RES)


def render_gerber(text):
    img = Image.new("L", (int(W * RES) + 2, int(H * RES) + 2), 0)
    d = ImageDraw.Draw(img)
    aps, cur, x, y = {}, None, 0.0, 0.0
    dark, region, pts = True, False, []
    for m in re.finditer(r"%ADD(\d+)([CRO]),([\d.X]+)\*%", text):
        aps[int(m.group(1))] = (m.group(2), [float(v) for v in m.group(3).split("X")])
    body = re.sub(r"%[^%]*%", lambda m: m.group(0) if m.group(0) in ("%LPD*%", "%LPC*%") else "", text)
    for cmd in re.findall(r"%LP[DC]\*%|[^*%\n]+\*", body):
        if cmd == "%LPD*%":
            dark = True
            continue
        if cmd == "%LPC*%":
            dark = False
            continue
        cmd = cmd.rstrip("*")
        col = 255 if dark else 0
        if cmd == "G36":
            region, pts = True, []
            continue
        if cmd == "G37":
            if len(pts) > 2:
                d.polygon([_px(*p) for p in pts], fill=col)
            region = False
            continue
        m = re.match(r"^D(\d+)$", cmd)
        if m and int(m.group(1)) >= 10:
            cur = int(m.group(1))
            continue
        m = re.match(r"^X(-?\d+)Y(-?\d+)D0([123])$", cmd)
        if not m:
            continue
        nx, ny, op = int(m.group(1)) / 1e6, int(m.group(2)) / 1e6, m.group(3)
        if region:
            if op == "2":
                if len(pts) > 2:
                    d.polygon([_px(*p) for p in pts], fill=col)
                pts = [(nx, ny)]
            else:
                pts.append((nx, ny))
        elif op == "1":
            kind, dims = aps[cur]
            w = dims[0]
            d.line([_px(x, y), _px(nx, ny)], fill=col, width=max(1, int(round(w * RES))))
            for px, py in ((x, y), (nx, ny)):
                d.ellipse([_px(px - w / 2, py + w / 2), _px(px + w / 2, py - w / 2)], fill=col)
        elif op == "3":
            kind, dims = aps[cur]
            if kind == "C":
                r = dims[0] / 2
                d.ellipse([_px(nx - r, ny + r), _px(nx + r, ny - r)], fill=col)
            elif kind == "R":
                w, h = dims
                d.rectangle([_px(nx - w / 2, ny + h / 2), _px(nx + w / 2, ny - h / 2)], fill=col)
            else:
                w, h = dims
                d.rounded_rectangle([_px(nx - w / 2, ny + h / 2), _px(nx + w / 2, ny - h / 2)], radius=min(w, h) / 2 * RES, fill=col)
        x, y = nx, ny
    return np.array(img) > 127


def read_drill(text):
    tools, holes, cur = {}, [], None
    for line in text.splitlines():
        m = re.match(r"^T(\d+)C([\d.]+)$", line)
        if m:
            tools[int(m.group(1))] = float(m.group(2))
            continue
        m = re.match(r"^T(\d+)$", line)
        if m:
            cur = int(m.group(1))
            continue
        m = re.match(r"^X([\d.\-]+)Y([\d.\-]+)(?:G85X([\d.\-]+)Y([\d.\-]+))?$", line)
        if m:
            x, y = float(m.group(1)), float(m.group(2))
            holes.append((tools[cur], x, y))
            if m.group(3):
                holes.append((tools[cur], float(m.group(3)), float(m.group(4))))
    return holes


def check(files, parts):
    """files: {имя слоя: текст}. Возвращает (ошибки, картинки слоёв)."""
    F = render_gerber(files["F_Cu"])
    B = render_gerber(files["B_Cu"])
    lf, nf = ndimage.label(F)
    lb, nb = ndimage.label(B)
    parent = {}

    def f(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def u(a, b):
        parent[f(a)] = f(b)

    def at(lab, x, y):
        px, py = int(round(x * RES)), int(round((H - y) * RES))
        return int(lab[py, px])
    for dia, x, y in read_drill(files["PTH"]):
        a, b = at(lf, x + dia / 2 + 0.05, y), at(lb, x + dia / 2 + 0.05, y)
        if a and b:
            u(("F", a), ("B", b))
    errs = []
    net_groups = {}
    same = {}
    for p in parts:
        for pad in p.pads:
            if pad.npth or not pad.net:
                continue
            for side, lab in (("F", lf), ("B", lb)):
                if side in pad.layers:
                    x, y = pad.x, H - pad.y
                    c = at(lab, x, y)
                    if not c:
                        errs.append(f"нет меди под выводом {pad.ref}.{pad.num}")
                        continue
                    k = (pad.ref, pad.num)
                    if k in same:
                        u(same[k], (side, c))   # выводы с одним номером соединены внутри детали
                    else:
                        same[k] = (side, c)
                    net_groups.setdefault(pad.net, set()).add((side, c))
    owner = {}
    for net, gs in net_groups.items():
        gs = {f(g) for g in gs}
        if len(gs) > 1:
            errs.append(f"цепь {net} разорвана на {len(gs)} части")
        for g in gs:
            if g in owner and owner[g] != net:
                errs.append(f"ЗАМЫКАНИЕ: {owner[g]} и {net}")
            owner[g] = net
    return errs, F, B


def picture(files, path, side="F"):
    """Картинка слоя из Gerber, как на заводе: маска зелёная, медь под маской темнее, открытая медь — золото, шелкография."""
    cu = render_gerber(files[f"{side}_Cu"])
    mask = render_gerber(files[f"{side}_Mask"])
    silk = render_gerber(files[f"{side}_Silk"])
    edge = render_gerber(files["Edge"])
    filled = ndimage.binary_fill_holes(ndimage.binary_dilation(edge, iterations=2))
    img = np.zeros(cu.shape + (3,), dtype=np.uint8)
    img[:] = (30, 30, 30)
    img[filled] = (20, 90, 45)
    img[filled & cu] = (40, 125, 60)
    img[cu & mask] = (205, 170, 60)
    img[silk & ~mask] = (245, 245, 245)
    for dia, x, y in read_drill(files["PTH"]) + read_drill(files["NPTH"]):
        yy, xx = np.ogrid[:cu.shape[0], :cu.shape[1]]
        r = dia / 2 * RES
        cx, cy = x * RES, (H - y) * RES
        sl = (slice(max(0, int(cy - r - 1)), int(cy + r + 2)), slice(max(0, int(cx - r - 1)), int(cx + r + 2)))
        sub = (yy[sl[0]] - cy) ** 2 + (xx[:, sl[1]] - cx) ** 2 <= r * r
        img[sl][sub] = (15, 15, 15)
    img[edge] = (230, 210, 60)
    im = Image.fromarray(img)
    if side == "B":
        im = im.transpose(Image.FLIP_LEFT_RIGHT)
    im = im.resize((im.width // 2, im.height // 2), Image.LANCZOS)
    im.save(path)
