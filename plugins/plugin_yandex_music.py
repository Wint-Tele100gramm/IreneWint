# Яндекс Музыка: "Моя волна", поиск песен/исполнителей/альбомов/плейлистов, управление плеером, мультирум.
# Воспроизведение — mpv (sudo apt install mpv), API — библиотека yandex-music (MarshalX).
#
# Мультирум (Snapcast, ставит scripts/rk3528/install.sh): две независимые линии музыки — например, у вас в комнате
# одна, у жены в своей другая. Музыка включается там, где сказана команда; другие комнаты можно добавить к ней.
# Комнаты — клиенты ESP32 (прошивка 3.1+) и динамик приставки. Без snapserver (Windows) музыка играет на этом компьютере.
#
# Токен: веб-интерфейс → Настройки → Яндекс Музыка, или  python scripts/rk3528/yandex_token.py
#
# Примеры: "ирина включи музыку", "ирина включи мою волну на кухне", "ирина поставь песню кино группа крови",
#          "ирина включи исполнителя сплин", "ирина включи любимые песни", "ирина дальше", "ирина пауза",
#          "ирина громче", "ирина громкость сорок", "ирина что играет", "ирина лайк", "ирина выключи музыку",
#          мультирум: "ирина добавь кухню", "ирина и в спальне", "ирина музыку везде", "ирина перенеси музыку на кухню",
#          "ирина как на кухне", "ирина выключи музыку на кухне", "ирина выключи музыку везде", "ирина где играет музыка"

import os
import re
import time
import random
import logging
import threading

from vacore import VACore
from utils.ru_words_to_num import extract_number
from utils.mpv_ipc import MpvPlayer, IS_WIN
from utils.cmd_route import delegate_longer_command
from utils.multiroom import MultiRoom, find_rooms, join_names

modname = os.path.basename(__file__)[:-3]
logger = logging.getLogger(__name__)

WAVE_STATION = "user:onyourwave"
LOCAL = "local"              # id комнаты приставки и её snapclient

_core = None
_options = {}
_api_client = None
_api_lock = threading.Lock()
_players = {}                # линия (поток Snapcast или "local") -> Player
_players_lock = threading.RLock()
_mr = None                   # MultiRoom, если snapserver доступен
_mr_checked = 0.0
_duck = {"saved": None, "until": 0.0}   # громкость до приглушения по имени помощника


def start(core: VACore):
    manifest = {
        "name": "Яндекс Музыка",
        "version": "2.0",
        "require_online": True,
        "description": "Музыка из Яндекс Музыки через mpv, мультирум через Snapcast.\n"
                       "Команды: включи музыку|мою волну [на кухне|везде], поставь <песню>, "
                       "включи исполнителя|альбом|плейлист <имя>, любимые песни, пауза, продолжи, дальше, назад, "
                       "громче, тише, громкость <число>, что играет, лайк, не нравится, выключи музыку [на кухне|везде], "
                       "добавь <комнату>, музыку везде, перенеси музыку <куда>, как на <комнате>, где играет музыка",
        "options_label": {
            "token": "OAuth-токен Яндекс Музыки",
            "volume": "Громкость при запуске (0-100)",
            "duck_volume": "Громкость музыки, пока Ирина говорит (0 - пауза)",
            "max_bitrate": "Максимальный битрейт, кбит/с (320, 192, 128)",
            "mpv_args": "Дополнительные аргументы mpv, например [\"--audio-device=alsa/plughw:0,0\"]",
            "multiroom": "Мультирум через Snapcast (если snapserver запущен)",
            "snap_host": "Адрес управления snapserver",
            "snap_port": "Порт управления snapserver (JSON-RPC)",
        },
        "default_options": {
            "token": "",
            "volume": 60,
            "duck_volume": 15,
            "max_bitrate": 320,
            "mpv_args": ["--ao=wasapi"] if IS_WIN else [],
            "multiroom": True,
            "snap_host": "127.0.0.1",
            "snap_port": 1705,
        },
        "commands": {
            "включи музыку|поставь музыку|включи мою волну|моя волна|включи волну|включи радио": cmd_wave,
            "включи любимые песни|включи любимую музыку|включи мои лайки|включи мне нравится": cmd_likes,
            "поставь|включи песню|включи трек|найди песню": cmd_search_track,
            "включи исполнителя|включи группу|включи артиста|поставь исполнителя|поставь группу": cmd_artist,
            "включи альбом|поставь альбом": cmd_album,
            "включи плейлист|поставь плейлист": cmd_playlist,
            "пауза|паузу|на паузу": cmd_pause,
            "продолжи|продолжай|играй|сними с паузы": cmd_resume,
            "дальше|следующий|следующая|следующую|следующий трек|следующая песня|пропусти": cmd_next,
            "назад|предыдущий|предыдущая|предыдущий трек|верни": cmd_prev,
            "стоп|хватит|выключи музыку|останови музыку|выключи всю музыку": cmd_stop,
            "громче": (cmd_volume_step, 10),
            "чуть громче": (cmd_volume_step, 5),
            "сильно громче": (cmd_volume_step, 25),
            "тише": (cmd_volume_step, -10),
            "чуть тише": (cmd_volume_step, -5),
            "сильно тише": (cmd_volume_step, -25),
            "громкость|сделай громкость|поставь громкость|громкость музыки": cmd_volume_set,
            "что играет|что сейчас играет|что это за песня|как называется песня|кто поёт|кто поет": cmd_what,
            "лайк|лайкни|мне нравится|нравится эта песня|сохрани песню|добавь в любимые|добавь в избранное": cmd_like,
            "не нравится|дизлайк|убери эту песню": cmd_dislike,
            # мультирум
            "добавь музыку|подключи музыку|добавь комнату|добавь|музыку тоже|музыку еще|музыку ещё|и на|и в|и во|"
            "музыку везде|музыку во всех комнатах|музыку на|музыку в": cmd_join,
            "перенеси музыку|переключи музыку|переведи музыку|перемести музыку|музыку перенеси|перенеси": cmd_move,
            "как на|как в|как во|присоединись к|присоединиться к|то же что": cmd_follow,
            "где играет музыка|где играет|где музыка|что где играет|где включена музыка": cmd_where,
        },
    }
    return manifest


