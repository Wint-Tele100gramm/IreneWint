# Управление умным домом через Home Assistant (в т.ч. устройствами ZHA/Zigbee)
# Работает через REST API Home Assistant и долгоживущий токен доступа.
#
# Примеры: "ирина включи свет на кухне", "ирина выключи весь свет",
#          "ирина яркость в спальне тридцать процентов", "ирина закрой шторы",
#          "ирина какая температура в детской", "ирина активируй сцену кино"
#
# Устройства находятся автоматически по их имени и зоне (area) в Home Assistant.
# Если имя в HA неудобно произносить, добавьте синоним в options/plugin_homeassistant.json -> "aliases".

import os
import re
import time
import json
import logging
import threading
import difflib

import requests

from vacore import VACore, DialogTimeout
from utils.ru_words_to_num import extract_number
from utils.cmd_route import delegate_longer_command

modname = os.path.basename(__file__)[:-3]
logger = logging.getLogger(__name__)
journal = logging.getLogger("rooms")      # журнал сервера комнат — виден в веб-интерфейсе

# Слова-подсказки домена: если они есть во фразе, ищем только среди этих доменов
DOMAIN_HINTS = {
    "свет": ["light"], "света": ["light"], "освещение": ["light"], "лампу": ["light"], "лампа": ["light"],
    "лампочку": ["light"], "люстру": ["light"], "люстра": ["light"], "подсветку": ["light"], "ленту": ["light"],
    "розетку": ["switch"], "розетка": ["switch"], "выключатель": ["switch", "light"],
    "шторы": ["cover"], "штору": ["cover"], "жалюзи": ["cover"], "ворота": ["cover"], "рольставни": ["cover"],
    "кондиционер": ["climate"], "отопление": ["climate"], "термостат": ["climate"], "обогреватель": ["climate", "switch"],
    "вентилятор": ["fan"], "вытяжку": ["fan", "switch"], "очиститель": ["fan"],
    "пылесос": ["vacuum"], "увлажнитель": ["humidifier"],
    "телевизор": ["media_player", "switch"], "колонку": ["media_player"],
    "замок": ["lock"], "сцену": ["scene"], "сцена": ["scene"], "сценарий": ["script"], "автоматизацию": ["automation"],
    "температура": ["sensor", "climate"], "температуру": ["sensor", "climate"], "влажность": ["sensor"],
    "датчик": ["sensor", "binary_sensor"],
}
STOP_WORDS = {"в", "во", "на", "у", "и", "с", "со", "для", "по", "мне", "пожалуйста", "весь", "всю", "все", "всё",
              "свой", "мой", "какая", "какой", "сколько", "что", "там", "сейчас", "процентов", "процента", "процент",
              "градусов", "градуса", "градус", "это", "этот", "эту"}
CONTROL_DOMAINS = ["light", "switch", "fan", "cover", "climate", "media_player", "input_boolean", "scene",
                   "script", "lock", "vacuum", "humidifier", "automation", "siren", "valve"]
QUERY_DOMAINS = ["sensor", "binary_sensor", "climate", "weather"]

# общие слова, которые не являются частью имени устройства ("свет на кухне" -> ищем по зоне "кухня")
GENERIC_WORDS = {"свет", "света", "освещение", "датчик", "устройство"}

_options = {}
_entities = []          # [{"id","name","area","domain","stems","area_stems"}]
_entities_time = 0.0
_lock = threading.Lock()


