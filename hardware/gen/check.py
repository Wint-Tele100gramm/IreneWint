# Проверки платы без KiCad: связность цепей (все ли выводы соединены дорожками) и зазоры между разными цепями.

import math

EPAD = {}      # центр сетки площадок «41» модуля (заполняет set_parts)


def set_parts(parts):
    for p in parts:
        if p.ref == "U1":
            pads = [pd for pd in p.pads if pd.num == "41"]
            EPAD["U1"] = (sum(pd.x for pd in pads) / len(pads), sum(pd.y for pd in pads) / len(pads))


def _pad_contains(pad, x, y, layer, tol=0.02):
    if layer not in pad.layers or pad.npth:
        return False
    if pad.ref == "U1" and pad.num == "41":
        # центральная площадка модуля: трассировщик видел её как один квадрат 3,7 мм
        cx, cy = EPAD.get("U1", (pad.x, pad.y))
        return abs(x - cx) <= 1.85 + tol and abs(y - cy) <= 1.85 + tol
    if pad.shape in ("circle", "oval"):
        return _capsule_dist(pad, x, y) <= tol
    return abs(x - pad.x) <= pad.w / 2 + tol and abs(y - pad.y) <= pad.h / 2 + tol


def _capsule_dist(pad, x, y):
    """Расстояние от точки до круглой/овальной площадки (<= 0 — внутри)."""
    r = min(pad.w, pad.h) / 2
    lx = (pad.w / 2 - r)
    ly = (pad.h / 2 - r)
    return _seg_dist(x, y, pad.x - lx, pad.y - ly, pad.x + lx, pad.y + ly) - r


def _seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / L))
    return math.hypot(px - x1 - t * dx, py - y1 - t * dy)


