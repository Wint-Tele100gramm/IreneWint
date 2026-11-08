#!/usr/bin/env python3
"""Сборка производственных файлов платы клиента Русланы.

    IRENE_PCB_EXT=<папка с kiutils, kicad-footprints, kicad-libraries, freerouting.jar> python3 build.py [--route]

--route — заново развести плату Freerouting (иначе берётся сохранённая разводка routed.ses).
Результат — в ../irene-client: проект KiCad (kicad/), Gerber (zip), перечень, файл расстановки, картинки, схема.
"""

import csv
import io
import math
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import board  # noqa: E402
import check  # noqa: E402
import finish  # noqa: E402
import gerber  # noqa: E402
import render  # noqa: E402
import widen  # noqa: E402
import route  # noqa: E402
from placement import W, H, SILK_TEXT, PIN_LABELS  # noqa: E402

OUT = os.path.join(os.path.dirname(HERE), "irene-client")
NAME = "irene-client"
SES = os.path.join(HERE, "routed.ses")


def silk_items(parts):
    """Все надписи: [(текст, x, y, высота, сторона, по центру)]."""
    out = [(t, x, y, h, side, False) for t, x, y, h, side in SILK_TEXT]
    byref = {p.ref: p for p in parts}
    for ref, (labels, (dx, dy), side) in PIN_LABELS.items():
        pads = {pd.num: pd for pd in byref[ref].pads}
        for i, lab in enumerate(labels, 1):
            pd = pads[str(i)]
            out.append((lab, pd.x + dx, pd.y + dy, 0.8, side, True))
    return out


def silk_strokes(items, side):
    strokes = []
    for text, x, y, h, s, center in items:
        if s == side:
            strokes += gerber.text_strokes(text, x, y, h, center=center, mirror=side == "B")
    return strokes


def route_all(parts):
    """Разводка из SES + дотрассировка, снятие лишних перемычек, расширение дорожек."""
    tracks, vias = route.read_ses(SES)
    # заранее проложенные дорожки (веер ЦАП, перемычка LP5907) — в SES они есть, помечаем как неизменные
    pre = route.prewires(parts) + (route.body_ground(parts) if route.ROUTE_GND else [])
    for t in tracks:
        if any(t["net"] == p["net"] and all(abs(a - b) < 0.02 for a, b in zip(sorted(t["pts"]), sorted(p["pts"]))) for p in pre):
            t["fixed"] = True
    nets = board.nets_of(parts)

    def widths(n):
        return (route.WIDTH[route.net_class(n)], 0.4, 0.3)
    # дотрассировка того, что Freerouting не довёл
    for n, groups in check.connectivity(parts, tracks, vias, nets).items():
        if n == "GND":
            continue
        while groups:
            if not any(finish.connect(parts, tracks, vias, n, groups, w) for w in widths(n)):
                break
            print("  дотрассировано:", n)
            groups = check.connectivity(parts, tracks, vias, nets).get(n)
    for n in [n for n in check.connectivity(parts, tracks, vias, nets) if n != "GND"]:
        finish.rip_and_route(parts, tracks, vias, n, nets, widths)
    print("Убираю лишние перемычки…")
    finish.reduce_vias(parts, tracks, vias, nets, widths)
    print("  расширено дорожек:", widen.widen(parts, tracks, vias), "| доля длины по ширине, %:", widen.report(tracks))
    return tracks, vias, nets


