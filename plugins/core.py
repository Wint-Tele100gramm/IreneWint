# Основные настройки Ирины (options/core.json)
# author: Vladislav Janvarev; упрощено: без MPC-HC, веб-API и ИИ-плагинов

import logging
import os

from vacore import VACore


def start(core: VACore):
    return {
        "name": "Core plugin",
        "version": "6.0",
        "description": "Основные настройки Ирины. Команды — в других плагинах.",

        "options_label": {
            "isOnline": "Выполнять команды плагинов, которым нужен интернет (Яндекс Музыка)",
            "useTTSCache": "Кэшировать озвучку (повторяющиеся ответы звучат сразу)",
            "ttsEngineId": "Движок озвучки: vosk (или console — только текст)",
            "playWavEngineId": "Проигрывание WAV: aplay (Linux) или sounddevice (Windows и др.)",
            "voiceAssNames": "Имена помощника через | (все формы)",
            "logPolicy": "all|cmd|none — выводить в консоль все распознанные фразы | только команды | ничего",
            "replyNoCommandFound": "Ответ, если команда не понята",
            "replyNoCommandFoundInContext": "Ответ, если не понят ответ на вопрос",
            "replyOnlineRequired": "Ответ, если команде нужен интернет, а он выключен",
            "contextDefaultDuration": "Сколько секунд ждать ответа на вопрос Ирины (без имени)",
            "contextExitCommand": "Слово досрочного выхода из диалога (пусто — нет)",
            "tempDir": "Папка временных файлов",
            "fuzzyThreshold": "Порог уверенности нечётких сравнителей команд",
            "voiceAssNameRunCmd": "Имя -> команда-префикс: {\"альбина\": \"скажи\"}",
            "log_console": "Писать логи в консоль",
            "log_console_level": "Уровень логов консоли",
            "log_file": "Писать логи в файл",
            "log_file_level": "Уровень логов файла",
            "log_file_name": "Имя лог-файла",
            "normalization_engine": "Подготовка текста к озвучке: prepare (числа, латиница, символы) или none",
        },

        "default_options": {
            "isOnline": True,
            "useTTSCache": True,
            "ttsEngineId": "vosk",
            "playWavEngineId": "sounddevice" if os.name == "nt" else "aplay",
            "voiceAssNames": "ирина|ирины|ирину",
            "logPolicy": "cmd",
            "replyNoCommandFound": "Извини, я не поняла",
            "replyNoCommandFoundInContext": "Не поняла...",
            "replyOnlineRequired": "Для этой команды необходим онлайн",
            "contextDefaultDuration": 10,
            "contextExitCommand": "",
            "tempDir": "temp",
            "fuzzyThreshold": 0.5,
            "voiceAssNameRunCmd": {},
            "log_console": True,
            "log_console_level": "WARNING",
            "log_file": False,
            "log_file_level": "INFO",
            "log_file_name": "log.txt",
            "normalization_engine": "prepare",
        },
    }


def start_with_options(core: VACore, manifest: dict):
    options = manifest["options"]

    core.isOnline = options["isOnline"]
    core.voiceAssNames = options["voiceAssNames"].split("|")
    core.voiceAssNameRunCmd = options["voiceAssNameRunCmd"]
    core.ttsEngineId = options["ttsEngineId"]
    core.playWavEngineId = options["playWavEngineId"]
    core.logPolicy = options["logPolicy"]
    core.contextDefaultDuration = options["contextDefaultDuration"]
    core.contextExitCommand = options["contextExitCommand"]
    core.useTTSCache = options["useTTSCache"]
    core.normalization_engine = options["normalization_engine"]
    if core.normalization_engine == "default":      # старые настройки
        core.normalization_engine = "prepare"

    core.tmpdir = options["tempDir"]
    os.makedirs(core.tmpdir, exist_ok=True)
    core.tts_cache_dir = "tts_cache"
    os.makedirs(os.path.join(core.tts_cache_dir, core.ttsEngineId), exist_ok=True)

    _setup_logging(options)
    return manifest


def _setup_logging(options):
    if not (options["log_console"] or options["log_file"]):
        return
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    levels = [options["log_console_level"] if options["log_console"] else "CRITICAL",
              options["log_file_level"] if options["log_file"] else "CRITICAL"]
    root.setLevel(min(logging.getLevelName(level) for level in levels))
    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    if options["log_console"]:
        h = logging.StreamHandler()
        h.setLevel(options["log_console_level"])
        h.setFormatter(fmt)
        root.addHandler(h)
    if options["log_file"]:
        h = logging.FileHandler(options["log_file_name"], encoding="utf-8")
        h.setLevel(options["log_file_level"])
        h.setFormatter(fmt)
        root.addHandler(h)