def start(core: VACore):
    manifest = {
        "name": "Home Assistant",
        "version": "1.0",
        "require_online": False,  # HA работает в локальной сети
        "description": "Управление умным домом через Home Assistant (ZHA, Zigbee и др.).\n"
                       "Нужны url и token (Профиль HA -> Безопасность -> Долгосрочные токены доступа).\n"
                       "Команды: включи|выключи|переключи <устройство>, выключи весь свет, "
                       "яркость <где> <число>, открой|закрой шторы, температура <где>, сцена <имя>",
        "options_label": {
            "url": "Адрес Home Assistant, например http://192.168.1.10:8123",
            "token": "Долгосрочный токен доступа Home Assistant",
            "aliases": "Синонимы: {\"произносимое имя\": \"entity_id\" или [\"entity_id\", ...]}",
            "match_threshold": "Минимальная похожесть имени устройства (0..1)",
            "refresh_minutes": "Как часто обновлять список устройств из HA",
            "say_confirm": "Подтверждать выполнение голосом",
        },
        "default_options": {
            "url": "http://homeassistant.local:8123",
            "token": "",
            "aliases": {},
            "match_threshold": 0.45,
            "refresh_minutes": 10,
            "say_confirm": True,
        },
        "commands": {
            "выключи весь свет|выключи свет везде|погаси весь свет": (all_lights, "turn_off"),
            "включи весь свет|включи свет везде": (all_lights, "turn_on"),
            "включи|зажги|запусти|активируй": (do_action, "on"),
            "выключи|погаси|отключи|останови": (do_action, "off"),
            "переключи": (do_action, "toggle"),
            "открой|подними": (do_action, "open"),
            "закрой|опусти": (do_action, "close"),
            "яркость|сделай яркость": set_brightness,
            "температура|какая температура|сколько градусов|влажность|какая влажность|состояние": query_state,
            "сцена|включи сцену": (do_action, "scene"),
            "обнови устройства|обнови умный дом": refresh_cmd,
        },
    }
    return manifest


def start_with_options(core: VACore, manifest: dict):
    global _options
    options = manifest["options"]
    _options = options
    if not options.get("token"):
        print("plugin_homeassistant: не задан token в options/plugin_homeassistant.json — команды умного дома не будут работать")
        return
    # загрузим список устройств в фоне, чтобы не тормозить запуск
    threading.Thread(target=_load_entities, args=(core,), daemon=True).start()


# ---------------- HA API ----------------

def _opts(core):
    # во время start_with_options core.plugin_options() ещё не знает о плагине, поэтому храним свои
    return _options or core.plugin_options(modname)


def _api(core, method: str, path: str, data=None, timeout=6):
    o = _opts(core)
    url = o["url"].rstrip("/") + "/api/" + path.lstrip("/")
    headers = {"Authorization": "Bearer " + o["token"], "Content-Type": "application/json"}
    r = requests.request(method, url, headers=headers, data=json.dumps(data) if data is not None else None,
                         timeout=timeout)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    return r.json() if "json" in ctype else r.text


def _call_service(core, domain: str, service: str, entity_ids, extra=None):
    data = {"entity_id": entity_ids}
    if extra:
        data.update(extra)
    logger.info(f"HA call {domain}.{service} {data}")
    return _api(core, "POST", f"services/{domain}/{service}", data)


_ENDINGS = sorted(["ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ая", "яя", "ой", "ей", "ий", "ый",
                   "ую", "юю", "ое", "ее", "ом", "ем", "ым", "им", "ых", "их", "ах", "ях", "ам", "ям", "ов", "ев",
                   "а", "я", "у", "ю", "е", "ы", "и", "о", "ь"], key=len, reverse=True)


def _stem(word: str) -> str:
    # простой стемминг для падежей: "кухне"/"кухня" -> "кухн", "мокрой"/"мокрая" -> "мокр"
    word = word.lower().replace("ё", "е")
    for end in _ENDINGS:
        if word.endswith(end) and len(word) - len(end) >= 3:
            word = word[:-len(end)]
            break
    return word[:5]


def _stems(text: str):
    words = re.findall(r"[a-zа-яё0-9]+", (text or "").lower())
    return {_stem(w) for w in words if w not in STOP_WORDS}


