# Дотрассировка: соединения, которые Freerouting не довёл, прокладываются простым волновым трассировщиком (A*)
# по сетке 0,05 мм на двух слоях, с теми же зазорами. Подходит для коротких связей, которые остались в конце.

import heapq
import math

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from placement import W, H, ANTENNA_KEEPOUT

from route import CLEARANCE, PAD_CLEARANCE, FINE_PARTS, VIA

RES = 20                     # клеток на мм
GAP_TRACK = CLEARANCE + 0.1  # до чужих дорожек и переходов (с запасом на шаг сетки)
GAP_PAD = CLEARANCE + 0.1    # до чужих выводов
GAP_FINE = PAD_CLEARANCE + 0.1   # до выводов с мелким шагом (ЦАП, LP5907)
EDGE = 0.5
VIA_COST = 400               # переход «стоит» как 20 мм дорожки: перемычек — как можно меньше


def _img():
    im = Image.new("1", (int(W * RES) + 1, int(H * RES) + 1), 0)
    return im, ImageDraw.Draw(im)


def _P(x, y):
    return (x * RES, y * RES)


def _pad(d, pd, grow):
    w, h = pd.w + 2 * grow, pd.h + 2 * grow
    box = [*_P(pd.x - w / 2, pd.y - h / 2), *_P(pd.x + w / 2, pd.y + h / 2)]
    if pd.shape in ("circle", "oval"):
        d.rounded_rectangle(box, radius=min(w, h) / 2 * RES, fill=1)
    else:
        d.rectangle(box, fill=1)


def _seg(d, x1, y1, x2, y2, w):
    d.line([_P(x1, y1), _P(x2, y2)], fill=1, width=max(1, int(round(w * RES))))
    for x, y in ((x1, y1), (x2, y2)):
        d.ellipse([*_P(x - w / 2, y - w / 2), *_P(x + w / 2, y + w / 2)], fill=1)


def _circle(d, x, y, dia):
    d.ellipse([*_P(x - dia / 2, y - dia / 2), *_P(x + dia / 2, y + dia / 2)], fill=1)


def _maps(parts, tracks, vias, net, w):
    """Запрещённые клетки (для оси дорожки ширины w) и клетки своей цепи, по слоям."""
    blocked, own = {}, {}
    for side in "FB":
        bi, bd = _img()
        oi, od = _img()
        g_t = GAP_TRACK + w / 2
        g_p = GAP_PAD + w / 2
        for p in parts:
            for pd in p.pads:
                if pd.npth:
                    _circle(bd, pd.x, pd.y, pd.w + 2 * (0.5 + w / 2))
                    continue
                if side not in pd.layers:
                    continue
                if pd.net == net:
                    _pad(od, pd, 0)
                else:
                    _pad(bd, pd, (GAP_FINE + w / 2) if pd.ref in FINE_PARTS else g_p)
        for t in tracks:
            if t["layer"] != side:
                continue
            if t["net"] == net:
                _seg(od, *t["pts"], t["w"])
            else:
                _seg(bd, *t["pts"], t["w"] + 2 * g_t)
        for v in vias:
            if v["net"] == net:
                _circle(od, v["x"], v["y"], v["d"])
            else:
                _circle(bd, v["x"], v["y"], v["d"] + 2 * g_t)
        if side == "F" and net != "GND":
            # под модулем, ЦАП и усилителем — только их земля (как запреты в DSN)
            for p in parts:
                if p.ref == "U1":
                    bd.rectangle([*_P(p.x - 7.4 - w / 2, p.y - 13), *_P(p.x + 7.4 + w / 2, p.y + 8.2 + w / 2)], fill=1)
                elif p.ref == "U3":
                    bd.rectangle([*_P(p.x - 2.08 - w / 2, p.y - 3.6), *_P(p.x + 2.08 + w / 2, p.y + 4.9 + w / 2)], fill=1)
                elif p.ref == "U5":
                    bd.rectangle([*_P(p.x - 1.35 - w / 2, p.y - 6.9 - w / 2), *_P(p.x + 1.35 + w / 2, p.y + 4.9)], fill=1)
        kx1, ky1, kx2, ky2 = ANTENNA_KEEPOUT
        bd.rectangle([*_P(kx1 - w / 2 - 0.1, ky1), *_P(kx2, ky2 + w / 2 + 0.1)], fill=1)
        b = np.array(bi, dtype=bool)
        e = int(math.ceil((EDGE + w / 2) * RES))
        b[:e, :] = b[-e:, :] = True
        b[:, :e] = b[:, -e:] = True
        blocked[side] = b
        own[side] = np.array(oi, dtype=bool)
    return blocked, own


