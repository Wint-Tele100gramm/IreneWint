# Чтение и запись файлов Sprint Layout 6 (.lay6).
# Формат — по описанию из KiCad (pcbnew/pcb_io/sprint_layout/sprint_layout.ksy). Неизвестные байты
# сохраняются как есть, поэтому чтение + запись даёт побайтово тот же файл (проверено на файлах Sprint Layout).
# Координаты — 1/10000 мм, ось Y вверх. Слои: 1 C1 (медь сверху), 2 S1 (шелк сверху), 3 C2 (медь снизу),
# 4 S2 (шелк снизу), 5 I1, 6 I2, 7 O (контур).

import struct

THT_PAD, POLY, CIRCLE, LINE, TEXT, SMD_PAD = 2, 4, 5, 6, 7, 8
C1, S1, C2, S2, I1, I2, O = 1, 2, 3, 4, 5, 6, 7


class R:
    def __init__(self, data):
        self.d, self.p = data, 0

    def take(self, n):
        b = self.d[self.p:self.p + n]
        if len(b) != n:
            raise ValueError(f"конец файла на {self.p}")
        self.p += n
        return b

    def u(self, fmt):
        v = struct.unpack_from("<" + fmt, self.d, self.p)
        self.p += struct.calcsize(fmt)
        return v[0]


class W:
    def __init__(self):
        self.b = bytearray()

    def put(self, raw):
        self.b += raw

    def u(self, fmt, v):
        self.b += struct.pack("<" + fmt, v)


def _fixed_r(r, n):
    ln = r.u("B")
    raw = r.take(n)
    return {"len": ln, "raw": raw}


def _fixed_w(w, s, n):
    w.u("B", s["len"])
    w.put(s["raw"].ljust(n, b"\0")[:n])


def fixed(text, n):
    raw = text.encode("cp1251")[:n]
    return {"len": len(raw), "raw": raw.ljust(n, b"\0")}


def _var_r(r):
    return r.take(r.u("I")).decode("cp1251")


def _var_w(w, s):
    raw = s.encode("cp1251")
    w.u("I", len(raw))
    w.put(raw)


# поля объекта по порядку: (имя, формат struct или число байт)
OBJ_FIELDS = [
    ("type", "B"), ("x", "f"), ("y", "f"), ("outer", "f"), ("inner", "f"), ("line_width", "I"),
    ("pad1", 1), ("layer", "B"), ("tht_shape", "B"), ("pad2", 4), ("component_id", "H"), ("selected", "B"),
    ("start_angle", "i"), ("unk1", 5), ("filled", "B"), ("clearance", "i"), ("unk2", 5), ("thermal_width", "B"),
    ("mirror", "B"), ("keepout", "B"), ("rotation", "i"), ("plated", "B"), ("soldermask", "B"), ("unk3", 18),
]


def read_obj(r, version, child=False):
    o = {}
    for name, f in OBJ_FIELDS:
        o[name] = r.take(f) if isinstance(f, int) else r.u(f)
    if not child:
        o["text"] = _var_r(r)
        o["net"] = _var_r(r)
        o["groups"] = [r.u("I") for _ in range(r.u("I"))]
    if o["type"] == CIRCLE:
        pass
    elif o["type"] == TEXT:
        o["children"] = [read_obj(r, version, True) for _ in range(r.u("I"))]
        if o["tht_shape"] == 1 and version >= 6:
            o["component"] = {"off_x": r.u("f"), "off_y": r.u("f"), "center_mode": r.u("B"), "rotation": r.u("d"),
                              "package": _var_r(r), "comment": _var_r(r), "use": r.u("B")}
    else:
        o["points"] = [(r.u("f"), r.u("f")) for _ in range(r.u("I"))]
    return o


