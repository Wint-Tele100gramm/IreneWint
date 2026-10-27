# Мультирум через Snapcast: какой комнате какой поток музыки.
#
# snapserver (ставит scripts/rk3528/install.sh) держит несколько потоков: stream1, stream2 — музыка
# (плеер пишет PCM в их fifo) и idle — пустой поток для комнат, где музыка не играет.
# Каждая комната — клиент Snapcast с ID = id комнаты: ESP32 (прошивка 3.1+) или snapclient приставки (ID "local").
# Управление — JSON-RPC по TCP (порт 1705): Server.GetStatus, Group.SetStream, Group.SetClients, Client.SetVolume.
#
# Каждый клиент держим в своей группе: тогда поток назначается комнате, а не группе.

import json
import logging
import re
import socket
import threading
import time

logger = logging.getLogger(__name__)

IDLE_STREAM = "idle"


class SnapError(Exception):
    pass


class SnapRpcError(SnapError):
    pass


class SnapControl:
    """Синхронный клиент JSON-RPC snapserver. Потокобезопасный; при обрыве переподключается."""

    def __init__(self, host="127.0.0.1", port=1705, timeout=3.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock = None
        self._buf = b""
        self._id = 0
        self._lock = threading.Lock()

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        self._sock, self._buf = s, b""

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _readline(self, deadline):
        while b"\n" not in self._buf:
            if time.time() > deadline:
                raise SnapError("snapserver не ответил")
            chunk = self._sock.recv(65536)
            if not chunk:
                raise SnapError("snapserver закрыл соединение")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.strip()

    def call(self, method, params=None):
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._connect()
                    self._id += 1
                    req = {"id": self._id, "jsonrpc": "2.0", "method": method}
                    if params is not None:
                        req["params"] = params
                    self._sock.sendall(json.dumps(req).encode() + b"\r\n")
                    deadline = time.time() + self.timeout
                    while True:
                        line = self._readline(deadline)
                        if not line:
                            continue
                        msg = json.loads(line)
                        if isinstance(msg, list) or msg.get("id") != self._id:
                            continue          # уведомления сервера (изменилась громкость и т.п.) — не нужны
                        if "error" in msg:
                            raise SnapRpcError(f"{method}: {msg['error']}")
                        return msg.get("result")
                except SnapRpcError:
                    raise                     # сервер ответил ошибкой — повторять бессмысленно
                except (OSError, ValueError, SnapError) as e:
                    self.close()              # обрыв связи — одна повторная попытка
                    if attempt == 2:
                        raise SnapError(str(e) or e.__class__.__name__) from e

    # ---------- состояние ----------
    def status(self):
        return self.call("Server.GetStatus")["server"]


class MultiRoom:
    """Комнаты и потоки поверх snapserver. Комната = клиент Snapcast с ID, равным id комнаты."""

    def __init__(self, host="127.0.0.1", port=1705):
        self.ctl = SnapControl(host, port)
        self._status = None
        self._status_time = 0.0
        self._lock = threading.RLock()

    # ---------- состояние (с коротким кэшем: команды подряд не дёргают сервер) ----------
    def status(self, fresh=False):
        with self._lock:
            if fresh or self._status is None or time.time() - self._status_time > 1.0:
                self._status = self.ctl.status()
                self._status_time = time.time()
            return self._status

    def available(self):
        try:
            self.status(fresh=True)
            return True
        except Exception as e:
            logger.debug(f"snapserver недоступен: {e}")
            return False

    def streams(self):
        """Потоки для музыки: {id: {"path": fifo, "rate", "bits", "channels", "status"}} (без idle)."""
        result = {}
        for s in self.status().get("streams", []):
            uri = s.get("uri", {})
            if s["id"] == IDLE_STREAM or uri.get("scheme") != "pipe":
                continue
            fmt = (uri.get("query", {}).get("sampleformat") or "48000:16:2").split(":")
            result[s["id"]] = {"path": uri.get("path"), "rate": int(fmt[0]), "bits": int(fmt[1]),
                               "channels": int(fmt[2]), "status": s.get("status")}
        return result

    def clients(self):
        """{id клиента: {"group", "stream", "connected", "volume", "muted", "name", "ip"}}"""
        result = {}
        for g in self.status().get("groups", []):
            for c in g.get("clients", []):
                vol = c.get("config", {}).get("volume", {})
                result[c["id"]] = {"group": g["id"], "stream": g.get("stream_id"),
                                   "connected": c.get("connected", False),
                                   "volume": vol.get("percent", 100), "muted": vol.get("muted", False),
                                   "name": c.get("config", {}).get("name") or c.get("host", {}).get("name", ""),
                                   "ip": c.get("host", {}).get("ip", ""), "group_size": len(g.get("clients", []))}
        return result

    def has_player(self, room_id):
        c = self.clients().get(room_id)
        return bool(c and c["connected"])

    def stream_of(self, room_id):
        """Поток, назначенный комнате, или None (idle / нет такого клиента)."""
        c = self.clients().get(room_id)
        if not c or c["stream"] == IDLE_STREAM:
            return None
        return c["stream"]

    def rooms_of(self, stream_id, connected_only=True):
        return [cid for cid, c in self.clients().items()
                if c["stream"] == stream_id and (c["connected"] or not connected_only)]

    # ---------- изменения ----------
    def assign(self, room_id, stream_id):
        """Назначить комнате поток (None — тишина). Комнату сперва отделяем в свою группу."""
        stream_id = stream_id or IDLE_STREAM
        with self._lock:
            clients = self.clients()
            c = clients.get(room_id)
            if c is None:
                raise SnapError(f"нет клиента Snapcast «{room_id}»")
            if c["group_size"] > 1:
                others = [cid for cid, x in clients.items() if x["group"] == c["group"] and cid != room_id]
                self.ctl.call("Group.SetClients", {"id": c["group"], "clients": others})
                c = self.clients_fresh().get(room_id)
            if c["stream"] != stream_id:
                self.ctl.call("Group.SetStream", {"id": c["group"], "stream_id": stream_id})
            self._status = None

    def clients_fresh(self):
        self._status = None
        return self.clients()

    def set_volume(self, room_id, percent=None, muted=None):
        c = self.clients().get(room_id)
        if c is None:
            raise SnapError(f"нет клиента Snapcast «{room_id}»")
        vol = {"percent": int(max(0, min(100, c["volume"] if percent is None else percent))),
               "muted": c["muted"] if muted is None else bool(muted)}
        self.ctl.call("Client.SetVolume", {"id": room_id, "volume": vol})
        self._status = None

    def volume(self, room_id):
        c = self.clients().get(room_id)
        return None if c is None else c["volume"]

    def split_all(self):
        """Каждого клиента — в отдельную группу (после ручных правок в приложениях Snapcast)."""
        with self._lock:
            for _ in range(10):
                multi = [c for c in self.clients_fresh().items() if c[1]["group_size"] > 1]
                if not multi:
                    return
                cid, c = multi[0]
                others = [x for x, y in self.clients().items() if y["group"] == c["group"] and x != cid]
                self.ctl.call("Group.SetClients", {"id": c["group"], "clients": others})


# ---------------- комнаты в речи: «на кухне», «в сухой зоне», «везде» ----------------

_ENDINGS = sorted(["ая", "яя", "ой", "ей", "ую", "юю", "ом", "ем", "ые", "ие", "ых", "их", "ым", "им", "ого", "его",
                   "ах", "ях", "ам", "ям", "ами", "ями", "е", "и", "у", "ю", "а", "я", "о", "ы"], key=len, reverse=True)
EVERYWHERE = re.compile(r"\b(везде|повсюду|во всех комнатах|по всему дому|по всем комнатам|во всём доме|во всем доме)\b")
HERE = re.compile(r"\b(здесь|тут|в этой комнате|сюда)\b")
_PREP = r"(?:на|в|во|к|ко|из|для|у)"


def stem(word):
    word = word.lower().replace("ё", "е")
    for end in _ENDINGS:
        if word.endswith(end) and len(word) - len(end) >= 3:
            word = word[:-len(end)]
            break
    return word[:5]


def find_rooms(phrase, rooms):
    """Комнаты, названные во фразе. rooms: {id: название}.
    Возвращает (список id, остаток фразы без названий комнат, everywhere: bool, here: bool)."""
    text = " " + phrase.lower().replace("ё", "е") + " "
    everywhere = bool(EVERYWHERE.search(text))
    text = EVERYWHERE.sub(" ", text)
    here = bool(HERE.search(text))
    text = HERE.sub(" ", text)
    found = []
    # длинные названия первыми: «сухая зона» раньше «зона»
    for rid, name in sorted(rooms.items(), key=lambda kv: -len(kv[1] or "")):
        name_words = re.findall(r"[a-zа-я0-9]+", (name or "").lower().replace("ё", "е"))
        if not name_words:
            continue
        words = re.findall(r"[a-zа-я0-9]+", text)
        stems = [stem(w) for w in words]
        need = [stem(w) for w in name_words]
        for i in range(len(stems) - len(need) + 1):
            if stems[i:i + len(need)] == need:
                # убираем название вместе с предлогом перед ним
                pattern = r"(?:\b" + _PREP + r"\s+)?" + r"\s+".join(re.escape(w) for w in words[i:i + len(need)]) + r"\b"
                text = re.sub(pattern, " ", text, count=1)
                found.append(rid)
                break
    rest = re.sub(r"\s+", " ", text).strip()
    rest = re.sub(r"(^|\s)(и|тоже|еще|ещё|и в|и на)(\s|$)", " ", rest).strip()
    return found, rest, everywhere, here


def join_names(names):
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " и " + names[-1]