def _load_entities(core):
    global _entities, _entities_time
    domains = CONTROL_DOMAINS + QUERY_DOMAINS
    template = (
        "{% set ns = namespace(r=[]) %}"
        "{% for s in states %}{% if s.domain in " + json.dumps(domains) + " %}"
        "{% set ns.r = ns.r + [[s.entity_id, s.name, area_name(s.entity_id) or '']] %}"
        "{% endif %}{% endfor %}{{ ns.r | tojson }}"
    )
    try:
        try:
            raw = _api(core, "POST", "template", {"template": template}, timeout=15)
            rows = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            logger.warning(f"HA template API недоступен ({e}), берём список без зон")
            rows = [[s["entity_id"], s.get("attributes", {}).get("friendly_name", s["entity_id"]), ""]
                    for s in _api(core, "GET", "states", timeout=15)
                    if s["entity_id"].split(".")[0] in domains]
        result = []
        for eid, name, area in rows:
            result.append({"id": eid, "name": name or eid, "area": area or "", "domain": eid.split(".")[0],
                           "stems": _stems(name), "area_stems": _stems(area)})
        with _lock:
            _entities = result
            _entities_time = time.time()
        print(f"plugin_homeassistant: загружено устройств: {len(result)}")
    except Exception as e:
        logger.error(f"plugin_homeassistant: не удалось получить устройства из HA: {e}")


def _ensure_entities(core):
    o = _opts(core)
    if not _entities or time.time() - _entities_time > o.get("refresh_minutes", 10) * 60:
        _load_entities(core)


# ---------------- поиск устройства по фразе ----------------

def _find_entities(core, phrase: str, allowed_domains=None, alternatives=None):
    """Возвращает (список entity, читаемое имя) для фразы вроде 'свет на кухне'.
    alternatives — если передан список, в него попадут равноценные устройства из разных комнат
    (тогда стоит переспросить, какое имелось в виду)."""
    phrase = phrase.lower().replace("ё", "е").strip()
    o = _opts(core)

    # 1) синонимы из настроек
    for alias, target in o.get("aliases", {}).items():
        a = alias.lower().replace("ё", "е")
        if phrase == a or _stems(phrase) == _stems(a):
            ids = target if isinstance(target, list) else [target]
            return [{"id": i, "domain": i.split(".")[0], "name": alias} for i in ids], alias

    _ensure_entities(core)
    words = re.findall(r"[a-zа-яё0-9]+", phrase)
    hint_domains = set()
    for w in words:
        hint_domains.update(DOMAIN_HINTS.get(w, []))
    if allowed_domains:
        hint_domains = (hint_domains & set(allowed_domains)) or set(allowed_domains)
    hint_words = {_stem(w) for w in words if w in GENERIC_WORDS}
    query = _stems(phrase) - hint_words

    with _lock:
        all_entities = list(_entities)
    candidates = [e for e in all_entities if (e["domain"] in hint_domains if hint_domains
                                              else e["domain"] in CONTROL_DOMAINS)]
    if "light" in hint_domains and not allowed_domains:
        # реле и выключатели света в ZHA часто числятся как switch — тоже свет, но лампы важнее
        candidates += [e for e in all_entities if e["domain"] in ("switch", "input_boolean")
                       and e not in candidates]

    # комната, откуда пришла команда (сервер комнат runva_rooms.py); её имя = зона в Home Assistant
    room = getattr(core, "current_room", None)
    room_stems = _stems(getattr(room, "name", "") or "")

    # 2) названа комната ("свет на кухне") — ищем только в ней, иначе похожее имя из другой комнаты
    #    может перехватить команду, и выключится не тот свет
    named = {e["area"] for e in all_entities if e["area_stems"] and e["area_stems"] <= query}
    if named:
        longest = max(len(_stems(a)) for a in named)          # «сухая зона» важнее просто «зоны»
        named = {a for a in named if len(_stems(a)) == longest}
        named_stems = set().union(*(_stems(a) for a in named))
        candidates = [e for e in candidates if e["area"] in named]
        lamps = [e for e in candidates if e["domain"] == "light"]
        if "light" in hint_domains and lamps:
            candidates = lamps                                   # есть лампы — выключатели не трогаем
        rest = query - named_stems
        if not rest and not hint_domains:
            # «выключи кухню» — это про свет, а не про все устройства зоны
            candidates = lamps or [e for e in candidates if e["domain"] in ("switch", "input_boolean")]
        if not rest:
            # только комната: "свет на кухне" -> все подходящие устройства этой зоны
            if not candidates:
                return [], phrase
            return candidates, ", ".join(sorted(named))
        query = rest

    if not query:
        if not room_stems:
            # только "свет" без уточнения и без комнаты — не угадываем
            return [], phrase
        query = set(room_stems)  # "включи свет" в спальне -> свет в зоне "Спальня"

    scored = []
    for e in candidates:
        name_stems = e["stems"] - hint_words
        # зона считается, только если названа целиком: «мокрая зона» не должна найти «сухую зону»
        area_full = bool(e["area_stems"]) and e["area_stems"] <= query
        target = name_stems | (e["area_stems"] if area_full else set())
        overlap = len(query & target)
        score = overlap / max(len(query), 1)
        if overlap and area_full and not (query & name_stems):
            score -= 0.01  # попали только в зону — чуть ниже точного совпадения по имени
        # похожесть имени на случай неточного распознавания
        if name_stems:
            ratio = difflib.SequenceMatcher(None, " ".join(sorted(query)), " ".join(sorted(name_stems))).ratio()
            score = max(score, ratio * 0.9)
        if room_stems and score > 0 and e["area_stems"] & room_stems:
            score += 0.05  # при равенстве выбираем устройство в той же комнате
        if e["domain"] in ("switch", "input_boolean") and "light" in hint_domains:
            score -= 0.02  # выключатель — только если ламп с таким именем нет
        scored.append((score, e))

    if not scored:
        return [], phrase
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score = scored[0][0]
    if best_score < o.get("match_threshold", 0.45):
        return [], phrase

    best = [e for s, e in scored if s >= best_score - 0.001]
    # "свет на кухне" -> все лампы зоны "Кухня", если имя прямо не названо
    # одинаково подходят устройства из разных комнат ("включи лампу" без комнаты) — пусть спросят, где
    if alternatives is not None and len(best) > 1 and len({e["area"] for e in best if e["area"]}) > 1:
        alternatives.extend(best)
        return best[:1], best[0]["name"]
    if len(best) > 1 and hint_domains:
        names = ", ".join(sorted({e["area"] or e["name"] for e in best}))
        return best, names
    return best[:1], best[0]["name"]


