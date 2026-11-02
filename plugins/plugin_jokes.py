# Анекдоты: обычные и пошлые (для взрослых) — по запросу.
#
# Примеры: «ирина расскажи анекдот», «ирина ещё анекдот», «ирина пошути»,
#          «ирина расскажи пошлый анекдот», «ирина взрослый анекдот», «ирина анекдот для взрослых».
#
# Анекдоты лежат в data/jokes.json; скачивает их scripts/get_jokes.py (на приставке, один раз; его вызывает
# install.sh). Источник по умолчанию — датасет igorktech/anekdots (ODC-BY, тексты с anekdot.ru), лучшие по
# оценкам читателей. Ирина не повторяется, пока не расскажет все анекдоты из набора.
#
# Пошлые анекдоты можно выключить совсем ("adult_enabled": false) или только в некоторых комнатах
# ("adult_blocked_rooms": ["Детская"]).

import json
import logging
import os
import random
import re
import threading
import time

from vacore import VACore

modname = os.path.basename(__file__)[:-3]
logger = logging.getLogger(__name__)
journal = logging.getLogger("rooms")      # журнал сервера комнат — виден в веб-интерфейсе

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "data", "jokes.json")
SEEN_FILE = os.path.join(ROOT, "data", "jokes_seen.json")
ADULT_WORDS = re.compile(r"пошл|взросл|неприлич|похаб|грязн|\b18\b|восемнадцать")

_lock = threading.Lock()
_jokes = {"mtime": None, "normal": [], "adult": []}
_seen = None
_last = {"kind": "normal", "time": 0.0}


def start(core: VACore):
    return {
        "name": "Анекдоты",
        "version": "1.0",
        "require_online": False,
        "description": "Обычные и пошлые анекдоты по запросу. Наполнение: scripts/get_jokes.py",
        "default_options": {
            "adult_enabled": True,          # пошлые анекдоты по прямой просьбе
            "adult_blocked_rooms": [],      # комнаты, где пошлых не рассказывать (например ["Детская"])
        },
        "commands": {
            "анекдот|расскажи анекдот|давай анекдот|скажи анекдот|хочу анекдот|ещё анекдот|еще анекдот|"
            "давай ещё анекдот|давай еще анекдот|ещё один анекдот|еще один анекдот|расскажи ещё анекдот|"
            "расскажи еще анекдот|пошути|рассмеши|рассмеши меня|расскажи шутку|расскажи что-нибудь смешное|"
            "расскажи что нибудь смешное": tell,
            "пошлый анекдот|расскажи пошлый анекдот|давай пошлый анекдот|ещё пошлый анекдот|еще пошлый анекдот|"
            "взрослый анекдот|расскажи взрослый анекдот|давай взрослый анекдот|ещё взрослый анекдот|"
            "еще взрослый анекдот|анекдот для взрослых|расскажи анекдот для взрослых|неприличный анекдот|"
            "расскажи неприличный анекдот|похабный анекдот|расскажи похабный анекдот|пошлую шутку|"
            "расскажи пошлую шутку|пошути пошло": (tell, "adult"),
        },
    }


def start_with_options(core: VACore, manifest: dict):
    pass


# ---------------------------------------------------------------- данные
def _load():
    """Перечитать data/jokes.json, если он появился или обновился (после get_jokes.py — без перезапуска)."""
    try:
        mtime = os.path.getmtime(DATA_FILE)
    except OSError:
        return False
    if mtime != _jokes["mtime"]:
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                data = json.load(f)
            _jokes.update(mtime=mtime, normal=list(data.get("normal") or []), adult=list(data.get("adult") or []))
            logger.info(f"Анекдоты: обычных {len(_jokes['normal'])}, пошлых {len(_jokes['adult'])}")
        except Exception as e:
            logger.warning(f"Анекдоты: не прочитать {DATA_FILE}: {e}")
            return False
    return True


def _seen_state():
    global _seen
    if _seen is None:
        try:
            with open(SEEN_FILE, encoding="utf-8") as f:
                _seen = json.load(f)
        except Exception:
            _seen = {}
    return _seen


def _save_seen():
    try:
        tmp = SEEN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_seen, f)
        os.replace(tmp, SEEN_FILE)
    except Exception as e:
        logger.warning(f"Анекдоты: не сохранить {SEEN_FILE}: {e}")


def pick(kind: str):
    """Случайный ещё не рассказанный анекдот; когда рассказаны все — круг заново."""
    with _lock:
        if not _load():
            return None
        pool = _jokes[kind]
        if not pool:
            return None
        state = _seen_state()
        told = set(i for i in state.get(kind, []) if 0 <= i < len(pool))
        if len(told) >= len(pool):
            told = set()
        i = random.choice([n for n in range(len(pool)) if n not in told])
        told.add(i)
        state[kind] = sorted(told)
        _save_seen()
        return pool[i]


# ---------------------------------------------------------------- команда
def _room_name(core) -> str:
    room = getattr(core, "current_room", None)
    return (getattr(room, "name", "") or getattr(room, "id", "") or "") if room is not None else ""


def tell(core: VACore, phrase: str, kind: str = "normal"):
    o = core.plugin_options(modname)
    full = ((core.input_cmd_full or "") + " " + (phrase or "")).lower().replace("ё", "е")
    if kind == "normal":
        if ADULT_WORDS.search(full):
            kind = "adult"
        elif re.search(r"\bеще\b", full) and _last["kind"] == "adult" and time.time() - _last["time"] < 600:
            kind = "adult"                       # «ещё анекдот» после пошлого — ещё пошлый
    if kind == "adult":
        if not o.get("adult_enabled", True):
            core.say("Пошлые анекдоты выключены в настройках. Могу рассказать обычный")
            return
        room = _room_name(core).lower()
        blocked = [r.lower() for r in (o.get("adult_blocked_rooms") or [])]
        if room and room in blocked:
            core.say("В этой комнате пошлые анекдоты не рассказываю")
            return
    joke = pick(kind)
    if joke is None:
        journal.warning(f"Анекдоты: нет {'пошлых ' if kind == 'adult' else ''}анекдотов в {DATA_FILE} "
                        f"(файл {'есть' if os.path.isfile(DATA_FILE) else 'не найден'}; обычных "
                        f"{len(_jokes['normal'])}, пошлых {len(_jokes['adult'])})")
        if not os.path.isfile(DATA_FILE):
            core.say("Анекдотов пока нет. Их нужно один раз скачать на сервер, как — написано в инструкции")
        else:
            core.say("Пошлых анекдотов в наборе нет" if kind == "adult" else "Анекдотов в наборе нет")
        return
    _last.update(kind=kind, time=time.time())
    logger.info(f"Анекдот ({'пошлый' if kind == 'adult' else 'обычный'}): {joke[:80]}…")
    core.say(joke)
