# Ирина в режиме "сервер комнат".
#
# В каждой комнате стоит клиент (ESP32 с I2S-микрофоном и усилителем, см. esp32_client/).
# Клиент по Wi-Fi отправляет звук на сервер (WebSocket), сервер распознаёт речь (Vosk),
# выполняет команду и озвучивает ответ (TTS) — звук ответа уходит в ту комнату, откуда пришла команда.
#
# Запуск:  python runva_rooms.py              (настройки в runva_rooms.json, создаётся при первом запуске)
#          python runva_rooms.py --local-room "Гостиная"   (+ микрофон и динамик самой приставки как комната)
#
# Протокол (WebSocket, путь "/"):
#   клиент -> сервер, текст:  {"type":"hello","room":"kitchen","name":"Кухня","token":"...","rate":16000,"fw":"1.0","volume":70}
#                             {"type":"vad","state":"start"} / {"type":"vad","state":"end","level":1234}
#                             {"type":"text","text":"ирина привет"}         (отладка: команда текстом)
#   клиент -> сервер, binary: PCM s16le mono 16 кГц (только пока клиент слышит речь)
#   сервер -> клиент, текст:  {"type":"welcome","volume":70} {"type":"wake"} {"type":"idle"}
#                             {"type":"tts_start","rate":22050} ... {"type":"tts_end"}
#                             {"type":"volume","value":0..100} {"type":"error","message":"..."}
#   сервер -> клиент, binary: PCM s16le mono с частотой из tts_start (ответ Ирины)

import argparse
import asyncio
import collections
import difflib
import json
import logging
import os
import queue
import threading
import time
import wave

import numpy as np

from jaa import load_options
from vacore import VACore

logger = logging.getLogger("rooms")

DEFAULT_OPTIONS = {
    "host": "0.0.0.0",
    "port": 8765,
    "token": "",                 # общий ключ для клиентов; пустой — без проверки (не рекомендуется)
    "asr": "sherpa",             # распознавание: "sherpa" (новая потоковая модель Vosk) или "kaldi" (старая, папка model)
    "sherpa_model": "model-sherpa",  # папка потоковой модели Vosk для sherpa-onnx
    "asr_threads": 2,            # потоков на распознавание (sherpa)
    "model": "model",            # путь к модели Vosk для "kaldi"
    "local_room": "",            # имя комнаты для микрофона/динамика самой приставки ("" — выключено)
    "local_device": None,        # устройство ввода sounddevice (None — по умолчанию)
    "arbitration_ms": 450,       # окно, в котором одна фраза из нескольких комнат считается одной командой
    "max_segment_sec": 12,       # максимальная длина одной фразы от клиента
    "default_volume": 70,        # громкость ответов на клиентах по умолчанию
    "log_heard": False,          # писать в лог все распознанные фразы, даже без имени помощника (для проверки микрофона)
    "barge_in": False,           # микрофон приставки слушает и во время ответа Ирины (нужно эхоподавление:
                                 # bash scripts/rk3528/enable_aec.sh) — ответ можно прервать словом "стоп"
    "tts_chunk_bytes": 4096,
    "dump_audio": "",            # папка: сохранять каждую фразу из комнат в WAV (отладка распознавания); "" — выключено
    "dump_keep": 300,            # сколько последних WAV хранить в этой папке
    "tts_cache_max_mb": 300,     # кэш озвучки: не больше стольких МБ...
    "tts_cache_max_days": 90,    # ...и без фраз, не звучавших столько дней (0 — без ограничения)
    "web_ui": True,              # веб-интерфейс http://<IP приставки>:<port>/
    "ui_password": "",           # пароль страницы; пусто — ключ клиентов (token)
    "room_names": {},            # id клиента -> комната (зона HA), назначается на странице; важнее имени из клиента
}
OPTIONS_FILE = os.path.splitext(os.path.abspath(__file__))[0] + ".json"


# ------------------------------------------------------------------ комнаты