# ---------------- команды ----------------

def _say_ok(core, text):
    if _opts(core).get("say_confirm", True):
        core.say(text)


def _check_ready(core) -> bool:
    if not _opts(core).get("token"):
        core.say("Home Assistant не настроен. Укажите токен в настройках плагина")
        return False
    return True


def do_action(core: VACore, phrase: str, action: str):
    phrase = phrase.strip()
    # более точная команда другого плагина ("включи блютуз", "включи альбом ...") важнее общего "включи"
    if delegate_longer_command(core, __name__):
        return
    # музыка перехватывается плагином Яндекс Музыки: "включи музыку", "выключи музыку"...
    music_hook = getattr(core, "music_try_handle", None)
    if music_hook is not None and action in ("on", "off") and music_hook(core, action, phrase):
        return
    if not phrase:
        return _ask_what(core, action)
    if not _check_ready(core):
        return

    allowed = None
    if action in ("open", "close"):
        allowed = ["cover", "valve", "lock"]
    elif action == "scene":
        allowed = ["scene", "script"]

    alternatives = []
    try:
        entities, name = _find_entities(core, phrase, allowed, alternatives)
    except Exception as e:
        logger.exception(e)
        core.say("Не удалось связаться с Home Assistant")
        return
    if not entities:
        core.say(f"Не нашла устройство {phrase}")
        return
    if alternatives:
        return _ask_room(core, phrase, action, alternatives)
    return _execute(core, entities, name, phrase, action)


_ACTION_WORDS = {"on": "включить", "off": "выключить", "toggle": "переключить", "open": "открыть",
                 "close": "закрыть", "scene": "включить"}


