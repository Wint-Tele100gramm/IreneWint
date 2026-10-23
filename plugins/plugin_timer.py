# Таймер
# author: Vladislav Janvarev; переработано для сервера комнат: разбор «на час двадцать минут»,
# правильные падежи (utils/ru_speech из форка AlexeyBond), отмена и «сколько осталось».
# Сигнал и фраза звучат в той комнате, где таймер поставили (это делает runva_rooms.py).

import os
import time

from vacore import VACore
from utils.ru_speech.pronounce_numbers_ru import pronounce_integer
from utils.ru_speech.time_units_ru import HOUR, MINUTE, SECOND
from utils.ru_speech.word_forms import WordCaseRU

modname = os.path.basename(__file__)[:-3]

_NUM = {
    "ноль": 0, "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
    "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
    "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100,
    "полтора": 1.5, "полторы": 1.5,
}
_SPECIAL = {"полчаса": 1800, "полминуты": 30, "час": 3600, "минуту": 60, "минутку": 60, "секунду": 1}
_UNITS = (("час", 3600), ("мин", 60), ("сек", 1))

_timers = []   # [(индекс таймера ядра, время окончания, номер таймера)]
_counter = 0


def start(core: VACore):
    return {
        "name": "Таймер",
        "version": "2.0",
        "require_online": False,

        "description": "Голосовые команды: таймер, таймер на пять минут, поставь таймер на час двадцать минут,\n"
                       "засеки полчаса, сколько осталось, отмени таймер",

        "options_label": {
            "wavRepeatTimes": "число повторений звукового сигнала",
            "wavPath": "путь к звуковому файлу",
            "defaultMinutes": "на сколько минут ставить «просто таймер»",
        },

        "default_options": {
            "wavRepeatTimes": 2,
            "wavPath": 'media/timer.wav',
            "defaultMinutes": 5,
        },

        "commands": {
            "поставь таймер|поставь тайгер|таймер|тайгер|засеки": set_timer,
            "отмени таймер|сбрось таймер|убери таймер|отмени таймеры|выключи таймер": cancel_timers,
            "сколько осталось|таймер сколько осталось|сколько осталось таймера": time_left,
        }
    }


def start_with_options(core: VACore, manifest: dict):
    pass


def parse_duration(phrase: str):
    """«на час двадцать минут» -> 4800, «десять секунд» -> 10, «пять» -> 300 (без единиц — минуты).
    None — если чисел и единиц во фразе нет."""
    total, number, found = 0.0, None, False
    for w in phrase.lower().replace("ё", "е").split():
        if w in ("на", "и", "ещё", "еще"):
            continue
        if w.isdigit():
            number = (number or 0) + int(w)
            continue
        if w in _NUM:
            number = (number or 0) + _NUM[w]
            continue
        unit = next((sec for stem, sec in _UNITS if w.startswith(stem)), None)
        if w in _SPECIAL and number is None:
            total += _SPECIAL[w]
            found = True
        elif unit is not None:
            total += (number if number is not None else 1) * unit
            number, found = None, True
    if number is not None:
        total += number * 60     # «таймер пять» — минуты
        found = True
    return int(total) if found and total > 0 else None


def duration_text(seconds: int, case=WordCaseRU.ACCUSATIVE) -> str:
    """4800 -> «один час двадцать минут» (в нужном падеже)."""
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    parts = []
    for value, unit in ((h, HOUR), (m, MINUTE), (s, SECOND)):
        if value:
            parts.extend(pronounce_integer(value, unit, case))
    if h == 1 and parts[:1] == ["один"]:
        parts = parts[1:]      # «на час», а не «на один час»
    return " ".join(parts) or "ноль секунд"


def set_timer(core: VACore, phrase: str):
    phrase = phrase.strip()
    if phrase:
        seconds = parse_duration(phrase)
        if seconds is None:
            core.say("На сколько поставить таймер?")
            core.context_set(set_timer)
            return
    else:
        seconds = int(core.plugin_options(modname)["defaultMinutes"]) * 60

    global _counter
    _counter += 1
    text = duration_text(seconds)
    index = core.set_timer(seconds, (_after_timer, (text, _counter)))
    if index < 0:
        core.say("Больше таймеров поставить не могу")
        return
    _timers.append((index, time.time() + seconds, _counter))
    core.say("Ставлю таймер на " + text)


def _after_timer(core: VACore, arg):
    text, number = arg
    _timers[:] = [t for t in _timers if t[2] != number]
    options = core.plugin_options(modname)
    for _ in range(int(options["wavRepeatTimes"])):
        core.play_wav(options["wavPath"])
        time.sleep(0.2)
    core.say(f"Время вышло. Таймер на {text}")


def _active():
    return sorted(_timers, key=lambda t: t[1])


def cancel_timers(core: VACore, phrase: str):
    active = _active()
    if not active:
        core.say("Таймеров нет")
        return
    for index, _, _ in active:
        core.clear_timer(index)
    _timers.clear()
    core.say("Таймер отменён" if len(active) == 1 else "Все таймеры отменены")


def time_left(core: VACore, phrase: str):
    active = _active()
    if not active:
        core.say("Таймеров нет")
        return
    left = int(active[0][1] - time.time()) + 1
    if left > 60:
        left = (left + 30) // 60 * 60    # до минуты — точнее не нужно
    core.say("До конца таймера " + duration_text(left, WordCaseRU.NOMINATIVE))