def start_with_options(core: VACore, manifest: dict):
    global _core, _options
    _core, _options = core, manifest["options"]
    # хук для plugin_homeassistant: "включи ..." / "выключи ..." сначала проверяются здесь
    core.music_try_handle = music_try_handle
    core.music_status = status_for_ui
    core.music_assign = ui_assign
    _install_ducking(core)
    if not _options.get("token"):
        print("plugin_yandex_music: не задан token — войдите в Яндекс в веб-интерфейсе (Настройки)")


# ---------------- линии музыки ----------------

def _api():
    global _api_client
    with _api_lock:
        if _api_client is None:
            from yandex_music import Client
            token = _options.get("token")
            if not token:
                raise RuntimeError("no token")
            _api_client = Client(token).init()
        return _api_client


def _multiroom():
    """MultiRoom, если snapserver работает; иначе None (музыка — на этом компьютере)."""
    global _mr, _mr_checked
    if not _options.get("multiroom", True):
        return None
    if _mr is not None:
        return _mr
    if time.time() - _mr_checked < 15:
        return None
    _mr_checked = time.time()
    mr = MultiRoom(_options.get("snap_host", "127.0.0.1"), int(_options.get("snap_port", 1705)))
    if not mr.available():
        logger.info("snapserver не отвечает — музыка играет на этом компьютере")
        return None
    streams = mr.streams()
    if not streams:
        logger.warning("В snapserver нет потоков pipe:// для музыки — мультирум выключен")
        return None
    mr.split_all()
    for cid, c in mr.clients().items():         # приглушённые на паузе до перезапуска Ирины
        if c["muted"]:
            try:
                mr.set_volume(cid, muted=False)
            except Exception as e:
                logger.debug(e)
    with _players_lock:
        for sid, info in sorted(streams.items()):
            if sid not in _players:
                _players[sid] = Player(sid, info)
        logger.warning(f"Мультирум Snapcast: линии музыки {', '.join(sorted(streams))}")
    _mr = mr
    return mr


def _all_players():
    mr = _multiroom()
    with _players_lock:
        if mr is None and LOCAL not in _players:
            _players[LOCAL] = Player(LOCAL, None)
        return [p for sid, p in sorted(_players.items()) if (mr is None) == (sid == LOCAL)]