def _ask_what(core, action):
    """«Ирина, включи» — переспрашиваем, что именно (диалог: ответ без имени)."""
    try:
        answer = yield f"Что {_ACTION_WORDS.get(action, 'сделать')}?"
    except DialogTimeout:
        return
    answer = (answer or "").strip()
    if answer and not set(answer.split()) & {"ничего", "отмена", "отмени", "стоп", "никакой"}:
        res = do_action(core, answer, action)
        if res is not None:
            yield from res


def _ask_room(core, phrase, action, alternatives):
    """Одинаковые устройства в разных комнатах — уточняем: «Торшер где: в гостиной или в спальне?»."""
    areas = []
    for e in alternatives:
        if e["area"] and e["area"] not in areas:
            areas.append(e["area"])
    question = f"{phrase.capitalize()} где: " + ", ".join(areas[:-1]) + " или " + areas[-1] + "?"
    try:
        answer = yield (question, 10)
    except DialogTimeout:
        return
    answer_stems = _stems(answer or "")
    chosen = [e for e in alternatives if e["area_stems"] & answer_stems]
    if not chosen:
        if any(w in (answer or "").split() for w in ("везде", "все", "всё", "оба", "обе")):
            chosen = alternatives
        else:
            core.say("Не поняла, отменяю")
            return
    where = ", ".join(sorted({e["area"] for e in chosen if e["area"]})).lower()
    _execute(core, chosen, phrase, f"{phrase}, {where}" if where else phrase, action)


def _state(core, entity_id):
    try:
        return _api(core, "GET", f"states/{entity_id}", timeout=4).get("state")
    except Exception:
        return None


def _describe(entities):
    return ", ".join(f"{e['id']} «{e.get('name', '')}»" + (f" ({e['area']})" if e.get("area") else "")
                     for e in entities)


def _execute(core, entities, name, phrase, action):
    journal.info(f"Home Assistant: «{phrase}» -> {_describe(entities)}")
    # Zigbee-устройство без связи HA принимает команду молча — проверим заранее
    states = {e["id"]: _state(core, e["id"]) for e in entities if e["domain"] not in ("scene", "script")}
    alive = [e for e in entities if states.get(e["id"]) not in ("unavailable",)]
    dead = [e for e in entities if e not in alive]
    if dead:
        journal.warning(f"Home Assistant: недоступны (нет связи с устройством): {_describe(dead)}")
    if not alive:
        core.say(f"{phrase.capitalize()}: устройство недоступно, нет связи. Проверьте его в Home Assistant")
        return
    try:
        by_domain = {}
        for e in alive:
            by_domain.setdefault(e["domain"], []).append(e["id"])
        changed = []
        for domain, ids in by_domain.items():
            service = _service_for(domain, action)
            if service is None:
                core.say(f"{name} так не умеет")
                return
            svc_domain = "homeassistant" if service.startswith("homeassistant.") else domain
            result = _call_service(core, svc_domain, service.split(".")[-1], ids)
            if isinstance(result, list):
                changed += [st.get("entity_id") for st in result if isinstance(st, dict)]
    except Exception as e:
        logger.exception(e)
        journal.error(f"Home Assistant: ошибка вызова для «{phrase}»: {e}")
        core.say("Home Assistant вернул ошибку")
        return

    if not changed:
        journal.info("Home Assistant: состояние не изменилось (уже было так или устройство не ответило)")
    verbs = {"on": "Включаю", "off": "Выключаю", "toggle": "Переключаю", "open": "Открываю",
             "close": "Закрываю", "scene": "Активирую сцену"}
    what = name if action == "scene" else phrase
    if action != "scene" and not _names_match(phrase, alive):
        what = name           # нашлось не совсем то, что сказали, — называем, что именно переключили
    tail = f", кроме: {_describe(dead)}" if dead else ""
    _say_ok(core, f"{verbs[action]} {what}{' — часть устройств недоступна' if tail else ''}")


def _names_match(phrase, entities):
    """Сказанное совпадает с выбранным (по имени или зоне)? Иначе в ответе называем выбранное устройство."""
    said = _stems(phrase) - {_stem(w) for w in GENERIC_WORDS}
    if not said:
        return True
    return all(said & (e.get("stems", set()) | e.get("area_stems", set())) for e in entities)


