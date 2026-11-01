# Автотрассировка: плата -> Specctra DSN -> Freerouting -> SES -> дорожки и переходные отверстия.

import os
import re
import subprocess

from placement import W, H, ANTENNA_KEEPOUT
from design import POWER_NETS, SUPPLY_NETS

CLEARANCE = 0.3   # зазор между проводниками (по просьбе: 0,3–0,5 мм)
WIDTH = {"default": 0.25, "supply": 0.3, "power": 0.6}
PAD_CLEARANCE = 0.2   # от дорожки до вывода микросхемы: у TSSOP шаг 0,65 мм, больше не войти
VIAS = {"default": (0.6, 0.3), "supply": (0.6, 0.3), "power": (0.8, 0.4)}
SPEAKER = {"OUTL_P", "OUTL_N", "OUTR_P", "OUTR_N", "SPK_LP", "SPK_LN", "SPK_RP", "SPK_RN"}


def net_class(net):
    if net == "VBUS":
        return "supply"   # выводы USB-C узкие (0,6 мм с зазором 0,2): дорожка 0,6 мм от них не выходит
    if net in POWER_NETS:
        return "power"
    if net in SUPPLY_NETS:
        return "supply"
    return "default"


def um(v):
    return f"{v * 1000:.1f}"


def X(x):
    return um(x)


def Y(y):
    return um(H - y)   # DSN: ось Y вверх


def q(s):
    return '"' + s.replace('"', "") + '"'


def via_name(cls):
    d, dr = VIAS[cls]
    return f"Via[0-1]_{int(d * 1000)}:{int(dr * 1000)}_um"


def padstack(pad, cache):
    if pad.ref == "U1" and pad.num == "41":
        key = "esp_epad"
    else:
        key = f"{pad.shape}_{pad.w:.3f}x{pad.h:.3f}_{''.join(sorted(pad.layers))}"
    if key in cache:
        return cache[key][0]
    name = f"PS{len(cache)}"
    layers = [lname for k, lname in (("F", "F.Cu"), ("B", "B.Cu")) if k in pad.layers]
    shapes = []
    for L in layers:
        if key == "esp_epad":
            shapes.append(f"(shape (rect {L} {um(-1.85)} {um(-1.85)} {um(1.85)} {um(1.85)}))")
        elif pad.shape == "circle":
            shapes.append(f"(shape (circle {L} {um(pad.w)}))")
        elif pad.shape == "oval" and abs(pad.w - pad.h) > 1e-6:
            r = min(pad.w, pad.h)
            ln = (max(pad.w, pad.h) - r) / 2
            if pad.w > pad.h:
                shapes.append(f"(shape (path {L} {um(r)} {um(-ln)} 0 {um(ln)} 0))")
            else:
                shapes.append(f"(shape (path {L} {um(r)} 0 {um(-ln)} 0 {um(ln)}))")
        elif pad.shape == "oval":
            shapes.append(f"(shape (circle {L} {um(pad.w)}))")
        else:
            shapes.append(f"(shape (rect {L} {um(-pad.w / 2)} {um(-pad.h / 2)} {um(pad.w / 2)} {um(pad.h / 2)}))")
    cache[key] = (name, f"    (padstack {name}\n      " + "\n      ".join(shapes) + "\n      (attach off)\n    )")
    return name


