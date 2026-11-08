# Принципиальная схема (SVG -> PDF) из того же описания design.py: детали, выводы, цепи.
# Соединения показаны метками цепей и символами питания у выводов — так схема всегда совпадает с платой.

import html
import math

from design import PARTS, G, V5, V33, VA, VD, VM, VAMP

SHEET_W, SHEET_H = 420.0, 297.0
POWER = {G, V5, V33, VA, VD, VM, VAMP}
PL = 2.54   # длина вывода

# ---------------------------------------------------------------- символы (локальные координаты, Y вниз)


def two_pin(kind):
    """Вертикальная двухвыводная деталь: вывод 1 сверху, 2 снизу (у диодов: 1 = катод снизу, 2 = анод сверху)."""
    g = []
    top, bot = -5.08, 5.08
    pins = {"1": (0, top, "up"), "2": (0, bot, "down")}
    if kind in ("D", "TVS", "SCHOTTKY", "LED"):
        pins = {"2": (0, top, "up"), "1": (0, bot, "down")}
    if kind == "R":
        g += [("rect", -1.0, -2.54, 1.0, 2.54), ("line", 0, top, 0, -2.54), ("line", 0, 2.54, 0, bot)]
    elif kind in ("C", "CP"):
        g += [("line", 0, top, 0, -0.6), ("line", 0, 0.6, 0, bot), ("thick", -2.0, -0.6, 2.0, -0.6)]
        if kind == "CP":
            g += [("arc", -2.0, 1.2, 2.0, 1.2, 0.8), ("text", -2.6, -1.5, "+", 1.6, "middle")]
            g[1] = ("line", 0, 0.75, 0, bot)
        else:
            g += [("thick", -2.0, 0.6, 2.0, 0.6)]
    elif kind == "L":
        g += [("line", 0, top, 0, -2.54), ("line", 0, 2.54, 0, bot)]
        for i in range(4):
            y0 = -2.54 + i * 1.27
            g.append(("arc", 0, y0, 0, y0 + 1.27, -0.64))
    elif kind == "FB":
        g += [("line", 0, top, 0, -1.8), ("line", 0, 1.8, 0, bot), ("frect", -1.1, -1.8, 1.1, 1.8)]
    elif kind == "FUSE":
        g += [("rect", -1.0, -2.54, 1.0, 2.54), ("line", 0, top, 0, bot)]
    elif kind in ("D", "TVS", "SCHOTTKY", "LED"):
        g += [("line", 0, top, 0, -1.0), ("line", 0, 1.0, 0, bot),
              ("poly", [(-1.5, -1.0), (1.5, -1.0), (0, 1.0)]), ("thick", -1.5, 1.0, 1.5, 1.0)]
        if kind == "TVS":
            g += [("line", -1.5, 1.0, -1.5, 0.4), ("line", 1.5, 1.0, 1.5, 1.6)]
        if kind == "SCHOTTKY":
            g += [("line", -1.5, 1.0, -1.5, 1.6), ("line", -1.5, 1.6, -2.0, 1.6), ("line", 1.5, 1.0, 1.5, 0.4),
                  ("line", 1.5, 0.4, 2.0, 0.4)]
        if kind == "LED":
            g += [("line", 1.8, -0.6, 3.2, -1.6), ("line", 1.8, 0.4, 3.2, -0.6), ("poly", [(3.2, -1.6), (2.4, -1.4), (2.9, -0.9)]),
                  ("poly", [(3.2, -0.6), (2.4, -0.4), (2.9, 0.1)])]
    elif kind == "SW":
        g += [("line", 0, top, 0, -2.0), ("line", 0, 2.0, 0, bot), ("dot", 0, -2.0), ("dot", 0, 2.0),
              ("line", 0, 2.0, -1.8, -1.6), ("line", -1.3, -0.4, -2.6, -0.4)]
    return {"g": g, "pins": pins, "box": (-2.5, -3, 2.5, 3), "names": {}}


