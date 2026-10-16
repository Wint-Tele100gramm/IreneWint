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
    "tts_chunk_bytes": 4096,
}


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
        self.thread = threading.Thread(target=self._recognize_loop, name=f"asr-{room_id}", daemon=True)
        self.thread.start()

    def __repr__(self):
        return f"<Room {self.id} '{self.name}'>"

    # --- приём звука (вызывается из asyncio) ---
    def feed(self, pcm: bytes):
        if self.speaking or time.time() < self.mute_until:
            return
        try:
            self.audio_q.put_nowait(pcm)
        except queue.Full:
            logger.warning(f"{self}: распознавание не успевает, кусок звука пропущен")

    def end_segment(self, level=0):
        self.level = level
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
                if full:
                    self._phrase(full)
                if item == "END":
                    continue
            if isinstance(item, bytes):
                seg_bytes += len(item)
                text, partial = rec.accept(item)
                if text is not None:
                    seg_bytes = 0 if self.local else seg_bytes
                    if self.local:
                        self.wake_sent = False
                        if text:
                            self._phrase(text)
                    elif text:
                        parts.append(text)
                else:
                    if partial and not self.wake_sent and self.server.has_name(partial):
                        self.wake_sent = True
                        self.send_json({"type": "wake"})
                        self.server.on_wake(self, partial)

    def _phrase(self, text):
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
        chunk = self.server.options["tts_chunk_bytes"]
        await self.ws.send(json.dumps({"type": "tts_start", "rate": rate}))
        for i in range(0, len(pcm), chunk):
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

        threading.Thread(target=self._command_loop, name="commands", daemon=True).start()
        threading.Thread(target=self._ticker, name="timers", daemon=True).start()

    # --- утилиты ---
    def has_name(self, text: str) -> bool:
        words = text.split()
        return any(n in words for n in self.core.voiceAssNames)

    def run_coro(self, coro, wait=False, timeout=30):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if wait:
            return fut.result(timeout)
        return fut

    def on_wake(self, room, partial):
        # приглушить музыку на приставке, если имя прозвучало в комнате с приставкой
        if room.local and hasattr(self.core, "on_partial"):
            try:
                self.core.on_partial(partial)
            except Exception as e:
                logger.debug(e)

    # --- выбор комнаты: одну фразу могли услышать несколько клиентов ---
    def submit(self, room: Room, text: str):
        in_context = self.core.context is not None
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

        def routed(text):
            room = core.current_room
            if room is None or room.local:
                if room is not None:
                    room.speaking = True
                try:
                    return local_say(text)
                finally:
                    if room is not None:
                        room.speaking = False
                        room.mute_until = time.time() + 0.4
            if not room.connected:
                logger.warning(f"{room} отключилась, ответ «{text}» не озвучен")
                return
            core.lastSay = text
            rate, pcm = self.synth(text)
            if pcm:
                room.play_pcm(rate, pcm)

        core.play_voice_assistant_speech = routed
        core.say_in_all_rooms = self.broadcast

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
        rooms = [r for r in self.rooms.values() if r.connected]
        threads = [threading.Thread(target=r.play_pcm, args=(rate, pcm)) for r in rooms if not r.local]
        for t in threads:
            t.start()
        local = [r for r in rooms if r.local]
        if local:
            prev = self.core.current_room
            self.core.current_room = local[0]
            try:
                self.core.play_voice_assistant_speech(text)
            finally:
                self.core.current_room = prev
        for t in threads:
            t.join()
        return len(rooms)

    # --- WebSocket ---
    async def handle_client(self, ws):
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
            name = str(hello.get("name") or room_id)
            old = self.rooms.get(room_id)
            if old:
                old.close()
            room = Room(self, room_id, name, ws=ws)
            if isinstance(hello.get("volume"), int):
                room.volume = max(0, min(100, hello["volume"]))
            self.rooms[room_id] = room
            await ws.send(json.dumps({"type": "welcome", "volume": room.volume}))
            logger.warning(f"Подключилась комната «{name}» ({room_id}) с {ws.remote_address[0]}, прошивка {hello.get('fw')}")

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
                         max_size=2 ** 20, ping_interval=20, ping_timeout=20) as srv:
            print(f"Сервер комнат Ирины слушает ws://{self.options['host']}:{self.options['port']}/")
            if not self.options["token"]:
                print("ВНИМАНИЕ: token не задан в runva_rooms.json — подключиться может любой в сети")
            await srv.serve_forever()

    # --- микрофон самой приставки как ещё одна комната ---
    def _start_local_room(self, name):
        import sounddevice as sd
        room = Room(self, "local", name, local=True)
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


def main():
    parser = argparse.ArgumentParser(description="Ирина: сервер для клиентов в комнатах")
    parser.add_argument("--local-room", help="использовать микрофон приставки как комнату с этим именем")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()

    options = load_options(py_file=os.path.abspath(__file__), default_options=DEFAULT_OPTIONS)
    if args.local_room is not None:
        options["local_room"] = args.local_room
    if args.port:
        options["port"] = args.port

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