class Player:
    """Одна линия музыки: свой mpv, своя очередь треков. В мультируме mpv пишет PCM в fifo snapserver."""

    def __init__(self, stream, info):
        self.stream = stream
        self.info = info
        if info:
            ch = "mono" if info["channels"] == 1 else "stereo"
            # маленькие буферы: пауза, «дальше» и «стоп» слышны сразу, а не через несколько секунд
            args = ["--ao=pcm", "--ao-pcm-file=" + info["path"], "--ao-pcm-waveheader=no",
                    "--audio-format=s16", "--audio-samplerate=%d" % info["rate"], "--audio-channels=" + ch,
                    "--volume=100", "--audio-buffer=0.05"]
        else:
            args = ["--volume=%d" % _options.get("volume", 60)] + list(_options.get("mpv_args", []))
        sock = (r"\\.\pipe\irene-mpv-" if IS_WIN else "/tmp/irene-mpv-") + stream + ("" if IS_WIN else ".sock")
        self.mpv = MpvPlayer(socket_path=sock, extra_args=args, on_event=self._on_mpv_event)
        self.lock = threading.RLock()
        self.queue = []          # список Track
        self.index = -1
        self.station = None      # для потоковых станций (Моя волна, радио по треку)
        self.station_name = ""
        self.batch_id = None
        self.track_started_at = 0.0
        self.active = False      # что-то играет или на паузе
        self.last_error = ""
        self._fifo_fd = None
        if info:
            threading.Thread(target=self._hold_fifo, name=f"fifo-{stream}", daemon=True).start()

    def __repr__(self):
        return f"<линия {self.stream}>"

    def _hold_fifo(self):
        """Держим fifo snapserver открытым и уменьшаем его буфер с 64 КБ (≈0,4–1,5 с звука) до 8 КБ.
        Пока fifo открыт хоть кем-то, буфер остаётся маленьким и для mpv."""
        import fcntl
        path = self.info["path"]
        for _ in range(60):
            try:
                fd = os.open(path, os.O_WRONLY)       # ждёт, пока snapserver откроет fifo на чтение
                try:
                    fcntl.fcntl(fd, 1031, 8192)        # F_SETPIPE_SZ
                except OSError as e:
                    logger.debug(f"F_SETPIPE_SZ: {e}")
                self._fifo_fd = fd                     # не закрываем: иначе буфер вернётся к 64 КБ
                return
            except OSError as e:
                logger.debug(f"fifo {path}: {e}")
                time.sleep(5)

    @property
    def current(self):
        if 0 <= self.index < len(self.queue):
            return self.queue[self.index]
        return None

    def paused(self):
        return bool(self.mpv.get("pause", False))

    def _stream_url(self, track):
        infos = track.get_download_info()
        max_br = _options.get("max_bitrate", 320)
        infos = [i for i in infos if i.codec in ("mp3", "aac", "he-aac") and i.bitrate_in_kbps <= max_br] or infos
        infos.sort(key=lambda i: (i.codec == "mp3", i.bitrate_in_kbps), reverse=True)
        return infos[0].get_direct_link()

    # --- запуск источников ---
    def play_list(self, tracks, shuffle=False, name=""):
        tracks = [t for t in tracks if t is not None and getattr(t, "available", True) is not False]
        if shuffle:
            random.shuffle(tracks)
        with self.lock:
            self._finish_feedback(skip=True)
            self.station, self.batch_id, self.station_name = None, None, name
            self.queue, self.index = tracks, -1
        return self.next(feedback=False)

    def play_station(self, station, name=""):
        with self.lock:
            self._finish_feedback(skip=True)
            self.station, self.queue, self.index, self.station_name = station, [], -1, name
            self._fetch_station(first=True)
        return self.next(feedback=False)

    def play_track_radio(self, track):
        """Найденная песня, а после неё — похожие."""
        with self.lock:
            self._finish_feedback(skip=True)
            self.station, self.station_name = f"track:{track.id}", ""
            self.queue, self.index = [track], -1
            try:
                self._fetch_station(first=True)
            except Exception as e:
                logger.debug(e)
        return self.next(feedback=False)

    def _fetch_station(self, first=False):
        cur = self.current
        res = _api().rotor_station_tracks(self.station, queue=cur.id if cur and not first else None)
        if res is None:
            return
        self.batch_id = res.batch_id
        if first:
            _bg(_api().rotor_station_feedback_radio_started, self.station, "irene", res.batch_id)
        known = {t.id for t in self.queue}
        for seq in res.sequence:
            if seq.track and seq.track.id not in known:
                self.queue.append(seq.track)

    # --- навигация ---
    def next(self, feedback=True, skipped=True):
        with self.lock:
            if feedback:
                self._finish_feedback(skip=skipped)
            if self.station and self.index + 2 >= len(self.queue):
                self._fetch_station()
            if self.index + 1 >= len(self.queue):
                self.active = False
                return None
            self.index += 1
            return self._play_current()

    def prev(self):
        with self.lock:
            if self.index <= 0:
                self.mpv.command("seek", 0, "absolute")
                return self.current
            self._finish_feedback(skip=True)
            self.index -= 1
            return self._play_current()

    def _play_current(self):
        track = self.current
        loaded = False
        self.last_error = ""
        for _ in range(3):  # если трек недоступен, пробуем следующий
            try:
                self.mpv.load(self._stream_url(track))
                loaded = True
                break
            except Exception as e:
                logger.warning(f"Не удалось запустить трек {track.title}: {e}")
                self.last_error = str(e)
                if "mpv" in self.last_error:
                    break                 # нет проигрывателя — другие треки не помогут
                if self.index + 1 >= len(self.queue):
                    break
                self.index += 1
                track = self.current
        if not loaded:
            self.active = False
            return None
        if _mr is not None:                      # после паузы комнаты были приглушены
            try:
                _set_muted(_mr.rooms_of(self.stream), False)
            except Exception as e:
                logger.debug(e)
        self.active = True
        self.track_started_at = time.time()
        if self.station:
            _bg(_api().rotor_station_feedback_track_started, self.station, track.id, self.batch_id)
        return track

    def _finish_feedback(self, skip):
        if not self.station or self.current is None:
            return
        played = max(0.0, time.time() - self.track_started_at)
        # отзывы «Моей волне» — в фоне: музыка не ждёт ответа Яндекса
        if skip:
            _bg(_api().rotor_station_feedback_skip, self.station, self.current.id, played, self.batch_id)
        else:
            _bg(_api().rotor_station_feedback_track_finished, self.station, self.current.id, played, self.batch_id)

    def _on_mpv_event(self, ev):
        if ev.get("event") == "end-file" and ev.get("reason") in ("eof", "error") and self.active:
            # следующий трек — в отдельном потоке, чтобы не блокировать чтение сокета mpv
            threading.Thread(target=self.next, kwargs={"feedback": True, "skipped": False}, daemon=True).start()

    def stop(self):
        with self.lock:
            if self.active:
                self._finish_feedback(skip=True)
            self.active = False
            self.queue, self.index, self.station, self.station_name = [], -1, None, ""
            if self.mpv._proc is not None:
                self.mpv.stop()

    def describe(self):
        t = self.current
        if t is None:
            return self.station_name or ""
        return _describe(t)