class Room:
    """Источник звука и получатель ответов: ESP32-клиент или локальный микрофон приставки."""

    def __init__(self, server: "RoomServer", room_id: str, name: str, ws=None, local=False):
        self.server = server
        self.id = room_id
        self.name = name                      # "Кухня" — совпадает с зоной (area) в Home Assistant
        self.ws = ws
        self.local = local
        self.connected = True
        self.speaking = False                 # сейчас звучит ответ — микрофон этой комнаты игнорируем
        self.mute_until = 0.0
        self.volume = server.options["default_volume"]
        self.level = 0                        # громкость последней фразы (для выбора комнаты)
        self.audio_q: "queue.Queue" = queue.Queue(maxsize=400)
        self.segment_started = 0.0
        self.wake_sent = False
        self.armed_until = 0.0
        self.fragment = None                  # (текст, время) — короткий обрывок фразы без имени
        self.dump = bytearray() if server.options.get("dump_audio") else None
        self.client_name = name               # имя комнаты из настроек клиента (name — может переназначить сервер)
        self.info = {}                        # прошивка, Wi-Fi, IP — для веб-интерфейса
        self.levels = collections.deque(maxlen=20)   # уровни последних фраз
        self.thread = threading.Thread(target=self._recognize_loop, name=f"asr-{room_id}", daemon=True)
        self.thread.start()

    def __repr__(self):
        return f"<Room {self.id} '{self.name}'>"

    # --- приём звука (вызывается из asyncio) ---
    def feed(self, pcm: bytes):
        barge_in = self.local and self.server.options.get("barge_in")
        if not barge_in and (self.speaking or time.time() < self.mute_until):
            return
        try:
            self.audio_q.put_nowait(pcm)
        except queue.Full:
            logger.warning(f"{self}: распознавание не успевает, кусок звука пропущен")

    def end_segment(self, level=0):
        self.level = level
        self.levels.append(level)
        self.audio_q.put("END")

    def close(self):
        self.connected = False
        self.audio_q.put(None)

    # --- распознавание (отдельный поток на комнату; Vosk отпускает GIL) ---
    def _recognize_loop(self):
        # у клиентов ESP32 конец фразы определяет их VAD, у микрофона приставки — сам распознаватель
        rec = self.server.asr.session(endpointing=self.local)
        max_bytes = int(self.server.options["max_segment_sec"] * 16000 * 2)
        seg_bytes = 0
        parts = []   # у клиентов с VAD фраза собирается целиком до конца сегмента
        while True:
            item = self.audio_q.get()
            if item is None:
                return
            if item == "END" or seg_bytes > max_bytes:
                text = rec.finish()
                parts.append(text)
                full = " ".join(p for p in parts if p)
                parts, seg_bytes, self.wake_sent = [], 0, False
                self._dump_segment(full)
                if full:
                    self._phrase(full)
                if item == "END":
                    continue
            if isinstance(item, bytes):
                seg_bytes += len(item)
                if self.dump is not None:
                    self.dump += item
                    if len(self.dump) > max_bytes:
                        del self.dump[:len(self.dump) - max_bytes]
                text, partial = rec.accept(item)
                if text is not None:
                    seg_bytes = 0 if self.local else seg_bytes
                    if self.local:
                        self.wake_sent = False
                        if text:
                            self._dump_segment(text)
                            self._phrase(text)
                    elif text:
                        parts.append(text)
                else:
                    if self.speaking and partial and self.server.is_stop_phrase(partial):
                        self.server.interrupt(self, partial)
                    if partial and not self.wake_sent and self.server.has_name(partial):
                        self.wake_sent = True
                        self.send_json({"type": "wake"})
                        self.server.on_wake(self, partial)

    def _dump_segment(self, text):
        """Сохраняет звук фразы в WAV (опция dump_audio): слышно, что на самом деле пришло с микрофона."""
        if self.dump is None:
            return
        pcm, self.dump = bytes(self.dump), bytearray()
        if not pcm or (self.local and not text):
            return
        try:
            folder = self.server.options["dump_audio"]
            os.makedirs(folder, exist_ok=True)
            safe = "".join(c if c.isalnum() else "_" for c in (text or "тишина"))[:40]
            path = os.path.join(folder, f"{time.strftime('%Y%m%d-%H%M%S')}_{self.id}_{self.level}_{safe}.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm)
            files = sorted(f for f in os.listdir(folder) if f.endswith(".wav"))
            for old in files[:-int(self.server.options.get("dump_keep", 300))]:
                os.unlink(os.path.join(folder, old))
        except Exception as e:
            logger.warning(f"Не удалось сохранить звук фразы: {e}")

    def _phrase(self, text):
        if self.speaking and self.server.is_stop_phrase(text) and not self.server.has_name(text):
            self.server.interrupt(self, text)   # "стоп" во время ответа — просто замолчать
            return
        # тихо сказанное имя детектор речи клиента иногда режет надвое: «ири» + «на привет» -> «ирина привет»
        now = time.time()
        if self.fragment and now - self.fragment[1] < 2.5 and not self.server.has_name(text):
            for joined in (self.fragment[0] + text, self.fragment[0] + " " + text):
                if self.server.has_name(joined):
                    logger.info(f"{self.name}: склеила «{self.fragment[0]}» + «{text}»")
                    text = joined
                    break
        self.fragment = None
        if not self.server.has_name(text) and len(text.split()) <= 3:
            self.fragment = (text, now)
        words = text.split()
        names = self.server.core.voiceAssNames
        if words and all(w in names for w in words):
            # сказали только "Ирина" — ждём команду следующей фразой
            self.armed_until = time.time() + 6
            self.send_json({"type": "wake"})
            return
        if time.time() < self.armed_until and not self.server.has_name(text):
            text = names[0] + " " + text
        self.armed_until = 0.0
        self.server.submit(self, text)

    # --- отправка клиенту (из любых потоков) ---
    def send_json(self, obj):
        if self.local or not self.connected or self.ws is None:
            return
        self.server.run_coro(self.ws.send(json.dumps(obj, ensure_ascii=False)), wait=False)

    def play_pcm(self, rate: int, pcm: bytes):
        """Отправляет ответ клиенту и ждёт, пока он уйдёт (клиент читает со скоростью воспроизведения)."""
        if not self.connected or self.ws is None:
            return
        self.speaking = True
        try:
            self.server.run_coro(self._send_audio(rate, pcm), wait=True, timeout=len(pcm) / (rate * 2) + 15)
        except Exception as e:
            logger.warning(f"{self}: не удалось отправить звук: {e}")
        finally:
            self.speaking = False
            self.mute_until = time.time() + 0.4   # хвост эха в комнате

    async def _send_audio(self, rate, pcm):
        # отдаём звук в темпе воспроизведения (с запасом 0,3 с впереди): буфер клиента невелик,
        # а если его переполнить, клиент перестаёт обслуживать сеть и соединение рвётся
        chunk = self.server.options["tts_chunk_bytes"]
        bytes_per_sec = rate * 2
        lead = 0.3
        await self.ws.send(json.dumps({"type": "tts_start", "rate": rate}))
        loop = asyncio.get_running_loop()
        started = loop.time()
        for i in range(0, len(pcm), chunk):
            ahead = i / bytes_per_sec - (loop.time() - started)
            if ahead > lead:
                await asyncio.sleep(ahead - lead)
            await self.ws.send(pcm[i:i + chunk])
        await self.ws.send(json.dumps({"type": "tts_end"}))


# ------------------------------------------------------------------ сервер

class RoomServer:
    def __init__(self, options):
        self.options = options
        self.rooms = {}                           # id -> Room
        self.loop: asyncio.AbstractEventLoop = None
        self.cmd_q: "queue.Queue" = queue.Queue()
        self.pending = []                         # кандидаты одной команды из разных комнат
        self.pending_timer = None
        self.pending_lock = threading.Lock()
        self.context_room = None
        self.say_everywhere = False               # отложенное событие, а его комната отключилась — во все комнаты
        self._in_broadcast = False

        from utils.asr_backends import create_backend
        self.asr = create_backend(options)
        logger.warning(f"Распознавание речи: {self.asr.name}")

        logger.warning("Загружаю плагины и модель голоса. Если модели vosk-tts ещё нет, она скачивается (~130 МБ) "
                       "— быстрее и с прогрессом: bash scripts/rk3528/get_tts_model.sh")
        self.core = VACore()
        self.core.init_with_plugins()
        logger.warning("Модель голоса загружена")
        self.core.current_room = None
        self.core.rooms_server = self
        self._install_tts_router()
        self._install_room_timers()
        # таймаут диалога приходит из потока таймера — выполняем его в потоке команд, в комнате диалога
        self.core.run_in_main = lambda fn: self.cmd_q.put(("call", self.context_room, fn))

        threading.Thread(target=self._command_loop, name="commands", daemon=True).start()
        threading.Thread(target=self._ticker, name="timers", daemon=True).start()
        threading.Thread(target=self._cache_cleaner, name="tts-cache", daemon=True).start()

        self.ui = None
        if options.get("web_ui", True):
            from web_ui import WebUI
            self.ui = WebUI(self)

    def run_in_commands(self, room, fn, wait=False, timeout=60):
        """Выполнить fn в потоке команд (ядро не потокобезопасно) от имени комнаты room (None — приставка)."""
        if not wait:
            self.cmd_q.put(("call", room, fn))
            return
        done, box = threading.Event(), {}

        def run():
            try:
                box["result"] = fn()
            except Exception as e:
                box["error"] = e
            finally:
                done.set()
        self.cmd_q.put(("call", room, run))
        if not done.wait(timeout):
            raise TimeoutError("Ирина занята — команда не выполнилась")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def save_options(self):
        with open(OPTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.options, f, sort_keys=True, indent=4, ensure_ascii=False)

    def apply_live_options(self):
        """Настройки, которые можно применить без перезапуска."""
        dump = bool(self.options.get("dump_audio"))
        for room in self.rooms.values():
            if dump and room.dump is None:
                room.dump = bytearray()
            elif not dump:
                room.dump = None

    # --- утилиты ---
    def has_name(self, text: str) -> bool:
        words = text.split()
        return any(n in words for n in self.core.voiceAssNames)

    def run_coro(self, coro, wait=False, timeout=30):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if wait:
            return fut.result(timeout)
        return fut

    STOP_WORDS = ("стоп", "хватит", "замолчи", "помолчи", "перестань", "тихо", "заткнись", "отмена")

    def is_stop_phrase(self, text: str) -> bool:
        words = text.split()
        return any(w in self.STOP_WORDS for w in words[-3:])

    def interrupt(self, room, heard=""):
        """Прервать ответ Ирины в комнате (только приставка: ESP32 не слушает, пока говорит)."""
        if not (room.local and room.speaking):
            return
        try:
            import plugins.plugin_playwav_aplay as aplay_plugin
            if aplay_plugin.stop_playback(self.core):
                logger.warning(f"{room.name}: ответ прерван («{heard}»)")
        except Exception as e:
            logger.debug(e)

    def on_wake(self, room, partial):
        # "Ирина, ..." во время ответа — замолкаем и слушаем новую команду
        if room.local and room.speaking and self.options.get("barge_in"):
            self.interrupt(room, partial)
        # приглушить музыку на приставке, если имя прозвучало в комнате с приставкой
        if room.local and hasattr(self.core, "on_partial"):
            try:
                self.core.on_partial(partial)
            except Exception as e:
                logger.debug(e)

    # --- выбор комнаты: одну фразу могли услышать несколько клиентов ---
    def submit(self, room: Room, text: str):
        in_context = self.core.context is not None
        if self.ui:
            self.ui.on_phrase(room, text, in_context or self.has_name(text))
        if in_context or self.has_name(text):
            logger.info(f"{room.name}: «{text}» (уровень {room.level})")
        elif self.options.get("log_heard"):
            logger.info(f"{room.name}: услышала «{text}» (без имени — не команда)")
        if in_context:
            # в режиме контекста (игра, диалог) слушаем только комнату, где он начался
            if self.context_room is None or room is self.context_room:
                self.cmd_q.put(("cmd", room, text))
            return
        if not self.has_name(text):
            return
        with self.pending_lock:
            self.pending.append((time.time(), room, text, room.level))
            if self.pending_timer is None:
                self.pending_timer = threading.Timer(self.options["arbitration_ms"] / 1000, self._resolve_pending)
                self.pending_timer.start()

    def _resolve_pending(self):
        with self.pending_lock:
            items, self.pending, self.pending_timer = self.pending, [], None
        groups = []   # похожие фразы — одна команда
        for item in items:
            for g in groups:
                if difflib.SequenceMatcher(None, g[0][2], item[2]).ratio() > 0.6:
                    g.append(item)
                    break
            else:
                groups.append([item])
        for g in groups:
            best = max(g, key=lambda it: it[3])
            if len(g) > 1:
                logger.info(f"Фразу услышали в {[it[1].name for it in g]}, отвечаю в {best[1].name}")
            self.cmd_q.put(("cmd", best[1], best[2]))

    # --- выполнение команд: ядро Ирины не потокобезопасно, поэтому один поток ---
    def _command_loop(self):
        core = self.core
        while True:
            kind, room, text = self.cmd_q.get()
            try:
                if kind == "tick":
                    core._update_timers()
                    continue
                core.current_room = room
                if kind == "call":
                    text()           # здесь text — функция (продолжение диалога после таймаута)
                else:
                    core.run_input_str(text)
                self.context_room = room if core.context is not None else None
                if hasattr(core, "on_phrase_done"):
                    core.on_phrase_done()
            except Exception as e:
                logger.exception(e)
            finally:
                if kind == "cmd":
                    room.send_json({"type": "idle"})

    def _ticker(self):
        while True:
            time.sleep(0.5)
            self.cmd_q.put(("tick", None, None))

    # --- ответы: в ту комнату, откуда пришла команда ---
    def _install_tts_router(self):
        core = self.core
        local_say = core.play_voice_assistant_speech   # уже с приглушением музыки из плагина Яндекс Музыки
        local_wav = core.play_wav
        self._local_say, self._local_wav = local_say, local_wav

        def routed(text):
            if self.say_everywhere and not self._in_broadcast:
                self.broadcast(text)
                return
            room = core.current_room
            if room is None or room.local:
                return self._play_local(room, local_say, text)
            if not room.connected:
                logger.warning(f"{room} отключилась, ответ «{text}» не озвучен")
                return
            core.lastSay = text
            rate, pcm = self.synth(text)
            if pcm:
                room.play_pcm(rate, pcm)

        def routed_wav(path):
            # звуки плагинов (сигнал таймера и т.п.) — тоже в комнату, а не в динамик приставки
            if self.say_everywhere and not self._in_broadcast:
                self._broadcast_audio(*limit_rate(*read_wav_mono16(path)), local_fn=lambda: local_wav(path))
                return
            room = core.current_room
            if room is None or room.local:
                return self._play_local(room, local_wav, path)
            if room.connected:
                room.play_pcm(*limit_rate(*read_wav_mono16(path)))

        core.play_voice_assistant_speech = routed
        core.play_wav = routed_wav
        core.say_in_all_rooms = self.broadcast

    @staticmethod
    def _play_local(room, fn, arg):
        if room is not None:
            room.speaking = True
        try:
            return fn(arg)
        finally:
            if room is not None:
                room.speaking = False
                room.mute_until = time.time() + 0.4

    def synth(self, text):
        """Текст -> (частота, PCM s16le mono) через выбранный TTS Ирины (с кэшем)."""
        core = self.core
        if core.useTTSCache:
            path = core.get_tts_cache_file(text)
            os.makedirs(os.path.dirname(path), exist_ok=True)
        else:
            path = core.get_tempfilename() + ".wav"
        if not (core.useTTSCache and os.path.exists(path)):
            core.tts_to_filewav(text, path)
        elif core.useTTSCache:
            core.touch_cache_file(path)
        try:
            return read_wav_mono16(path)
        except Exception as e:
            logger.error(f"TTS не создал wav для «{text}»: {e}")
            return 16000, b""
        finally:
            if not core.useTTSCache and os.path.exists(path):
                os.unlink(path)

    def broadcast(self, text):
        """Озвучить во всех комнатах сразу (объявления)."""
        rate, pcm = self.synth(text)
        return self._broadcast_audio(rate, pcm, local_fn=lambda: self._local_say(text))

    def _broadcast_audio(self, rate, pcm, local_fn):
        rooms = [r for r in self.rooms.values() if r.connected]
        threads = [threading.Thread(target=r.play_pcm, args=(rate, pcm)) for r in rooms if not r.local]
        for t in threads:
            t.start()
        local = [r for r in rooms if r.local]
        if local:
            self._in_broadcast = True
            try:
                self._play_local(local[0], lambda _: local_fn(), None)
            finally:
                self._in_broadcast = False
        for t in threads:
            t.join()
        return len(rooms)

    # --- отложенные события (таймеры) звучат в той комнате, где их поставили ---
    def _install_room_timers(self):
        core = self.core
        orig_set_timer = core.set_timer

        def set_timer(duration, func_end, func_upd=None):
            room = core.current_room
            return orig_set_timer(duration, (self._fire_in_room, (room.id if room else None, func_end)), func_upd)

        core.set_timer = set_timer

    def _fire_in_room(self, core, arg):
        room_id, func = arg
        room = self.rooms.get(room_id) if room_id else None
        everywhere = False
        if room is None or not room.connected:
            others = [r for r in self.rooms.values() if r.connected]
            if room_id and others:
                logger.warning(f"Комната «{room_id}» отключилась — событие таймера озвучу во всех комнатах")
                everywhere = True
            room = None
        prev = core.current_room
        core.current_room, self.say_everywhere = room, everywhere
        try:
            core.call_ext_func(func)
        finally:
            core.current_room, self.say_everywhere = prev, False

    # --- кэш озвучки не должен съесть eMMC ---
    def _cache_cleaner(self):
        from utils.tts_cache_cleanup import cleanup
        while True:
            try:
                deleted, freed = cleanup(self.core.tts_cache_dir, self.options.get("tts_cache_max_mb", 0),
                                         self.options.get("tts_cache_max_days", 0))
                if deleted:
                    logger.info(f"Кэш озвучки: удалено {deleted} фраз, освобождено {freed / 1e6:.1f} МБ")
            except Exception as e:
                logger.warning(f"Очистка кэша озвучки: {e}")
            time.sleep(6 * 3600)

    # --- WebSocket ---
    async def handle_client(self, ws):
        if self.ui and ws.request.path.split("?")[0] == "/ui":
            await self.ui.handle(ws)              # страница веб-интерфейса
            return
        room = None
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if hello.get("type") != "hello":
                raise ValueError("первым сообщением должен быть hello")
            if self.options["token"] and hello.get("token") != self.options["token"]:
                await ws.send(json.dumps({"type": "error", "message": "bad token"}))
                logger.warning(f"Неверный токен от {ws.remote_address}")
                return
            if int(hello.get("rate", 16000)) != 16000:
                await ws.send(json.dumps({"type": "error", "message": "rate must be 16000"}))
                return
            room_id = str(hello.get("room") or ws.remote_address[0])
            client_name = str(hello.get("name") or room_id)
            name = (self.options.get("room_names") or {}).get(room_id) or client_name
            old = self.rooms.get(room_id)
            if old:
                old.close()
            room = Room(self, room_id, name, ws=ws)
            room.client_name = client_name
            room.info = {"fw": hello.get("fw"), "rssi": hello.get("rssi"), "reset": hello.get("reset"),
                         "drops": hello.get("drops"), "ip": ws.remote_address[0], "since": time.time(),
                         "client_name": client_name}
            if isinstance(hello.get("volume"), int):
                room.volume = max(0, min(100, hello["volume"]))
            self.rooms[room_id] = room
            await ws.send(json.dumps({"type": "welcome", "volume": room.volume}))
            extra = ""
            if "rssi" in hello:
                extra = f", Wi-Fi {hello['rssi']} дБм, запуск: {hello.get('reset', '?')}, обрывов Wi-Fi: {hello.get('drops', 0)}"
            logger.warning(f"Подключилась комната «{name}» ({room_id}) с {ws.remote_address[0]}, "
                           f"прошивка {hello.get('fw')}{extra}")
            if isinstance(hello.get("rssi"), int) and hello["rssi"] < -75:
                logger.warning(f"«{name}»: слабый сигнал Wi-Fi ({hello['rssi']} дБм) — возможны обрывы")
            if "ПРОСАДКА" in str(hello.get("reset", "")):
                logger.warning(f"«{name}»: клиент перезагрузился из-за просадки питания — проверьте блок питания")

            async for msg in ws:
                if isinstance(msg, bytes):
                    room.feed(msg)
                    continue
                data = json.loads(msg)
                t = data.get("type")
                if t == "vad" and data.get("state") == "end":
                    room.end_segment(int(data.get("level", 0)))
                elif t == "text":
                    room.level = int(data.get("level", 1 << 30))
                    room._phrase(str(data.get("text", "")).lower().strip())
        except Exception as e:
            logger.info(f"Клиент {getattr(ws, 'remote_address', '?')}: {e}")
        finally:
            if room is not None:
                room.close()
                if self.rooms.get(room.id) is room:
                    del self.rooms[room.id]
                logger.warning(f"Комната «{room.name}» отключилась")

    async def serve(self):
        from websockets.asyncio.server import serve
        self.loop = asyncio.get_running_loop()
        if self.options.get("local_room"):
            self._start_local_room(self.options["local_room"])
        async with serve(self.handle_client, self.options["host"], self.options["port"],
                         max_size=2 ** 20, ping_interval=20, ping_timeout=20,
                         process_request=self.ui.process_request if self.ui else None) as srv:
            print(f"Сервер комнат Ирины слушает ws://{self.options['host']}:{self.options['port']}/")
            if self.ui:
                asyncio.ensure_future(self.ui.ticker())
                logger.warning(f"Веб-интерфейс: http://<IP приставки>:{self.options['port']}/")
            if not self.options["token"]:
                print("ВНИМАНИЕ: token не задан в runva_rooms.json — подключиться может любой в сети")
            await srv.serve_forever()

    # --- микрофон самой приставки как ещё одна комната ---
    def _start_local_room(self, name):
        import sounddevice as sd
        name = (self.options.get("room_names") or {}).get("local") or name
        room = Room(self, "local", name, local=True)
        room.info = {"fw": "микрофон приставки", "since": time.time()}
        self.rooms["local"] = room
        levels = []

        def callback(indata, frames, t, status):
            pcm = bytes(indata)
            samples = np.frombuffer(pcm, dtype=np.int16)
            levels.append(int(np.sqrt(np.mean(samples.astype(np.float32) ** 2))))
            del levels[:-12]                       # ~3 секунды по 0.25 с
            room.level = max(levels)
            room.feed(pcm)

        def run():
            try:
                with sd.RawInputStream(samplerate=16000, blocksize=4000, device=self.options.get("local_device"),
                                       dtype="int16", channels=1, callback=callback):
                    logger.warning(f"Локальный микрофон слушает как комната «{name}» "
                                   f"(устройство: {sd.query_devices(self.options.get('local_device'), 'input')['name']})")
                    while True:
                        time.sleep(1)
            except Exception as e:
                logger.error(f"Не удалось открыть микрофон приставки: {e}. Устройства ввода:\n"
                             + "\n".join(f"  {i}: {d['name']}" for i, d in enumerate(sd.query_devices())
                                          if d["max_input_channels"] > 0)
                             + "\nУкажите номер или часть имени в runva_rooms.json -> \"local_device\"")

        threading.Thread(target=run, name="local-mic", daemon=True).start()


def read_wav_mono16(path):
    with wave.open(path, "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        a = np.frombuffer(raw, dtype=np.int16).astype(np.int32)
    elif width == 4:
        a = (np.frombuffer(raw, dtype=np.int32) >> 16).astype(np.int32)
    elif width == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.int32) - 128) << 8
    else:
        raise ValueError(f"неподдерживаемая разрядность {width * 8} бит")
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype(np.int32)
    return rate, np.clip(a, -32768, 32767).astype("<i2").tobytes()


