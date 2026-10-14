# Яндекс Музыка: "Моя волна", поиск песен/исполнителей/альбомов/плейлистов, управление плеером.
# Воспроизведение — через mpv (sudo apt install mpv), API — библиотека yandex-music (MarshalX).
#
# Токен: запустите  python scripts/rk3528/yandex_token.py  и подтвердите вход на ya.ru/device
#
# Примеры: "ирина включи музыку", "ирина включи мою волну", "ирина поставь песню кино группа крови",
#          "ирина включи исполнителя сплин", "ирина включи альбом ...", "ирина включи любимые песни",
#          "ирина дальше", "ирина пауза", "ирина продолжи", "ирина громче", "ирина громкость сорок",
#          "ирина что играет", "ирина лайк", "ирина не нравится", "ирина выключи музыку"

import os
import re
import time
import random
import logging
import threading

from vacore import VACore
from utils.ru_words_to_num import extract_number
from utils.mpv_ipc import MpvPlayer
from utils.cmd_route import delegate_longer_command

modname = os.path.basename(__file__)[:-3]
logger = logging.getLogger(__name__)

WAVE_STATION = "user:onyourwave"

_player = None   # type: Player | None
_duck = {"saved": None, "until": 0.0}  # громкость до приглушения по имени помощника


def start(core: VACore):
    manifest = {
        "name": "Яндекс Музыка",
        "version": "1.0",
        "require_online": True,
        "description": "Музыка из Яндекс Музыки через mpv.\n"
                       "Токен получите скриптом scripts/rk3528/yandex_token.py.\n"
                       "Команды: включи музыку|мою волну, поставь <песню>, включи исполнителя|альбом|плейлист <имя>, "
                       "любимые песни, пауза, продолжи, дальше, назад, громче, тише, громкость <число>, "
                       "что играет, лайк, не нравится, выключи музыку",
        "options_label": {
            "token": "OAuth-токен Яндекс Музыки",
            "volume": "Громкость при запуске (0-100)",
            "duck_volume": "Громкость музыки, пока Ирина говорит (0 - пауза)",
            "max_bitrate": "Максимальный битрейт, кбит/с (320, 192, 128)",
            "mpv_args": "Дополнительные аргументы mpv, например [\"--audio-device=alsa/plughw:0,0\"]",
        },
        "default_options": {
            "token": "",
            "volume": 60,
            "duck_volume": 15,
            "max_bitrate": 320,
            "mpv_args": [],
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
            "стоп|хватит|выключи музыку|останови музыку": cmd_stop,
            "громче": (cmd_volume_step, 10),
            "чуть громче": (cmd_volume_step, 5),
            "сильно громче": (cmd_volume_step, 25),
            "тише": (cmd_volume_step, -10),
            "чуть тише": (cmd_volume_step, -5),
            "сильно тише": (cmd_volume_step, -25),
            "громкость|сделай громкость|поставь громкость": cmd_volume_set,
            "что играет|что сейчас играет|что это за песня|как называется песня|кто поёт|кто поет": cmd_what,
            "лайк|лайкни|мне нравится|нравится эта песня|сохрани песню": cmd_like,
            "не нравится|дизлайк|убери эту песню": cmd_dislike,
        },
    }
    return manifest


def start_with_options(core: VACore, manifest: dict):
    global _player
    options = manifest["options"]
    _player = Player(core, options)
    # хук для plugin_homeassistant: "включи ..." / "выключи ..." сначала проверяются здесь
    core.music_try_handle = music_try_handle
    _install_ducking(core, options)
    if not options.get("token"):
        print("plugin_yandex_music: не задан token — запустите scripts/rk3528/yandex_token.py")


# ---------------- плеер ----------------