def _bg(fn, *args):
    def run():
        try:
            fn(*args)
        except Exception as e:
            logger.debug(f"Яндекс: {e}")
    threading.Thread(target=run, daemon=True).start()


def _describe(track):
    if track is None:
        return ""
    artists = ", ".join(track.artists_name()) if track.artists else ""
    return f"{artists} — {track.title}" if artists else track.title


# ---------------- комнаты ----------------

def _room_id(core):
    room = getattr(core, "current_room", None)
    return room.id if room is not None else LOCAL


def _room_names(core):
    """{id: название} всех комнат: подключённые к Ирине и клиенты Snapcast."""
    names = {}
    server = getattr(core, "rooms_server", None)
    if server is not None:
        for rid, name in (server.options.get("room_names") or {}).items():
            names[rid] = name
        for rid, room in list(server.rooms.items()):
            names[rid] = room.name
        if LOCAL not in names:
            names[LOCAL] = server.options.get("local_room") or "Приставка"
    names.setdefault(LOCAL, "Приставка")
    mr = _mr
    if mr is not None:
        try:
            for cid, c in mr.clients().items():
                names.setdefault(cid, c["name"] or cid)
        except Exception as e:
            logger.debug(e)
    return names


def _name(core, rid):
    return _room_names(core).get(rid, rid)


def _names(core, rids):
    return join_names([_name(core, r) for r in rids])


def _parse_rooms(core, phrase, default_here=True):
    """(id комнат, остаток фразы). Без названий — комната, где сказана команда (если default_here)."""
    mr = _multiroom()
    if mr is None:
        return ([_room_id(core)] if default_here else []), find_rooms(phrase, {})[1]
    names = _room_names(core)
    found, rest, everywhere, here = find_rooms(phrase, names)
    if everywhere:
        found = [cid for cid, c in mr.clients().items() if c["connected"]]
    if here or (not found and default_here):
        found = [_room_id(core)] + [r for r in found if r != _room_id(core)]
    return found, rest


def _player_of_room(rid):
    mr = _multiroom()
    if mr is None:
        return _all_players()[0]
    sid = mr.stream_of(rid)
    return _players.get(sid) if sid else None


def _target_player(core):
    """Линия, к которой относится команда «пауза», «дальше», «что играет»...: музыка этой комнаты,
    а если здесь ничего не играет — единственная играющая линия."""
    p = _player_of_room(_room_id(core))
    if p is not None and p.active:
        return p
    active = [p for p in _all_players() if p.active]
    return active[0] if len(active) == 1 else None


def _cleanup():
    """Остановить линии, которые больше никто не слушает; комнаты остановленных линий — в тишину."""
    mr = _multiroom()
    if mr is None:
        return
    for p in _all_players():
        rooms = mr.rooms_of(p.stream)
        if p.active and not rooms:
            logger.info(f"{p}: никто не слушает — останавливаю")
            p.stop()
        elif not p.active:
            for r in mr.rooms_of(p.stream, connected_only=False):
                try:
                    mr.assign(r, None)
                except Exception as e:
                    logger.debug(e)


def _set_muted(rooms, muted):
    """Пауза: клиенты сразу замолкают (иначе доиграли бы буфер ~0,6 с), снятие с паузы — снова звучат."""
    mr = _multiroom()
    if mr is None:
        return
    clients = mr.clients()
    for r in rooms:
        c = clients.get(r)
        if c and c["muted"] != muted:
            try:
                mr.set_volume(r, muted=muted)
            except Exception as e:
                logger.debug(e)


def _assign(core, rooms, player):
    """Подключить комнаты к линии. Возвращает список комнат без плеера Snapcast."""
    mr = _multiroom()
    if mr is None:
        return []
    missing = []
    for r in rooms:
        if not mr.has_player(r):
            missing.append(r)
            continue
        mr.assign(r, player.stream if player else None)
    _set_muted([r for r in rooms if r not in missing], bool(player and player.active and player.paused()))
    _cleanup()
    return missing


def _no_player_text(core, missing):
    if not missing:
        return ""
    return (f"В комнате {_names(core, missing)} нет плеера для музыки — обновите прошивку клиента."
            if len(missing) == 1 else f"В комнатах {_names(core, missing)} нет плеера для музыки.")


