# Звук с телефона: Bluetooth (телефон подключается к приставке как к колонке) и DLNA (gmediarender).
# Сами приёмники (bt-agent, gmediarender) ставит scripts/rk3528/install.sh.
#
# С мультирумом звук с телефона не играет на динамике приставки, а идёт в отдельную линию Snapcast «Телефон»
# (поток phone): PipeWire получает выход «Ирина: комнаты» (module-pipe-sink в fifo snapserver), и всё, что приходит
# по Bluetooth или DLNA, перенаправляется туда. Комнаты подключаются к линии так же, как к Яндекс Музыке:
# «Ирина, блютуз на кухне», «добавь спальню», «как на кухне», «выключи музыку».
# Пауза, «дальше», «назад», «что играет» управляют телефоном (AVRCP через BlueZ).
#
# Примеры: "ирина режим сопряжения", "ирина подключи телефон", "ирина блютуз на кухне", "ирина телефон везде",
#          "ирина блютуз выключи"

import json
import os
import shutil
import subprocess
import threading
import time
import logging

from vacore import VACore

modname = os.path.basename(__file__)[:-3]
logger = logging.getLogger(__name__)

PHONE = "phone"              # поток snapserver
SINK = "irene_phone"         # выход PipeWire, который пишет в его fifo
IDLE_OFF_SEC = 20            # телефон молчит столько секунд — комнаты линии выключаются

_line = None                 # PhoneLine


def start(core: VACore):
    manifest = {
        "name": "Звук с телефона (Bluetooth / DLNA)",
        "version": "2.0",
        "require_online": False,
        "description": "Bluetooth-сопряжение голосом; DLNA работает всегда, приёмник называется «Ирина». "
                       "С мультирумом звук с телефона идёт в комнаты: «блютуз на кухне», «телефон везде».",
        "commands": {
            # раньше сопряжения: иначе «блютуз на кухне» поймает команда «блютуз»
            "блютуз на|блютуз в|блютуз во|блютуз здесь|блютуз везде|блютуз тут|"
            "телефон на|телефон в|телефон во|телефон здесь|телефон везде|"
            "музыку с телефона|звук с телефона|включи телефон|включи музыку с телефона": phone_here,
            "режим сопряжения|подключи телефон|включи блютуз|блютуз|сопряжение": bt_pair,
            "выключи блютуз|отключи блютуз|блютуз выключи": bt_off,
            "как подключить телефон|как включить музыку с телефона": howto,
        },
    }
    return manifest


def start_with_options(core: VACore, manifest: dict):
    global _line
    if os.name == "nt" or not shutil.which("pactl"):
        return                      # Windows или нет PipeWire/PulseAudio — звук с телефона только как раньше
    _line = PhoneLine(core)
    lines = getattr(core, "extra_music_lines", None)
    if lines is None:
        lines = core.extra_music_lines = []
    lines.append(_line)
    threading.Thread(target=_line.watch, name="phone-line", daemon=True).start()


# ---------------- линия «Телефон» ----------------

def _run(args, timeout=5):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except Exception as e:
        logger.debug(f"{args[0]}: {e}")
        return -1, ""


def _pactl_json(what):
    code, out = _run(["pactl", "-f", "json", "list", what])
    if code != 0 or not out.strip():
        return []
    try:
        return json.loads(out)
    except ValueError:
        return []


def _is_phone_stream(props):
    """Звук пришёл с телефона: Bluetooth (bluez) или DLNA (gmediarender)."""
    api = str(props.get("device.api", "")) + str(props.get("api.bluez5.address", ""))
    name = str(props.get("node.name", ""))
    app = (str(props.get("application.name", "")) + " " + str(props.get("application.process.binary", ""))).lower()
    return "bluez" in api or name.startswith("bluez_") or "gmediarender" in app or "gmrender" in app