def write_dsn(parts, path):
    cache = {}
    images, placement = [], []
    pins_by_net = {}
    for p in parts:
        lines = []
        for pad in p.pads:
            px, py = pad.x - p.x, -(pad.y - p.y)
            if pad.npth:
                lines.append(f"      (keepout \"\" (circle signal {um(pad.w + (2.4 if p.sym == 'MH' else 0.3))} {um(px)} {um(py)}))")
                continue
            if p.ref == "U1" and pad.num == "41":
                if pad.pin_name != "41":
                    continue
                cx, cy = 69.0 - 1.5, 15.75 - 0.54   # центр сетки из 9 площадок
                px, py = cx - p.x, -(cy - p.y)
            ps = padstack(pad, cache)
            lines.append(f"      (pin {ps} {q(pad.pin_name)} {um(px)} {um(py)})")
            if pad.net:
                pins_by_net.setdefault(pad.net, []).append(f"{p.ref}-{pad.pin_name}")
        img = f"IMG_{p.ref}"
        images.append(f"    (image {img}\n" + "\n".join(lines) + "\n    )")
        placement.append(f"    (component {img} (place {p.ref} {X(p.x)} {Y(p.y)} front 0))")
    vias = sorted({via_name(c) for c in VIAS})
    for v in vias:
        d, dr = [int(x) / 1000 for x in re.findall(r"_(\d+):(\d+)_", v)[0]]
        cache[v] = (v, f"    (padstack {q(v)}\n      (shape (circle F.Cu {um(d)}))\n      (shape (circle B.Cu {um(d)}))\n      (attach off)\n    )")
    kx1, ky1, kx2, ky2 = ANTENNA_KEEPOUT
    r = 0.6
    e = 0.5
    inset = [(e, e), (W - e, e), (W - e, H - e), (e, H - e), (e, e)]
    boundary = [(r, 0), (W - r, 0), (W, r), (W, H - r), (W - r, H), (r, H), (0, H - r), (0, r), (r, 0)]
    classes = {}
    for net in pins_by_net:
        classes.setdefault(net_class(net), []).append(net)
    cls_txt = []
    for c, nets in classes.items():
        w = WIDTH[c]
        cls_txt.append(f"    (class {c} " + " ".join(q(n) for n in nets) +
                       f"\n      (circuit (use_via {q(via_name(c))}))\n      (rule (width {um(w)}))\n    )")
    out = f"""(pcb {q(os.path.basename(path))}
  (parser (string_quote ") (space_in_quoted_tokens on) (host_cad "KiCad's Pcbnew") (host_version "7.0"))
  (resolution um 10)
  (unit um)
  (structure
    (layer F.Cu (type signal) (property (index 0)))
    (layer B.Cu (type signal) (property (index 1)))
    (boundary (path pcb 0 {' '.join(X(x) + ' ' + Y(y) for x, y in boundary)}))
    (keepout "antenna" (polygon signal 0 {X(kx1)} {Y(ky1)} {X(kx2)} {Y(ky1)} {X(kx2)} {Y(ky2)} {X(kx1)} {Y(ky2)}))
    (plane "GND" (polygon B.Cu 0 {' '.join(X(x) + ' ' + Y(y) for x, y in inset)}))
    (via {' '.join(q(v) for v in vias)})
    (rule (width {um(WIDTH['default'])}) (clearance {um(CLEARANCE)}) (clearance {um(PAD_CLEARANCE)} (type default_smd)) (clearance 100 (type smd_smd)))
  )
  (placement
{chr(10).join(placement)}
  )
  (library
{chr(10).join(images)}
{chr(10).join(v[1] for v in cache.values())}
  )
  (network
{chr(10).join(f'    (net {q(n)} (pins {" ".join(pins)}))' for n, pins in pins_by_net.items())}
{chr(10).join(cls_txt)}
  )
  (wiring)
)
"""
    with open(path, "w") as f:
        f.write(out)
    return pins_by_net