def _pick_player(core, rooms):
    """Линия для новой музыки в комнатах rooms. (Player, None) или (None, текст отказа)."""
    mr = _multiroom()
    players = _all_players()
    if mr is None:
        return players[0], None
    here = _room_id(core)
    # 1. музыка этих комнат, если её слушают только они — заменяем
    for r in [here] + rooms:
        p = _player_of_room(r)
        if p is not None and p.active and set(mr.rooms_of(p.stream)) <= set(rooms):
            return p, None
    # 2. свободная линия
    for p in players:
        if not p.active or not mr.rooms_of(p.stream):
            if p.active:
                p.stop()
            return p, None
    # 3. обе заняты: заменяем музыку этой комнаты (её слушают вместе — значит, и менять вместе)
    p = _player_of_room(here)
    if p is not None and p.active and here in rooms:
        return p, None
    return None, ("Обе линии музыки заняты: " + _where_text(core) +
                  ". Скажите, например: как на кухне — и комната подключится к той музыке, "
                  "или выключите музыку в одной из комнат")


def _where_text(core):
    mr = _multiroom()
    parts = []
    for p in _all_players():
        if not p.active:
            continue
        what = p.describe() or "музыка"
        if p.paused():
            what += " (на паузе)"
        if mr is None:
            parts.append(f"играет {what}")
            continue
        rooms = mr.rooms_of(p.stream)
        parts.append(f"{_names(core, rooms) or 'нигде'}: {what}")
    return "; ".join(parts)


# ---------------- приглушение музыки, пока Ирина говорит ----------------
# Клиенты ESP32 приглушают музыку сами. Здесь — динамик приставки: громкость её snapclient или своего mpv.

def _local_volume_get():
    mr = _multiroom()
    if mr is not None:
        p = _player_of_room(LOCAL)
        if p is None or not p.active or p.paused():
            return None
        return mr.volume(LOCAL)
    p = _all_players()[0]
    if not p.active or p.paused():
        return None
    return p.mpv.get("volume", _options.get("volume", 60))


def _local_volume_set(value):
    mr = _multiroom()
    if mr is not None:
        mr.set_volume(LOCAL, value)
    else:
        _all_players()[0].mpv.set("volume", value)


def _install_ducking(core):
    if getattr(core, "_ym_ducking_installed", False):
        return
    core._ym_ducking_installed = True
    original = core.play_voice_assistant_speech

    def speech_with_ducking(text):
        if _duck["saved"] is not None:
            return original(text)          # уже приглушено по имени
        try:
            vol = _local_volume_get()
        except Exception as e:
            logger.debug(e)
            vol = None
        if vol is None:
            return original(text)
        duck = max(1, _options.get("duck_volume", 15))
        try:
            _local_volume_set(min(vol, duck))
            return original(text)
        finally:
            try:
                _local_volume_set(vol)
            except Exception as e:
                logger.debug(e)

    core.play_voice_assistant_speech = speech_with_ducking

    # приглушение по имени помощника (сервер комнат вызывает on_partial для микрофона приставки)
    names = core.voiceAssNames if isinstance(core.voiceAssNames, list) else str(core.voiceAssNames).split("|")

    def restore():
        if _duck["saved"] is not None:
            try:
                _local_volume_set(_duck["saved"])
            except Exception:
                pass
        _duck["saved"] = None

    def on_partial(text):
        if _duck["saved"] is not None and time.time() > _duck["until"]:
            restore()
            return
        if not text or _duck["saved"] is not None or not any(n and n in text for n in names):
            return
        try:
            vol = _local_volume_get()
            if vol is None:
                return
            _duck["saved"], _duck["until"] = vol, time.time() + 8
            _local_volume_set(min(vol, max(_options.get("duck_volume", 15), 1)))
        except Exception as e:
            logger.debug(e)

    core.on_partial = on_partial
    core.on_phrase_done = restore


# ---------------- обработка команд ----------------

def _run(core, func, *args, announce=None):
    """Выполняет запрос к API в фоне, чтобы распознавание не подвисало."""
    if not _options.get("token"):
        core.say("Яндекс Музыка не настроена. Войдите в Яндекс в веб-интерфейсе")
        return
    room = getattr(core, "current_room", None)

    def say(text):
        # ответ — в ту комнату, где была команда (поток команд сам знает текущую комнату)
        server = getattr(core, "rooms_server", None)
        if server is not None:
            server.run_in_commands(room, lambda: core.say(text))
        else:
            core.say(text)

    def worker():
        try:
            result = func(*args)
            if announce:
                text = announce(result) if callable(announce) else announce
                if text:
                    say(text)
        except Exception as e:
            logger.exception(e)
            say("Не получилось включить музыку")

    threading.Thread(target=worker, daemon=True).start()


def _start(core, phrase, starter, announce):
    """Новая музыка: в комнатах из фразы или там, где сказана команда.
    starter(player, остаток_фразы) запускает воспроизведение и возвращает трек/объект для announce."""
    rooms, rest = _parse_rooms(core, phrase)
    player, refusal = _pick_player(core, rooms)
    if player is None:
        core.say(refusal)
        return
    here = _room_id(core)

    def go():
        result = starter(player, rest)
        missing = []
        if result is not None and player.active:
            missing = _assign(core, rooms, player)
        return result, missing

    def text(res):
        result, missing = res
        if result is None and "mpv" in player.last_error:
            return "Не получилось включить музыку: не запускается проигрыватель mpv"
        if result is None and not callable(announce):
            return "Не получилось включить музыку"
        t = announce(result) if callable(announce) else announce
        if result is not None and _mr is not None and rooms != [here]:
            t += ": " + _names(core, [r for r in rooms if r not in missing])
        warn = _no_player_text(core, missing)
        return f"{t}. {warn}" if warn else t

    _run(core, go, announce=text)


