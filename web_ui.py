# Веб-интерфейс сервера комнат: http://<IP приставки>:8765/
#
# Лёгкий: без новых библиотек и отдельного процесса. Страница (web/index.html) отдаётся тем же
# сервером websockets, что обслуживает клиентов ESP32; все действия идут через WebSocket /ui
# (JSON: {"id":1,"method":"...","params":{...}} -> {"id":1,"result":...} или {"id":1,"error":"..."}),
# сервер сам присылает события {"event":"rooms"|"log"|"phrase"|..., "data":...}.

import asyncio
import collections
import dataclasses
import hmac
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse

logger = logging.getLogger("rooms")

ROOT = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(ROOT, "web", "index.html")


# ---------------------------------------------------------------- описание настроек для страницы
# file: "rooms" — runva_rooms.json, иначе имя плагина (options/<plugin>.json).
# live: изменение действует сразу; иначе — после перезапуска Ирины (страница предложит перезапустить).

SETTINGS = [
    {"id": "assistant", "title": "Ассистент", "file": "core", "fields": [
        {"key": "voiceAssNames", "label": "Имена помощника", "type": "text",
         "help": "Через |, все формы: ирина|ирины|ирину"},
        {"key": "replyNoCommandFound", "label": "Ответ, если команда не понята", "type": "text", "live": True},
        {"key": "replyNoCommandFoundInContext", "label": "Ответ, если не понят ответ на вопрос", "type": "text",
         "live": True},
    ]},
    {"id": "voice", "title": "Голос", "file": "plugin_tts_vosk", "fields": [
        {"key": "speakerId", "label": "Голос vosk-tts", "type": "select", "live": True,
         "choices": [[0, "0 — женский"], [1, "1 — мужской"], [2, "2 — Ирина"], [3, "3 — женский"],
                     [4, "4 — мужской"]],
         "help": "Кнопка «Послушать» скажет фразу выбранным голосом в выбранной комнате"},
    ]},
    {"id": "ha", "title": "Home Assistant", "file": "plugin_homeassistant", "fields": [
        {"key": "url", "label": "Адрес", "type": "text", "live": True, "help": "Например http://192.168.2.10:8123"},
        {"key": "token", "label": "Токен", "type": "secret", "live": True,
         "help": "HA → Профиль → Безопасность → Долгосрочные токены доступа"},
        {"key": "say_confirm", "label": "Подтверждать голосом («Включаю свет»)", "type": "bool", "live": True},
        {"key": "match_threshold", "label": "Строгость поиска устройств (0–1)", "type": "number", "step": 0.05,
         "live": True, "help": "Больше — меньше ложных срабатываний, но чаще «не нашла устройство»"},
        {"key": "refresh_minutes", "label": "Обновлять список устройств, мин", "type": "number", "live": True},
    ]},
    {"id": "music", "title": "Яндекс Музыка", "file": "plugin_yandex_music", "fields": [
        {"key": "token", "label": "Токен", "type": "secret",
         "help": "Проще получить кнопкой «Войти в Яндекс» ниже"},
        {"key": "volume", "label": "Громкость музыки", "type": "number"},
        {"key": "duck_volume", "label": "Громкость, пока Ирина слушает команду", "type": "number"},
        {"key": "max_bitrate", "label": "Качество, кбит/с", "type": "select",
         "choices": [[128, "128"], [192, "192"], [320, "320"]]},
    ]},
    {"id": "time", "title": "Время", "file": "plugin_datetime", "fields": [
        {"key": "style", "label": "Как называть время", "type": "select", "live": True,
         "choices": [["natural", "без четверти восемь вечера"], ["digital", "девятнадцать сорок пять"]]},
        {"key": "sayDayPart", "label": "Добавлять «утра», «вечера»", "type": "bool", "live": True},
        {"key": "sayNoon", "label": "Говорить «полдень», «полночь»", "type": "bool", "live": True},
        {"key": "prefix", "label": "Начало ответа", "type": "text", "live": True},
    ]},
    {"id": "timer", "title": "Таймер", "file": "plugin_timer", "fields": [
        {"key": "defaultMinutes", "label": "«Просто таймер» — на сколько минут", "type": "number", "live": True},
        {"key": "wavRepeatTimes", "label": "Сколько раз звучит сигнал", "type": "number", "live": True},
    ]},
    {"id": "server", "title": "Сервер и клиенты", "file": "rooms", "fields": [
        {"key": "token", "label": "Ключ клиентов", "type": "secret",
         "help": "Тот же ключ вписывается в настройках каждого клиента ESP32"},
        {"key": "ui_password", "label": "Пароль этой страницы", "type": "secret",
         "help": "Пусто — используется ключ клиентов"},
        {"key": "default_volume", "label": "Громкость голоса для новых клиентов", "type": "number"},
        {"key": "local_room", "label": "Микрофон приставки как комната", "type": "text",
         "help": "Название комнаты (зона HA) или пусто — не слушать микрофон приставки"},
        {"key": "arbitration_ms", "label": "Окно выбора комнаты, мс", "type": "number", "live": True,
         "help": "Если фразу услышали несколько клиентов — ответит тот, где громче"},
        {"key": "log_heard", "label": "Писать в журнал все услышанные фразы", "type": "bool", "live": True},
        {"key": "dump_audio", "label": "Записывать фразы в WAV (папка)", "type": "text", "live": True,
         "help": "Например /tmp/irene-dump; пусто — не записывать. Записи слушаются на вкладке «Журнал»"},
        {"key": "tts_cache_max_mb", "label": "Кэш озвучки, не больше МБ", "type": "number", "live": True},
        {"key": "tts_cache_max_days", "label": "Удалять фразы, не звучавшие дней", "type": "number", "live": True},
    ]},
]


