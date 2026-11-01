#!/usr/bin/env python3
"""Сборка производственных файлов платы клиента Ирины.

    IRENE_PCB_EXT=<папка с kiutils, kicad-footprints, kicad-libraries, freerouting.jar> python3 build.py [--route]

--route — заново развести плату Freerouting (иначе берётся сохранённая разводка routed.ses).
Результат — в ../irene-client: gerber/*.zip, перечень, файл расстановки, картинки, схема.
"""

import csv
import io
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import board  # noqa: E402
import check  # noqa: E402
import gerber  # noqa: E402
import render  # noqa: E402
import route  # noqa: E402
from placement import W, H, SILK_TEXT, PIN_LABELS  # noqa: E402

OUT = os.path.join(os.path.dirname(HERE), "irene-client")
NAME = "irene-client"
SES = os.path.join(HERE, "routed.ses")


def silk_strokes(parts):
    strokes = []
    for text, x, y, h, _ in SILK_TEXT:
        strokes += gerber.text_strokes(text, x, y, h)
    byref = {p.ref: p for p in parts}
    for ref, (labels, (dx, dy)) in PIN_LABELS.items():
        pads = {pd.num: pd for pd in byref[ref].pads}
        for i, lab in enumerate(labels, 1):
            pd = pads[str(i)]
            strokes += gerber.text_strokes(lab, pd.x + dx, pd.y + dy, 0.8, center=True)
    return strokes


def main():
    os.makedirs(OUT, exist_ok=True)
    parts = board.load_parts()
    bad = board.check_overlaps(parts)
    if bad:
        sys.exit(f"Детали перекрываются: {bad}")
    if "--route" in sys.argv or not os.path.exists(SES):
        work = os.path.join(OUT, "_route")
        os.makedirs(work, exist_ok=True)
        dsn = os.path.join(work, NAME + ".dsn")
        route.write_dsn(parts, dsn)
        jar = os.path.join(board.EXT, "freerouting19.jar")
        cmd = ["xvfb-run", "-a", "java", "-jar", jar, "-de", dsn, "-do", SES, "-mp", "100"]
        print("Трассировка…", " ".join(cmd))
        subprocess.run(cmd, check=True, capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
    tracks, vias = route.read_ses(SES)
    nets = board.nets_of(parts)
    unrouted = {n: g for n, g in check.connectivity(parts, tracks, vias, nets).items() if n != "GND"}
    for n, groups in unrouted.items():
        print("НЕ РАЗВЕДЕНО:", n, [[f"{p.ref}.{p.num}" for p in g] for g in groups])
    errs = check.clearances(parts, tracks, vias)
    for e in errs:
        print("ЗАЗОР:", e)
    print("Заливка земли…")
    pours = gerber.build_pours(parts, tracks, vias)
    if pours["orphans"]:
        print("  выводы земли без связи:", pours["orphans"], "— добавляю переходы на нижний слой")
        route.gnd_fanout(parts, tracks, vias, sorted(set(pours["orphans"])))
        pours = gerber.build_pours(parts, tracks, vias)
    print(f"  сшивающих переходов: {len(pours['stitch'])}; земляные выводы без связи: {pours['orphans'] or 'нет'}")
    silk = silk_strokes(parts)
    layers = {
        f"{NAME}-F_Cu.gtl": gerber.copper_layer("F", parts, tracks, vias, pours),
        f"{NAME}-B_Cu.gbl": gerber.copper_layer("B", parts, tracks, vias, pours),
        f"{NAME}-F_Mask.gts": gerber.mask_layer("F", parts),
        f"{NAME}-B_Mask.gbs": gerber.mask_layer("B", parts),
        f"{NAME}-F_Paste.gtp": gerber.paste_layer("F", parts),
        f"{NAME}-F_Silkscreen.gto": gerber.silk_layer("F", parts, silk),
        f"{NAME}-B_Silkscreen.gbo": gerber.silk_layer("B", parts, []),
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
             "Edge": layers[f"{NAME}-Edge_Cuts.gm1"], "PTH": pth, "NPTH": npth}
    gerr, _, _ = verify.check(files, parts)
    for e in gerr:
        print("GERBER:", e)
    write_bom(parts)
    write_cpl(parts)
    # картинки
    all_vias = vias + pours["stitch"]
    img = os.path.join(OUT, "img")
    os.makedirs(img, exist_ok=True)
    verify.picture(files, os.path.join(img, "pcb-top.png"), "F")
    verify.picture(files, os.path.join(img, "pcb-bottom.png"), "B")
    render.render(parts, tracks, all_vias, os.path.join(img, "pcb-layout.png"), "F", labels=True)
    render.render_assembly(parts, os.path.join(img, "assembly.png"))
    print(f"Готово: {zpath}; дорожек {len(tracks)}, переходов {len(all_vias)}; не разведено: {len(unrouted)}; зазоров: {len(errs)}")
    import schematic
    schematic.write(os.path.join(OUT, "schematic.svg"), os.path.join(OUT, "schematic.pdf"), os.path.join(img, "schematic.png"))
    return 0 if not unrouted and not errs and not pours["orphans"] and not gerr else 1


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
        "L0805": "0805", "L6045": "6x6 мм", "F1812": "1812", "SMA": "SMA", "LED0603": "0603", "SOT23": "SOT-23",
        "SOT23-5": "SOT-23-5", "SOT23-6": "SOT-23-6", "SOT223": "SOT-223", "TSSOP20": "TSSOP-20", "SOIC16": "SOP-16",
        "ESP": "модуль", "SW": "5,2x5,2 мм", "USBC": "USB-C 16P", "H2": "PLS-2", "H3": "PLS-3", "H4": "PLS-4", "H6": "PLS-6",
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
            pads = [pd for pd in p.pads if not pd.npth]
            cx = (min(pd.x - pd.w / 2 for pd in pads) + max(pd.x + pd.w / 2 for pd in pads)) / 2
            cy = (min(pd.y - pd.h / 2 for pd in pads) + max(pd.y + pd.h / 2 for pd in pads)) / 2
            if p.ref == "U1":
                cx, cy = p.x, p.y
            w.writerow([p.ref, f"{cx:.3f}mm", f"{H - cy:.3f}mm", "Top", int(p.rot) % 360])


if __name__ == "__main__":
    sys.exit(main())