def music_try_handle(core, action, phrase):
    """Хук для общих команд 'включи ...'/'выключи ...' из plugin_homeassistant. True — команда обработана."""
    p = phrase.lower().strip()
    if action == "off":
        m = re.match(r"^(всю музыку|музык\w*|плеер|песн\w*|волн\w*|радио|трек)\b", p)
        if m:
            cmd_stop(core, ("везде " if m.group(1) == "всю музыку" else "") + p[m.end():].strip())
            return True
        return False
    rules = [
        (r"^(музык\w*|мою волну|волну|моя волна|радио)\b", lambda rest: cmd_wave(core, rest)),
        (r"^(любим\w* (песни|музыку|треки)|мои лайки|мне нравится)\b", lambda rest: cmd_likes(core, rest)),
        (r"^(песню|трек|композицию)\b", lambda rest: _search_track(core, rest)),
        (r"^(исполнителя|группу|артиста|певца|певицу)\b", lambda rest: cmd_artist(core, rest)),
        (r"^(альбом)\b", lambda rest: cmd_album(core, rest)),
        (r"^(плейлист)\b", lambda rest: cmd_playlist(core, rest)),
        (r"^(то же,? что( и)?|то же самое,? что( и)?)\b", lambda rest: cmd_follow(core, rest)),
    ]
    for pattern, handler in rules:
        m = re.match(pattern, p)
        if m:
            handler(p[m.end():].strip())
            return True
    return False


def cmd_wave(core: VACore, phrase: str):
    rooms, rest = _parse_rooms(core, phrase)
    # «включи музыку везде» / «...и на кухне», когда здесь уже играет, — это подключить комнаты к этой музыке
    here = _player_of_room(_room_id(core)) if _multiroom() else None
    if here is not None and here.active and not rest and rooms != [_room_id(core)]:
        return _join(core, rooms, here)
    _start(core, phrase, lambda p, rest: p.play_station(WAVE_STATION, "ваша волна"), "Включаю вашу волну")


def cmd_likes(core: VACore, phrase: str):
    def go(p, rest):
        return p.play_list(_api().users_likes_tracks().fetch_tracks(), shuffle=True, name="любимые песни")
    _start(core, phrase, go, "Включаю любимые песни")


def cmd_search_track(core: VACore, phrase: str):
    if delegate_longer_command(core, __name__):  # "поставь таймер ..." и т.п. — в свой плагин
        return
    _search_track(core, phrase)


def _search_track(core: VACore, phrase: str):
    phrase = phrase.strip()
    if phrase.startswith("на паузу"):
        return cmd_pause(core, "")
    if phrase.startswith("громкость"):
        return cmd_volume_set(core, phrase)
    # "поставь исполнителя/альбом/плейлист ..." через общий разбор
    if phrase and music_try_handle(core, "on", phrase):
        return
    if not _parse_rooms(core, phrase, default_here=False)[1]:
        return cmd_wave(core, phrase)       # «поставь на кухне» — волна

    def go(p, query):
        res = _api().search(query)
        if res is None:
            return None
        best = res.best
        if best and best.type == "artist":
            p.play_station(f"artist:{best.result.id}", best.result.name)
            return p.current
        if best and best.type == "album":
            album = _api().albums_with_tracks(best.result.id)
            return p.play_list([t for vol in album.volumes for t in vol], name="альбом " + album.title)
        if best and best.type == "playlist":
            return p.play_list([t.track or t.fetch_track() for t in best.result.fetch_tracks()],
                               name="плейлист " + best.result.title)
        track = best.result if best and best.type == "track" else (
            res.tracks.results[0] if res.tracks and res.tracks.results else None)
        if track is None:
            return None
        return p.play_track_radio(track)

    _start(core, phrase, go, lambda t: f"Включаю {_describe(t)}" if t else "Ничего не нашла")


def cmd_artist(core: VACore, phrase: str):
    if not _parse_rooms(core, phrase, default_here=False)[1]:
        core.say("Какого исполнителя?")
        return

    def go(p, query):
        res = _api().search(query, type_="artist")
        if not res or not res.artists or not res.artists.results:
            return None
        artist = res.artists.results[0]
        tracks = _api().artists_tracks(artist.id, page_size=50).tracks
        p.play_list(tracks, shuffle=True, name=artist.name)
        return artist

    _start(core, phrase, go, lambda a: f"Включаю {a.name}" if a else "Не нашла такого исполнителя")