class Player:
    def __init__(self, core, options):
        self.core = core
        self.options = options
        self.client = None
        self.mpv = MpvPlayer(extra_args=["--volume=%d" % options.get("volume", 60)] + list(options.get("mpv_args", [])),
                             on_event=self._on_mpv_event)
        self.lock = threading.RLock()
        self.queue = []          # список Track
        self.index = -1
        self.station = None      # для потоковых станций (Моя волна, радио по треку)
        self.batch_id = None
        self.track_started_at = 0.0
        self.active = False      # что-то играет или на паузе
        self.ducked = False

    # --- API ---
    def api(self):
        if self.client is None:
            from yandex_music import Client
            token = self.options.get("token")
            if not token:
                raise RuntimeError("no token")
            self.client = Client(token).init()
        return self.client

    @property
    def current(self):
        if 0 <= self.index < len(self.queue):
            return self.queue[self.index]
        return None

    def _stream_url(self, track):
        infos = track.get_download_info()
        max_br = self.options.get("max_bitrate", 320)
        infos = [i for i in infos if i.codec in ("mp3", "aac", "he-aac") and i.bitrate_in_kbps <= max_br] or infos
        infos.sort(key=lambda i: (i.codec == "mp3", i.bitrate_in_kbps), reverse=True)
        return infos[0].get_direct_link()

    # --- запуск источников ---
    def play_list(self, tracks, shuffle=False):
        tracks = [t for t in tracks if t is not None and getattr(t, "available", True) is not False]
        if shuffle:
            random.shuffle(tracks)
        with self.lock:
            self._finish_feedback(skip=True)
            self.station, self.batch_id = None, None
            self.queue, self.index = tracks, -1
        return self.next(feedback=False)

    def play_station(self, station):
        with self.lock:
            self._finish_feedback(skip=True)
            self.station, self.queue, self.index = station, [], -1
            self._fetch_station(first=True)
        return self.next(feedback=False)

    def _fetch_station(self, first=False):
        cur = self.current
        res = self.api().rotor_station_tracks(self.station, queue=cur.id if cur and not first else None)
        if res is None:
            return
        self.batch_id = res.batch_id
        if first:
            try:
                self.api().rotor_station_feedback_radio_started(self.station, "irene", res.batch_id)
            except Exception as e:
                logger.debug(e)
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
        for _ in range(3):  # если трек недоступен, пробуем следующий
            try:
                self.mpv.load(self._stream_url(track))
                break
            except Exception as e:
                logger.warning(f"Не удалось запустить трек {track.title}: {e}")
                if self.index + 1 >= len(self.queue):
                    self.active = False
                    return None
                self.index += 1
                track = self.current
        self.active = True
        self.track_started_at = time.time()
        if self.station:
            try:
                self.api().rotor_station_feedback_track_started(self.station, track.id, self.batch_id)
            except Exception as e:
                logger.debug(e)
        return track

    def _finish_feedback(self, skip):
        if not self.station or self.current is None:
            return
        played = max(0.0, time.time() - self.track_started_at)
        try:
            if skip:
                self.api().rotor_station_feedback_skip(self.station, self.current.id, played, self.batch_id)
            else:
                self.api().rotor_station_feedback_track_finished(self.station, self.current.id, played, self.batch_id)
        except Exception as e:
            logger.debug(e)

    def _on_mpv_event(self, ev):
        if ev.get("event") == "end-file" and ev.get("reason") in ("eof", "error") and self.active:
            # следующий трек — в отдельном потоке, чтобы не блокировать чтение сокета mpv
            threading.Thread(target=self.next, kwargs={"feedback": True, "skipped": False}, daemon=True).start()

    def stop(self):
        with self.lock:
            self._finish_feedback(skip=True)
            self.active = False
            self.mpv.stop()

    def is_playing(self):
        return self.active and not self.mpv.get("pause", False)


def _describe(track):
    if track is None:
        return ""
    artists = ", ".join(track.artists_name()) if track.artists else ""
    return f"{artists} — {track.title}" if artists else track.title


# ---------------- приглушение музыки, пока Ирина говорит ----------------