class PhoneLine:
    """Линия мультирума для звука с телефона. Интерфейс как у линии Яндекс Музыки (см. plugin_yandex_music)."""
    stream = PHONE

    def __repr__(self):
        return "<линия «Телефон»>"

    def __init__(self, core):
        self.core = core
        self.active = False           # телефон сейчас передаёт звук
        self.last_seen = 0.0
        self.pair_room = None         # комната, где сказали «режим сопряжения»
        self.last_rooms = []          # где слушали телефон в прошлый раз
        self.loopbacks = {}           # источник bluez -> номер модуля loopback
        self.sink_ok = False
        self.warned = False

    # --- как у линии Яндекс Музыки ---
    def paused(self):
        return False

    def set_pause(self, on):
        _avrcp("Pause" if on else "Play")

    def next(self, *a, **k):
        _avrcp("Next")

    def prev(self, *a, **k):
        _avrcp("Previous")

    def stop(self):
        _avrcp("Pause")               # линию никто не слушает — телефон на паузу

    def describe(self):
        t = _avrcp_track()
        return f"с телефона: {t}" if t else "музыка с телефона"

    # --- PipeWire ---
    def _snap(self):
        from utils.multiroom import MultiRoom
        opts = self.core.plugin_options("plugin_yandex_music") or {}
        return MultiRoom(opts.get("snap_host", "127.0.0.1"), int(opts.get("snap_port", 1705)))

    def _ensure_sink(self, mr):
        """Выход «Ирина: комнаты» в PipeWire, пишущий в fifo потока phone."""
        if any(s.get("name") == SINK for s in _pactl_json("sinks")):
            self.sink_ok = True
            return True
        info = mr.streams().get(PHONE)
        if not info:
            if not self.warned:
                logger.warning("В snapserver нет потока phone — звук с телефона играет на приставке. "
                               "Обновите настройки: bash scripts/rk3528/install.sh")
                self.warned = True
            return False
        code, out = _run(["pactl", "load-module", "module-pipe-sink", f"file={info['path']}", "format=s16le",
                          f"rate={info['rate']}", f"channels={info['channels']}", f"sink_name={SINK}",
                          "sink_properties=device.description=Irene-rooms"])
        if code != 0:
            if not self.warned:
                logger.warning(f"PipeWire не создал выход в комнаты (module-pipe-sink): {out.strip()} — "
                               "звук с телефона играет на приставке")
                self.warned = True
            return False
        logger.warning("Звук с телефона (Bluetooth/DLNA) идёт в линию «Телефон» мультирума")
        self.sink_ok = True
        return True

    def _route(self):
        """Перенаправить звук с телефона в выход комнат. True — телефон сейчас что-то передаёт."""
        sinks = {s.get("index"): s.get("name") for s in _pactl_json("sinks")}
        playing = False
        # Bluetooth как поток (в новых PipeWire) и DLNA — переносим в наш выход
        for si in _pactl_json("sink-inputs"):
            props = si.get("properties", {})
            if not _is_phone_stream(props):
                continue
            playing = playing or not si.get("corked", False)
            if sinks.get(si.get("sink")) != SINK:
                _run(["pactl", "move-sink-input", str(si.get("index")), SINK])
        # Bluetooth как источник звука — подключаем петлёй
        present = set()
        for src in _pactl_json("sources"):
            props = src.get("properties", {})
            if props.get("device.api") != "bluez5" or props.get("device.class") == "monitor":
                continue
            name = src.get("name")
            present.add(name)
            playing = playing or src.get("state") == "RUNNING"
            if name not in self.loopbacks:
                code, out = _run(["pactl", "load-module", "module-loopback", f"source={name}", f"sink={SINK}",
                                  "latency_msec=60"])
                if code == 0 and out.strip().isdigit():
                    self.loopbacks[name] = out.strip()
        for name in list(self.loopbacks):
            if name not in present:
                _run(["pactl", "unload-module", self.loopbacks.pop(name)])
        return playing

    def watch(self):
        time.sleep(10)                # пусть сначала поднимутся snapserver и плагин музыки
        while True:
            try:
                self._tick()
            except Exception as e:
                logger.debug(f"линия «Телефон»: {e}")
            time.sleep(2)

    def _tick(self):
        mr = self._snap()
        if PHONE not in mr.streams():
            if not self.warned:
                logger.warning("В snapserver нет потока phone — звук с телефона играет на приставке. "
                               "Обновите настройки: bash scripts/rk3528/install.sh")
                self.warned = True
            return
        if not self._ensure_sink(mr):
            return
        playing = self._route()
        now = time.time()
        if playing:
            self.last_seen = now
            if not self.active:
                self.active = True
                rooms = [r for r in mr.rooms_of(PHONE)]
                if not rooms:          # телефон заиграл — туда, где просили сопряжение, или где слушали в прошлый раз
                    want = [self.pair_room] if self.pair_room else list(self.last_rooms)
                    want = [r for r in want if r and mr.has_player(r)] or (["local"] if mr.has_player("local") else [])
                    assign = getattr(self.core, "music_assign", None)
                    for r in want:
                        try:
                            if assign:
                                assign(r, PHONE)
                        except Exception as e:
                            logger.debug(e)
                logger.info(f"Телефон играет: {', '.join(mr.rooms_of(PHONE)) or 'нигде'}")
        elif self.active and now - self.last_seen > IDLE_OFF_SEC:
            self.active = False
            rooms = mr.rooms_of(PHONE)
            if rooms:
                self.last_rooms = rooms
            assign = getattr(self.core, "music_assign", None)
            for r in rooms:
                try:
                    if assign:
                        assign(r, None)
                except Exception as e:
                    logger.debug(e)
            logger.info("Телефон замолчал — линия «Телефон» выключена")