def cmd_album(core: VACore, phrase: str):
    def go(p, query):
        res = _api().search(query, type_="album")
        if not res or not res.albums or not res.albums.results:
            return None
        album = _api().albums_with_tracks(res.albums.results[0].id)
        p.play_list([t for vol in (album.volumes or []) for t in vol], name="альбом " + album.title)
        return album

    _start(core, phrase, go, lambda a: f"Включаю альбом {a.title}" if a else "Не нашла такой альбом")


def cmd_playlist(core: VACore, phrase: str):
    query = _parse_rooms(core, phrase, default_here=False)[1].strip().lower()
    if query in ("", "мне нравится", "любимое", "избранное"):
        return cmd_likes(core, phrase)

    def go(p, query):
        import difflib
        query = query.lower()
        own = _api().users_playlists_list()
        names = {pl.title.lower(): pl for pl in own}
        match = difflib.get_close_matches(query, list(names.keys()), n=1, cutoff=0.5)
        if match:
            pl = names[match[0]]
        else:
            res = _api().search(query, type_="playlist")
            if not res or not res.playlists or not res.playlists.results:
                return None
            pl = res.playlists.results[0]
        p.play_list([t.track or t.fetch_track() for t in pl.fetch_tracks()], name="плейлист " + pl.title)
        return pl

    _start(core, phrase, go, lambda pl: f"Включаю плейлист {pl.title}" if pl else "Не нашла такой плейлист")


def _pause(p, on):
    mr = _multiroom()
    rooms = mr.rooms_of(p.stream) if mr is not None else []
    if on:
        _set_muted(rooms, True)              # тишина сразу, а не после буфера
        p.mpv.set("pause", True)
    else:
        p.mpv.set("pause", False)
        _set_muted(rooms, False)


def cmd_pause(core: VACore, phrase: str):
    p = _target_player(core)
    if p:
        _pause(p, True)


def cmd_resume(core: VACore, phrase: str):
    p = _target_player(core)
    if p:
        _pause(p, False)
    else:
        cmd_wave(core, "")


def cmd_next(core: VACore, phrase: str):
    p = _target_player(core)
    if p:
        _run(core, p.next)


def cmd_prev(core: VACore, phrase: str):
    p = _target_player(core)
    if p:
        _run(core, p.prev)


def cmd_stop(core: VACore, phrase: str):
    """Выключить музыку: здесь (если её слушают и в других комнатах — только здесь), в названных комнатах, везде."""
    mr = _multiroom()
    if mr is None:
        for p in _all_players():
            p.stop()
        return
    if re.search(r"\b(всю|везде|повсюду|всех)\b", phrase) or "всю музыку" in (core.input_cmd_full or ""):
        for p in _all_players():
            p.stop()
        _cleanup()
        return
    rooms, _rest = _parse_rooms(core, phrase, default_here=False)
    if rooms:
        _assign(core, rooms, None)
        return
    here = _room_id(core)
    p = _player_of_room(here)
    if p is not None and p.active:
        others = [r for r in mr.rooms_of(p.stream) if r != here]
        if others:
            _assign(core, [here], None)          # другие комнаты продолжают слушать
        else:
            p.stop()
            _cleanup()
        return
    active = [p for p in _all_players() if p.active]
    if len(active) == 1:
        active[0].stop()
        _cleanup()
    elif active:
        core.say("Музыка играет в нескольких местах: " + _where_text(core) + ". Скажите, где выключить, или: выключи всю музыку")


def _join(core, rooms, player):
    rooms = [r for r in rooms if _player_of_room(r) is not player]
    if not rooms:
        core.say("Там уже играет эта музыка")
        return
    try:
        missing = _assign(core, rooms, player)
    except Exception as e:
        logger.exception(e)
        core.say("Не получилось подключить комнату")
        return
    ok = [r for r in rooms if r not in missing]
    text = f"Добавила: {_names(core, ok)}" if ok else ""
    warn = _no_player_text(core, missing)
    core.say(" ".join(t for t in (text + "." if text else "", warn) if t))


def _source_player(core):
    """Чью музыку добавлять в другие комнаты: этой комнаты, иначе единственной играющей линии."""
    p = _target_player(core)
    if p is not None:
        return p, None
    if not any(x.active for x in _all_players()):
        return None, "Сейчас музыка не играет"
    return None, "Музыка играет в нескольких местах: " + _where_text(core) + ". Скажите из нужной комнаты, или: как на кухне"


def _need_multiroom(core):
    if _multiroom() is None:
        core.say("Мультирум не работает: нет сервера Snapcast")
        return False
    return True


def cmd_join(core: VACore, phrase: str):
    """«добавь кухню», «и в спальне», «музыку везде» — подключить комнаты к музыке этой комнаты."""
    if delegate_longer_command(core, __name__):
        return
    if not _need_multiroom(core):
        return
    full = core.input_cmd_full or ""
    rooms, rest = _parse_rooms(core, full, default_here=False)
    rooms = [r for r in rooms if r != _room_id(core)] or rooms
    if not rooms:
        core.say("Куда добавить музыку? Скажите, например: добавь кухню")
        return
    p, refusal = _source_player(core)
    if p is None:
        if refusal == "Сейчас музыка не играет":
            return cmd_wave(core, phrase)    # «музыку на кухню» без музыки — включить там волну
        core.say(refusal)
        return
    _join(core, rooms, p)