def main():
    os.makedirs(OUT, exist_ok=True)
    parts = board.load_parts()
    bad = board.check_overlaps(parts)
    if bad:
        sys.exit(f"Детали перекрываются: {bad}")
    import json
    state = os.path.join(HERE, "_routed_state.json")
    links_file = os.path.join(HERE, "gnd_links.json")
    by = {(pd.ref, pd.num): pd for p in parts for pd in p.pads}

    def freerouting(links):
        work = os.path.join(OUT, "_route")
        os.makedirs(work, exist_ok=True)
        dsn = os.path.join(work, NAME + ".dsn")
        route.write_dsn(parts, dsn, links)
        jar = os.path.join(board.EXT, "freerouting19.jar")
        cmd = ["xvfb-run", "-a", "java", "-jar", jar, "-de", dsn, "-do", SES, "-mp", "100"]
        print("Трассировка…", " ".join(cmd), f"(связей земли: {len(links)})")
        subprocess.run(cmd, check=True, capture_output=True)
        shutil.rmtree(work, ignore_errors=True)

    def routed(links, fresh):
        if fresh:
            freerouting(links)
        if "--reuse" in sys.argv and os.path.exists(state) and not fresh:
            st = json.load(open(state))
            tracks, vias = st["tracks"], st["vias"]
            for t in tracks:
                t["pts"] = tuple(t["pts"])
            print("Разводка — из", state)
            nets = board.nets_of(parts)
        else:
            tracks, vias, nets = route_all(parts)
            json.dump({"tracks": tracks, "vias": vias}, open(state, "w"))
        if not route.ROUTE_GND:          # иначе оси земли уже в разводке (трассировщик вёл их как готовые)
            tracks += route.body_ground(parts)
        return tracks, vias, nets, gerber.build_pours(parts, tracks, vias)

    links = []
    if os.path.exists(links_file):
        links = [(by[tuple(a)], by[tuple(b)]) for a, b in json.load(open(links_file))]
    if "--route" in sys.argv or not os.path.exists(SES):
        # зажатые земляные выводы: связь с ближайшим связанным земляным выводом — и трассировка заново
        links = []
        passes = int(os.environ.get("GND_PASSES", "1"))
        for it in range(passes):
            tracks, vias, nets, pours = routed(links, True)
            orph = sorted({o for o in pours["orphans"] if not o.startswith("U1.41")})
            print(f"  проход {it + 1}: переходов {len(vias)}, земляные выводы без связи: {orph or 'нет'}")
            if not orph or it == passes - 1:
                break
            used = {(a.ref, a.num) for pair in links for a in pair}
            bad = set(orph) | set(pours["orphans"])
            for o in orph:
                a = by[tuple(o.split(".", 1))]
                cand = [pd for (r, n), pd in by.items() if pd.net == "GND" and not pd.npth and f"{r}.{n}" not in bad
                        and (r, n) not in used and r != a.ref and (pd.layers & a.layers) and not (r == "U1" and n == "41")]
                if not cand or (a.ref, a.num) in used:
                    continue
                b = min(cand, key=lambda pd: math.hypot(pd.x - a.x, pd.y - a.y))
                links.append((a, b))
                used |= {(a.ref, a.num), (b.ref, b.num)}
        json.dump([[[a.ref, a.num], [b.ref, b.num]] for a, b in links], open(links_file, "w"))
    else:
        tracks, vias, nets, pours = routed(links, False)
    unrouted = {n: g for n, g in check.connectivity(parts, tracks, vias, nets).items() if n != "GND"}
    for n, groups in unrouted.items():
        print("НЕ РАЗВЕДЕНО:", n, [[f"{p.ref}.{p.num}" for p in g] for g in groups])
    print("Заливка земли…")
    for _ in range(3):
        if not pours["orphans"]:
            break
        print("  выводы земли без связи:", pours["orphans"])
        done = set()
        for pid in sorted(set(pours["orphans"])):
            if pid in done:
                continue
            done.add(pid)
            ref, num = pid.split(".", 1)
            pad = next(pd for p in parts for pd in p.pads if pd.ref == ref and pd.num == num)
            masks = {s: pours["pours"][s + "_keep"] for s in "FB"}
            if any(finish.connect(parts, tracks, vias, "GND", [[pad]], w, dst_masks=masks, allow_vias=av)
                   for av in (False, True) for w in (0.6, 0.4, 0.3)):
                print("  выведен на землю дорожкой:", pid)
            elif finish.rip_gnd(parts, tracks, vias, pad, masks, nets, lambda n: (route.WIDTH[route.net_class(n)], 0.4, 0.3)):
                print("  выведен на землю с переразводкой соседней цепи:", pid)
        pours = gerber.build_pours(parts, tracks, vias)
    errs = check.clearances(parts, tracks, vias + pours["stitch"])
    for e in errs:
        print("ЗАЗОР:", e)
    under = check.vias_under_parts(parts, vias + pours["stitch"])
    for u in under:
        print("ПЕРЕХОД ПОД ДЕТАЛЬЮ:", u)
    print(f"  переходов: трассировка {len(vias)}, сшивка земли {len(pours['stitch'])}; "
          f"земляные выводы без связи: {pours['orphans'] or 'нет'}")
    texts = silk_items(parts)
    layers = {
        f"{NAME}-F_Cu.gtl": gerber.copper_layer("F", parts, tracks, vias, pours),
        f"{NAME}-B_Cu.gbl": gerber.copper_layer("B", parts, tracks, vias, pours),
        f"{NAME}-F_Mask.gts": gerber.mask_layer("F", parts),
        f"{NAME}-B_Mask.gbs": gerber.mask_layer("B", parts),
        f"{NAME}-F_Paste.gtp": gerber.paste_layer("F", parts),
        f"{NAME}-B_Paste.gbp": gerber.paste_layer("B", parts),
        f"{NAME}-F_Silkscreen.gto": gerber.silk_layer("F", parts, silk_strokes(texts, "F")),
        f"{NAME}-B_Silkscreen.gbo": gerber.silk_layer("B", parts, silk_strokes(texts, "B")),
        f"{NAME}-Edge_Cuts.gm1": gerber.edge_layer(),
    }
    pth, npth = gerber.drill_files(parts, vias, pours["stitch"])
    layers[f"{NAME}-PTH.drl"] = pth
    layers[f"{NAME}-NPTH.drl"] = npth
    gdir = os.path.join(OUT, "gerber")
    shutil.rmtree(gdir, ignore_errors=True)
    os.makedirs(gdir)
    for fn, text in layers.items():
        with open(os.path.join(gdir, fn), "w") as f:
            f.write(text)
    zpath = os.path.join(OUT, f"{NAME}-gerber.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in layers:
            z.write(os.path.join(gdir, fn), fn)
    import verify
    files = {"F_Cu": layers[f"{NAME}-F_Cu.gtl"], "B_Cu": layers[f"{NAME}-B_Cu.gbl"], "F_Mask": layers[f"{NAME}-F_Mask.gts"],
             "B_Mask": layers[f"{NAME}-B_Mask.gbs"], "F_Silk": layers[f"{NAME}-F_Silkscreen.gto"],
             "B_Silk": layers[f"{NAME}-B_Silkscreen.gbo"],
             "Edge": layers[f"{NAME}-Edge_Cuts.gm1"], "PTH": pth, "NPTH": npth}
    gerr, _, _ = verify.check(files, parts)
    for e in gerr:
        print("GERBER:", e)
    write_bom(parts)
    write_cpl(parts)
    import lay6_export
    cut = [(sd, outer) for sd in "FB" for outer, _ in gerber.pour_polygons(pours["remove"][sd])]
    lay6_export.write(os.path.join(OUT, NAME + ".lay6"), parts, tracks, vias, pours["stitch"], texts, cutouts=cut)
    print(f"Sprint Layout 6: {NAME}.lay6")
    # картинки
    all_vias = vias + pours["stitch"]
    img = os.path.join(OUT, "img")
    os.makedirs(img, exist_ok=True)
    verify.picture(files, os.path.join(img, "pcb-top.png"), "F")
    verify.picture(files, os.path.join(img, "pcb-bottom.png"), "B")
    render.render(parts, tracks, all_vias, os.path.join(img, "pcb-layout.png"), "F", labels=True)
    render.render_assembly(parts, os.path.join(img, "assembly-top.png"), "F")
    render.render_assembly(parts, os.path.join(img, "assembly-bottom.png"), "B")
    print(f"Готово: {zpath}; дорожек {len(tracks)}, переходов {len(all_vias)}; не разведено: {len(unrouted)}; зазоров: {len(errs)}")
    # проект KiCad
    import kicad_pcb
    import kicad_sch
    import schematic
    kdir = os.path.join(OUT, "kicad")
    os.makedirs(kdir, exist_ok=True)
    netnums = kicad_pcb.write_pcb(os.path.join(kdir, NAME + ".kicad_pcb"), parts, tracks, all_vias, texts)
    kicad_pcb.write_project(os.path.join(kdir, NAME + ".kicad_pro"), list(netnums))
    kicad_sch.write(os.path.join(kdir, NAME + ".kicad_sch"))
    import pcb_check
    perr, _ = pcb_check.compare(os.path.join(kdir, NAME + ".kicad_pcb"), parts)
    for e in perr:
        print("KICAD:", e)
    import sch_check
    serr = sch_check.compare(os.path.join(kdir, NAME + ".kicad_sch"))
    for e in serr:
        print("СХЕМА:", e)
    schematic.write(os.path.join(OUT, "schematic.svg"), os.path.join(OUT, "schematic.pdf"), os.path.join(img, "schematic.png"))
    return 0 if not unrouted and not errs and not pours["orphans"] and not gerr and not serr and not perr else 1


def _pour_img(pours, side):
    import numpy as np
    from PIL import Image
    keep = pours["pours"][side + "_keep"]
    # растр анализа (gerber.RES) -> растр картинки (render.PX) со сдвигом на поле
    im = Image.fromarray(keep.astype(np.uint8) * 255).resize(
        (int(keep.shape[1] * render.PX / gerber.RES), int(keep.shape[0] * render.PX / gerber.RES)))
    full = Image.new("L", (int((W + 2 * render.M) * render.PX), int((H + 2 * render.M) * render.PX)), 0)
    full.paste(im, (render.M * render.PX, render.M * render.PX))
    return np.array(full) > 127


GROUP_KEYS = ("value", "fpkey")


def write_bom(parts):
    groups = {}
    for p in parts:
        if p.sym == "MH":
            continue
        groups.setdefault((p.value, p.fpkey, p.descr if p.sym in ("CONN2", "CONN3", "CONN4", "CONN6", "USBC") else ""),
                          []).append(p)
    generic = {"C": "Керамика X5R/X7R", "R": "Резистор 1%"}
    rows = []
    for (value, fpkey, _), ps in groups.items():
        if len({p.descr for p in ps}) > 1:
            ps[0] = _Desc(ps[0], generic.get(ps[0].sym, ps[0].descr))
        refs = sorted((p.ref for p in ps), key=lambda r: (r.rstrip("0123456789"), int(r[len(r.rstrip("0123456789")):])))
        rows.append((refs, value, fpkey, ps[0]))
    rows.sort(key=lambda r: (r[0][0].rstrip("0123456789"), int(r[0][0][len(r[0][0].rstrip("0123456789")):])))
    pkg = {
        "R0603": "0603", "C0603": "0603", "C0805": "0805", "C1206": "1206", "CP8x10": "SMD 8x10", "L0603": "0603",
        "L0805": "0805", "L4040": "4x4 мм", "CP6x7": "SMD 6,3x7,7", "F1812": "1812", "SMA": "SMA", "LED0603": "0603", "SOT23": "SOT-23",
        "SOT23-5": "SOT-23-5", "SOT23-6": "SOT-23-6", "SOT223": "SOT-223", "TSSOP20": "TSSOP-20", "SOIC16": "SOP-16",
        "ESP": "модуль", "SW": "4,2x2,8 мм", "H2": "PLS-2", "H3": "PLS-3", "H4": "PLS-4", "H6": "PLS-6",
    }
    with open(os.path.join(OUT, "bom.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["№", "Обозначения", "Кол-во", "Номинал", "Корпус", "Описание"])
        for i, (refs, value, fpkey, p) in enumerate(rows, 1):
            w.writerow([i, ", ".join(refs), len(refs), value, pkg.get(fpkey, fpkey), p.descr])
    # формат JLCPCB: Comment, Designator, Footprint
    with open(os.path.join(OUT, "bom-jlcpcb.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
        for refs, value, fpkey, p in rows:
            w.writerow([value, ",".join(refs), board.FP[fpkey].split(":")[1], ""])
    # для README
    md = io.StringIO()
    md.write("| № | Обозначения | Кол. | Номинал | Корпус | Описание |\n|---|---|---|---|---|---|\n")
    for i, (refs, value, fpkey, p) in enumerate(rows, 1):
        md.write(f"| {i} | {', '.join(refs)} | {len(refs)} | {value} | {pkg.get(fpkey, fpkey)} | {p.descr} |\n")
    with open(os.path.join(OUT, "bom.md"), "w", encoding="utf-8") as f:
        f.write(md.getvalue())
    return rows


class _Desc:
    def __init__(self, part, descr):
        self.__dict__.update(part.__dict__)
        self.descr = descr


def write_cpl(parts):
    with open(os.path.join(OUT, "cpl-jlcpcb.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        for p in parts:
            if p.sym == "MH" or not p.smd and p.sym.startswith("CONN"):
                continue
            layer = "Bottom" if p.side == "B" else "Top"
            pads = [pd for pd in p.pads if not pd.npth]
            cx = (min(pd.x - pd.w / 2 for pd in pads) + max(pd.x + pd.w / 2 for pd in pads)) / 2
            cy = (min(pd.y - pd.h / 2 for pd in pads) + max(pd.y + pd.h / 2 for pd in pads)) / 2
            if p.ref == "U1":
                cx, cy = p.x, p.y
            w.writerow([p.ref, f"{cx:.3f}mm", f"{H - cy:.3f}mm", layer, int(p.rot) % 360])


if __name__ == "__main__":
    sys.exit(main())