def ic(left, right, bottom=(), top=(), width=15.24, title=""):
    """Прямоугольник с выводами. Списки: (номер, имя) или None (пропуск)."""
    rows = max(len(left), len(right), 1)
    h = (rows + 1) * 2.54
    x1, x2 = -width / 2, width / 2
    y1 = -h / 2
    y1 = round(y1 / 2.54) * 2.54
    y2 = y1 + h
    pins, names = {}, {}
    for i, p in enumerate(left):
        if p:
            y = y1 + 2.54 * (i + 1)
            pins[str(p[0])] = (x1 - PL, y, "left")
            names[str(p[0])] = p[1]
    for i, p in enumerate(right):
        if p:
            y = y1 + 2.54 * (i + 1)
            pins[str(p[0])] = (x2 + PL, y, "right")
            names[str(p[0])] = p[1]
    for side, lst, yy, d in (("bottom", bottom, y2 + PL, "down"), ("top", top, y1 - PL, "up")):
        n = len(lst)
        for i, p in enumerate(lst):
            x = (i - (n - 1) / 2) * 5.08
            pins[str(p[0])] = (x, yy, d)
            names[str(p[0])] = p[1]
    g = [("rect", x1, y1, x2, y2)]
    if title and width >= 20:
        g.append(("text", 0, y1 + (y2 - y1) / 2, title, 1.6, "middle", "bold"))
    return {"g": g, "pins": pins, "box": (x1, y1, x2, y2), "names": names, "ic": True}


def conn(names, side="right", title=""):
    lst = [(i + 1, n) for i, n in enumerate(names)]
    return ic(lst if side == "left" else [], lst if side == "right" else [], width=7.62, title=title)


SYMBOLS = {
    "ESP32-S3-WROOM-1": ic(
        [(2, "3V3"), (3, "EN"), (27, "IO0/BOOT"), (13, "IO19/USB_D-"), (14, "IO20/USB_D+"), (36, "RXD0"), (37, "TXD0"), None,
         (12, "IO8"), (15, "IO3"), (16, "IO46"), (17, "IO9"), (18, "IO10"), (19, "IO11"), (20, "IO12"), (21, "IO13"),
         (22, "IO14"), (23, "IO21")],
        [(4, "IO4"), (5, "IO5"), (6, "IO6"), (7, "IO7"), (8, "IO15"), (9, "IO16"), (10, "IO17"), (11, "IO18"), None,
         (24, "IO47"), (25, "IO48"), (26, "IO45"), (28, "IO35"), (29, "IO36"), (30, "IO37"), (31, "IO38"), (32, "IO39"),
         (33, "IO40"), (34, "IO41"), (35, "IO42"), (38, "IO2"), (39, "IO1")],
        bottom=[(1, "GND"), (40, "GND"), (41, "EPAD")], width=30.48, title="ESP32-S3-WROOM-1"),
    "PCM5102A": ic(
        [(1, "CPVDD"), (8, "AVDD"), (20, "DVDD"), None, (13, "BCK"), (14, "DIN"), (15, "LRCK"), (12, "SCK"), None,
         (17, "XSMT"), (16, "FMT"), (11, "FLT"), (10, "DEMP")],
        [(6, "OUTL"), (7, "OUTR"), None, (2, "CAPP"), (4, "CAPM"), (5, "VNEG"), None, (18, "LDOO")],
        bottom=[(3, "CPGND"), (9, "AGND"), (19, "DGND")], width=20.32, title="PCM5102A"),
    "PAM8403": ic(
        [(4, "PVDD"), (13, "PVDD"), (6, "VDD"), (5, "MUTE"), (12, "SHDN"), None, (7, "INL"), (10, "INR"), (8, "VREF")],
        [(3, "+OUTL"), (1, "-OUTL"), None, (14, "+OUTR"), (16, "-OUTR")],
        bottom=[(2, "PGND"), (15, "PGND"), (9, "GND"), (11, "GND")], width=20.32, title="PAM8403"),
    "AMS1117": ic([(3, "VIN")], [(2, "VOUT")], bottom=[(1, "GND")], width=12.7, title="AMS1117-3.3"),
    "LP5907": ic([(1, "IN"), (3, "EN")], [(5, "OUT"), (4, "NC")], bottom=[(2, "GND")], width=12.7, title="LP5907-3.3"),
    "USBLC6": ic([(1, "IO1"), (3, "IO2")], [(6, "IO1"), (4, "IO2")], top=[(5, "VBUS")], bottom=[(2, "GND")], width=12.7,
                 title="USBLC6-2"),
    "USBC": ic([], [(n, n) for n in ("A4", "A9", "B4", "B9", "A5", "B5", "A6", "B6", "A7", "B7", "A8", "B8")],
               bottom=[("A1", "GND"), ("A12", ""), ("B1", ""), ("B12", ""), ("S1", "SH")], width=10.16, title="USB-C"),
    "PMOS": ic([(3, "D")], [(2, "S")], bottom=[(1, "G")], width=10.16, title="P-MOS"),
}
for k in ("R", "C", "CP", "L", "FB", "FUSE", "D", "TVS", "SCHOTTKY", "LED", "SW"):
    SYMBOLS[k] = two_pin(k)