def _install_ducking(core, options):
    if getattr(core, "_ym_ducking_installed", False):
        return
    core._ym_ducking_installed = True
    original = core.play_voice_assistant_speech

    def speech_with_ducking(text):
        p = _player
        if p is None or not p.active or p.mpv.get("pause", True):
            return original(text)
        vol = p.mpv.get("volume", options.get("volume", 60))
        duck = options.get("duck_volume", 15)
        try:
            if duck <= 0:
                p.mpv.set("pause", True)
            else:
                p.mpv.set("volume", min(vol, duck))
            return original(text)
        finally:
            if duck <= 0:
                p.mpv.set("pause", False)
            else:
                p.mpv.set("volume", vol)

    core.play_voice_assistant_speech = speech_with_ducking

    # приглушение по имени помощника (runva_vosk.py вызывает on_partial/on_phrase_done)
    names = [n for n in core.voiceAssNames] if isinstance(core.voiceAssNames, list) else str(core.voiceAssNames).split("|")
    state = _duck

    def restore():
        p = _player
        if state["saved"] is not None and p is not None:
            try:
                p.mpv.set("volume", state["saved"])
            except Exception:
                pass
        state["saved"] = None

    def on_partial(text):
        p = _player
        if state["saved"] is not None and time.time() > state["until"]:
            restore()
            return
        if not text or state["saved"] is not None or p is None or not p.active:
            return
        if any(n and n in text for n in names) and not p.mpv.get("pause", True):
            state["saved"] = p.mpv.get("volume", options.get("volume", 60))
            state["until"] = time.time() + 8
            p.mpv.set("volume", min(state["saved"], max(options.get("duck_volume", 15), 1)))

    core.on_partial = on_partial
    core.on_phrase_done = restore


# ---------------- обработка команд ----------------

def _run(core, func, *args, announce=None):
    """Выполняет запрос к API в фоне, чтобы распознавание не подвисало."""
    if _player is None or not _player.options.get("token"):
        core.say("Яндекс Музыка не настроена. Нужен токен")
        return

    def worker():
        try:
            track = func(*args)
            if announce:
                core.say(announce(track) if callable(announce) else announce)
        except Exception as e:
            logger.exception(e)
            core.say("Не получилось включить музыку")

    threading.Thread(target=worker, daemon=True).start()


def music_try_handle(core, action, phrase):
    """Хук для общих команд 'включи ...'/'выключи ...' из plugin_homeassistant. True — команда обработана."""
    p = phrase.lower().strip()
    if action == "off":
        if re.match(r"^(музык|плеер|песн|волн|радио|трек)", p):
            cmd_stop(core, "")
            return True
        return False
    rules = [
        (r"^(музык\w*|мою волну|волну|моя волна|радио)\b", lambda rest: cmd_wave(core, rest)),
        (r"^(любим\w* (песни|музыку|треки)|мои лайки|мне нравится)\b", lambda rest: cmd_likes(core, rest)),
        (r"^(песню|трек|композицию)\b", lambda rest: _search_track(core, rest)),
        (r"^(исполнителя|группу|артиста|певца|певицу)\b", lambda rest: cmd_artist(core, rest)),
        (r"^(альбом)\b", lambda rest: cmd_album(core, rest)),
        (r"^(плейлист)\b", lambda rest: cmd_playlist(core, rest)),
    ]
    for pattern, handler in rules:
        m = re.match(pattern, p)
        if m:
            handler(p[m.end():].strip())
            return True
    return False


def cmd_wave(core: VACore, phrase: str):
    _run(core, _player.play_station, WAVE_STATION, announce="Включаю вашу волну")


def cmd_likes(core: VACore, phrase: str):
    def go():
        tracks = _player.api().users_likes_tracks().fetch_tracks()
        return _player.play_list(tracks, shuffle=True)
    _run(core, go, announce="Включаю любимые песни")


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
    if not phrase:
        return cmd_wave(core, "")
    # "поставь исполнителя/альбом/плейлист ..." через общий разбор
    if music_try_handle(core, "on", phrase):
        return

    def go():
        res = _player.api().search(phrase)
        if res is None:
            return None
        best = res.best
        if best and best.type == "artist":
            return _player.play_station(f"artist:{best.result.id}")
        if best and best.type == "album":
            album = _player.api().albums_with_tracks(best.result.id)
            return _player.play_list([t for vol in album.volumes for t in vol])
        if best and best.type == "playlist":
            return _player.play_list([t.track or t.fetch_track() for t in best.result.fetch_tracks()])
        track = best.result if best and best.type == "track" else (res.tracks.results[0] if res.tracks and res.tracks.results else None)
        if track is None:
            return None
        # после найденной песни продолжаем похожими треками
        with _player.lock:
            _player._finish_feedback(skip=True)
            _player.station = f"track:{track.id}"
            _player.queue, _player.index = [track], -1
            try:
                _player._fetch_station(first=True)
            except Exception as e:
                logger.debug(e)
        return _player.next(feedback=False)

    _run(core, go, announce=lambda t: f"Включаю {_describe(t)}" if t else "Ничего не нашла")


