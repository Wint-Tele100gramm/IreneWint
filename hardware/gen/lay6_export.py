# Плата в формате Sprint Layout 6 (.lay6): детали (компоненты с подписью и номиналом), площадки, дорожки,
# переходные отверстия, шелкография, контур, запрет заливки под антенной.
#
# Заливка земли в Sprint Layout — своя («Массив» / ground plane): включена на обоих слоях, каждый объект
# хранит зазор до неё. У объектов цепи GND зазор 0 — они сливаются с массивом (дорожки, переходы,
# SMD-выводы), выводы в отверстиях подключаются термобарьером 0,5 мм. У остальных — зазор 0,4 мм, как в
# Gerber. Sprint Layout считает массив сам: результат близок к нашей заливке, но островки он не удаляет —
# после открытия стоит посмотреть.
#
# Координаты Sprint Layout: 1/10000 мм, ось Y вверх, плата занимает x 0…size_x, y −size_y…0.

import math

import lay6
from gerber import POUR_CLEAR, THERMAL_W, board_outline_pts, text_strokes
from placement import W, H, ANTENNA_KEEPOUT

U = 10000.0                    # единиц Sprint Layout в 1 мм
PAD_MAGIC = bytes.fromhex("00000015cd5b070000000000000000000000")   # так у всех площадок в файлах Sprint Layout
SPOKES_HV = 0x55555555         # термобарьер: перемычки по горизонтали и вертикали на всех слоях
CLEAR = int(round(POUR_CLEAR * U))


def X(x):
    return float(round(x * U, 1))


def Y(y):
    return float(round(-y * U, 1))


def P(x, y):
    return (X(x), Y(y))


def _obj(type_, layer, **kw):
    o = {"type": type_, "layer": layer, "x": 0.0, "y": 0.0, "outer": 0.0, "inner": 0.0, "line_width": 0,
         "tht_shape": 0, "component_id": 0, "start_angle": 0, "filled": 0, "clearance": CLEAR,
         "thermal_width": 0, "mirror": 0, "keepout": 0, "rotation": 0, "plated": 0, "soldermask": 0,
         "text": "", "net": "", "groups": []}
    o.update(kw)
    return o


def line(layer, pts, w, **kw):
    return _obj(lay6.LINE, layer, line_width=int(round(w * U)), points=[P(x, y) for x, y in pts], **kw)


def tht(x, y, d, drill, square=False, plated=True, gnd=False, thermal=False, mask=True, comp=0):
    o = d / 2 * U
    if square:
        pts = [P(x - d / 2, y + d / 2), P(x - d / 2, y - d / 2), P(x + d / 2, y - d / 2), P(x + d / 2, y + d / 2)]
    else:
        pts = [P(x, y + d / 2), P(x, y - d / 2)]
    kw = dict(x=X(x), y=Y(y), outer=o, inner=drill / 2 * U, tht_shape=3 if square else 1, start_angle=SPOKES_HV,
              rotation=int(round(THERMAL_W * 200)), plated=1 if plated else 0, soldermask=1 if mask else 0,
              unk3=PAD_MAGIC, points=pts, component_id=comp)
    if gnd:
        if thermal:
            kw.update(clearance=CLEAR, thermal_width=1)       # термобарьер, как в Gerber
        else:
            kw.update(clearance=0)                            # сплошное соединение с массивом
    return _obj(lay6.THT_PAD, lay6.C1, **kw)


def smd(pad, layer, comp=0, local=(0.0, 0.0)):
    x1, y1, x2, y2 = pad.x - pad.w / 2, pad.y - pad.h / 2, pad.x + pad.w / 2, pad.y + pad.h / 2
    # первая сторона (точки 0→1) — ширина outer, вторая — высота inner
    pts = [P(x1, y2), P(x2, y2), P(x2, y1), P(x1, y1)]
    return _obj(lay6.SMD_PAD, layer, x=local[0], y=local[1], outer=pad.w * U, inner=pad.h * U, start_angle=85,
                rotation=100, soldermask=1, unk3=PAD_MAGIC, points=pts, component_id=comp,
                clearance=0 if pad.net == "GND" else CLEAR)


def text(s, x, y, h, layer, mirror=False, center=False, visible=True, comp=0, kind=0):
    """Надпись: сам текст и его штрихи (Sprint Layout хранит их готовыми) нашим шрифтом шелкографии."""
    strokes = text_strokes(s, x, y, h, center=center, mirror=mirror) if s else []
    if strokes:
        xs = [v for a, b, c, d, w in strokes for v in (a, c)]
        left, base = min(xs), max(v for a, b, c, d, w in strokes for v in (b, d))
    else:
        left, base = x, y + h / 2
    children = [_obj(lay6.LINE, layer, line_width=int(round(w * U)), clearance=0, points=[P(a, b), P(c, d)])
                for a, b, c, d, w in strokes]
    if not children:      # пустой текст (скрытый номинал) — как в файлах Sprint Layout: одна точка
        children = [_obj(lay6.LINE, layer, line_width=int(round(max(0.15, h / 8) * U)), clearance=0,
                         points=[P(left, base), P(left, base)])]
    return _obj(lay6.TEXT, layer, x=X(left), y=Y(base), outer=h / 0.89 * U, inner=1.0, line_width=0,
                filled=1 if visible else 0, clearance=2000, thermal_width=1 if mirror else 0,
                tht_shape=kind, component_id=comp, text=s, children=children)


