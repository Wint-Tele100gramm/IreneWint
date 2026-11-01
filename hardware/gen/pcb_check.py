# Проверка файла платы KiCad: заново читает .kicad_pcb и сравнивает положения и цепи всех площадок с моделью платы.

import math

from kiutils.board import Board
from kicad_pcb import X0, Y0


def compare(path, parts):
    bd = Board.from_file(path)
    byref = {}
    for fp in bd.footprints:
        ref = next(t.text for t in fp.graphicItems if type(t).__name__ == "FpText" and t.type == "reference")
        byref[ref] = fp
    errs = []
    for p in parts:
        fp = byref.get(p.ref)
        if fp is None:
            errs.append(f"{p.ref}: нет на плате")
            continue
        if (fp.layer == "B.Cu") != (p.side == "B"):
            errs.append(f"{p.ref}: не та сторона")
        a = math.radians(fp.position.angle or 0)
        c, s = math.cos(a), math.sin(a)
        for pd, mine in zip(fp.pads, p.pads):
            x = fp.position.X + pd.position.X * c + pd.position.Y * s - X0
            y = fp.position.Y - pd.position.X * s + pd.position.Y * c - Y0
            if math.hypot(x - mine.x, y - mine.y) > 0.005:
                errs.append(f"{p.ref}.{mine.num}: площадка в ({x:.3f},{y:.3f}), должна быть ({mine.x:.3f},{mine.y:.3f})")
            net = pd.net.name if pd.net else None
            if net != mine.net:
                errs.append(f"{p.ref}.{mine.num}: цепь {net}, должна быть {mine.net}")
            side = "B" if any(lay.startswith("B.") for lay in pd.layers) and not any(lay.startswith("F.") for lay in pd.layers) else None
            if mine.layers == {"B"} and side != "B":
                errs.append(f"{p.ref}.{mine.num}: площадка не на нижнем слое")
    return errs, len(bd.traceItems)