def cmd_move(core: VACore, phrase: str):
    """«перенеси музыку на кухню» — музыка этой комнаты переезжает туда."""
    if not _need_multiroom(core):
        return
    rooms, _rest = _parse_rooms(core, phrase, default_here=False)
    if not rooms:
        core.say("Куда перенести музыку?")
        return
    p, refusal = _source_player(core)
    if p is None:
        core.say(refusal)
        return
    here = _room_id(core)
    missing = _assign(core, rooms, p)
    if len(missing) == len(rooms):
        core.say(_no_player_text(core, missing))
        return
    if here not in rooms and _player_of_room(here) is p:
        _assign(core, [here], None)
    warn = _no_player_text(core, missing)
    core.say(f"Перенесла: {_names(core, [r for r in rooms if r not in missing])}." + (" " + warn if warn else ""))


def cmd_follow(core: VACore, phrase: str):
    """«как на кухне» — эта комната слушает то же, что на кухне."""
    if not _need_multiroom(core):
        return
    rooms, _rest = _parse_rooms(core, phrase, default_here=False)
    rooms = [r for r in rooms if r != _room_id(core)]
    if not rooms:
        core.say("К какой комнате присоединиться?")
        return
    p = _player_of_room(rooms[0])
    if p is None or not p.active:
        core.say(f"В комнате {_name(core, rooms[0])} музыка не играет")
        return
    _join(core, [_room_id(core)], p)


def cmd_where(core: VACore, phrase: str):
    text = _where_text(core)
    core.say(text if text else "Музыка сейчас нигде не играет")


def cmd_volume_step(core: VACore, phrase: str, step: int):
    try:
        vol = _get_volume(core)
        if vol is not None:
            _set_volume(core, max(0, min(100, vol + step)))
    except Exception as e:
        logger.warning(f"громкость музыки: {e}")


def _get_volume(core):
    if _duck["saved"] is not None and _room_id(core) == LOCAL:
        return _duck["saved"]
    mr = _multiroom()
    if mr is not None:
        return mr.volume(_room_id(core))
    return _all_players()[0].mpv.get("volume", _options.get("volume", 60))


def _set_volume(core, value):
    # если музыка сейчас приглушена из-за обращения к Ирине — меняем громкость, к которой вернёмся
    if _duck["saved"] is not None and _room_id(core) == LOCAL:
        _duck["saved"] = value
        return
    mr = _multiroom()
    if mr is not None:
        mr.set_volume(_room_id(core), value)
    else:
        _all_players()[0].mpv.set("volume", value)


def cmd_volume_set(core: VACore, phrase: str):
    if delegate_longer_command(core, __name__):  # "громкость голоса ..." — громкость ответов в комнате
        return
    value = extract_number(phrase)
    if value is None:
        core.say("Скажите громкость от нуля до ста")
        return
    try:
        _set_volume(core, max(0, min(100, value)))
    except Exception as e:
        logger.warning(f"громкость музыки: {e}")
        core.say("В этой комнате нет плеера для музыки")


def cmd_what(core: VACore, phrase: str):
    p = _target_player(core)
    if p and p.current:
        core.say(f"Играет {_describe(p.current)}")
    else:
        core.say("Сейчас ничего не играет")


def cmd_like(core: VACore, phrase: str):
    p = _target_player(core)
    t = p.current if p else None
    if not t:
        core.say("Сейчас ничего не играет")
        return
    _run(core, lambda: _api().users_likes_tracks_add(t.track_id), announce="Добавила в любимые")


def cmd_dislike(core: VACore, phrase: str):
    p = _target_player(core)
    t = p.current if p else None
    if not t:
        return

    def go():
        _api().users_dislikes_tracks_add(t.track_id)
        return p.next()
    _run(core, go)


# ---------------- для веб-интерфейса ----------------

def status_for_ui():
    """{"multiroom": bool, "lines": [{"id", "playing", "paused", "what"}], "rooms": {id: линия|None}, "volumes": {id: %}}"""
    mr = _multiroom()
    lines = [{"id": p.stream, "playing": p.active, "paused": p.active and p.paused(), "what": p.describe()}
             for p in _all_players()]
    rooms, volumes = {}, {}
    if mr is not None:
        try:
            for cid, c in mr.clients().items():
                if c["connected"]:
                    rooms[cid] = None if c["stream"] == "idle" else c["stream"]
                    volumes[cid] = c["volume"]
        except Exception as e:
            logger.debug(e)
    return {"multiroom": mr is not None, "lines": lines, "rooms": rooms, "volumes": volumes}


def ui_assign(room_id, line, volume=None):
    """Из веб-интерфейса: подключить комнату к линии (None — выключить), громкость музыки комнаты."""
    mr = _multiroom()
    if mr is None:
        raise RuntimeError("Мультирум не работает: нет сервера Snapcast")
    if volume is not None:
        mr.set_volume(room_id, volume)
        return
    if line and line not in _players:
        raise ValueError("нет такой линии")
    mr.assign(room_id, line or None)
    _cleanup()