def _groups_cells(group, side, w):
    im, d = _img()
    for item in group:
        if hasattr(item, "ref"):
            if side in item.layers:
                _pad(d, item, -0.05)
        elif "pts" in item:
            if item["layer"] == side:
                _seg(d, *item["pts"], max(item["w"] - 0.1, 0.05))
        else:
            _circle(d, item["x"], item["y"], item["d"] - 0.1)
    return np.array(im, dtype=bool)


def connect(parts, tracks, vias, net, groups, w, dst_masks=None, allow_vias=True):
    """Соединить группы выводов одной цепи. groups — списки площадок. Добавляет в tracks/vias. True, если удалось.
    dst_masks — {"F": маска, "B": маска} цели вместо остальных групп (например, основная заливка земли)."""
    blocked, _ = _maps(parts, tracks, vias, net, w)
    # переход ставится там, где свободно на обоих слоях с запасом на его радиус
    vr = int(math.ceil((VIA[0] / 2 - w / 2) * RES))
    via_ok = ~ndimage.binary_dilation(blocked["F"] | blocked["B"], iterations=max(vr, 1))
    # переходы — не под корпусами деталей
    from board import body_box
    im, d = _img()
    for p in parts:
        b = body_box(p)
        r = VIA[0] / 2
        if b and b[0] == "rect":
            d.rectangle([*_P(b[1] - r, b[2] - r), *_P(b[3] + r, b[4] + r)], fill=1)
        elif b:
            _circle(d, b[1], b[2], 2 * (b[3] + r))
    via_ok &= ~np.array(im, dtype=bool)
    if not allow_vias:
        via_ok[:] = False
    src = {s: _groups_cells(groups[0], s, w) for s in "FB"}
    dst = {s: np.zeros_like(src[s]) for s in "FB"}
    if dst_masks is not None:
        for s in "FB":
            m = dst_masks[s]
            hh, ww = dst[s].shape
            # маска заливки на другой сетке — приводим к своей и отступаем от края заливки
            ys = (np.arange(hh) * m.shape[0] / hh).astype(int).clip(0, m.shape[0] - 1)
            xs = (np.arange(ww) * m.shape[1] / ww).astype(int).clip(0, m.shape[1] - 1)
            mm = m[np.ix_(ys, xs)]
            dst[s] = ndimage.binary_erosion(mm, iterations=int(math.ceil((w / 2 + 0.1) * RES)))
    for g in groups[1:]:
        for s in "FB":
            dst[s] |= _groups_cells(g, s, w)
    hgt, wid = blocked["F"].shape
    L = {"F": 0, "B": 1}
    free = [~blocked["F"], ~blocked["B"]]
    for s in "FB":   # сами площадки своей цепи проходимы
        free[L[s]] |= src[s] | dst[s]
    tgt = [dst["F"], dst["B"]]
    ty, tx = np.nonzero(dst["F"] | dst["B"])
    if not len(tx):
        return False
    # эвристика: расстояние до ближайшей цели (по центру масс — грубо, но допустимо: берём минимум по выборке)
    sample = list(zip(tx[:: max(1, len(tx) // 50)], ty[:: max(1, len(ty) // 50)]))

    def hfun(x, y):
        return min(math.hypot(x - a, y - b) for a, b in sample) * 0.9

    dist = {}
    prev = {}
    heap = []
    sy, sx = np.nonzero(src["F"])
    for x, y in zip(sx, sy):
        if free[0][y, x]:
            dist[(0, x, y)] = 0
            heapq.heappush(heap, (hfun(x, y), 0, (0, x, y)))
    sy, sx = np.nonzero(src["B"])
    for x, y in zip(sx, sy):
        if free[1][y, x]:
            dist[(1, x, y)] = 0
            heapq.heappush(heap, (hfun(x, y), 0, (1, x, y)))
    steps = [(1, 0, 1), (-1, 0, 1), (0, 1, 1), (0, -1, 1), (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414)]
    end = None
    n = 0
    while heap:
        f, g, node = heapq.heappop(heap)
        if dist.get(node, 1e18) < g:
            continue
        l, x, y = node
        if tgt[l][y, x] and not (src["FB"[l]][y, x]):
            end = node
            break
        n += 1
        if n > 3_000_000:
            break
        for dx, dy, c in steps:
            nx, ny = x + dx, y + dy
            if 0 <= nx < wid and 0 <= ny < hgt and free[l][ny, nx]:
                nd = (l, nx, ny)
                # штраф за смену направления не нужен: путь потом спрямляется
                ng = g + c * (1.0 if l == 0 else 1.6)
                if ng < dist.get(nd, 1e18):
                    dist[nd] = ng
                    prev[nd] = node
                    heapq.heappush(heap, (ng + hfun(nx, ny), ng, nd))
        if via_ok[y, x]:
            nd = (1 - l, x, y)
            ng = g + VIA_COST
            if free[1 - l][y, x] and ng < dist.get(nd, 1e18):
                dist[nd] = ng
                prev[nd] = node
                heapq.heappush(heap, (ng + hfun(x, y), ng, nd))
    if end is None:
        return False
    path = [end]
    while path[-1] in prev:
        path.append(prev[path[-1]])
    path.reverse()
    # путь -> отрезки и переходы
    new_t, new_v = [], []
    run = [path[0]]
    for node in path[1:]:
        if node[0] != run[-1][0]:
            _emit(run, new_t, net, w)
            new_v.append({"net": net, "x": node[1] / RES, "y": node[2] / RES, "d": VIA[0], "drill": VIA[1]})
            run = [node]
        else:
            run.append(node)
    _emit(run, new_t, net, w)
    tracks.extend(new_t)
    vias.extend(new_v)
    return True


def _emit(run, out, net, w):
    if len(run) < 2:
        return
    cells = [(x, y) for _, x, y in run]
    # спрямление: оставляем точки смены направления
    keep = [cells[0]]
    for a, b, c in zip(cells, cells[1:], cells[2:]):
        if (b[0] - a[0], b[1] - a[1]) != (c[0] - b[0], c[1] - b[1]):
            keep.append(b)
    keep.append(cells[-1])
    keep = [(x / RES, y / RES) for x, y in keep]
    side = "F" if run[0][0] == 0 else "B"
    for a, b in zip(keep, keep[1:]):
        out.append({"net": net, "layer": side, "w": w, "pts": (a[0], a[1], b[0], b[1])})


def rip_and_route(parts, tracks, vias, net, nets_of, width_of, log=print):
    """Связь не прокладывается — мешает чужая дорожка. Снимаем участок одной из соседних цепей поблизости,
    прокладываем нашу связь, затем заново доводим снятую цепь. Не вышло — всё возвращаем как было."""
    from check import connectivity, _seg_seg
    groups = connectivity(parts, tracks, vias, nets_of).get(net)
    if not groups:
        return True
    a, b = min(((p, q) for p in groups[0] for g in groups[1:] for q in g),
               key=lambda pq: math.hypot(pq[0].x - pq[1].x, pq[0].y - pq[1].y))
    line = (a.x, a.y, b.x, b.y)
    near = {}
    for i, t in enumerate(tracks):
        if t["net"] in (net, "GND"):
            continue
        if _seg_seg(line, t["pts"]) < 1.5:
            near.setdefault(t["net"], []).append(i)
    for other, idx in sorted(near.items(), key=lambda kv: len(kv[1])):
        bt, bv = [dict(t) for t in tracks], [dict(v) for v in vias]
        for i in sorted(idx, reverse=True):
            del tracks[i]
        ok = True
        for n in (net, other):
            g = connectivity(parts, tracks, vias, nets_of).get(n)
            while g:
                if not any(connect(parts, tracks, vias, n, g, w) for w in width_of(n)):
                    ok = False
                    break
                g = connectivity(parts, tracks, vias, nets_of).get(n)
            if not ok:
                break
        if ok:
            log(f"  переразведено: {net} (сняли и вернули участок {other})")
            return True
        tracks[:], vias[:] = bt, bv
    return False


def reduce_vias(parts, tracks, vias, nets_of, width_of, log=print, full=False):
    """Убрать перемычки: для каждой цепи с переходами снимаем её участки на нижнем слое и сами переходы
    и пробуем соединить заново только по верхнему слою (без переходов). Вышло — оставляем."""
    from check import connectivity
    removed = 0
    for net in sorted({v["net"] for v in vias if v["net"] != "GND"}):
        bt, bv = [dict(t) for t in tracks], [dict(v) for v in vias]
        tracks[:] = [t for t in tracks if not (t["net"] == net and (full or t["layer"] == "B") and not t.get("fixed"))]
        vias[:] = [v for v in vias if v["net"] != net]
        ok = True
        g = connectivity(parts, tracks, vias, nets_of).get(net)
        while g:
            if not any(connect(parts, tracks, vias, net, g, w, allow_vias=False) for w in width_of(net)):
                ok = False
                break
            g = connectivity(parts, tracks, vias, nets_of).get(net)
        before = sum(1 for v in bv if v["net"] == net)
        if ok:
            removed += before
            log(f"  без перемычек: {net} (-{before})")
        else:
            tracks[:], vias[:] = bt, bv
    return removed


def rip_gnd(parts, tracks, vias, pad, masks, nets_of, width_of, log=print, reach=3.0):
    """Земляной вывод заперт чужими дорожками: снимаем участок соседней цепи у вывода, выводим землю
    к заливке, снятую цепь доводим заново. Не вышло — возвращаем как было."""
    from check import connectivity, _seg_dist
    near = {}
    for i, t in enumerate(tracks):
        if t["net"] == "GND" or t.get("fixed") or t["layer"] not in pad.layers:
            continue
        d = min(_seg_dist(pad.x, pad.y, *t["pts"]), 99)
        if d < reach:
            near.setdefault(t["net"], []).append(i)
    for other, idx in sorted(near.items(), key=lambda kv: len(kv[1])):
        bt, bv = [dict(t) for t in tracks], [dict(v) for v in vias]
        for i in sorted(idx, reverse=True):
            del tracks[i]
        ok = any(connect(parts, tracks, vias, "GND", [[pad]], w, dst_masks=masks, allow_vias=av)
                 for av in (False, True) for w in (0.6, 0.4))
        if ok:
            g = connectivity(parts, tracks, vias, nets_of).get(other)
            while g:
                if not any(connect(parts, tracks, vias, other, g, w) for w in width_of(other)):
                    ok = False
                    break
                g = connectivity(parts, tracks, vias, nets_of).get(other)
        if ok:
            log(f"  земля {pad.ref}.{pad.num}: переразведено {other}")
            return True
        tracks[:], vias[:] = bt, bv
    return False