def cmd_artist(core: VACore, phrase: str):
    if not phrase.strip():
        core.say("Какого исполнителя?")
        return

    def go():
        res = _player.api().search(phrase, type_="artist")
        if not res or not res.artists or not res.artists.results:
            return None
        artist = res.artists.results[0]
        tracks = _player.api().artists_tracks(artist.id, page_size=50).tracks
        _player.play_list(tracks, shuffle=True)
        return artist

    _run(core, go, announce=lambda a: f"Включаю {a.name}" if a else "Не нашла такого исполнителя")


def cmd_album(core: VACore, phrase: str):
    def go():
        res = _player.api().search(phrase, type_="album")
        if not res or not res.albums or not res.albums.results:
            return None
        album = _player.api().albums_with_tracks(res.albums.results[0].id)
        _player.play_list([t for vol in (album.volumes or []) for t in vol])
        return album

    _run(core, go, announce=lambda a: f"Включаю альбом {a.title}" if a else "Не нашла такой альбом")


def cmd_playlist(core: VACore, phrase: str):
    phrase = phrase.strip().lower()
    if phrase in ("", "мне нравится", "любимое", "избранное"):
        return cmd_likes(core, phrase)

    def go():
        import difflib
        own = _player.api().users_playlists_list()
        names = {pl.title.lower(): pl for pl in own}
        match = difflib.get_close_matches(phrase, list(names.keys()), n=1, cutoff=0.5)
        if match:
            pl = names[match[0]]
        else:
            res = _player.api().search(phrase, type_="playlist")
            if not res or not res.playlists or not res.playlists.results:
                return None
            pl = res.playlists.results[0]
        _player.play_list([t.track or t.fetch_track() for t in pl.fetch_tracks()])
        return pl

    _run(core, go, announce=lambda pl: f"Включаю плейлист {pl.title}" if pl else "Не нашла такой плейлист")


def cmd_pause(core: VACore, phrase: str):
    if _player and _player.active:
        _player.mpv.set("pause", True)


def cmd_resume(core: VACore, phrase: str):
    if _player and _player.active:
        _player.mpv.set("pause", False)
    else:
        cmd_wave(core, "")


def cmd_next(core: VACore, phrase: str):
    if _player and _player.active:
        _run(core, _player.next)


def cmd_prev(core: VACore, phrase: str):
    if _player and _player.active:
        _run(core, _player.prev)


def cmd_stop(core: VACore, phrase: str):
    if _player:
        _player.stop()


def cmd_volume_step(core: VACore, phrase: str, step: int):
    if not _player:
        return
    vol = _duck["saved"] if _duck["saved"] is not None else _player.mpv.get("volume", 60)
    _set_volume(max(0, min(100, vol + step)))


def _set_volume(value):
    # если музыка сейчас приглушена из-за обращения к Ирине — меняем громкость, к которой вернёмся
    if _duck["saved"] is not None:
        _duck["saved"] = value
    else:
        _player.mpv.set("volume", value)


def cmd_volume_set(core: VACore, phrase: str):
    value = extract_number(phrase)
    if value is None or not _player:
        core.say("Скажите громкость от нуля до ста")
        return
    _set_volume(max(0, min(100, value)))


def cmd_what(core: VACore, phrase: str):
    if _player and _player.active and _player.current:
        core.say(f"Играет {_describe(_player.current)}")
    else:
        core.say("Сейчас ничего не играет")


def cmd_like(core: VACore, phrase: str):
    t = _player.current if _player else None
    if not t:
        core.say("Сейчас ничего не играет")
        return
    _run(core, lambda: _player.api().users_likes_tracks_add(t.track_id), announce="Добавила в любимые")


def cmd_dislike(core: VACore, phrase: str):
    t = _player.current if _player else None
    if not t:
        return

    def go():
        _player.api().users_dislikes_tracks_add(t.track_id)
        return _player.next()
    _run(core, go)