def run_freerouting(jar, dsn, ses, passes=40, log=None):
    cmd = ["java", "-jar", jar, "-de", dsn, "-do", ses, "-mp", str(passes), "--gui.enabled=false"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if log:
        with open(log, "w") as f:
            f.write(r.stdout + r.stderr)
    return r.returncode


def parse_sexpr(text):
    tokens = re.findall(r'\(|\)|"[^"]*"|[^\s()]+', text)
    stack = [[]]
    for t in tokens:
        if t == "(":
            stack.append([])
        elif t == ")":
            last = stack.pop()
            stack[-1].append(last)
        else:
            stack[-1].append(t.strip('"'))
    return stack[0][0]


def find(node, name):
    for n in node:
        if isinstance(n, list) and n and n[0] == name:
            yield n


def read_ses(path):
    tree = parse_sexpr(open(path).read())
    routes = next(find(tree, "routes"))
    res = next(find(routes, "resolution"))
    unit, k = res[1], float(res[2])
    scale = {"um": 0.001, "mm": 1.0, "mil": 0.0254}[unit] / k
    tracks, vias = [], []
    netout = next(find(routes, "network_out"))
    for net in find(netout, "net"):
        name = net[1]
        for w in find(net, "wire"):
            path = next(find(w, "path"))
            layer = "F" if path[1] == "F.Cu" else "B"
            width = float(path[2]) * scale
            nums = [float(v) for v in path[3:]]
            pts = [(nums[i] * scale, H - nums[i + 1] * scale) for i in range(0, len(nums), 2)]
            for a, b in zip(pts, pts[1:]):
                tracks.append({"net": name, "layer": layer, "w": round(width, 4), "pts": (a[0], a[1], b[0], b[1])})
        for v in find(net, "via"):
            d, dr = [int(x) / 1000 for x in re.findall(r"_(\d+):(\d+)_", v[1])[0]]
            vias.append({"net": name, "x": float(v[2]) * scale, "y": H - float(v[3]) * scale, "d": d, "drill": dr})
    return tracks, vias


def gnd_fanout(parts, tracks, vias, pad_ids, via=(0.6, 0.3), width=0.3, gap=0.3):
    """Короткая дорожка от вывода земли к новому переходному отверстию на сплошную землю нижнего слоя.
    Для выводов, которые трассировщик оставил без связи (pad_ids — «REF.номер»)."""
    import math
    from check import _seg_dist, _seg_seg
    allpads = [pd for p in parts for pd in p.pads]

    def pad_gap(pd, x1, y1, x2, y2):
        if pd.shape in ("circle", "oval"):
            r = min(pd.w, pd.h) / 2
            lx, ly = pd.w / 2 - r, pd.h / 2 - r
            return _seg_seg((x1, y1, x2, y2), (pd.x - lx, pd.y - ly, pd.x + lx, pd.y + ly)) - r
        best = 1e9
        n = max(2, int(math.hypot(x2 - x1, y2 - y1) / 0.05) + 1)
        for i in range(n + 1):
            x = x1 + (x2 - x1) * i / n
            y = y1 + (y2 - y1) * i / n
            best = min(best, math.hypot(max(abs(x - pd.x) - pd.w / 2, 0), max(abs(y - pd.y) - pd.h / 2, 0)))
        return best

    kx1, ky1, kx2, ky2 = ANTENNA_KEEPOUT
    added = []
    for pid in pad_ids:
        ref, num = pid.split(".", 1)
        pad = next(pd for pd in allpads if pd.ref == ref and pd.num == num)
        best = None
        for r10 in range(8, 35):
            r = r10 / 10
            for a in range(0, 360, 10):
                vx = pad.x + r * math.cos(math.radians(a))
                vy = pad.y + r * math.sin(math.radians(a))
                if not (1.0 < vx < W - 1.0 and 1.0 < vy < H - 1.0) or (kx1 - 1 < vx < kx2 and ky1 < vy < ky2 + 1):
                    continue
                seg = (pad.x, pad.y, vx, vy)
                ok = True
                for o in allpads:
                    if o.net == "GND" and not o.npth:
                        continue
                    if abs(o.x - vx) > 8 or abs(o.y - vy) > 8:
                        continue
                    if pad_gap(o, vx, vy, vx, vy) - via[0] / 2 < (gap if not o.npth else 0.4):
                        ok = False
                        break
                    if "F" in o.layers and pad_gap(o, *seg) - width / 2 < gap:
                        ok = False
                        break
                if not ok:
                    continue
                for t in tracks + added:
                    if t["net"] == "GND" or "pts" not in t:
                        continue
                    lim = gap if t["layer"] == "F" else 0.3   # снизу переход должен касаться заливки (проверяется анализом заливки)
                    if _seg_dist(vx, vy, *t["pts"]) - t["w"] / 2 - via[0] / 2 < lim:
                        ok = False
                        break
                    if t["layer"] == "F" and _seg_seg(seg, t["pts"]) - t["w"] / 2 - width / 2 < gap:
                        ok = False
                        break
                if not ok:
                    continue
                for v in vias:
                    if v["net"] != "GND" and (math.hypot(v["x"] - vx, v["y"] - vy) - v["d"] / 2 - via[0] / 2 < gap
                                              or _seg_dist(v["x"], v["y"], *seg) - v["d"] / 2 - width / 2 < gap):
                        ok = False
                        break
                if ok:
                    best = (vx, vy)
                    break
            if best:
                break
        if not best:
            print("  не удалось вывести на землю:", pid)
            continue
        vx, vy = best
        tracks.append({"net": "GND", "layer": "F", "w": width, "pts": (pad.x, pad.y, vx, vy)})
        vias.append({"net": "GND", "x": vx, "y": vy, "d": via[0], "drill": via[1]})
        added.append(tracks[-1])
    return added