SYMBOLS["USBC"]["names"].update({"A4": "VBUS", "A9": "VBUS", "B4": "VBUS", "B9": "VBUS", "A5": "CC1", "B5": "CC2",
                                 "A6": "D+", "B6": "D+", "A7": "D-", "B7": "D-", "A8": "SBU1", "B8": "SBU2"})

CONN_NAMES = {
    "J1": ["+5V", "GND"], "J3": ["GND", "VDD", "SD", "SCK", "WS", "L/R"], "J4": ["GND", "VDD", "SD", "SCK", "WS", "L/R"],
    "J5": ["L+", "L-", "R+", "R-"], "J6": ["GND", "TX", "RX"], "J7": ["L", "R", "GND"],
}

# ---------------------------------------------------------------- расстановка на листе A3: ref -> (x, y, поворот)

POS = {
    # питание
    "J1": (24, 40, 0), "F1": (45, 34.92, 90), "Q1": (66, 34.92, 0), "R1": (66, 52, 0), "D1": (86, 45, 180),
    "C1": (98, 45, 0), "C2": (108, 45, 0), "L1": (122, 34.92, 90), "C26": (134, 45, 0), "C27": (144, 45, 0),
    "U2": (52, 82, 0), "C3": (32, 92, 0), "C4": (74, 92, 0), "C5": (84, 92, 0),
    "U4": (114, 82, 0), "C11": (96, 94, 0), "C12": (134, 94, 0), "C13": (142, 94, 0),
    # кнопки, светодиод
    "SW1": (30, 146, 0), "R4": (40, 136, 0), "C8": (50, 146, 0), "SW2": (68, 146, 0), "R5": (78, 136, 0),
    "R6": (98, 134, 0), "D3": (98, 156, 0),
    # ESP32
    "U1": (205, 95, 0), "C6": (170, 36, 0), "C7": (180, 36, 0),
    # микрофоны
    "R7": (180, 220, 90), "R8": (180, 232, 90), "R9": (196, 256, 0), "R10": (180, 205, 90), "C9": (196, 214, 0),
    "C10": (206, 214, 0), "J3": (238, 220, 0), "J4": (238, 255, 0),
    # ЦАП
    "U3": (330, 72, 0), "C14": (296, 36, 0), "C15": (286, 36, 0),
    "FB1": (306, 30, 90), "C16": (318, 36, 0), "C17": (328, 36, 0),
    "C18": (362, 77, 0), "C19": (372, 80, 0), "C20": (372, 98, 0),
    "R11": (372, 52, 90), "R12": (372, 60, 90), "C21": (388, 62, 0), "C22": (398, 70, 0),
    "R13": (300, 110, 0),
    # усилитель
    "R14": (284, 170, 90), "R15": (303, 186, 0), "C23": (322, 170, 90),
    "R16": (284, 196, 90), "R17": (303, 222, 0), "C24": (322, 196, 90), "C25": (290, 240, 0),
    "U5": (350, 190, 0), "C28": (318, 250, 0), "C29": (328, 250, 0), "C30": (338, 250, 0),
    "FB2": (380, 168, 90), "FB3": (380, 176, 90), "FB4": (380, 206, 90), "FB5": (380, 214, 90),
    "J5": (405, 190, 0),
}


def snap(v, g=1.27):
    return round(round(v / g) * g, 4)


POS = {k: (snap(x), snap(y), r) for k, (x, y, r) in POS.items()}   # выводы — на сетке 1,27 мм, как в KiCad