def _service_for(domain: str, action: str):
    if domain in ("scene",):
        return "turn_on" if action in ("on", "scene", "toggle") else None
    if domain == "script":
        return {"on": "turn_on", "scene": "turn_on", "off": "turn_off", "toggle": "toggle"}.get(action)
    if domain == "cover":
        return {"on": "open_cover", "open": "open_cover", "off": "close_cover", "close": "close_cover",
                "toggle": "toggle"}.get(action)
    if domain == "valve":
        return {"on": "open_valve", "open": "open_valve", "off": "close_valve", "close": "close_valve"}.get(action)
    if domain == "lock":
        return {"open": "unlock", "off": "lock", "close": "lock"}.get(action)
    if domain == "vacuum":
        return {"on": "start", "off": "return_to_base", "toggle": "start_pause"}.get(action)
    if domain in ("sensor", "binary_sensor", "weather"):
        return None
    return {"on": "turn_on", "off": "turn_off", "toggle": "toggle", "open": "turn_on",
            "close": "turn_off"}.get(action)


def all_lights(core: VACore, phrase: str, service: str):
    if not _check_ready(core):
        return
    try:
        _call_service(core, "light", service, "all")
        _say_ok(core, "Готово")
    except Exception as e:
        logger.exception(e)
        core.say("Не удалось связаться с Home Assistant")


def set_brightness(core: VACore, phrase: str):
    if not _check_ready(core):
        return
    value = extract_number(phrase)
    if value is None:
        core.say("Скажите яркость в процентах, например яркость на кухне пятьдесят")
        return
    value = max(1, min(100, value))
    where = re.sub(r"\b(\d+|на|в|процент\w*)\b", " ", phrase)
    where = " ".join(w for w in where.split() if extract_number(w) is None)
    entities, name = _find_entities(core, "свет " + where, ["light"])
    if not entities:
        core.say(f"Не нашла свет {where}")
        return
    try:
        _call_service(core, "light", "turn_on", [e["id"] for e in entities], {"brightness_pct": value})
        _say_ok(core, f"Яркость {value} процентов")
    except Exception as e:
        logger.exception(e)
        core.say("Home Assistant вернул ошибку")


def query_state(core: VACore, phrase: str):
    if not _check_ready(core):
        return
    cmd = (core.input_cmd_full or "").lower() if hasattr(core, "input_cmd_full") else ""
    want_humidity = "влажн" in cmd
    kind = "влажность" if want_humidity else ("температура" if "темпер" in cmd or "градус" in cmd else "")
    entities, name = _find_entities(core, f"{kind} {phrase}".strip(), QUERY_DOMAINS)
    if not entities:
        core.say(f"Не нашла датчик {phrase}")
        return
    try:
        st = _api(core, "GET", f"states/{entities[0]['id']}")
    except Exception as e:
        logger.exception(e)
        core.say("Не удалось связаться с Home Assistant")
        return
    attrs = st.get("attributes", {})
    state = st.get("state")
    if entities[0]["domain"] == "climate":
        state = attrs.get("current_temperature", state)
        unit = "градусов"
    else:
        unit = attrs.get("unit_of_measurement", "")
    if state in ("on", "off"):
        state = "включено" if state == "on" else "выключено"
    elif state in ("unavailable", "unknown"):
        state = "нет данных"
    else:
        try:
            num = round(float(state))
            state = str(num)
            forms = {"°C": ("градус", "градуса", "градусов"), "градусов": ("градус", "градуса", "градусов"),
                     "%": ("процент", "процента", "процентов")}.get(unit)
            if forms:
                unit = _plural(num, forms)
        except (TypeError, ValueError):
            pass
    where = phrase.strip() or name
    core.say(f"{kind} {where} {state} {unit}".strip() if kind else f"{where}: {state} {unit}".strip())


def _plural(n: int, forms) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def refresh_cmd(core: VACore, phrase: str):
    if not _check_ready(core):
        return
    _load_entities(core)
    core.say(f"Нашла {len(_entities)} устройств")