def _seg_seg(a, b):
    (ax1, ay1, ax2, ay2), (bx1, by1, bx2, by2) = a, b

    def ccw(x1, y1, x2, y2, x3, y3):
        return (y3 - y1) * (x2 - x1) - (y2 - y1) * (x3 - x1)
    d1 = ccw(bx1, by1, bx2, by2, ax1, ay1)
    d2 = ccw(bx1, by1, bx2, by2, ax2, ay2)
    d3 = ccw(ax1, ay1, ax2, ay2, bx1, by1)
    d4 = ccw(ax1, ay1, ax2, ay2, bx2, by2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 0.0
    return min(_seg_dist(ax1, ay1, *b), _seg_dist(ax2, ay2, *b), _seg_dist(bx1, by1, *a), _seg_dist(bx2, by2, *a))


class DSU:
    def __init__(self):
        self.p = {}

    def f(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def u(self, a, b):
        self.p[self.f(a)] = self.f(b)


def connectivity(parts, tracks, vias, nets):
    """Вернуть {цепь: число несвязанных групп - 1} для цепей, которые соединены не полностью."""
    set_parts(parts)
    pads = [pd for p in parts for pd in p.pads if pd.net]
    by_net = {}
    for i, pd in enumerate(pads):
        by_net.setdefault(pd.net, []).append(("p", i))
    d = DSU()
    # одинаковые номера выводов одной детали соединены внутри детали (кнопки, экраны USB, площадки модуля)
    first = {}
    for i, pd in enumerate(pads):
        k = (pd.ref, pd.num)
        if k in first:
            d.u(("p", i), ("p", first[k]))
        else:
            first[k] = i
    for i, a in enumerate(pads):
        for j in range(i + 1, len(pads)):
            b = pads[j]
            if a.ref == b.ref and a.net == b.net and abs(a.x - b.x) < 0.01 and abs(a.y - b.y) < 0.01:
                d.u(("p", i), ("p", j))
    tr = [t for t in tracks]
    for ti, t in enumerate(tr):
        x1, y1, x2, y2 = t["pts"]
        d.f(("t", ti))
        for i, pd in enumerate(pads):
            if pd.net != t["net"]:
                continue
            if _pad_contains(pd, x1, y1, t["layer"]) or _pad_contains(pd, x2, y2, t["layer"]):
                d.u(("t", ti), ("p", i))
    # дорожка к дорожке: общий конец
    ends = {}
    for ti, t in enumerate(tr):
        x1, y1, x2, y2 = t["pts"]
        for x, y in ((x1, y1), (x2, y2)):
            key = (t["net"], t["layer"], round(x, 2), round(y, 2))
            if key in ends:
                d.u(("t", ti), ends[key])
            else:
                ends[key] = ("t", ti)
    for ti, t in enumerate(tr):
        for tj, o in enumerate(tr):
            if ti == tj or o["net"] != t["net"] or o["layer"] != t["layer"]:
                continue
            x1, y1, x2, y2 = t["pts"]
            for x, y in ((x1, y1), (x2, y2)):
                if _seg_dist(x, y, *o["pts"]) < 0.01:
                    d.u(("t", ti), ("t", tj))
    for vi, v in enumerate(vias):
        d.f(("v", vi))
        for ti, t in enumerate(tr):
            if t["net"] != v["net"]:
                continue
            x1, y1, x2, y2 = t["pts"]
            if math.hypot(x1 - v["x"], y1 - v["y"]) < 0.02 or math.hypot(x2 - v["x"], y2 - v["y"]) < 0.02:
                d.u(("v", vi), ("t", ti))
        for i, pd in enumerate(pads):
            if pd.net == v["net"] and (_pad_contains(pd, v["x"], v["y"], "F") or _pad_contains(pd, v["x"], v["y"], "B")):
                d.u(("v", vi), ("p", i))
    bad = {}
    for net, items in by_net.items():
        groups = {}
        for it in items:
            groups.setdefault(d.f(it), []).append(pads[it[1]])
        if len(groups) > 1:
            bad[net] = list(groups.values())
    return bad


def pad_gap(pad):
    """Нужный зазор от чужой дорожки/перехода до площадки: 0,4 мм, у выводов с мелким шагом — 0,2 мм."""
    from route import CLEARANCE, PAD_CLEARANCE, FINE_PARTS
    return PAD_CLEARANCE if pad.ref in FINE_PARTS else CLEARANCE


def pad_dist(pd, x1, y1, x2, y2):
    """Расстояние от отрезка до площадки (до её края)."""
    if pd.shape in ("circle", "oval"):
        r = min(pd.w, pd.h) / 2
        lx, ly = pd.w / 2 - r, pd.h / 2 - r
        return _seg_seg((x1, y1, x2, y2), (pd.x - lx, pd.y - ly, pd.x + lx, pd.y + ly)) - r
    hw, hh = pd.w / 2, pd.h / 2
    # отрезок против прямоугольника: пересечение — 0, иначе минимум по рёбрам
    corners = [(pd.x - hw, pd.y - hh), (pd.x + hw, pd.y - hh), (pd.x + hw, pd.y + hh), (pd.x - hw, pd.y + hh)]
    for x, y in ((x1, y1), (x2, y2)):
        if abs(x - pd.x) <= hw and abs(y - pd.y) <= hh:
            return 0.0
    best = 1e9
    for a, b in zip(corners, corners[1:] + corners[:1]):
        best = min(best, _seg_seg((x1, y1, x2, y2), (*a, *b)))
    return best


def track_gap(a, b):
    from route import CLEARANCE, DAC_GAP, DAC_NETS
    dac = DAC_NETS | {"GND"}        # земля ЦАП под корпусом идёт между его выводами
    return DAC_GAP if (a in dac and b in dac and (a in DAC_NETS or b in DAC_NETS)) else CLEARANCE


def clearances(parts, tracks, vias, tol=0.005):
    """Нарушения зазоров между разными цепями: дорожка-площадка, дорожка-дорожка, переход-всё.
    Земля (GND) — тоже цепь: её дорожки и переходы проверяются наравне."""
    errs = []
    pads = [pd for p in parts for pd in p.pads if not pd.npth]
    holes = [pd for p in parts for pd in p.pads if pd.npth]
    for t in tracks:
        x1, y1, x2, y2 = t["pts"]
        bx1, bx2 = min(x1, x2) - 4, max(x1, x2) + 4
        by1, by2 = min(y1, y2) - 4, max(y1, y2) + 4
        for pd in pads:
            if pd.net == t["net"] or t["layer"] not in pd.layers:
                continue
            if not (bx1 < pd.x < bx2 and by1 < pd.y < by2):
                continue
            g = pad_dist(pd, x1, y1, x2, y2) - t["w"] / 2
            if g < pad_gap(pd) - tol:
                errs.append(("track-pad", t["net"], f"{pd.ref}.{pd.num}", round(g, 3), (round(x1, 2), round(y1, 2))))
        for pd in holes:
            g = _seg_dist(pd.x, pd.y, x1, y1, x2, y2) - pd.w / 2 - t["w"] / 2
            if g < 0.4 - tol:
                errs.append(("track-hole", t["net"], pd.ref, round(g, 3), (round(x1, 2), round(y1, 2))))
    for i, a in enumerate(tracks):
        for b in tracks[i + 1:]:
            if a["net"] == b["net"] or a["layer"] != b["layer"]:
                continue
            g = _seg_seg(a["pts"], b["pts"]) - a["w"] / 2 - b["w"] / 2
            if g < track_gap(a["net"], b["net"]) - tol:
                errs.append(("track-track", a["net"], b["net"], round(g, 3), tuple(round(v, 2) for v in a["pts"][:2])))
    from route import CLEARANCE
    for v in vias:
        for t in tracks:
            if t["net"] == v["net"]:
                continue
            g = _seg_dist(v["x"], v["y"], *t["pts"]) - v["d"] / 2 - t["w"] / 2
            if g < CLEARANCE - tol:
                errs.append(("via-track", v["net"], t["net"], round(g, 3), (round(v["x"], 2), round(v["y"], 2))))
        for pd in pads:
            if pd.net == v["net"]:
                continue
            g = pad_dist(pd, v["x"], v["y"], v["x"], v["y"]) - v["d"] / 2
            if g < CLEARANCE - tol:
                errs.append(("via-pad", v["net"], f"{pd.ref}.{pd.num}", round(g, 3), (round(v["x"], 2), round(v["y"], 2))))
    for i, a in enumerate(vias):
        for b in vias[i + 1:]:
            if a["net"] != b["net"] and math.hypot(a["x"] - b["x"], a["y"] - b["y"]) - (a["d"] + b["d"]) / 2 < CLEARANCE - tol:
                errs.append(("via-via", a["net"], b["net"], 0, (round(a["x"], 2), round(a["y"], 2))))
    return errs


def vias_under_parts(parts, vias, margin=0.0):
    """Переходы под корпусами деталей."""
    from board import body_box
    bad = []
    for v in vias:
        r = v["d"] / 2
        for p in parts:
            b = body_box(p)
            if not b:
                continue
            if b[0] == "rect":
                _, x1, y1, x2, y2 = b
                dx = max(x1 - v["x"], 0, v["x"] - x2)
                dy = max(y1 - v["y"], 0, v["y"] - y2)
                if math.hypot(dx, dy) < r + margin - 1e-6:
                    bad.append((p.ref, round(v["x"], 2), round(v["y"], 2)))
            elif math.hypot(v["x"] - b[1], v["y"] - b[2]) < b[3] + r + margin:
                bad.append((p.ref, round(v["x"], 2), round(v["y"], 2)))
    return bad