BLOCKS = [
    ("Питание 5 В: защита, 3,3 В для ESP32, малошумящие 3,3 В для ЦАП, фильтр усилителя", 10, 15, 150, 108),
    ("Сброс, BOOT, светодиод", 10, 115, 150, 198),
    ("Первая прошивка", 10, 205, 150, 285),
    ("ESP32-S3", 156, 15, 266, 190),
    ("Микрофоны INMP441 x2", 156, 195, 266, 285),
    ("ЦАП PCM5102A", 272, 15, 410, 125),
    ("Усилитель PAM8403 2x3 Вт", 272, 132, 410, 285),
]

NOTES = [
    (12, 289, "Плата клиента Русланы v3. Питание 5 В (J1). GPIO: микрофоны SCK 4, WS 5, SD 6; ЦАП DIN 7, BCK 15, LRCK 16; "
              "AMP_EN 17 (XSMT + SHDN); LED 18."),
    (14, 215, "Разъёма USB на плате нет. Для первой прошивки подпаяться проводами к выводам модуля U1:"),
    (14, 221, "USB: вывод 13 (IO19, D-), 14 (IO20, D+), GND, питание 5 В на J1;"),
    (14, 227, "или UART: вывод 37 (TXD0), 36 (RXD0), GND — через переходник USB-UART 3,3 В."),
    (14, 233, "Вход в загрузчик: держать BOOT, нажать и отпустить RESET, отпустить BOOT."),
    (14, 239, "Дальше прошивка обновляется по Wi-Fi через веб-интерфейс сервера Русланы."),
]


def tf(x, y, ox, oy, rot):
    """Локальная точка -> лист (поворот против часовой стрелки на экране, Y вниз)."""
    a = math.radians(rot)
    c, s = round(math.cos(a), 9), round(math.sin(a), 9)
    return ox + x * c + y * s, oy - x * s + y * c


DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


def rot_dir(d, rot):
    dx, dy = DIRS[d]
    x, y = tf(dx, dy, 0, 0, rot)
    for k, v in DIRS.items():
        if abs(v[0] - x) < 1e-6 and abs(v[1] - y) < 1e-6:
            return k
    return d


class Svg:
    def __init__(self):
        self.o = []

    def line(self, x1, y1, x2, y2, w=0.25, color="#8b0000"):
        self.o.append(f'<line x1="{x1:.3f}" y1="{y1:.3f}" x2="{x2:.3f}" y2="{y2:.3f}" stroke="{color}" stroke-width="{w}" stroke-linecap="round"/>')

    def text(self, x, y, s, size=1.6, anchor="start", color="#004070", weight="normal", rot=0, baseline="middle"):
        tr = f' transform="rotate({rot} {x:.3f} {y:.3f})"' if rot else ""
        self.o.append(f'<text x="{x:.3f}" y="{y:.3f}" font-size="{size}" text-anchor="{anchor}" dominant-baseline="{baseline}" '
                      f'fill="{color}" font-weight="{weight}"{tr}>{html.escape(s)}</text>')

    def raw(self, s):
        self.o.append(s)


def draw_symbol(svg, sym, ox, oy, rot):
    col = "#8b0000"
    for it in sym["g"]:
        k = it[0]
        if k in ("line", "thick"):
            a = tf(it[1], it[2], ox, oy, rot)
            b = tf(it[3], it[4], ox, oy, rot)
            svg.line(*a, *b, 0.45 if k == "thick" else 0.25, col)
        elif k in ("rect", "frect"):
            pts = [tf(x, y, ox, oy, rot) for x, y in ((it[1], it[2]), (it[3], it[2]), (it[3], it[4]), (it[1], it[4]))]
            fill = col if k == "frect" else "#fffbe6" if sym.get("ic") else "none"
            svg.raw(f'<polygon points="{" ".join(f"{x:.3f},{y:.3f}" for x, y in pts)}" fill="{fill}" stroke="{col}" stroke-width="0.3"/>')
        elif k == "poly":
            pts = [tf(x, y, ox, oy, rot) for x, y in it[1]]
            svg.raw(f'<polygon points="{" ".join(f"{x:.3f},{y:.3f}" for x, y in pts)}" fill="{col}" stroke="{col}" stroke-width="0.2"/>')
        elif k == "arc":
            a = tf(it[1], it[2], ox, oy, rot)
            b = tf(it[3], it[4], ox, oy, rot)
            r = abs(math.hypot(b[0] - a[0], b[1] - a[1])) / 2
            sweep = 1 if it[5] > 0 else 0
            svg.raw(f'<path d="M{a[0]:.3f},{a[1]:.3f} A{r:.3f},{r:.3f} 0 0 {sweep} {b[0]:.3f},{b[1]:.3f}" fill="none" '
                    f'stroke="{col}" stroke-width="0.35"/>')
        elif k == "dot":
            x, y = tf(it[1], it[2], ox, oy, rot)
            svg.raw(f'<circle cx="{x:.3f}" cy="{y:.3f}" r="0.45" fill="{col}"/>')
        elif k == "text":
            x, y = tf(it[1], it[2], ox, oy, rot)
            svg.text(x, y, it[3], it[4], it[5] if len(it) > 5 else "start", col, it[6] if len(it) > 6 else "normal")


