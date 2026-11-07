# Расширение дорожек после трассировки: каждую дорожку делаем как можно ближе к нужной ширине
# (сигнальные 0,6 мм, силовые 0,8 мм), пока зазоры до чужих цепей не меньше правил (0,4 мм; у выводов
# с мелким шагом — 0,2 мм). Дорожка, которая входит в вывод ЦАП или LP5907, не шире самого вывода.
# Ничего не ухудшаем: если зазор уже был меньше правила (узкое место у ЦАП), он не уменьшается.

import math

from check import _seg_dist, _seg_seg, pad_dist, pad_gap, track_gap
from route import CLEARANCE, FINE_PARTS, target_width

STEPS = (0.8, 0.7, 0.6, 0.5, 0.45, 0.4, 0.35, 0.3)


def _touches_fine_pad(t, pads):
    x1, y1, x2, y2 = t["pts"]
    lim = None
    for pd in pads:
        if pd.ref not in FINE_PARTS or pd.net != t["net"] or t["layer"] not in pd.layers:
            continue
        if pad_dist(pd, x1, y1, x2, y2) < 0.05:
            narrow = min(pd.w, pd.h)
            lim = narrow if lim is None else min(lim, narrow)
    return lim


def widen(parts, tracks, vias, holes_gap=0.4):
    pads = [pd for p in parts for pd in p.pads if not pd.npth]
    holes = [pd for p in parts for pd in p.pads if pd.npth]
    changed = 0
    for _ in range(2):                       # второй проход: соседи могли сузить место
        for i, t in enumerate(tracks):
            goal = target_width(t["net"])
            lim = _touches_fine_pad(t, pads)
            if lim is not None:
                goal = min(goal, lim)
            if t["w"] >= goal - 1e-6:
                continue
            x1, y1, x2, y2 = t["pts"]
            bx1, bx2 = min(x1, x2) - 3, max(x1, x2) + 3
            by1, by2 = min(y1, y2) - 3, max(y1, y2) + 3
            # расстояния до соседей от оси дорожки и нужные зазоры
            near = []
            for pd in pads:
                if pd.net == t["net"] or t["layer"] not in pd.layers or not (bx1 < pd.x < bx2 and by1 < pd.y < by2):
                    continue
                d = pad_dist(pd, x1, y1, x2, y2)
                near.append((d, pad_gap(pd)))
            for pd in holes:
                d = _seg_dist(pd.x, pd.y, x1, y1, x2, y2) - pd.w / 2
                near.append((d, holes_gap))
            for j, o in enumerate(tracks):
                if j == i or o["net"] == t["net"] or o["layer"] != t["layer"]:
                    continue
                ox1, oy1, ox2, oy2 = o["pts"]
                if max(ox1, ox2) < bx1 or min(ox1, ox2) > bx2 or max(oy1, oy2) < by1 or min(oy1, oy2) > by2:
                    continue
                d = _seg_seg(t["pts"], o["pts"]) - o["w"] / 2
                near.append((d, track_gap(t["net"], o["net"])))
            for v in vias:
                if v["net"] == t["net"]:
                    continue
                d = _seg_dist(v["x"], v["y"], x1, y1, x2, y2) - v["d"] / 2
                near.append((d, CLEARANCE))
            best = t["w"]
            for w in STEPS:
                if w > goal + 1e-6 or w <= best:
                    continue
                ok = True
                for d, need in near:
                    now = d - t["w"] / 2
                    req = min(need, now)          # уже узкое место — не хуже, чем было
                    if d - w / 2 < req - 1e-6:
                        ok = False
                        break
                if ok:
                    best = w
                    break
            if best > t["w"] + 1e-6:
                t["w"] = round(best, 3)
                changed += 1
    return changed


def report(tracks):
    from collections import Counter
    c = Counter()
    for t in tracks:
        L = math.dist(t["pts"][:2], t["pts"][2:])
        c[round(t["w"], 2)] += L
    total = sum(c.values()) or 1
    return {w: round(100 * L / total, 1) for w, L in sorted(c.items())}