# ---------------- AVRCP: пауза, «дальше», что играет ----------------

def _avrcp_player():
    code, out = _run(["busctl", "--system", "tree", "org.bluez", "--list"])
    if code != 0:
        return None
    paths = [p.strip() for p in out.splitlines() if "/player" in p and p.strip().count("/") == 5]
    return paths[0] if paths else None


def _avrcp(method):
    path = _avrcp_player()
    if path:
        _run(["busctl", "--system", "call", "org.bluez", path, "org.bluez.MediaPlayer1", method])


def _avrcp_track():
    path = _avrcp_player()
    if not path:
        return ""
    code, out = _run(["busctl", "--system", "--json=short", "get-property", "org.bluez", path,
                      "org.bluez.MediaPlayer1", "Track"])
    if code != 0:
        return ""
    try:
        data = json.loads(out).get("data", {})
        title = data.get("Title", {}).get("data", "")
        artist = data.get("Artist", {}).get("data", "")
        return f"{artist} — {title}" if artist and title else title
    except ValueError:
        return ""


# ---------------- команды ----------------

def _btctl(*args) -> bool:
    try:
        r = subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=10)
        logger.info(r.stdout)
        return r.returncode == 0
    except Exception as e:
        logger.warning(f"bluetoothctl: {e}")
        return False


def phone_here(core: VACore, phrase: str):
    """«блютуз на кухне», «телефон везде», «музыку с телефона» — комнаты слушают телефон."""
    full = (getattr(core, "input_cmd_full", "") or phrase)
    here = getattr(core, "music_line_here", None)
    if _line is None or here is None:
        core.say("Звук с телефона в комнаты не передаётся: нет мультирума")
        return
    if not _line.active:
        room = getattr(core, "current_room", None)
        _line.pair_room = room.id if room is not None else "local"
        core.say("Телефон сейчас не играет. Включите музыку на телефоне — она зазвучит здесь. "
                 "Если телефон не подключён, скажите: режим сопряжения")
        return
    if not here(core, PHONE, full):
        core.say("Мультирум не работает: нет сервера Snapcast")


def bt_pair(core: VACore, phrase: str):
    if phrase.strip().startswith("выключ"):
        return bt_off(core, phrase)
    room = getattr(core, "current_room", None)
    if _line is not None:
        _line.pair_room = room.id if room is not None else "local"
    ok = _btctl("power", "on") and _btctl("discoverable", "on") and _btctl("pairable", "on")
    if ok:
        where = " Музыка с телефона зазвучит здесь." if _line is not None and _line.sink_ok else ""
        core.say("Блютуз включён на три минуты. Найдите на телефоне устройство Irene." + where)
    else:
        core.say("Не получилось включить блютуз. Проверьте адаптер")


def bt_off(core: VACore, phrase: str):
    if not _btctl("discoverable", "off"):
        core.say("Не получилось управлять блютузом. Проверьте адаптер")
        return
    try:
        out = subprocess.run(["bluetoothctl", "devices", "Connected"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception as e:
        logger.warning(f"bluetoothctl: {e}")
        out = ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            _btctl("disconnect", parts[1])
    core.say("Блютуз отключён")


def howto(core: VACore, phrase: str):
    core.say("Скажите: режим сопряжения, и подключите телефон по блютузу к Irene. Музыка с телефона зазвучит "
             "в этой комнате; другие комнаты добавляются так же, как с Яндекс Музыкой. "
             "С андроида можно ещё выбрать Ирину в приложении с поддержкой DLNA")
