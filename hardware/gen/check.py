# Проверки платы без KiCad: связность цепей (все ли выводы соединены дорожками) и зазоры между разными цепями.

import math


def _pad_contains(pad, x, y, layer, tol=0.02):
    if layer not in pad.layers or pad.npth:
        return False
    if pad.ref == "U1" and pad.num == "41":
        # центральная площадка модуля: трассировщик видел её как один квадрат 3,7 мм
        return abs(x - 67.5) <= 1.85 + tol and abs(y - 15.21) <= 1.85 + tol
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


PAD_GAP = 0.19   # дорожка — вывод (у TSSOP шаг 0,65 мм)


def clearances(parts, tracks, vias, min_gap=0.29):
    """Нарушения зазора дорожка-площадка, дорожка-дорожка, переход-всё между разными цепями."""
    errs = []
    pads = [pd for p in parts for pd in p.pads if not pd.npth]

    def pad_dist(pd, x1, y1, x2, y2):
        # расстояние от отрезка до прямоугольника площадки (приближённо: до прямоугольника)
        if pd.shape in ("circle", "oval"):
            r = min(pd.w, pd.h) / 2
            lx, ly = pd.w / 2 - r, pd.h / 2 - r
            return _seg_seg((x1, y1, x2, y2), (pd.x - lx, pd.y - ly, pd.x + lx, pd.y + ly)) - r
        hw, hh = pd.w / 2, pd.h / 2
        best = 1e9
        n = max(2, int(math.hypot(x2 - x1, y2 - y1) / 0.05) + 1)
        for i in range(n + 1):
            x = x1 + (x2 - x1) * i / n
            y = y1 + (y2 - y1) * i / n
            dx = max(abs(x - pd.x) - hw, 0)
            dy = max(abs(y - pd.y) - hh, 0)
            best = min(best, math.hypot(dx, dy))
        return best

    for t in tracks:
        x1, y1, x2, y2 = t["pts"]
        bx1, bx2 = min(x1, x2) - 3, max(x1, x2) + 3
        by1, by2 = min(y1, y2) - 3, max(y1, y2) + 3
        for pd in pads:
            if pd.net == t["net"] or t["layer"] not in pd.layers:
                continue
            if not (bx1 < pd.x < bx2 and by1 < pd.y < by2):
                continue
            g = pad_dist(pd, x1, y1, x2, y2) - t["w"] / 2
            if g < min(min_gap, PAD_GAP):
                errs.append(("track-pad", t["net"], f"{pd.ref}.{pd.num}", round(g, 3), (round(x1, 2), round(y1, 2))))
    for i, a in enumerate(tracks):
        for b in tracks[i + 1:]:
            if a["net"] == b["net"] or a["layer"] != b["layer"]:
                continue
            g = _seg_seg(a["pts"], b["pts"]) - a["w"] / 2 - b["w"] / 2
            if g < min_gap:
                errs.append(("track-track", a["net"], b["net"], round(g, 3), a["pts"][:2]))
    for v in vias:
        for t in tracks:
            if t["net"] == v["net"]:
                continue
            g = _seg_dist(v["x"], v["y"], *t["pts"]) - v["d"] / 2 - t["w"] / 2
            if g < min_gap:
                errs.append(("via-track", v["net"], t["net"], round(g, 3), (v["x"], v["y"])))
        for pd in pads:
            if pd.net == v["net"]:
                continue
            g = pad_dist(pd, v["x"], v["y"], v["x"], v["y"]) - v["d"] / 2
            if g < min(min_gap, PAD_GAP):
                errs.append(("via-pad", v["net"], f"{pd.ref}.{pd.num}", round(g, 3), (v["x"], v["y"])))
    return errs
