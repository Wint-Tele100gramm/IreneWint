# Опоры земли для зажатых земляных выводов.
#
# Трассировщик не знает, что выводы земли потом подключит заливка, и иногда обводит мелкие конденсаторы
# сигнальными дорожками со всех сторон — заливка там превращается в отрезанный островок. Для таких выводов
# ставим «опору»: переход 1,4/0,6 мм на сплошную землю нижнего слоя в свободном месте рядом (не под деталью),
# и при следующей трассировке вывод и опора — одна цепь, к опоре идёт короткая дорожка. Соседние зажатые
# выводы делят одну опору.

import math

from board import body_box
from check import _seg_dist, pad_dist
from placement import W, H, ANTENNA_KEEPOUT
from route import VIA, CLEARANCE

R = VIA[0] / 2
CLUSTER = 3.5          # выводы ближе этого — одна опора
SEARCH = 5.0           # где искать место для опоры, мм от выводов


def _clusters(pads):
    groups = []
    for pd in pads:
        hit = [g for g in groups if any(math.hypot(pd.x - o.x, pd.y - o.y) < CLUSTER for o in g)]
        merged = [pd] + [o for g in hit for o in g]
        groups = [g for g in groups if g not in hit] + [merged]
    return groups


def _blocked(parts, x, y, taken):
    if not (R + 0.6 < x < W - R - 0.6 and R + 0.6 < y < H - R - 0.6):
        return True
    kx1, ky1, kx2, ky2 = ANTENNA_KEEPOUT
    if kx1 - R - 0.5 < x and y < ky2 + R + 0.5:
        return True
    for ax, ay in taken:
        if math.hypot(x - ax, y - ay) < 2 * R + CLEARANCE + 0.6:
            return True
    for p in parts:
        b = body_box(p)
        if b and b[0] == "rect":
            _, x1, y1, x2, y2 = b
            if x1 - R - 0.2 < x < x2 + R + 0.2 and y1 - R - 0.2 < y < y2 + R + 0.2:
                return True
        elif b:
            _, cx, cy, r = b
            if math.hypot(x - cx, y - cy) < r + R + 0.2:
                return True
        for pd in p.pads:
            if pd.npth:
                if math.hypot(x - pd.x, y - pd.y) < pd.w / 2 + R + 0.5:
                    return True
                continue
            need = R + (0.25 if pd.net == "GND" else CLEARANCE + 0.1)
            if abs(x - pd.x) < 4 and abs(y - pd.y) < 4 and pad_dist(pd, x, y, x, y) < need:
                return True
    return False


def find(parts, orphan_pads, tracks=(), vias=(), taken=()):
    """Опоры для зажатых выводов: [{"x", "y", "pads": [[ref, num], ...]}]. Место выбираем свободное и в
    текущей разводке (чтобы трассировке почти ничего не пришлось переделывать), иначе — ближайшее к выводам."""
    taken = list(taken)
    out = []
    sig = [t for t in tracks if t["net"] != "GND"]
    for g in _clusters(orphan_pads):
        cx = sum(p.x for p in g) / len(g)
        cy = sum(p.y for p in g) / len(g)
        best = None
        n = int(SEARCH / 0.2)
        for i in range(-n, n + 1):
            for j in range(-n, n + 1):
                x, y = cx + i * 0.2, cy + j * 0.2
                dist = max(math.hypot(x - p.x, y - p.y) for p in g)
                if dist > SEARCH or _blocked(parts, x, y, taken):
                    continue
                free = min([_seg_dist(x, y, *t["pts"]) - t["w"] / 2 for t in sig] +
                           [math.hypot(x - v["x"], y - v["y"]) - v["d"] / 2 for v in vias if v["net"] != "GND"] + [9])
                score = dist + (0 if free >= R + CLEARANCE else 6)
                if best is None or score < best[0]:
                    best = (score, x, y)
        if best:
            taken.append((best[1], best[2]))
            out.append({"x": round(best[1], 2), "y": round(best[2], 2), "pads": [[p.ref, p.num] for p in g]})
    return out
