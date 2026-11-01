# Принципиальная схема в формате KiCad (.kicad_sch, версия 6 — открывается KiCad 6–9).
# Те же символы и расстановка, что в schematic.py (PDF); цепи — глобальными метками и символами питания у выводов,
# поэтому «Обновить плату по схеме» в KiCad даёт те же цепи, что уже разведены на плате.

import math

from design import PARTS
from kicad_pcb import uid
from board import FP
from schematic import (BLOCKS, CONN_NAMES, DIRS, NOTES, POS, POWER, SYMBOLS, conn, rot_dir, tf)
from design import G

F = "(effects (font (size 1.27 1.27)))"
FH = "(effects (font (size 1.27 1.27)) hide)"
STROKE = "(stroke (width 0) (type default) (color 0 0 0 0))"


def q(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def n(v):
    return f"{round(v, 4):g}"


def sym_name(ref, sym):
    return f"CONN_{ref}" if sym.startswith("CONN") else sym


def lib_symbol(name, s):
    """Описание символа для lib_symbols. Локальные Y вниз -> в KiCad вверх."""
    two_pin = not s.get("ic")
    g = []
    for it in s["g"]:
        k = it[0]
        if k in ("line", "thick"):
            w = 0.45 if k == "thick" else 0.254
            g.append(f"(polyline (pts (xy {n(it[1])} {n(-it[2])}) (xy {n(it[3])} {n(-it[4])})) "
                     f"(stroke (width {w}) (type default) (color 0 0 0 0)) (fill (type none)))")
        elif k in ("rect", "frect"):
            fill = "outline" if k == "frect" else ("background" if s.get("ic") else "none")
            g.append(f"(rectangle (start {n(it[1])} {n(-it[2])}) (end {n(it[3])} {n(-it[4])}) "
                     f"(stroke (width 0.254) (type default) (color 0 0 0 0)) (fill (type {fill})))")
        elif k == "poly":
            pts = list(it[1]) + [it[1][0]]
            g.append("(polyline (pts " + " ".join(f"(xy {n(x)} {n(-y)})" for x, y in pts) + ") "
                     "(stroke (width 0.2) (type default) (color 0 0 0 0)) (fill (type outline)))")
        elif k == "arc":
            ax, ay, bx, by, bulge = it[1:6]
            cx, cy = (ax + bx) / 2, (ay + by) / 2
            r = math.hypot(bx - ax, by - ay) / 2
            dx, dy = (bx - ax) / (2 * r), (by - ay) / (2 * r)
            sgn = 1 if bulge > 0 else -1
            mx, my = cx + sgn * r * dy, cy - sgn * r * dx
            g.append(f"(arc (start {n(ax)} {n(-ay)}) (mid {n(mx)} {n(-my)}) (end {n(bx)} {n(-by)}) "
                     f"(stroke (width 0.3) (type default) (color 0 0 0 0)) (fill (type none)))")
        elif k == "dot":
            g.append(f"(circle (center {n(it[1])} {n(-it[2])}) (radius 0.45) "
                     f"(stroke (width 0) (type default) (color 0 0 0 0)) (fill (type outline)))")
        elif k == "text":
            g.append(f"(text {q(it[3])} (at {n(it[1])} {n(-it[2])} 0) (effects (font (size {it[4]} {it[4]}))))")
    pins = []
    ang = {"left": 0, "right": 180, "up": 270, "down": 90}
    for num, (x, y, d) in s["pins"].items():
        pname = s["names"].get(num, "~") or "~"
        length = 2.54 if s.get("ic") else 0
        pins.append(f"(pin passive line (at {n(x)} {n(-y)} {ang[d]}) (length {length}) "
                    f"(name {q(pname)} {F}) (number {q(num)} {F}))")
    x1, y1, x2, y2 = s["box"]
    flags = "(pin_numbers hide) (pin_names (offset 0) hide)" if two_pin else "(pin_names (offset 0.762))"
    return (f'(symbol "irene:{name}" {flags} (in_bom yes) (on_board yes)\n'
            f'  (property "Reference" "{ "U" if s.get("ic") else "X"}" (id 0) (at {n(x1)} {n(-y1 + 1.5)} 0) {F})\n'
            f'  (property "Value" {q(name)} (id 1) (at {n(x1)} {n(-y2 - 1.5)} 0) {F})\n'
            f'  (property "Footprint" "" (id 2) (at 0 0 0) {FH})\n'
            f'  (property "Datasheet" "" (id 3) (at 0 0 0) {FH})\n'
            f'  (symbol "{name}_0_1" {" ".join(g)})\n'
            f'  (symbol "{name}_1_1" {" ".join(pins)})\n)')


def power_symbol(net, flag=False):
    if flag:
        body = ("(polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27)) "
                f"{STROKE} (fill (type none)))")
        return (f'(symbol "irene:PWR_FLAG" (power) (pin_numbers hide) (pin_names (offset 0) hide) (in_bom yes) (on_board yes)\n'
                f'  (property "Reference" "#FLG" (id 0) (at 0 1.905 0) {FH})\n'
                f'  (property "Value" "PWR_FLAG" (id 1) (at 0 3.81 0) {F})\n'
                f'  (property "Footprint" "" (id 2) (at 0 0 0) {FH})\n  (property "Datasheet" "~" (id 3) (at 0 0 0) {FH})\n'
                f'  (symbol "PWR_FLAG_0_0" (pin power_out line (at 0 0 90) (length 0) (name "pwr" {F}) (number "1" {F})))\n'
                f'  (symbol "PWR_FLAG_0_1" {body})\n)')
    if net == G:
        body = (f"(polyline (pts (xy 0 0) (xy 0 -1.27) (xy 1.27 -1.27) (xy 0 -2.54) (xy -1.27 -1.27) (xy 0 -1.27)) "
                f"{STROKE} (fill (type none)))")
        val = "(at 0 -3.81 0)"
        pin = f'(pin power_in line (at 0 0 270) (length 0) hide (name {q(net)} {F}) (number "1" {F}))'
    else:
        body = (f"(polyline (pts (xy -0.762 1.27) (xy 0 2.54) (xy 0.762 1.27)) {STROKE} (fill (type none))) "
                f"(polyline (pts (xy 0 0) (xy 0 2.54)) {STROKE} (fill (type none)))")
        val = "(at 0 3.556 0)"
        pin = f'(pin power_in line (at 0 0 90) (length 0) hide (name {q(net)} {F}) (number "1" {F}))'
    return (f'(symbol "irene:{net}" (power) (pin_names (offset 0)) (in_bom yes) (on_board yes)\n'
            f'  (property "Reference" "#PWR" (id 0) (at 0 0 0) {FH})\n'
            f'  (property "Value" {q(net)} (id 1) {val} {F})\n'
            f'  (property "Footprint" "" (id 2) (at 0 0 0) {FH})\n  (property "Datasheet" "" (id 3) (at 0 0 0) {FH})\n'
            f'  (symbol "{net}_0_1" {body})\n  (symbol "{net}_1_1" {pin})\n)')


def write(path):
    libs, items, instances = {}, [], []
    pwr_n = [0]

    def power_inst(net, x, y, rot, flag=False):
        pwr_n[0] += 1
        ref = f"#FLG{pwr_n[0]:03d}" if flag else f"#PWR{pwr_n[0]:03d}"
        lib = "PWR_FLAG" if flag else net
        u = uid("pwr", ref)
        val = "PWR_FLAG" if flag else net
        items.append(f'(symbol (lib_id "irene:{lib}") (at {n(x)} {n(y)} {rot}) (unit 1) (in_bom yes) (on_board yes)\n'
                     f'  (uuid {u})\n'
                     f'  (property "Reference" {q(ref)} (id 0) (at {n(x)} {n(y)} 0) {FH})\n'
                     f'  (property "Value" {q(val)} (id 1) (at {n(x)} {n(y - 3.5 if net != G else y + 4)} 0) {F})\n'
                     f'  (property "Footprint" "" (id 2) (at {n(x)} {n(y)} 0) {FH})\n'
                     f'  (property "Datasheet" "" (id 3) (at {n(x)} {n(y)} 0) {FH})\n'
                     f'  (pin "1" (uuid {uid("pwrpin", ref)}))\n)')
        instances.append(f'(path "/{u}" (reference {q(ref)}) (unit 1) (value {q(val)}) (footprint ""))')

    used_power = set()
    for ref, sym, value, fpkey, pins, descr in PARTS:
        if sym == "MH":
            continue
        s = conn(CONN_NAMES[ref], "left") if sym.startswith("CONN") else SYMBOLS[sym]
        name = sym_name(ref, sym)
        if name not in libs:
            libs[name] = lib_symbol(name, s)
        ox, oy, rot = POS[ref]
        u = uid("sym", ref)
        x1, y1, x2, y2 = s["box"]
        if s.get("ic"):
            rpos, vpos = (ox + x1, oy + y1 - 2.5), (ox + x1, oy + y2 + (6.5 if any(p[2] == "down" for p in s["pins"].values()) else 2.5))
            just = " (justify left)"
        else:
            rpos, vpos = ((ox + 2.6, oy - 1.0), (ox + 2.6, oy + 1.3)) if rot % 180 == 0 else ((ox - 2, oy - 3.2), (ox - 2, oy + 3.6))
            just = " (justify left)"
        pin_uuids = " ".join(f'(pin {q(num)} (uuid {uid("pin", ref, num)}))' for num in s["pins"])
        items.append(f'(symbol (lib_id "irene:{name}") (at {n(ox)} {n(oy)} {rot}) (unit 1) (in_bom yes) (on_board yes)\n'
                     f'  (uuid {u})\n'
                     f'  (property "Reference" {q(ref)} (id 0) (at {n(rpos[0])} {n(rpos[1])} 0) (effects (font (size 1.27 1.27)){just}))\n'
                     f'  (property "Value" {q(value)} (id 1) (at {n(vpos[0])} {n(vpos[1])} 0) (effects (font (size 1.27 1.27)){just}))\n'
                     f'  (property "Footprint" {q(FP[fpkey])} (id 2) (at {n(ox)} {n(oy)} 0) {FH})\n'
                     f'  (property "Datasheet" "" (id 3) (at {n(ox)} {n(oy)} 0) {FH})\n'
                     f'  (property "Описание" {q(descr)} (id 4) (at {n(ox)} {n(oy)} 0) {FH})\n'
                     f'  {pin_uuids}\n)')
        instances.append(f'(path "/{u}" (reference {q(ref)}) (unit 1) (value {q(value)}) (footprint {q(FP[fpkey])}))')
        pinmap = {str(k): v for k, v in pins.items()}
        for num, (px, py, d) in s["pins"].items():
            x, y = tf(px, py, ox, oy, rot)
            x, y = round(x, 4), round(y, 4)
            dd = rot_dir(d, rot)
            if num not in pinmap:
                continue
            net = pinmap[num]
            if net is None:
                items.append(f"(no_connect (at {n(x)} {n(y)}) (uuid {uid('nc', ref, num)}))")
            elif net in POWER:
                used_power.add(net)
                if net == G:
                    r = {"down": 0, "right": 90, "up": 180, "left": 270}[dd]
                else:
                    r = {"up": 0, "left": 90, "down": 180, "right": 270}[dd]
                power_inst(net, x, y, r)
            else:
                a = {"right": 0, "up": 90, "left": 180, "down": 270}[dd]
                jj = "left" if a in (0, 90) else "right"
                items.append(f'(global_label {q(net)} (shape passive) (at {n(x)} {n(y)} {a}) (fields_autoplaced)\n'
                             f'  (effects (font (size 1.27 1.27)) (justify {jj})) (uuid {uid("gl", ref, num)})\n'
                             f'  (property "Intersheet References" "${{INTERSHEET_REFS}}" (id 0) (at {n(x)} {n(y)} 0) {FH}))')
    for net in sorted(used_power):
        libs[net] = power_symbol(net)
    libs["PWR_FLAG"] = power_symbol(None, flag=True)
    # флаги питания: у каждой цепи питания — источник для проверки ERC
    for i, net in enumerate(sorted(used_power)):
        x = 20.32 + i * 15.24
        y = 270.51
        if net == G:
            power_inst(net, x, y, 0)
            power_inst(net, x, y - 5.08, 0, flag=True)
            items.append(f"(wire (pts (xy {n(x)} {n(y)}) (xy {n(x)} {n(y - 5.08)})) {STROKE} (uuid {uid('w', net)}))")
        else:
            power_inst(net, x, y - 5.08, 0)
            power_inst(net, x, y, 180, flag=True)
            items.append(f"(wire (pts (xy {n(x)} {n(y)}) (xy {n(x)} {n(y - 5.08)})) {STROKE} (uuid {uid('w', net)}))")
    for title, x1, y1, x2, y2 in BLOCKS:
        pts = f"(xy {x1} {y1}) (xy {x2} {y1}) (xy {x2} {y2}) (xy {x1} {y2}) (xy {x1} {y1})"
        items.append(f"(polyline (pts {pts}) (stroke (width 0) (type dash) (color 0 0 0 0)) (uuid {uid('blk', title)}))")
        items.append(f'(text {q(title)} (at {x1 + 2} {y1 + 4} 0) (effects (font (size 2 2) bold) (justify left bottom)) '
                     f'(uuid {uid("blkt", title)}))')
    for x, y, t in NOTES:
        items.append(f'(text {q(t)} (at {x} {y} 0) (effects (font (size 1.6 1.6)) (justify left bottom)) (uuid {uid("note", t)}))')
    root = uid("sheet")
    out = [f'(kicad_sch (version 20211123) (generator eeschema)\n\n  (uuid {root})\n\n  (paper "A3")\n',
           '  (title_block (title "Irene client") (rev "2") (company "IreneWint") '
           '(comment 1 "ESP32-S3 + PCM5102A + PAM8403 + 2x INMP441"))\n',
           "  (lib_symbols\n" + "\n".join(libs.values()) + "\n  )\n"]
    out += [it + "\n" for it in items]
    out.append('  (sheet_instances (path "/" (page "1")))\n')
    out.append("  (symbol_instances\n" + "\n".join("    " + i for i in instances) + "\n  )\n)\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(out))
