# Дата и время
# author: Vladislav Janvarev; произношение времени — из форка AlexeyBond (utils/ru_speech)

from datetime import datetime
import os

from vacore import VACore
from utils.ru_speech.pronounce_time_ru import pronounce_time_ru

modname = os.path.basename(__file__)[:-3]  # calculating modname

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
DAYS = ['первое', 'второе', 'третье', 'четвёртое', 'пятое', 'шестое', 'седьмое', 'восьмое', 'девятое',
        'десятое', 'одиннадцатое', 'двенадцатое', 'тринадцатое', 'четырнадцатое', 'пятнадцатое',
        'шестнадцатое', 'семнадцатое', 'восемнадцатое', 'девятнадцатое', 'двадцатое', 'двадцать первое',
        'двадцать второе', 'двадцать третье', 'двадцать четвёртое', 'двадцать пятое', 'двадцать шестое',
        'двадцать седьмое', 'двадцать восьмое', 'двадцать девятое', 'тридцатое', 'тридцать первое']
MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
          'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']


def start(core: VACore):
    manifest = {
        "name": "Дата и время",
        "version": "2.0",
        "require_online": False,

        "description": "Голосовые команды: время, который час, сколько времени, дата, какое сегодня число",

        "options_label": {
            "style": '"natural" — «без четверти семь вечера», «половина девятого утра»; '
                     '"digital" — «восемнадцать сорок пять»',
            "sayDayPart": 'добавлять «утра», «дня», «вечера», «ночи» (для "natural")',
            "sayHourUnits": 'произносить «часов» / «часа»',
            "sayNoon": 'говорить «полдень» и «полночь»',
            "prefix": 'начало ответа, например «Сейчас» (пусто — без него)',
        },

        "default_options": {
            "style": "natural",
            "sayDayPart": True,
            "sayHourUnits": False,
            "sayNoon": True,
            "prefix": "Сейчас",
        },

        "commands": {
            "дата|какое сегодня число|какое число|какой сегодня день": play_date,
            "время|который час|сколько времени|скажи время": play_time,
        }
    }
    return manifest


def start_with_options(core: VACore, manifest: dict):
    pass


def play_date(core: VACore, phrase: str):
    now = datetime.now()
    core.say(f"Сегодня {WEEKDAYS[now.weekday()]}, {DAYS[now.day - 1]} {MONTHS[now.month - 1]}")


def time_text(t, options) -> str:
    digital = options.get("style") == "digital"
    words = list(pronounce_time_ru(
        t,
        digital_format=digital,
        digital_format_separator=None,
        digital_skip_minutes_when_zero=True,
        pronounce_hour_units=bool(options.get("sayHourUnits")),
        day_time_enabled=bool(options.get("sayDayPart", True)),
        midnight_enabled=bool(options.get("sayNoon", True)),
        midday_enabled=bool(options.get("sayNoon", True)),
        half_short=False,           # «половина девятого»: «пол девятого» синтез читает хуже
        exactly_before=True,
    ))
    if options.get("prefix"):
        words.insert(0, options["prefix"])
    return " ".join(words)


def play_time(core: VACore, phrase: str):
    core.say(time_text(datetime.now().time(), core.plugin_options(modname)))
