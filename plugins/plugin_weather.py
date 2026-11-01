# Погода: Open-Meteo (бесплатно, без ключа и регистрации; нужен интернет).
#
# Примеры: «ирина какая погода» (сейчас), «ирина погода на завтра», «ирина погода на послезавтра»,
#          «ирина прогноз на неделю», «ирина погода в субботу», «ирина погода на выходные»,
#          «ирина погода сегодня», «ирина сколько градусов на улице».
#
# Дом задаётся в options/plugin_weather.json (или в веб-интерфейсе, раздел «Погода»):
#   "latitude"/"longitude" — координаты (например 55.7558 и 37.6173),
#   или "city" — название города: координаты найдутся сами (геокодер Open-Meteo) и запишутся в настройки.

import datetime
import logging
import os
import re
import threading
import time

import requests

from vacore import VACore
from utils.num_to_text_ru import num2text

modname = os.path.basename(__file__)[:-3]
logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

_DEGREE = (("градус", "градуса", "градусов"), "m")
_MPS = (("метр в секунду", "метра в секунду", "метров в секунду"), "m")
_PCT = (("процент", "процента", "процентов"), "m")
_MM = (("миллиметр", "миллиметра", "миллиметров"), "m")

# Коды погоды ВМО (WMO 4677), которые отдаёт Open-Meteo
WMO = {
    0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "туман с изморозью",
    51: "слабая морось", 53: "морось", 55: "сильная морось", 56: "слабая ледяная морось", 57: "ледяная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь", 66: "ледяной дождь", 67: "сильный ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
    80: "небольшой ливень", 81: "ливень", 82: "сильный ливень", 85: "небольшой снегопад", 86: "сильный снегопад",
    95: "гроза", 96: "гроза с градом", 99: "сильная гроза с градом",
}

# Дни недели: (как в вопросе — основа слова, «в какой день», род для «следующий/следующую/следующее»)
WEEKDAYS = [
    (r"понедельник", "в понедельник", "m"),
    (r"вторник", "во вторник", "m"),
    (r"сред[уаеы]", "в среду", "f"),
    (r"четверг", "в четверг", "m"),
    (r"пятниц", "в пятницу", "f"),
    (r"суббот", "в субботу", "f"),
    (r"воскресень", "в воскресенье", "n"),
]
NEXT = {"m": "в следующий", "f": "в следующую", "n": "в следующее"}

_cache = {"key": None, "time": 0.0, "data": None}
_lock = threading.Lock()


def start(core: VACore):
    return {
        "name": "Погода (Open-Meteo)",
        "version": "1.0",
        "require_online": True,
        "description": "Погода сейчас, на сегодня, завтра, послезавтра, любой день недели, выходные и всю неделю.\n"
                       "Координаты дома — в настройках (или название города).",
        "default_options": {
            "latitude": 0.0,           # широта дома; 0 и 0 — не задано
            "longitude": 0.0,          # долгота дома
            "city": "",                # или город: координаты найдутся сами
            "cache_minutes": 15,       # не спрашивать сервер чаще
            "say_feels_like": True,    # «ощущается как минус пять», если отличается на 3° и больше
            "say_humidity": False,     # влажность в погоде «сейчас»
            "say_wind": True,          # ветер
            "timeout": 8,              # ждать сервер, с
        },
        "commands": {
            "погода|погоду|какая погода|какая сейчас погода|какая будет погода|что с погодой|что по погоде|"
            "что там с погодой|скажи погоду|расскажи погоду|прогноз|прогноз погоды|какой прогноз|"
            "сколько градусов на улице|сколько градусов за окном|какая температура на улице|"
            "температура на улице|холодно на улице|тепло на улице|что на улице|"
            "нужен ли зонт|брать ли зонт|будет ли дождь|будет дождь|будет ли снег": weather,
        },
    }


def start_with_options(core: VACore, manifest: dict):
    pass


# ---------------------------------------------------------------- слова
def temp_text(value) -> str:
    t = int(round(float(value)))
    sign = "плюс " if t > 0 else ("минус " if t < 0 else "")
    return sign + num2text(abs(t), _DEGREE)


def temp_short(value) -> str:
    """Без слова «градусов»: «плюс восемь», «ноль»."""
    t = int(round(float(value)))
    if t == 0:
        return "ноль"
    return ("плюс " if t > 0 else "минус ") + num2text(abs(t))


