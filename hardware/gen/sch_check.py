# Проверка схемы KiCad: разбирает .kicad_sch и восстанавливает цепи по координатам выводов, меток и символов питания,
# затем сравнивает с design.py. Так видно, что «Обновить плату по схеме» в KiCad даст ровно эти цепи.

import math

from design import PARTS
from kiutils.schematic import Schematic


def nets_from_sch(path):
    s = Schematic.from_file(path)
    libs = {ls.libId if hasattr(ls, "libId") else ls.entryName: ls for ls in s.libSymbols}
    at = {}       # (x, y) -> имя цепи
    pins = []     # (ref, num, x, y)
    for gl in s.globalLabels:
        at[(round(gl.position.X, 3), round(gl.position.Y, 3))] = gl.text
    for sym in s.schematicSymbols:
        lib = libs[sym.libId if hasattr(sym, "libId") else sym.libraryNickname + ":" + sym.entryName]
        props = {p.key: p.value for p in sym.properties}
        a = math.radians(sym.position.angle or 0)
        c, si = round(math.cos(a), 9), round(math.sin(a), 9)
        for unit in lib.units:
            for pin in unit.pins:
                x, y = pin.position.X, pin.position.Y
                X = sym.position.X + x * c - y * si
                Y = sym.position.Y - (x * si + y * c)
                key = (round(X, 3), round(Y, 3))
                if lib.isPower if hasattr(lib, "isPower") else props["Reference"].startswith("#"):
                    if props["Reference"].startswith("#PWR"):
                        at[key] = props["Value"]
                else:
                    pins.append((props["Reference"], pin.number, key))
    result = {}
    for ref, num, key in pins:
        result[(ref, num)] = at.get(key)
    return result


def compare(path):
    got = nets_from_sch(path)
    errs = []
    for ref, sym, value, fpkey, pins, descr in PARTS:
        if sym == "MH":
            continue
        for num, net in pins.items():
            g = got.get((ref, str(num)), "нет вывода")
            if g != net:
                errs.append(f"{ref}.{num}: в схеме {g}, должно быть {net}")
    return errs