class _LogHandler(logging.Handler):
    def __init__(self, ui):
        super().__init__(logging.INFO)
        self.ui = ui

    def emit(self, record):
        try:
            line = {"t": record.created, "level": record.levelname, "text": record.getMessage()}
            self.ui.log.append(line)
            self.ui.emit("log", line)
        except Exception:
            pass


class WebUI:
    def __init__(self, server):
        self.server = server
        self.core = server.core
        self.sessions = {}                      # ключ -> время входа
        self.clients = set()                    # открытые страницы (WebSocket)
        self.log = collections.deque(maxlen=300)
        self.phrases = collections.deque(maxlen=100)
        self.restart_needed = False
        self.yandex_login = {"state": "idle"}
        self._yandex_cancel = False
        self.started = time.time()
        logger.addHandler(_LogHandler(self))

    # ---------------------------------------------------------------- события на страницы
    def emit(self, event, data):
        """Отправить событие всем открытым страницам (из любого потока)."""
        loop = self.server.loop
        if not self.clients or loop is None:
            return
        msg = json.dumps({"event": event, "data": data}, ensure_ascii=False)
        loop.call_soon_threadsafe(self._send_all, msg)

    def _send_all(self, msg):
        for ws in list(self.clients):
            asyncio.ensure_future(self._safe_send(ws, msg))

    @staticmethod
    async def _safe_send(ws, msg):
        try:
            await ws.send(msg)
        except Exception:
            pass

    def on_phrase(self, room, text, is_command):
        item = {"t": time.time(), "room": room.name, "room_id": room.id, "text": text,
                "level": room.level, "command": is_command}
        self.phrases.append(item)
        self.emit("phrase", item)

    async def ticker(self):
        n = 0
        while True:
            await asyncio.sleep(0.25)
            n += 1
            self._sync_meters()
            if not self.clients:
                continue
            # живой уровень микрофонов — 4 раза в секунду: [уровень, шум]
            live = {r.id: [r.live_level, r.info.get("noise")] for r in list(self.server.rooms.values())
                    if r.connected and r.live_level is not None}
            if live:
                self._send_all(json.dumps({"event": "meter", "data": live}))
            if n % 8 == 0:
                self._send_all(json.dumps({"event": "rooms", "data": self.rooms_state()}, ensure_ascii=False))

    def _sync_meters(self):
        """Клиенты шлют живой уровень, только пока открыта страница: зря не нагружаем Wi-Fi."""
        want = bool(self.clients)
        for room in list(self.server.rooms.values()):
            if room.local or "meter" not in room.caps or not room.connected:
                continue
            if room.meter_enabled != want:
                room.meter_enabled = want
                room.send_json({"type": "meter", "enable": want})
                if not want:
                    room.live_level = None

    # ---------------------------------------------------------------- HTTP (страница и записи)
    def password(self):
        o = self.server.options
        return o.get("ui_password") or o.get("token") or ""

    def process_request(self, connection, request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None                                    # клиент ESP32 или страница (/ui) — WebSocket
        url = urllib.parse.urlsplit(request.path)
        path = url.path
        if path in ("/", "/index.html"):
            try:
                with open(PAGE, "rb") as f:
                    return self._http(connection, 200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._http(connection, 500, "Нет файла web/index.html".encode(), "text/plain; charset=utf-8")
        if path.startswith("/dump/"):
            key = urllib.parse.parse_qs(url.query).get("k", [""])[0]
            if self.password() and key not in self.sessions:
                return self._http(connection, 403, b"forbidden", "text/plain")
            folder = self.server.options.get("dump_audio") or ""
            name = os.path.basename(urllib.parse.unquote(path[len("/dump/"):]))
            full = os.path.join(folder, name)
            if folder and name.endswith(".wav") and os.path.isfile(full):
                with open(full, "rb") as f:
                    return self._http(connection, 200, f.read(), "audio/wav")
        if path == "/favicon.ico":
            return self._http(connection, 204, b"", "image/x-icon")
        return self._http(connection, 404, "Не найдено".encode(), "text/plain; charset=utf-8")

    @staticmethod
    def _http(connection, status, body: bytes, ctype):
        r = connection.respond(status, "")
        for h in ("Content-Type", "Content-Length"):
            del r.headers[h]
        r.headers["Content-Type"] = ctype
        r.headers["Content-Length"] = str(len(body))
        r.headers["Cache-Control"] = "no-store"
        return dataclasses.replace(r, body=body)

    # ---------------------------------------------------------------- WebSocket страницы
    async def handle(self, ws):
        authorized = not self.password()
        try:
            await ws.send(json.dumps({"event": "hello", "data": {"need_login": not authorized}}))
            if authorized:
                self.clients.add(ws)
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
                try:
                    if method == "login":
                        result = self._login(params)
                        authorized = True
                        self.clients.add(ws)
                    elif not authorized:
                        raise PermissionError("Нужно войти")
                    else:
                        fn = getattr(self, "api_" + method.replace(".", "_"), None)
                        if fn is None:
                            raise ValueError(f"Неизвестная команда {method}")
                        result = await asyncio.get_running_loop().run_in_executor(None, fn, params)
                    await ws.send(json.dumps({"id": mid, "result": result}, ensure_ascii=False))
                except Exception as e:
                    if not isinstance(e, (PermissionError, ValueError, KeyError)):
                        logger.exception(e)
                    await ws.send(json.dumps({"id": mid, "error": str(e)}, ensure_ascii=False))
        finally:
            self.clients.discard(ws)

    def _login(self, params):
        pw = self.password()
        given = str(params.get("password", ""))
        key = str(params.get("key", ""))
        if key and key in self.sessions:
            return {"key": key}
        if pw and not hmac.compare_digest(given.encode(), pw.encode()):
            time.sleep(1)                       # от перебора
            raise PermissionError("Неверный пароль")
        key = secrets.token_urlsafe(18)
        self.sessions[key] = time.time()
        return {"key": key}

    # ---------------------------------------------------------------- состояние
    def rooms_state(self):
        srv = self.server
        names = srv.options.get("room_names") or {}
        result = []
        seen = set()
        for room in list(srv.rooms.values()):
            seen.add(room.id)
            info = getattr(room, "info", {})
            result.append({
                "id": room.id, "name": room.name, "connected": room.connected, "local": room.local,
                "volume": room.volume, "speaking": room.speaking, "levels": list(getattr(room, "levels", [])),
                "assigned": room.id in names, **info,
                "caps": sorted(room.caps),
                "mic_gain_db": srv.options.get("local_mic_gain_db", 0) if room.local else None,
            })
        for rid, name in names.items():
            if rid not in seen:
                result.append({"id": rid, "name": name, "connected": False, "local": rid == "local",
                               "assigned": True, "levels": []})
        result.sort(key=lambda r: (not r["connected"], r["name"]))
        return result

    def api_state(self, params):
        return {
            "rooms": self.rooms_state(),
            "phrases": list(self.phrases),
            "log": list(self.log)[-150:],
            "system": self.system_info(),
            "restart_needed": self.restart_needed,
            "assistant_names": self.core.voiceAssNames,
        }

    def system_info(self):
        info = {"uptime": int(time.time() - self.started), "asr": self.server.asr.name,
                "tts": self.core.ttsEngineId}
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        info["rss_mb"] = int(line.split()[1]) // 1024
            with open("/proc/meminfo") as f:
                mem = {l.split(":")[0]: int(l.split()[1]) for l in f}
            info["mem_total_mb"] = mem["MemTotal"] // 1024
            info["mem_avail_mb"] = mem["MemAvailable"] // 1024
            info["load"] = round(os.getloadavg()[0], 2)
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                info["temp_c"] = round(int(f.read()) / 1000)
        except Exception:
            pass
        try:
            st = os.statvfs(ROOT)
            info["disk_free_mb"] = st.f_bavail * st.f_frsize // (1024 * 1024)
        except Exception:
            pass
        return info

    def api_system(self, params):
        return self.system_info()

    # ---------------------------------------------------------------- комнаты
    def _room(self, room_id):
        room = self.server.rooms.get(room_id)
        if room is None or not room.connected:
            raise ValueError("Комната не подключена")
        return room

    def api_room_volume(self, params):
        room = self._room(params["id"])
        room.volume = max(0, min(100, int(params["value"])))
        if room.local:
            self.server.options["local_volume"] = room.volume     # громкость голоса на этом компьютере
            self.server.save_options()
        else:
            room.send_json({"type": "volume", "value": room.volume})
        return room.volume

    def api_room_mic_gain(self, params):
        """Усиление микрофона. Пока только у микрофона этого компьютера; клиентам — с новой прошивкой."""
        room = self._room(params["id"])
        if not room.local:
            raise ValueError("Усиление клиента меняется на его странице (Setup)")
        value = max(-10, min(30, int(params["value"])))
        self.server.options["local_mic_gain_db"] = value
        self.server.save_options()
        return value

    def api_room_mic(self, params):
        """Настройки микрофона клиента (прошивка 2.0+): применяются сразу, без перезагрузки клиента."""
        room = self._room(params["id"])
        if "config" not in room.caps:
            raise ValueError("Прошивка клиента не умеет получать настройки — обновите её до 2.0")
        limits = {"gain": (0, 30), "vad": (30, 5000), "hang": (300, 3000), "agc_max": (0, 30)}
        cfg = {}
        for key, (lo, hi) in limits.items():
            if key in params:
                cfg[key] = max(lo, min(hi, int(params[key])))
        if "agc" in params:
            cfg["agc"] = bool(params["agc"])
        if not cfg:
            raise ValueError("Нечего менять")
        room.send_json({"type": "config", **cfg})
        room.info.update(cfg)
        logger.info(f"{room.name}: настройки микрофона {cfg}")
        return cfg

    def api_room_say(self, params):
        room = self.server.rooms.get(params.get("id") or "")
        text = str(params.get("text") or "Проверка связи. Меня хорошо слышно?").strip()
        self.server.run_in_commands(room, lambda: self.core.say(text))
        return True

    def api_room_assign(self, params):
        """Назначить клиенту комнату (зону Home Assistant). Имя хранится на сервере и важнее имени из клиента."""
        rid, name = str(params["id"]), str(params.get("name") or "").strip()
        names = dict(self.server.options.get("room_names") or {})
        if name:
            names[rid] = name
        else:
            names.pop(rid, None)
        self.server.options["room_names"] = names
        self.server.save_options()
        room = self.server.rooms.get(rid)
        if room is not None:
            room.name = name or getattr(room, "client_name", rid)
        logger.warning(f"Клиент «{rid}» назначен комнате «{room.name if room else name}»")
        return True

    def api_room_forget(self, params):
        return self.api_room_assign({"id": params["id"], "name": ""})

    def api_ha_areas(self, params):
        """Зоны Home Assistant — чтобы выбирать комнату из списка, а не вписывать руками."""
        try:
            import plugins.plugin_homeassistant as ha
            r = ha._api(self.core, "POST", "template",
                        {"template": "{{ areas() | map('area_name') | list | tojson }}"})
            return json.loads(r) if isinstance(r, str) else r
        except Exception as e:
            logger.info(f"Не удалось получить зоны Home Assistant: {e}")
            return []

    # ---------------------------------------------------------------- команды
    def api_command(self, params):
        """Выполнить команду текстом (как будто её сказали в комнате, без имени)."""
        text = str(params.get("text") or "").lower().strip()
        if not text:
            raise ValueError("Пустая команда")
        names = self.core.voiceAssNames
        words = text.split()
        if words and words[0] in names:
            text = " ".join(words[1:])
        room = self.server.rooms.get(params.get("room") or "")
        logger.info(f"Команда со страницы ({room.name if room else 'приставка'}): «{text}»")

        def run():
            if self.core.context is not None:
                self.core.run_input_str(text)
            else:
                self.core.execute_next(text, None)
        self.server.run_in_commands(room, run)
        return True

    def api_commands_list(self, params):
        result = []
        for plugin, cmds in sorted(self.core.plugin_commands.items()):
            name = (self.core.plugin_manifest(plugin) or {}).get("name", plugin)
            result.append({"plugin": plugin, "name": name, "commands": cmds})
        return result

    # ---------------------------------------------------------------- настройки
    def _options_of(self, file):
        if file == "rooms":
            return self.server.options
        return self.core.plugin_options(file)

    def api_settings_get(self, params):
        sections = []
        for sec in SETTINGS:
            opts = self._options_of(sec["file"])
            if opts is None:
                continue                       # плагин не загружен
            fields = []
            for f in sec["fields"]:
                if f["key"] not in opts and sec["file"] != "rooms":
                    continue
                item = dict(f)
                value = opts.get(f["key"])
                if f["type"] == "secret":
                    item["is_set"] = bool(value)
                    value = ""
                item["value"] = value
                fields.append(item)
            sections.append({"id": sec["id"], "title": sec["title"], "fields": fields})
        return {"sections": sections, "restart_needed": self.restart_needed}

    def api_settings_set(self, params):
        sec = next(s for s in SETTINGS if s["id"] == params["section"])
        opts = self._options_of(sec["file"])
        changed, need_restart = [], False
        for f in sec["fields"]:
            if f["key"] not in params["values"]:
                continue
            value = params["values"][f["key"]]
            if f["type"] == "secret" and value in ("", None):
                continue                       # пустое поле — оставить прежний секрет
            if f["type"] == "number":
                value = float(value)
                value = int(value) if value.is_integer() and f.get("step", 1) >= 1 else value
            elif f["type"] == "bool":
                value = bool(value)
            elif f["type"] == "select":
                kinds = {type(c[0]) for c in f["choices"]}
                value = int(value) if int in kinds else value
            else:
                value = str(value).strip()
            if opts.get(f["key"]) != value:
                opts[f["key"]] = value
                changed.append(f["label"])
                need_restart = need_restart or not f.get("live")
        if sec["file"] == "rooms":
            self.server.save_options()
            self.server.apply_live_options()
        else:
            self.core.save_plugin_options(sec["file"], opts)
        if need_restart:
            self.restart_needed = True
        if changed:
            logger.warning(f"Изменены настройки «{sec['title']}»: {', '.join(changed)}")
        return {"changed": changed, "restart_needed": self.restart_needed}

    def api_voice_test(self, params):
        opts = self.core.plugin_options("plugin_tts_vosk")
        if opts is None:
            raise ValueError("Голос vosk-tts не загружен")
        opts["speakerId"] = int(params["speakerId"])
        self.core.save_plugin_options("plugin_tts_vosk", opts)
        return self.api_room_say({"id": params.get("room"), "text": "Привет! Я Ирина. Так звучит мой голос."})

    def api_server_restart(self, params):
        logger.warning("Перезапуск по команде со страницы")

        def bye():
            time.sleep(0.7)
            os._exit(3)                        # systemd (Restart=on-failure) запустит Ирину заново
        threading.Thread(target=bye, daemon=True).start()
        return True

    # ---------------------------------------------------------------- сценарии
    def api_scenes_get(self, params):
        opts = self.core.plugin_options("plugin_scenes")
        if opts is None:
            raise ValueError("Плагин сценариев не загружен")
        return {"aliases": opts.get("aliases") or {}, "scenes": opts.get("scenes") or {},
                "quiet_steps": opts.get("quiet_steps", True)}

    def api_scenes_set(self, params):
        opts = self.core.plugin_options("plugin_scenes")
        if opts is None:
            raise ValueError("Плагин сценариев не загружен")
        clean = lambda s: " ".join(str(s).lower().replace("ё", "е").split())
        aliases = {clean(k): clean(v) for k, v in (params.get("aliases") or {}).items() if clean(k) and clean(v)}
        scenes = {}
        for k, steps in (params.get("scenes") or {}).items():
            steps = [str(s).strip() for s in steps if str(s).strip()]
            if clean(k) and steps:
                scenes[clean(k)] = steps
        opts["aliases"], opts["scenes"] = aliases, scenes
        opts["quiet_steps"] = bool(params.get("quiet_steps", True))
        self.core.save_plugin_options("plugin_scenes", opts)
        import plugins.plugin_scenes as scenes_plugin
        self.server.run_in_commands(None, lambda: scenes_plugin.reload(self.core), wait=True)
        logger.warning(f"Сценарии сохранены: псевдонимов {len(aliases)}, сценариев {len(scenes)}")
        return True

    # ---------------------------------------------------------------- записи фраз
    def api_dump_list(self, params):
        folder = self.server.options.get("dump_audio") or ""
        if not folder or not os.path.isdir(folder):
            return {"enabled": bool(folder), "files": []}
        files = sorted((f for f in os.listdir(folder) if f.endswith(".wav")), reverse=True)[:100]
        return {"enabled": True, "files": [{"name": f, "size": os.path.getsize(os.path.join(folder, f))}
                                           for f in files]}

    # ---------------------------------------------------------------- вход в Яндекс Музыку
    def api_yandex_login(self, params):
        """Вход в Яндекс Музыку по коду. mode: music — только права Музыки (по умолчанию),
        music_login — плюс имя аккаунта по желанию, all — все права приложения, как раньше."""
        from utils import yandex_oauth as yo
        if self.yandex_login.get("state") == "waiting":
            self._yandex_cancel = True                     # новый вход отменяет предыдущий
            time.sleep(0.2)
        mode = params.get("mode") if params.get("mode") in yo.MODES else "music"
        try:
            code = yo.request_code(mode)
        except yo.OAuthError as e:
            hint = " Попробуйте «все права приложения»." if e.code == "invalid_scope" else ""
            self.yandex_login = {"state": "error", "message": f"Яндекс отклонил запрос: {e}.{hint}"}
            return self.yandex_login
        except Exception as e:
            self.yandex_login = {"state": "error", "message": f"Нет связи с Яндексом: {e}"}
            return self.yandex_login
        self._yandex_cancel = False
        self.yandex_login = {"state": "waiting", "url": code.get("verification_url", "https://ya.ru/device"),
                             "code": code.get("user_code"), "mode": yo.MODE_TITLES[mode]}

        def run():
            try:
                token = yo.wait_token(code, should_cancel=lambda: self._yandex_cancel)["access_token"]
                ok, text = yo.check_token(token)
                if not ok:
                    hint = " Попробуйте вход с правом «имя аккаунта»." if mode == "music" else ""
                    raise RuntimeError(text + hint)
                opts = self.core.plugin_options("plugin_yandex_music")
                if opts is None:
                    raise RuntimeError("плагин Яндекс Музыки не загружен")
                opts["token"] = token
                self.core.save_plugin_options("plugin_yandex_music", opts)
                try:
                    os.chmod(os.path.join(self.core.jaaOptionsPath, "plugin_yandex_music.json"), 0o600)
                except OSError:
                    pass
                self.restart_needed = True
                self.yandex_login = {"state": "done", "message": text}
                logger.warning(f"Яндекс Музыка: {text}, права: {yo.MODE_TITLES[mode]}")
            except yo.OAuthError as e:
                if e.code == "cancelled":
                    return
                self.yandex_login = {"state": "error", "message": str(e)}
            except Exception as e:
                self.yandex_login = {"state": "error", "message": str(e)}
            self.emit("yandex", self.yandex_login)

        threading.Thread(target=run, name="yandex-login", daemon=True).start()
        return self.yandex_login
        try:
            from yandex_music import Client
        except ImportError:
            raise ValueError("Библиотека yandex-music не установлена")
        self.yandex_login = {"state": "starting"}

        def on_code(code):
            self.yandex_login = {"state": "waiting", "url": code.verification_url, "code": code.user_code}
            self.emit("yandex", self.yandex_login)

        def run():
            try:
                token = Client().device_auth(on_code=on_code)
                opts = self.core.plugin_options("plugin_yandex_music")
                if opts is None:
                    raise RuntimeError("плагин Яндекс Музыки не загружен")
                opts["token"] = token.access_token
                self.core.save_plugin_options("plugin_yandex_music", opts)
                try:
                    os.chmod(os.path.join(self.core.jaaOptionsPath, "plugin_yandex_music.json"), 0o600)
                except OSError:
                    pass
                self.restart_needed = True
                self.yandex_login = {"state": "done"}
                logger.warning("Вход в Яндекс Музыку выполнен, токен сохранён")
            except Exception as e:
                self.yandex_login = {"state": "error", "message": str(e)}
            self.emit("yandex", self.yandex_login)

        threading.Thread(target=run, name="yandex-login", daemon=True).start()
        for _ in range(50):                        # код обычно приходит за секунду-две
            if self.yandex_login["state"] != "starting":
                break
            time.sleep(0.1)
        return self.yandex_login