def limit_rate(rate, pcm, max_rate=22050):
    """44,1/48 кГц -> 22,05 кГц: клиенту хватает, а поток вдвое меньше (буфер ESP32 небольшой)."""
    if rate <= max_rate or not pcm:
        return rate, pcm
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    n = int(len(a) * max_rate / rate)
    out = np.interp(np.arange(n) * (rate / max_rate), np.arange(len(a)), a)
    return max_rate, np.clip(out, -32768, 32767).astype("<i2").tobytes()


def main():
    parser = argparse.ArgumentParser(description="Ирина: сервер для клиентов в комнатах")
    parser.add_argument("--local-room", help="использовать микрофон приставки как комнату с этим именем")
    parser.add_argument("--port", type=int)
    parser.add_argument("--dump-audio", metavar="ПАПКА", help="сохранять звук каждой фразы в WAV (отладка)")
    args = parser.parse_args()

    options = load_options(py_file=os.path.abspath(__file__), default_options=DEFAULT_OPTIONS)
    if args.local_room is not None:
        options["local_room"] = args.local_room
    if args.port:
        options["port"] = args.port
    if args.dump_audio:
        options["dump_audio"] = args.dump_audio

    # ядро Ирины настраивает свои логи само; у сервера комнат — свой вывод, чтобы видеть распознанные фразы
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [комнаты] %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    server = RoomServer(options)
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
