# Выгрузка платы в проект KiCad: .kicad_pcb (детали, цепи, дорожки, переходы, заливки земли, контур, надписи),
# .kicad_pro (классы цепей, правила), .kicad_dru (зазор 0,3 мм между дорожками).
# Формат KiCad 6 (20211014) — открывается KiCad 6, 7, 8, 9 (новые версии сами обновят файл).

import copy
import json
import uuid

from board import FP
from gerber import board_outline_pts
from kiutils.board import Board
from kiutils.items.brditems import Segment, Via
from kiutils.items.common import Effects, Font, Justify, Net, Position
from kiutils.items.gritems import GrLine, GrText
from kiutils.items.zones import FillSettings, Hatch, KeepoutSettings, Zone, ZonePolygon
from placement import W, H, ANTENNA_KEEPOUT
from route import CLEARANCE, DAC_GAP, FINE_PARTS, PAD_CLEARANCE, VIA, VIAS, WIDTH, net_class

X0, Y0 = 100.0, 80.0   # плата в середине листа KiCad
NS = uuid.UUID("6b1f0f8e-5d4c-4b7e-9a51-3c1e2f6a7b10")


def uid(*key):
    """Стабильные UUID: повторная сборка не меняет файлы без причины."""
    return str(uuid.uuid5(NS, "/".join(str(k) for k in key)))


def P(x, y, a=None):
    return Position(X=round(x + X0, 4), Y=round(y + Y0, 4), angle=a)


def _flip_layer(name):
    if name.startswith("F."):
        return "B." + name[2:]
    if name.startswith("B."):
        return "F." + name[2:]
    return name


def _flip_pos(pos):
    if pos is not None:
        pos.Y = -pos.Y


def footprint_of(p, netnums):
    fp = copy.deepcopy(p.fp)
    fp.libId = FP[p.fpkey]
    fp.version = None          # внутри платы у посадочного места своей версии нет
    fp.generator = None
    fp.position = P(p.x, p.y, p.rot or None)
    fp.tstamp = uid("fp", p.ref)
    fp.path = "/" + uid("sym", p.ref)
    bottom = p.side == "B"
    fp.layer = "B.Cu" if bottom else "F.Cu"
    fp.zones = [] if bottom else fp.zones
    for g in fp.graphicItems:
        name = type(g).__name__
        if bottom:
            for attr in ("start", "end", "mid", "center"):
                _flip_pos(getattr(g, attr, None))
            for c in getattr(g, "coordinates", None) or []:
                _flip_pos(c)
            if getattr(g, "layer", None):
                g.layer = _flip_layer(g.layer)
        if name == "FpText":
            if bottom:
                _flip_pos(g.position)
                if g.effects.justify is None:
                    g.effects.justify = Justify()
                g.effects.justify.mirror = True
            local = (g.position.angle or 0)
            g.position.angle = ((local + p.rot) % 360) or None
            if g.type == "reference":
                g.text = p.ref
            elif g.type == "value":
                g.text = p.value
    for pd, mine in zip(fp.pads, p.pads):
        local = pd.position.angle or 0
        if bottom:
            pd.position.Y = -pd.position.Y
            local = -local
            pd.layers = [_flip_layer(lay) for lay in pd.layers]
        pd.position.angle = ((local + p.rot) % 360) or None
        pd.tstamp = uid("pad", p.ref, mine.pin_name)
        pd.net = Net(number=netnums[mine.net], name=mine.net) if mine.net else None
    return fp


def _zone(layers, net, name, poly, keepout=False, priority=None):
    z = Zone(net=net, netName=name, layers=layers, tstamp=uid("zone", name, *layers, priority),
             hatch=Hatch(style="edge", pitch=0.508), polygons=[ZonePolygon(coordinates=[P(x, y) for x, y in poly])],
             filledPolygons=[], priority=priority)
    if keepout:
        z.name = "antenna"
        z.keepoutSettings = KeepoutSettings(tracks="not_allowed", vias="not_allowed", pads="allowed",
                                            copperpour="not_allowed", footprints="allowed")
    else:
        z.minThickness = 0.5
        z.clearance = 0.4
        # зальёт KiCad по «B»; острова (не связанные с землёй куски) убираются всегда
        z.fillSettings = FillSettings(yes=False, thermalGap=0.4, thermalBridgeWidth=0.5, islandRemovalMode=0)
    return z


