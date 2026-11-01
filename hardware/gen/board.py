# Модель платы: детали из библиотек KiCad (посадочные места), расставленные по placement.py, с цепями из design.py.
# Все координаты — мм, начало в левом верхнем углу платы, Y вниз.

import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from design import PARTS, FP  # noqa: E402
from placement import PCB_POS  # noqa: E402

EXT = os.environ.get("IRENE_PCB_EXT", "")
sys.path.insert(0, os.path.join(EXT, "kiutils", "src"))
from kiutils.footprint import Footprint  # noqa: E402


def fp_path(key):
    lib, name = FP[key].split(":")
    if lib == "Espressif":
        return os.path.join(EXT, "kicad-libraries", "footprints", lib + ".pretty", name + ".kicad_mod")
    return os.path.join(EXT, "kicad-footprints", lib + ".pretty", name + ".kicad_mod")


def rot(x, y, deg):
    """Поворот точки посадочного места (Y вниз) на deg против часовой стрелки (вид сверху)."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


class Pad:
    __slots__ = ("ref", "num", "net", "shape", "x", "y", "w", "h", "rr", "layers", "drill", "slot", "npth", "pin_name")

    def __repr__(self):
        return f"Pad({self.ref}.{self.num} {self.net} {self.shape} {self.x:.2f},{self.y:.2f} {self.w}x{self.h})"


class Part:
    def __init__(self, ref, sym, value, fpkey, pins, descr):
        self.ref, self.sym, self.value, self.fpkey, self.pins, self.descr = ref, sym, value, fpkey, pins, descr
        pos = PCB_POS[ref]
        self.x, self.y, self.rot = pos[:3]
        self.side = pos[3] if len(pos) > 3 else "F"   # "B" — деталь на нижней стороне (зеркально)
        self.pads = []
        self.silk = []      # [("line", x1, y1, x2, y2, w) | ("circle", cx, cy, r, w)]
        self.fab = []
        self.court = None   # (x1, y1, x2, y2)
        self.court_circle = None
        self.smd = True


def _norm_angle(a):
    a = (a or 0) % 360
    return round(a, 3)


def load_parts():
    parts = []
    for ref, sym, value, fpkey, pins, descr in PARTS:
        p = Part(ref, sym, value, fpkey, pins, descr)
        fp = Footprint.from_file(fp_path(fpkey))
        p.fp = fp
        bot = p.side == "B"

        def L(x, y):
            return (x, -y) if bot else (x, y)

        def R(x, y, deg):
            return rot(*L(x, y), deg)
        pinmap = {str(k): v for k, v in pins.items()}
        seen = {}
        for pd in fp.pads:
            num = str(pd.number) if pd.number is not None else ""
            pad = Pad()
            pad.ref, pad.num = ref, num
            # одинаковые номера (общий вывод из нескольких площадок) — для трассировщика нужны уникальные имена
            k = seen.get(num, 0)
            seen[num] = k + 1
            pad.pin_name = num if k == 0 else f"{num}@{k}"
            pad.net = pinmap.get(num) if num else None
            pad.npth = pd.type == "np_thru_hole"
            ang = _norm_angle((-(pd.position.angle or 0) if bot else (pd.position.angle or 0)) + p.rot)
            w, h = pd.size.X, pd.size.Y
            if round(ang / 90) % 2 == 1:
                w, h = h, w
            if abs(ang / 90 - round(ang / 90)) > 1e-6:
                raise ValueError(f"{ref}.{num}: поворот площадки {ang} не кратен 90°")
            dx, dy = R(pd.position.X, pd.position.Y, p.rot)
            pad.x, pad.y = p.x + dx, p.y + dy
            pad.w, pad.h = w, h
            pad.shape = pd.shape
            pad.rr = (pd.roundrectRatio or 0.25) if pd.shape == "roundrect" else 0
            if pd.type == "smd":
                pad.layers = {p.side}
            else:
                pad.layers = {"F", "B"}
                p.smd = False
            pad.drill, pad.slot = None, None
            if pd.drill is not None and pd.type != "smd":
                if pd.drill.oval and pd.drill.width:
                    dw, dh = pd.drill.diameter, pd.drill.width
                    if round(ang / 90) % 2 == 1:
                        dw, dh = dh, dw
                    pad.slot = (dw, dh)
                    pad.drill = min(dw, dh)
                else:
                    pad.drill = pd.drill.diameter
            p.pads.append(pad)
        # для неназначенных выводов (например ESP с None) цепь None — это нормально; проверим опечатки в номерах
        nums = {pd.num for pd in p.pads}
        for k in pinmap:
            if k not in nums:
                raise ValueError(f"{ref}: в посадочном месте нет вывода {k}")
        xs, ys = [], []
        for g in fp.graphicItems:
            t = type(g).__name__
            layer = getattr(g, "layer", "")
            width = getattr(g, "width", None)
            if width is None and getattr(g, "stroke", None) is not None:
                width = g.stroke.width
            width = width or 0.12
            if t == "FpLine":
                a = R(g.start.X, g.start.Y, p.rot)
                b = R(g.end.X, g.end.Y, p.rot)
                item = ("line", p.x + a[0], p.y + a[1], p.x + b[0], p.y + b[1], width)
                if layer == "F.CrtYd":
                    xs += [item[1], item[3]]
                    ys += [item[2], item[4]]
            elif t == "FpCircle":
                c = R(g.center.X, g.center.Y, p.rot)
                r = math.hypot(g.end.X - g.center.X, g.end.Y - g.center.Y)
                item = ("circle", p.x + c[0], p.y + c[1], r, width)
                if layer == "F.CrtYd":
                    p.court_circle = (item[1], item[2], r)
            elif t == "FpArc" and g.mid is not None:
                pts = _arc_points((g.start.X, g.start.Y), (g.mid.X, g.mid.Y), (g.end.X, g.end.Y))
                pts = [R(x, y, p.rot) for x, y in pts]
                for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                    item = ("line", p.x + x1, p.y + y1, p.x + x2, p.y + y2, width)
                    (p.silk if layer == "F.SilkS" else p.fab if layer == "F.Fab" else []).append(item)
                continue
            elif t == "FpPoly":
                pts = [R(c.X, c.Y, p.rot) for c in g.coordinates]
                for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
                    item = ("line", p.x + x1, p.y + y1, p.x + x2, p.y + y2, width)
                    (p.silk if layer == "F.SilkS" else p.fab if layer == "F.Fab" else []).append(item)
                continue
            else:
                continue
            if layer == "F.SilkS":
                p.silk.append(item)
            elif layer == "F.Fab":
                p.fab.append(item)
        if xs:
            p.court = (min(xs), min(ys), max(xs), max(ys))
        elif p.court_circle:
            cx, cy, r = p.court_circle
            p.court = (cx - r, cy - r, cx + r, cy + r)
        parts.append(p)
    return parts


def _arc_points(a, m, b, n=12):
    (x1, y1), (x2, y2), (x3, y3) = a, m, b
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:
        return [a, b]
    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1) + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3) + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)

    def norm(t):
        return t % (2 * math.pi)
    # направление: через середину
    ccw = norm(a2 - a1) < norm(a3 - a1)
    span = norm(a3 - a1) if ccw else -norm(a1 - a3)
    return [(ux + r * math.cos(a1 + span * i / n), uy + r * math.sin(a1 + span * i / n)) for i in range(n + 1)]


def sides(p):
    return {"F", "B"} if not p.smd or p.sym == "MH" else {p.side}


def check_overlaps(parts, margin=0.0):
    """Пересечения курсов (зон деталей) на одной стороне — список пар."""
    bad = []
    for i, a in enumerate(parts):
        for b in parts[i + 1:]:
            if not a.court or not b.court or not (sides(a) & sides(b)):
                continue
            if a.court_circle or b.court_circle:
                # отверстие: круг против прямоугольника
                c, other = (a, b) if a.court_circle else (b, a)
                cx, cy, r = c.court_circle
                x1, y1, x2, y2 = other.court
                dx = max(x1 - cx, 0, cx - x2)
                dy = max(y1 - cy, 0, cy - y2)
                if math.hypot(dx, dy) < r - 1e-6:
                    bad.append((a.ref, b.ref))
                continue
            ax1, ay1, ax2, ay2 = a.court
            bx1, by1, bx2, by2 = b.court
            if ax1 < bx2 - margin and bx1 < ax2 - margin and ay1 < by2 - margin and by1 < ay2 - margin:
                bad.append((a.ref, b.ref))
    return bad


def nets_of(parts):
    nets = {}
    for p in parts:
        for pad in p.pads:
            if pad.net:
                nets.setdefault(pad.net, []).append(pad)
    return nets
