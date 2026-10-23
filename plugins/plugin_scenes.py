# Псевдонимы и сценарии: свои фразы для любых команд, без программирования (идея из форка AlexeyBond).
#
# Настройки — options/plugin_scenes.json:
#   "aliases":  своя фраза -> команда. Остаток фразы передаётся дальше:
#       "зажги|сделай светло": "включи свет"      («ирина зажги на кухне» = «ирина включи свет на кухне»)
#   "scenes":   фраза -> список шагов, выполняются по очереди:
#       "спокойной ночи": ["выключи весь свет", "выключи музыку", "скажи: Спокойной ночи!"]
#     Шаг — любая команда Ирины (как после имени), или:
#       "скажи: текст"  — произнести текст,
#       "пауза: 2"      — подождать 2 секунды.
#   Ответы шагов можно приглушить: "quiet_steps": true — тогда сценарий отвечает только своими «скажи:».

import os
import time

from vacore import VACore

modname = os.path.basename(__file__)[:-3]

_depth = 0          # защита от зацикливания: псевдоним, ссылающийся сам на себя
_MAX_DEPTH = 5


def start(core: VACore):
    return {
        "name": "Псевдонимы и сценарии",
        "version": "1.0",
        "require_online": False,

        "description": "Свои фразы для команд и сценарии из нескольких команд. Настройки: options/plugin_scenes.json",

        "default_options": {
            "aliases": {
                "сделай светло": "включи свет",
            },
            "scenes": {
                "спокойной ночи": ["выключи весь свет", "выключи музыку", "скажи: Спокойной ночи!"],
            },
            "quiet_steps": True,
        },

        "commands": {},
    }


def start_with_options(core: VACore, manifest: dict):
    options = manifest["options"]
    commands = manifest["commands"]
    for phrase, target in (options.get("aliases") or {}).items():
        commands[phrase.lower()] = (run_alias, str(target))
    for phrase, steps in (options.get("scenes") or {}).items():
        commands[phrase.lower()] = (run_scene, list(steps) if isinstance(steps, list) else [str(steps)])
    return manifest


def _run_command(core: VACore, text: str):
    global _depth
    if _depth >= _MAX_DEPTH:
        core.say("Сценарий зациклился, проверьте настройки")
        return
    _depth += 1
    try:
        core.execute_next(text.lower().strip(), None)
    finally:
        _depth -= 1


def run_alias(core: VACore, phrase: str, target: str):
    _run_command(core, f"{target} {phrase}".strip())


def run_scene(core: VACore, phrase: str, steps: list):
    quiet = core.plugin_options(modname).get("quiet_steps", True)
    for step in steps:
        step = str(step).strip()
        low = step.lower()
        if low.startswith("скажи:"):
            core.say(step.split(":", 1)[1].strip())
        elif low.startswith("пауза:"):
            try:
                time.sleep(min(float(step.split(":", 1)[1].strip().replace(",", ".")), 30))
            except ValueError:
                pass
        elif step:
            _run_quiet(core, step) if quiet else _run_command(core, step)


def _run_quiet(core: VACore, step: str):
    # подтверждения шагов («Выключаю свет») не произносим, ошибки — тоже в лог, чтобы сценарий звучал коротко
    said = []
    orig = core.play_voice_assistant_speech
    core.play_voice_assistant_speech = said.append
    try:
        _run_command(core, step)
    finally:
        core.play_voice_assistant_speech = orig
    if said:
        print(f"Сценарий, шаг «{step}»: {' / '.join(said)}")