def day_label(index: int, date: datetime.date) -> str:
    if index == 0:
        return "сегодня"
    if index == 1:
        return "завтра"
    if index == 2:
        return "послезавтра"
    _, label, gender = WEEKDAYS[date.weekday()]
    if index >= 7:
        return NEXT[gender] + " " + label.split(" ", 1)[1]
    return label


def cap(text: str) -> str:
    return text[:1].upper() + text[1:]


# ---------------------------------------------------------------- что спросили
def parse_request(text: str, today: datetime.date):
    """-> ("now", None) | ("day", индекс дня) | ("days", [индексы]) по фразе пользователя."""
    t = " " + (text or "").lower().replace("ё", "е") + " "
    if re.search(r"\bнедел|семь дней|7 дней", t):     # \b: «понедельник» — не неделя
        return "days", list(range(7))
    if re.search(r"выходн", t):
        wd = today.weekday()
        if wd == 6:                                   # воскресенье: «на выходные» — сегодня
            return "days", [0]
        sat = (5 - wd) % 7
        return "days", [sat, sat + 1] if wd != 5 else [0, 1]
    if "послезавтра" in t:
        return "day", 2
    if "завтра" in t:
        return "day", 1
    nxt = re.search(r"следующ", t) is not None
    for i, (stem, _, _) in enumerate(WEEKDAYS):
        if re.search(r"\b" + stem, t):
            delta = (i - today.weekday()) % 7
            if nxt and delta == 0:
                delta = 7
            return "day", delta
    if re.search(r"сегодня|днем|днём|вечером|на день", t):
        return "today", 0
    return "now", None


# ---------------------------------------------------------------- сервер
def _coords(core: VACore, o: dict):
    """Координаты дома; если задан город — найти его один раз и сохранить."""
    city = (o.get("city") or "").strip()
    if city and city != o.get("geocoded_city"):
        r = requests.get(GEOCODE_URL, params={"name": city, "language": "ru", "count": 1},
                         timeout=float(o.get("timeout", 8)))
        r.raise_for_status()
        found = (r.json().get("results") or [None])[0]
        if not found:
            raise LookupError(city)
        o = core.plugin_options(modname)
        o.update(latitude=round(found["latitude"], 4), longitude=round(found["longitude"], 4), geocoded_city=city)
        core.save_plugin_options(modname, o)
        logger.info(f"Погода: «{city}» — {found.get('name')}, {found.get('admin1', '')}, "
                    f"{o['latitude']}, {o['longitude']}")
    lat, lon = float(o.get("latitude") or 0), float(o.get("longitude") or 0)
    if lat == 0 and lon == 0:
        return None
    return lat, lon


