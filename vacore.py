# Ядро Ирины: команды плагинов, контекст и диалоги, таймеры, озвучка.
# На основе VACore Владислава Январева (MIT); убраны веб-API, ИИ-плагины, MPC-HC и второй движок озвучки.

import hashlib
import inspect
import json
import logging
import os
import time
import traceback
from threading import Timer
from typing import Dict

from termcolor import colored, cprint

from jaa import JaaCore

version = "14.0.0"

logger = logging.getLogger(__name__)


class DialogTimeout(Exception):
    """Бросается в генератор диалога, если пользователь не ответил вовремя."""


class VACore(JaaCore):
    def __init__(self):
        JaaCore.__init__(self)

        self.timers = [-1] * 8
        self.timersFuncEnd = [None] * 8

        self.commands = {}                       # фраза -> функция | (функция, параметр) | вложенный словарь
        self.plugin_commands: Dict[str, list] = {}
        self.ttss = {}                           # движок -> (init, say, to_wav_file)
        self.tts_owner: Dict[str, str] = {}      # движок -> плагин (его настройки входят в ключ кэша)
        self.playwavs = {}                       # движок -> (init, play)
        self.normalizers = {}                    # движок -> (init, normalize)
        self.fuzzy_processors: Dict[str, tuple] = {}
        self.run_in_main = lambda fn: fn()       # сервер комнат подменяет: выполнять в потоке команд

        self.isOnline = False
        self.version = version
        self.voiceAssNames = []
        self.voiceAssNameRunCmd = {}

        self.useTTSCache = False
        self.tts_cache_dir = "tts_cache"
        self.ttsEngineId = ""
        self.playWavEngineId = ""
        self.normalization_engine = ""

        self.logPolicy = ""
        self.tmpdir = "temp"
        self.tmpcnt = 0
        self.lastSay = ""

        self.context = None
        self.contextTimer = None
        self.contextExitCommand = ""
        self.contextTimerLastDuration = 0
        self.contextDefaultDuration = 10

        self.cur_callname = ""
        self.input_cmd_full = ""

    def init_with_plugins(self):
        self.init_plugins(["core"])
        self.display_init_info()
        self.setup_assistant_voice()

    # ---------------------------------------------------------------- манифесты плагинов
    def process_plugin_manifest(self, modname, manifest):
        req_online = manifest.get("require_online", True)
        for cmd, func in manifest.get("commands", {}).items():
            self.commands[cmd] = self.stub_online_required if (not self.isOnline and req_online) else func
            self.plugin_commands.setdefault(modname, []).append(cmd)
        for name, funcs in manifest.get("tts", {}).items():
            self.ttss[name] = funcs
            self.tts_owner[name] = modname
        self.playwavs.update(manifest.get("playwav", {}))
        self.normalizers.update(manifest.get("normalizer", {}))
        self.fuzzy_processors.update(manifest.get("fuzzy_processor", {}))

    def stub_online_required(self, core, phrase):
        self.say(self.plugin_options("core")["replyOnlineRequired"])

    def print_error(self, err_txt, e: Exception = None):
        cprint(err_txt, "red")
        traceback.print_exc()

    def print_red(self, txt):
        cprint(txt, "red")

    # ---------------------------------------------------------------- озвучка
    def setup_assistant_voice(self):
        try:
            self.playwavs[self.playWavEngineId][0](self)
        except Exception as e:
            self.print_error("Ошибка инициализации проигрывания WAV (playWavEngineId)", e)
            self.print_red('Попробуйте в options/core.json: "playWavEngineId": "aplay" (Linux) или "sounddevice"')
            self.ttsEngineId = "console"

        if self.normalization_engine != "none":
            try:
                self.normalizers[self.normalization_engine][0](self)
            except Exception as e:
                self.print_error(f"Ошибка инициализации нормализатора {self.normalization_engine}", e)
                self.normalization_engine = "none"

        try:
            self.ttss[self.ttsEngineId][0](self)
        except Exception as e:
            self.print_error("Ошибка инициализации озвучки (ttsEngineId) — ответы будут только текстом", e)
            self.ttsEngineId = "console"

        for name, (init, _) in self.fuzzy_processors.items():
            try:
                init(self)
            except Exception as e:
                self.print_error(f"Ошибка инициализации fuzzy_processor {name}", e)

    def normalize(self, text: str):
        if self.normalization_engine == "none":
            return text
        return self.normalizers[self.normalization_engine][1](self, text)

    def play_voice_assistant_speech(self, text_to_speech: str):
        """Сказать фразу. Сервер комнат подменяет этот метод: ответ уходит в комнату, откуда пришла команда."""
        self.lastSay = text_to_speech
        say_now = self.ttss[self.ttsEngineId][1]
        if say_now is not None:
            say_now(self, text_to_speech)
            return
        tts_file = self.get_tts_cache_file(text_to_speech) if self.useTTSCache else self.get_tempfilename() + ".wav"
        if self.useTTSCache and os.path.exists(tts_file):
            self.touch_cache_file(tts_file)
        else:
            os.makedirs(os.path.dirname(tts_file) or ".", exist_ok=True)
            self.tts_to_filewav(text_to_speech, tts_file)
        self.play_wav(tts_file)
        if not self.useTTSCache and os.path.exists(tts_file):
            os.unlink(tts_file)

    def say(self, text_to_speech: str):
        self.play_voice_assistant_speech(text_to_speech)

    def say2(self, text_to_speech: str):
        # совместимость со старыми плагинами (раньше был второй движок озвучки)
        self.say(text_to_speech)

    def tts_to_filewav(self, text_to_speech: str, filename: str):
        engine = self.ttss[self.ttsEngineId]
        if len(engine) > 2:
            engine[2](self, text_to_speech, filename)
        else:
            print("Этот движок озвучки не умеет сохранять в файл")

    def get_tempfilename(self):
        self.tmpcnt += 1
        return f"{self.tmpdir}/vacore_{self.tmpcnt}"

    def get_tts_cache_file(self, text_to_speech: str):
        # в ключ входят и настройки движка (голос, скорость): после их смены старые фразы не звучат прежним голосом
        settings = ""
        owner = self.tts_owner.get(self.ttsEngineId)
        if owner:
            try:
                settings = json.dumps(self.plugin_options(owner), sort_keys=True, ensure_ascii=False)
            except Exception:
                pass
        digest = hashlib.md5((settings + "\n" + text_to_speech).encode("utf-8")).hexdigest()
        text_slice = "".join(c if c.isalnum() or c in " ,.!?-" else " " for c in text_to_speech[:40])
        return f"{self.tts_cache_dir}/{self.ttsEngineId}/{text_slice}.{digest}.wav"

    @staticmethod
    def touch_cache_file(path: str):
        # время последнего использования — по нему очистка кэша удаляет давно не звучавшие фразы
        try:
            os.utime(path, None)
        except OSError:
            pass

    def all_num_to_text(self, text: str):
        from utils.all_num_to_text import all_num_to_text
        return all_num_to_text(text)

    # ---------------------------------------------------------------- поиск команды
    def find_best_cmd_with_fuzzy(self, command, context, allow_rest_phrase=True, threshold: float = None):
        """Ищет команду в context ({"фраза|вариант": ...}).
        Возвращает (ключ в context, уверенность 0..1, остаток фразы) или None.
        Сначала точное совпадение, потом начало фразы, потом нечёткие сравнители из плагинов."""
        for keyall in context:
            if command in keyall.split("|"):
                return keyall, 1.0, ""
        if allow_rest_phrase:
            for keyall in context:
                for key in keyall.split("|"):
                    if command.startswith(key + " "):
                        return keyall, 1.0, command[len(key) + 1:]
        if threshold is None:
            threshold = self.plugin_options("core")["fuzzyThreshold"]
        for name, (_, process) in self.fuzzy_processors.items():
            res = process(self, command, context, allow_rest_phrase)
            logger.info(f"Fuzzy {name} для «{command}»: {res}")
            if res is not None and res[1] > threshold:
                return res
        return None

    def fuzzy_get_command_key_from_context(self, predicted_command: str, context: dict):
        for keyall in context:
            if predicted_command in keyall.split("|"):
                return keyall
        return None

    def execute_next(self, command, context):
        if context is None:              # первый вход
            context = self.commands
            self.input_cmd_full = command

        if not isinstance(context, dict):   # функция: команда найдена
            self.context_clear()
            if command == self.contextExitCommand and self.contextExitCommand != "":
                print("Контекст сброшен командой выхода")
                return
            self.call_ext_func_phrase(command, context)
            return

        try:
            res = self.find_best_cmd_with_fuzzy(command, context, True)
            if res is not None:
                keyall, _, rest_phrase = res
                self.execute_next(rest_phrase, context[keyall])
                return
            if self.context is None:
                self.say(self.plugin_options("core")["replyNoCommandFound"])
            else:
                self.say(self.plugin_options("core")["replyNoCommandFoundInContext"])
                if self.contextTimer is not None:
                    self.context_set(self.context, self.contextTimerLastDuration)
        except Exception as err:
            logger.exception(err)

    # ---------------------------------------------------------------- таймеры
    @staticmethod
    def util_time_to_readable(curtime):
        import datetime
        return datetime.datetime.fromtimestamp(curtime).strftime('%Y-%m-%d %H:%M:%S')

    def set_timer(self, duration, timerFuncEnd, timerFuncUpd=None):
        """Через duration секунд вызвать timerFuncEnd (функция или (функция, параметр)). Возвращает номер или -1."""
        for i in range(len(self.timers)):
            if self.timers[i] <= 0:
                self.timers[i] = time.time() + duration
                self.timersFuncEnd[i] = timerFuncEnd
                print(f"Таймер {i}: через {duration} с ({self.util_time_to_readable(self.timers[i])})")
                return i
        return -1

    def clear_timer(self, index, runEndFunc=False):
        if runEndFunc and self.timersFuncEnd[index] is not None:
            self.call_ext_func(self.timersFuncEnd[index])
        self.timers[index] = -1
        self.timersFuncEnd[index] = None

    def clear_timers(self):
        for i in range(len(self.timers)):
            self.timers[i] = -1
            self.timersFuncEnd[i] = None

    def _update_timers(self):
        now = time.time()
        for i, end in enumerate(self.timers):
            if 0 < end <= now:
                print(f"Таймер {i} сработал")
                self.clear_timer(i, True)

    # ---------------------------------------------------------------- вызов функций плагинов
    def call_ext_func(self, funcparam):
        if isinstance(funcparam, tuple):
            funcparam[0](self, funcparam[1])
        else:
            funcparam(self)

    def call_ext_func_phrase(self, phrase, funcparam):
        if isinstance(funcparam, tuple):
            res = funcparam[0](self, phrase, funcparam[1])
        else:
            res = funcparam(self, phrase)
        if inspect.isgenerator(res):
            self.dialog_start(res)

    # ---------------------------------------------------------------- диалоги на генераторах (идея из форка AlexeyBond)
    # Команда может вернуть генератор:
    #     def ask_light(core, phrase):
    #         answer = yield "Где включить свет: на кухне или в зале?"   # сказать и ждать ответа (без имени)
    #         answer = yield ("Повторите", 15)                          # ...с таймаутом 15 с
    #         return "Готово"                                            # сказать и закончить диалог
    # Если ответа нет, в генератор приходит DialogTimeout: его можно перехватить и что-то сказать.
    # Ответ ждётся в той же комнате, где начался диалог.

    def dialog_start(self, gen):
        self._dialog_step(gen, lambda: next(gen))

    def _dialog_step(self, gen, step):
        try:
            value = step()
        except StopIteration as e:
            if isinstance(e.value, str) and e.value:
                self.say(e.value)
            return
        except DialogTimeout:
            return
        if inspect.isgenerator(value):
            return self.dialog_start(value)
        timeout = None
        if isinstance(value, tuple):
            value, timeout = (value + (None,))[:2]
        if isinstance(value, str) and value:
            self.say(value)

        def answer(core, phrase):
            core._dialog_step(gen, lambda: gen.send(phrase))
        answer.dialog_gen = gen
        self.context_set(answer, timeout)

    # ---------------------------------------------------------------- воспроизведение и разбор фраз
    def play_wav(self, wavfile):
        self.playwavs[self.playWavEngineId][1](self, wavfile)

    def run_input_str(self, voice_input_str, func_before_run_cmd=None):
        """Разбор распознанной фразы: «ирина таймер пять» -> команда «таймер пять».
        В контексте (диалог, игра) фраза выполняется целиком, без имени."""
        if voice_input_str is None:
            return False
        if self.logPolicy == "all":
            print("Input" + (" (in context)" if self.context is not None else "") + ":", voice_input_str)
        try:
            words = voice_input_str.split(" ")
            if self.context is None:
                for ind, callname in enumerate(words):
                    if callname not in self.voiceAssNames:
                        continue
                    self.cur_callname = callname
                    if self.logPolicy == "cmd":
                        print("Input (cmd): ", voice_input_str)
                    command = " ".join(words[ind + 1:])
                    if callname in self.voiceAssNameRunCmd:
                        command = self.voiceAssNameRunCmd[callname] + " " + command
                    if func_before_run_cmd is not None:
                        func_before_run_cmd()
                    self.execute_next(command, None)
                    return True
                return False
            if self.logPolicy == "cmd":
                print("Input (cmd in context): ", voice_input_str)
            if func_before_run_cmd is not None:
                func_before_run_cmd()
            self.execute_next(voice_input_str, self.context)
            return True
        except Exception:
            print(traceback.format_exc())
            return False

    # ---------------------------------------------------------------- контекст
    def context_set(self, context, duration=None):
        if duration is None:
            duration = self.contextDefaultDuration
        self.context_clear()
        self.context = context
        self.contextTimerLastDuration = duration
        self.contextTimer = Timer(duration, self._context_clear_timer)
        self.contextTimer.daemon = True
        self.contextTimer.start()

    def _context_clear_timer(self):
        print("Контекст сброшен по таймауту")
        context = self.context
        self.contextTimer = None
        self.context_clear()
        gen = getattr(context, "dialog_gen", None)
        if gen is not None:
            # даём диалогу ответить на тишину — в потоке команд (ядро не потокобезопасно)
            self.run_in_main(lambda: self._dialog_step(gen, lambda: gen.throw(DialogTimeout())))

    def context_clear(self):
        self.context = None
        if self.contextTimer is not None:
            self.contextTimer.cancel()
            self.contextTimer = None

    # ---------------------------------------------------------------- вывод при запуске
    def display_init_info(self):
        cprint(f"Ирина, ядро v{version}:", "blue", end=" ")
        print("онлайн-команды включены" if self.isOnline else "только офлайн-команды")
        self.format_print_key_list("Озвучка", self.ttss.keys())
        self.format_print_key_list("Проигрывание WAV", self.playwavs.keys())
        self.format_print_key_list("Имена помощника", self.voiceAssNames)
        cprint("Команды плагинов " + "#" * 55, "blue")
        for plugin, cmds in self.plugin_commands.items():
            self.format_print_key_list(plugin, cmds)
        cprint("#" * 72, "blue")

    @staticmethod
    def format_print_key_list(key: str, value):
        print(colored(key + ": ", "blue") + ", ".join(value))