def write_pcb(path, parts, tracks, vias, silk_text, title="Irene client"):
    bd = Board.create_new()
    bd.version = 20221018      # формат KiCad 7: открывается KiCad 7, 8, 9
    bd.generator = "pcbnew"
    names = sorted({pd.net for p in parts for pd in p.pads if pd.net})
    names = ["GND"] + [n for n in names if n != "GND"]
    netnums = {n: i + 1 for i, n in enumerate(names)}
    bd.nets = [Net(0, "")] + [Net(netnums[n], n) for n in names]
    bd.footprints = [footprint_of(p, netnums) for p in parts]
    items = []
    for i, t in enumerate(tracks):
        x1, y1, x2, y2 = t["pts"]
        items.append(Segment(start=P(x1, y1), end=P(x2, y2), width=round(t["w"], 4),
                             layer="F.Cu" if t["layer"] == "F" else "B.Cu", net=netnums[t["net"]], tstamp=uid("seg", i)))
    for i, v in enumerate(vias):
        items.append(Via(position=P(v["x"], v["y"]), size=v["d"], drill=v["drill"], layers=["F.Cu", "B.Cu"],
                         net=netnums[v["net"]], tstamp=uid("via", i)))
    bd.traceItems = items
    outline = board_outline_pts(0, n=8)
    gr = []
    for i, (a, b) in enumerate(zip(outline, outline[1:] + outline[:1])):
        gr.append(GrLine(start=P(*a), end=P(*b), layer="Edge.Cuts", width=0.1, tstamp=uid("edge", i)))
    for i, (text, x, y, h, side, center) in enumerate(silk_text):
        # снизу текст зеркальный: «right» у зеркального текста — это левый край, как в Gerber
        horiz = None if center else ("right" if side == "B" else "left")
        eff = Effects(font=Font(height=h, width=h, thickness=max(0.15, h / 7)),
                      justify=Justify(horizontally=horiz, mirror=side == "B"))
        gr.append(GrText(text=text, position=P(x, y), layer=f"{side}.SilkS", effects=eff, tstamp=uid("text", i)))
    bd.graphicItems = gr
    inset = board_outline_pts(0.3, n=4)
    kx1, ky1, kx2, ky2 = ANTENNA_KEEPOUT
    bd.zones = [
        _zone(["F.Cu"], netnums["GND"], "GND", inset, priority=0),
        _zone(["B.Cu"], netnums["GND"], "GND", inset, priority=0),
        _zone(["F.Cu", "B.Cu"], 0, "", [(kx1, ky1), (kx2, ky1), (kx2, ky2), (kx1, ky2)], keepout=True),
    ]
    bd.to_file(path)
    return netnums


def write_project(path, nets):
    classes = [{"name": "Default", "clearance": CLEARANCE, "track_width": WIDTH["default"],
                "via_diameter": VIAS["default"][0], "via_drill": VIAS["default"][1], "nets": []}]
    for cls, title in (("power", "Power"), ("dac", "DAC")):
        classes.append({"name": title, "clearance": CLEARANCE if cls == "power" else DAC_GAP, "track_width": WIDTH[cls],
                        "via_diameter": VIAS[cls][0], "via_drill": VIAS[cls][1],
                        "nets": sorted(n for n in nets if net_class(n) == cls)})
    for c in classes:
        c.update({"bus_width": 12, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25, "diff_pair_width": 0.2,
                  "line_style": 0, "microvia_diameter": 0.3, "microvia_drill": 0.1, "pcb_color": "rgba(0, 0, 0, 0.000)",
                  "schematic_color": "rgba(0, 0, 0, 0.000)", "wire_width": 6})
    pro = {
        "board": {
            "design_settings": {
                "defaults": {"board_outline_line_width": 0.1, "copper_line_width": 0.2, "silk_line_width": 0.15,
                             "silk_text_size_h": 1.0, "silk_text_size_v": 1.0, "silk_text_thickness": 0.15},
                "rules": {"min_clearance": PAD_CLEARANCE, "min_copper_edge_clearance": 0.5, "min_hole_clearance": 0.25,
                          "min_hole_to_hole": 0.25, "min_through_hole_diameter": 0.3, "min_track_width": 0.3,
                          "min_via_annular_width": 0.3, "min_via_diameter": 1.2, "min_silk_clearance": 0.0,
                          "solder_mask_clearance": 0.05, "solder_mask_min_width": 0.1},
                "track_widths": [0.0, 0.3, 0.6, 0.8, 1.0],
                "via_dimensions": [{"diameter": 0.0, "drill": 0.0}, {"diameter": VIA[0], "drill": VIA[1]}],
            },
        },
        "meta": {"filename": path.rsplit("/", 1)[-1], "version": 1},
        "net_settings": {"classes": classes, "meta": {"version": 2}},
        "pcbnew": {"page_layout_descr_file": ""},
        "schematic": {"drawing": {"default_line_thickness": 6.0, "default_text_size": 50.0},
                      "meta": {"version": 1}},
        "sheets": [[uid("sheet"), ""]],
        "text_variables": {},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pro, f, indent=2, ensure_ascii=False)
    dru = path.rsplit(".", 1)[0] + ".kicad_dru"
    with open(dru, "w", encoding="utf-8") as f:
        fine = " || ".join(f"A.intersectsCourtyard('{r}') || B.intersectsCourtyard('{r}')" for r in FINE_PARTS)
        f.write(f"""(version 1)

# Зазор между дорожками, площадками и переходами разных цепей — не меньше {CLEARANCE} мм (правило Default).
# Исключение — выводы с мелким шагом: ЦАП (TSSOP, 0,65 мм) и LP5907 (SOT-23-5, 0,95 мм): {PAD_CLEARANCE} мм,
# между дорожками цепей ЦАП у его выводов — {DAC_GAP} мм (класс DAC). Иначе к этим выводам не подойти.
(rule "fine_pitch"
  (constraint clearance (min {PAD_CLEARANCE}mm))
  (condition "{fine}"))

(rule "dac_tracks"
  (constraint clearance (min {DAC_GAP}mm))
  (condition "A.NetClass == 'DAC' && B.NetClass == 'DAC'"))

(rule "vias_not_under_parts"
  (constraint disallow via)
  (condition "A.intersectsCourtyard('*')"))
""")