def fetch(lat: float, lon: float, timeout: float = 8, cache_minutes: float = 15) -> dict:
    key = (round(lat, 4), round(lon, 4))
    with _lock:
        if _cache["key"] == key and time.time() - _cache["time"] < cache_minutes * 60:
            return _cache["data"]
    params = {
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 8, "wind_speed_unit": "ms",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,"
                   "wind_gusts_10m,precipitation",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                 "precipitation_probability_max,wind_speed_10m_max,wind_gusts_10m_max",
    }
    try:
        r = requests.get(FORECAST_URL, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception:
        with _lock:
            # нет связи — старый прогноз (до 6 часов) лучше, чем ничего
            if _cache["key"] == key and time.time() - _cache["time"] < 6 * 3600:
                logger.warning("Погода: сервер недоступен, отвечаю по прогнозу из кэша")
                return _cache["data"]
        raise
    with _lock:
        _cache.update(key=key, time=time.time(), data=data)
    return data


# ---------------------------------------------------------------- фразы
def _get(arr, i):
    try:
        return arr[i]
    except (IndexError, TypeError):
        return None


def say_now(data: dict, o: dict) -> str:
    c = data.get("current") or {}
    parts = []
    desc = WMO.get(c.get("weather_code"))
    head = "Сейчас " + (desc + ", " if desc else "") + temp_text(c.get("temperature_2m", 0))
    parts.append(head)
    feels = c.get("apparent_temperature")
    if o.get("say_feels_like", True) and feels is not None and abs(feels - c.get("temperature_2m", feels)) >= 3:
        parts.append("ощущается как " + temp_short(feels))
    if o.get("say_wind", True) and c.get("wind_speed_10m") is not None:
        w = int(round(c["wind_speed_10m"]))
        gust = int(round(c.get("wind_gusts_10m") or 0))
        if w == 0:
            parts.append("безветренно")
        else:
            s = "ветер " + num2text(w, _MPS)
            if gust >= max(w + 5, 10):
                s += ", порывы " + num2text(gust, _MPS)
            parts.append(s)
    if o.get("say_humidity") and c.get("relative_humidity_2m") is not None:
        parts.append("влажность " + num2text(int(round(c["relative_humidity_2m"])), _PCT))
    text = ", ".join(parts) + "."
    d = data.get("daily") or {}
    tmax, tmin = _get(d.get("temperature_2m_max"), 0), _get(d.get("temperature_2m_min"), 0)
    if tmax is not None and tmin is not None:
        text += f" Сегодня днём {temp_short(tmax)}, ночью {temp_short(tmin)}."
    return text


def say_day(data: dict, i: int, o: dict, brief: bool = False) -> str:
    d = data.get("daily") or {}
    dates = d.get("time") or []
    if i >= len(dates):
        return ""
    date = datetime.date.fromisoformat(dates[i])
    label = day_label(i, date)
    if i in (1, 2) and not brief:
        label += ", " + WEEKDAYS[date.weekday()][1] + ","   # «завтра, в среду, снег»
    parts = [cap(label)]
    desc = WMO.get(_get(d.get("weather_code"), i))
    if desc:
        parts[0] += " " + desc
    else:
        parts[0] = parts[0].rstrip(",")
    tmax, tmin = _get(d.get("temperature_2m_max"), i), _get(d.get("temperature_2m_min"), i)
    if tmax is not None:
        parts.append(("днём " + temp_short(tmax)) if brief else ("днём " + temp_text(tmax)))
    if tmin is not None:
        parts.append("ночью " + temp_short(tmin))
    prob = _get(d.get("precipitation_probability_max"), i)
    psum = _get(d.get("precipitation_sum"), i) or 0
    if prob is not None and prob >= (40 if brief else 20) and psum >= 0.3:
        parts.append("вероятность осадков " + num2text(int(round(prob)), _PCT))
    wind = _get(d.get("wind_speed_10m_max"), i)
    if o.get("say_wind", True) and wind is not None:
        w = int(round(wind))
        if not brief and w > 0:
            parts.append("ветер " + num2text(w, _MPS))
        elif brief and w >= 8:
            parts.append("сильный ветер")
    return ", ".join(parts) + "."


def answer(data: dict, kind: str, arg, o: dict) -> str:
    if kind == "now":
        return say_now(data, o)
    if kind == "today":
        return say_day(data, 0, o) + " " + say_now(data, o).split(".")[0] + "."
    if kind == "day":
        return say_day(data, arg, o) or "Так далеко прогноза нет"
    if kind == "days":
        if len(arg) >= 5:
            return "Прогноз на неделю. " + " ".join(say_day(data, i, o, brief=True) for i in arg)
        return " ".join(say_day(data, i, o) for i in arg)
    return say_now(data, o)


def weather(core: VACore, phrase: str):
    o = core.plugin_options(modname)
    full = " ".join(x for x in ((core.input_cmd_full or ""), phrase) if x)
    try:
        coords = _coords(core, o)
    except LookupError as e:
        core.say(f"Не нашла город {e}. Проверьте название в настройках погоды")
        return
    except Exception as e:
        logger.warning(f"Погода: геокодер недоступен: {e}")
        core.say("Не удалось найти город: нет связи с сервером погоды")
        return
    if coords is None:
        core.say("Не заданы координаты дома. Укажите их или город в настройках погоды")
        return
    try:
        data = fetch(*coords, timeout=float(o.get("timeout", 8)), cache_minutes=float(o.get("cache_minutes", 15)))
    except Exception as e:
        logger.warning(f"Погода: {e}")
        core.say("Не удалось узнать погоду: нет связи с сервером погоды")
        return
    try:
        today = datetime.date.fromisoformat((data.get("daily") or {}).get("time")[0])
    except Exception:
        today = datetime.date.today()
    kind, arg = parse_request(full, today)
    text = answer(data, kind, arg, o)
    logger.info(f"Погода ({kind} {arg}): {text}")
    core.say(text)