def draw_power(svg, net, x, y, d):
    """Символ питания/земли у конца вывода; d — куда смотрит вывод наружу."""
    dx, dy = DIRS[d]
    col = "#a00000"
    if net == G:
        # земля всегда рисуется вниз, при необходимости с коротким проводом
        ex, ey = x + dx * 2.54, y + dy * 2.54
        svg.line(x, y, ex, ey, 0.25, "#006000")
        if d == "up":
            svg.line(ex, ey, ex + 2.54, ey, 0.25, "#006000")
            ex += 2.54
        gx, gy = ex, ey
        svg.line(gx, gy, gx, gy + 1.0, 0.25, col)
        svg.raw(f'<polygon points="{gx - 1.5:.3f},{gy + 1.0:.3f} {gx + 1.5:.3f},{gy + 1.0:.3f} {gx:.3f},{gy + 2.6:.3f}" '
                f'fill="none" stroke="{col}" stroke-width="0.3"/>')
        return
    ex, ey = x + dx * 2.54, y + dy * 2.54
    svg.line(x, y, ex, ey, 0.25, "#006000")
    if d == "down":
        svg.line(ex, ey, ex + 2.54, ey, 0.25, "#006000")
        ex += 2.54
    svg.line(ex, ey, ex, ey - 1.2, 0.25, col)
    svg.line(ex - 1.2, ey - 1.2, ex + 1.2, ey - 1.2, 0.45, col)
    svg.text(ex, ey - 2.5, net, 1.4, "middle", col, "bold")


def draw_label(svg, net, x, y, d, color="#005000", weight="normal"):
    dx, dy = DIRS[d]
    ex, ey = x + dx * 1.27, y + dy * 1.27
    svg.line(x, y, ex, ey, 0.25, "#006000")
    if d == "right":
        svg.text(ex + 0.4, ey, net, 1.5, "start", color, weight)
    elif d == "left":
        svg.text(ex - 0.4, ey, net, 1.5, "end", color, weight)
    elif d == "up":
        svg.text(ex, ey - 0.4, net, 1.5, "start", color, weight, rot=-90)
    else:
        svg.text(ex, ey + 0.4, net, 1.5, "end", color, weight, rot=-90)