def build(parts, tracks, vias, stitch, silk_texts, name="Irene client v2"):
    objs = []
    # ---------------- контур платы
    outline = board_outline_pts(0, n=12)
    objs.append(line(lay6.O, outline + [outline[0]], 0.1, clearance=0))
    # ---------------- запрет массива под антенной (оба слоя)
    kx1, ky1, kx2, ky2 = ANTENNA_KEEPOUT
    rect = [(kx1, ky1), (kx2, ky1), (kx2, ky2), (kx1, ky2), (kx1, ky1)]
    for layer in (lay6.C1, lay6.C2):
        objs.append(_obj(lay6.POLY, layer, line_width=1000, rotation=1000, mirror=1, keepout=1, clearance=0,
                         points=[P(x, y) for x, y in rect]))
    # ---------------- детали
    for cid, p in enumerate(parts, 1):
        bottom = p.side == "B"
        cu = lay6.C2 if bottom else lay6.C1
        silk_side = (p.side if p.smd else "F")
        sl = lay6.S2 if silk_side == "B" else lay6.S1
        first = None
        for pad in p.pads:
            if pad.npth:
                objs.append(tht(pad.x, pad.y, pad.w, pad.w, plated=False, comp=cid))
            elif pad.drill:
                objs.append(tht(pad.x, pad.y, max(pad.w, pad.h), pad.drill, square=pad.shape == "rect",
                                gnd=pad.net == "GND", thermal=True, comp=cid))
            else:
                # SMD-выводы компонента: x, y — смещение от первого вывода в системе координат детали
                if first is None:
                    first = pad
                a = math.radians(p.rot)
                dx, dy = pad.x - first.x, -(pad.y - first.y)
                lx = dx * math.cos(a) + dy * math.sin(a)
                ly = -dx * math.sin(a) + dy * math.cos(a)
                objs.append(smd(pad, cu, cid, (X(lx), float(round(ly * U, 1)))))
        for it in p.silk:
            if it[0] == "line":
                objs.append(line(sl, [(it[1], it[2]), (it[3], it[4])], max(it[5], 0.15), component_id=cid))
            else:
                _, cx, cy, r, w = it
                w = max(w, 0.15)
                objs.append(_obj(lay6.CIRCLE, sl, x=X(cx), y=Y(cy), outer=(r + w / 2) * U, inner=(r - w / 2) * U,
                                 component_id=cid))
        # подпись (позиционное обозначение) и номинал компонента — скрытые, видны в свойствах детали
        ref = text(p.ref, p.x, p.y, 1.0, sl, mirror=silk_side == "B", center=True, visible=False, comp=cid, kind=1)
        ref["component"] = {"off_x": 0.0, "off_y": 0.0, "center_mode": 0, "rotation": float(p.rot % 360),
                            "package": p.fpkey, "comment": p.value or "", "use": 0}
        objs.append(ref)
        objs.append(text("", p.x, p.y, 1.0, sl, visible=False, comp=cid, kind=2))
    # ---------------- дорожки
    for t in tracks:
        x1, y1, x2, y2 = t["pts"]
        objs.append(line(lay6.C1 if t["layer"] == "F" else lay6.C2, [(x1, y1), (x2, y2)], t["w"],
                         clearance=0 if t["net"] == "GND" else CLEAR))
    # ---------------- переходные отверстия (закрыты маской); земляные — сплошь в массив
    for v in list(vias) + list(stitch):
        objs.append(tht(v["x"], v["y"], v["d"], v["drill"], gnd=v["net"] == "GND", mask=False))
    # ---------------- надписи на шелкографии
    for s, x, y, h, side, center in silk_texts:
        objs.append(text(s, x, y, h, lay6.S2 if side == "B" else lay6.S1, mirror=side == "B", center=center))

    board = {
        "name": lay6.fixed(name, 30), "unk1": b"\0" * 4, "size_x": int(round(W * U)), "size_y": int(round(H * U)),
        "ground_plane": bytes([1, 0, 1, 0, 0, 0, 0]),          # массив на C1 и C2
        "grid": 2500.0, "zoom": 0.0016, "view_x": 0, "view_y": 0, "active_layer": bytes([1, 0, 0, 0]),
        "layer_visible": bytes([1, 1, 1, 1, 0, 0, 1]), "scan_top": 0, "scan_bottom": 0,
        "scan_top_path": lay6.fixed("", 200), "scan_bottom_path": lay6.fixed("", 200),
        "scan_metrics": bytes.fromhex("5802000058020000") + b"\0" * 16,
        "unk2": bytes.fromhex("00ff000000ff0000"), "center_x": 0, "center_y": -1000000, "multilayer": 0,
        "objects": objs, "connections": [[] if o["type"] in (lay6.THT_PAD, lay6.SMD_PAD) else None for o in objs],
    }
    return {"version": 6, "boards": [board], "active_board": 0,
            "project_name": lay6.fixed(name, 100), "project_author": lay6.fixed("", 100),
            "project_company": lay6.fixed("", 100),
            "project_comment": "Irene voice assistant client: ESP32-S3 + PCM5102A + PAM8403 + 2x INMP441",
            "tail": b""}


def write(path, parts, tracks, vias, stitch, silk_texts):
    data = lay6.write(build(parts, tracks, vias, stitch, silk_texts))
    back = lay6.read(data)                       # самопроверка: файл читается тем же разбором, что и Sprint Layout
    assert lay6.write(back) == data
    with open(path, "wb") as f:
        f.write(data)
    return back