def write_obj(w, o, version, child=False):
    for name, f in OBJ_FIELDS:
        if isinstance(f, int):
            w.put(bytes(o.get(name, b"\0" * f)).ljust(f, b"\0")[:f])
        else:
            w.u(f, o.get(name, 0))
    if not child:
        _var_w(w, o.get("text", ""))
        _var_w(w, o.get("net", ""))
        g = o.get("groups", [])
        w.u("I", len(g))
        for x in g:
            w.u("I", x)
    if o["type"] == CIRCLE:
        pass
    elif o["type"] == TEXT:
        ch = o.get("children", [])
        w.u("I", len(ch))
        for c in ch:
            write_obj(w, c, version, True)
        if o["tht_shape"] == 1 and version >= 6:
            c = o["component"]
            w.u("f", c["off_x"]); w.u("f", c["off_y"]); w.u("B", c["center_mode"]); w.u("d", c["rotation"])
            _var_w(w, c["package"]); _var_w(w, c["comment"]); w.u("B", c["use"])
    else:
        pts = o.get("points", [])
        w.u("I", len(pts))
        for x, y in pts:
            w.u("f", x); w.u("f", y)


BOARD_HEAD = [
    ("name", ("fixed", 30)), ("unk1", 4), ("size_x", "I"), ("size_y", "I"), ("ground_plane", 7),
    ("grid", "d"), ("zoom", "d"), ("view_x", "I"), ("view_y", "I"), ("active_layer", 4), ("layer_visible", 7),
    ("scan_top", "B"), ("scan_bottom", "B"), ("scan_top_path", ("fixed", 200)), ("scan_bottom_path", ("fixed", 200)),
    ("scan_metrics", 24), ("unk2", 8), ("center_x", "i"), ("center_y", "i"), ("multilayer", "B"),
]


def read(data):
    r = R(data)
    f = {"version": r.u("B")}
    if r.take(3) != b"\x33\xaa\xff" or f["version"] != 6:
        raise ValueError("не файл Sprint Layout 6")
    f["boards"] = []
    for _ in range(r.u("I")):
        b = {}
        for name, t in BOARD_HEAD:
            if isinstance(t, tuple):
                b[name] = _fixed_r(r, t[1])
            elif isinstance(t, int):
                b[name] = r.take(t)
            else:
                b[name] = r.u(t)
        b["objects"] = [read_obj(r, 6) for _ in range(r.u("I"))]
        b["connections"] = []
        for o in b["objects"]:
            if o["type"] in (THT_PAD, SMD_PAD):
                b["connections"].append([r.u("I") for _ in range(r.u("I"))])
            else:
                b["connections"].append(None)
        f["boards"].append(b)
    f["active_board"] = r.u("I")
    f["project_name"] = _fixed_r(r, 100)
    f["project_author"] = _fixed_r(r, 100)
    f["project_company"] = _fixed_r(r, 100)
    f["project_comment"] = _var_r(r)
    f["tail"] = data[r.p:]
    return f


def write(f):
    w = W()
    w.u("B", 6)
    w.put(b"\x33\xaa\xff")
    w.u("I", len(f["boards"]))
    for b in f["boards"]:
        for name, t in BOARD_HEAD:
            if isinstance(t, tuple):
                _fixed_w(w, b[name], t[1])
            elif isinstance(t, int):
                w.put(bytes(b[name]).ljust(t, b"\0")[:t])
            else:
                w.u(t, b[name])
        w.u("I", len(b["objects"]))
        for o in b["objects"]:
            write_obj(w, o, 6)
        for o, c in zip(b["objects"], b["connections"]):
            if o["type"] in (THT_PAD, SMD_PAD):
                c = c or []
                w.u("I", len(c))
                for x in c:
                    w.u("I", x)
    w.u("I", f.get("active_board", 0))
    for k in ("project_name", "project_author", "project_company"):
        _fixed_w(w, f[k], 100)
    _var_w(w, f.get("project_comment", ""))
    w.put(f.get("tail", b""))
    return bytes(w.b)