def build_svg():
    svg = Svg()
    for title, x1, y1, x2, y2 in BLOCKS:
        svg.raw(f'<rect x="{x1}" y="{y1}" width="{x2 - x1}" height="{y2 - y1}" fill="none" stroke="#7a7a7a" '
                f'stroke-width="0.3" stroke-dasharray="2,1.2" rx="1.5"/>')
        svg.text(x1 + 2, y1 + 3.2, title, 2.2, "start", "#333", "bold")
    for ref, sym, value, fpkey, pins, descr in PARTS:
        if sym == "MH":
            continue
        if sym.startswith("CONN"):
            s = conn(CONN_NAMES[ref], "left")
        else:
            s = SYMBOLS[sym]
        ox, oy, rot = POS[ref]
        draw_symbol(svg, s, ox, oy, rot)
        bx1, by1, bx2, by2 = s["box"]
        # обозначение и номинал
        if s.get("ic"):
            tl = tf(bx1, by1, ox, oy, 0)
            label = ref if sym == "ESP32-S3-WROOM-1" else f"{ref}  {value}"
            svg.text(tl[0], tl[1] - 1.4, label, 1.8, "start", "#00008b", "bold")
        else:
            if rot % 180 == 0:
                svg.text(ox + 2.6, oy - 1.0, ref, 1.6, "start", "#00008b", "bold")
                svg.text(ox + 2.6, oy + 1.2, value, 1.3, "start", "#555")
            else:
                svg.text(ox, oy - 2.6, ref, 1.6, "middle", "#00008b", "bold")
                svg.text(ox, oy + 3.4, value, 1.3, "middle", "#555")
        pinmap = {str(k): v for k, v in pins.items()}
        for num, (px, py, d) in s["pins"].items():
            x, y = tf(px, py, ox, oy, rot)
            dd = rot_dir(d, rot)
            # линия вывода у микросхем
            if s.get("ic"):
                ix, iy = x - DIRS[dd][0] * PL, y - DIRS[dd][1] * PL
                svg.line(ix, iy, x, y, 0.25, "#8b0000")
                name = s["names"].get(num, "")
                if dd == "left":
                    svg.text(ix + 0.8, iy, name, 1.3, "start", "#333")
                    svg.text(x + 1.2, y - 0.7, num, 1.0, "middle", "#888")
                elif dd == "right":
                    svg.text(ix - 0.8, iy, name, 1.3, "end", "#333")
                    svg.text(x - 1.2, y - 0.7, num, 1.0, "middle", "#888")
                elif dd == "down":
                    if name:
                        svg.text(ix, iy - 0.8, name, 1.1, "end", "#333", rot=-90)
                    svg.text(x + 0.8, y - 1.2, num, 1.0, "start", "#888")
                else:
                    svg.text(ix, iy + 0.8, name, 1.1, "start", "#333", rot=-90)
                    svg.text(x + 0.8, y + 1.2, num, 1.0, "start", "#888")
            if num not in pinmap:
                continue
            net = pinmap[num]
            if net is None:
                svg.line(x - 0.8, y - 0.8, x + 0.8, y + 0.8, 0.25, "#0000c0")
                svg.line(x - 0.8, y + 0.8, x + 0.8, y - 0.8, 0.25, "#0000c0")
            elif net in POWER and dd in ("left", "right") and s.get("ic"):
                draw_label(svg, net, x, y, dd, "#a00000", "bold")   # у боковых выводов микросхем — подпись цепи питания
            elif net in POWER:
                draw_power(svg, net, x, y, dd)
            else:
                draw_label(svg, net, x, y, dd)
    for x, y, t in NOTES:
        svg.text(x, y, t, 1.8, "start", "#333")
    svg.text(408, 289, "Ruslana client v3 — схема", 2.0, "end", "#333", "bold")
    body = "\n".join(svg.o)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{SHEET_W}mm" height="{SHEET_H}mm" '
            f'viewBox="0 0 {SHEET_W} {SHEET_H}" font-family="DejaVu Sans, Arial, sans-serif">\n'
            f'<rect x="0" y="0" width="{SHEET_W}" height="{SHEET_H}" fill="white"/>\n'
            f'<rect x="5" y="5" width="{SHEET_W - 10}" height="{SHEET_H - 10}" fill="none" stroke="#333" stroke-width="0.5"/>\n'
            f'{body}\n</svg>\n')


def write(svg_path, pdf_path=None, png_path=None):
    s = build_svg()
    with open(svg_path, "w", encoding="utf-8") as f:
        f.write(s)
    if pdf_path or png_path:
        from playwright.sync_api import sync_playwright
        page_html = f"<html><head><style>@page {{ size: 420mm 297mm; margin: 0 }} body {{ margin: 0 }}</style></head><body>{s}</body></html>"
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 2400, "height": int(2400 * SHEET_H / SHEET_W)})
            pg.set_content(page_html.replace(f'width="{SHEET_W}mm" height="{SHEET_H}mm"', 'width="2400" height="1697"'))
            if png_path:
                pg.screenshot(path=png_path, full_page=True)
            if pdf_path:
                pg.set_content(page_html)
                pg.pdf(path=pdf_path, width="420mm", height="297mm", print_background=True)
            b.close()
