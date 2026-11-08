# Собеседник на Google Gemini: на всё, что не является командой Русланы, отвечает нейросеть.
# «руслана расскажи сказку про ёжика», «руслана почему небо голубое», «руслана а что было дальше?».
# Явно: «руслана спроси нейросеть …», «руслана спроси джемини …».
#
# Ключ: https://aistudio.google.com/apikey. Тариф ("tier"):
#   free — бесплатный уровень: ограничение запросов в минуту и в день; при «лимит исчерпан» Руслана повторит
#          вопрос облегчённой модели (fallback_model). Google может использовать запросы для обучения.
#   paid — к проекту ключа в AI Studio привязан платёжный аккаунт (Cloud Billing): лимиты выше, доступны
#          более сильные модели (model_paid), можно включить «размышления»; запросы не идут в обучение.
#          Подписка Google AI Pro / Gemini Advanced (для приложения Gemini) к API отношения не имеет.
#          Расход токенов считается помесячно (data/gemini_usage.json), есть лимит monthly_token_limit.
# Из России Google API не отвечает («User location is not supported») — нужен прокси за границей
# ("proxy": "socks5h://адрес:порт" или "http://адрес:порт") или свой ретранслятор ("base_url").
#
# Ответ говорится той комнате, где спросили; пока нейросеть думает, Руслана выполняет другие команды.
# Последние реплики помнятся несколько минут — можно переспросить или попросить продолжить.

import collections
import json
import logging
import os
import re
import threading
import time

import requests

from vacore import VACore

modname = os.path.basename(__file__)[:-3]
logger = logging.getLogger(__name__)
journal = logging.getLogger("rooms")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USAGE_FILE = os.path.join(ROOT, "data", "gemini_usage.json")

SYSTEM_PROMPT = (
    "Ты — Руслана, голосовой помощник в умном доме. Твой ответ будет озвучен синтезатором речи, поэтому:\n"
    "- отвечай по-русски, простым разговорным языком;\n"
    "- никакой разметки: без звёздочек, решёток, списков, таблиц, ссылок, эмодзи и кода;\n"
    "- иностранные слова и имена пиши русскими буквами (не «iPhone», а «айфон»);\n"
    "- обычно отвечай коротко, в два-четыре предложения; сказку, историю или рассказ — минуты на две-три чтения, "
    "если не попросили иначе;\n"
    "- тебя могут слушать дети: никакой грубости и взрослых тем;\n"
    "- если вопрос про управление домом, музыкой, таймером — скажи, что такой команды не знаешь, "
    "и предложи сказать её иначе; не выдумывай, что что-то включила или выключила;\n"
    "- если не знаешь ответа или он мог устареть (новости, цены, курсы), так и скажи."
)

_history = {}            # комната -> deque[(роль, текст)]
_history_time = {}
_lock = threading.Lock()
_thinking_cfg_ok = {}    # модель -> понимает ли thinkingConfig (иначе не повторять лишний запрос)


def start(core: VACore):
    return {
        "name": "Нейросеть Gemini",
        "version": "2.1",
        "require_online": True,
        "description": "Отвечает на всё, что не является командой Русланы: сказки, вопросы, разговор (Google Gemini).",
        "default_options": {
            "enabled": True,
            "api_key": "",                      # https://aistudio.google.com/apikey
            "tier": "free",                     # free | paid (к проекту ключа привязан платёжный аккаунт)
            "model": "gemini-flash-latest",     # модель на бесплатном тарифе
            "fallback_model": "gemini-flash-lite-latest",   # бесплатный тариф, лимит исчерпан — спросить её; "" — нет
            "model_paid": "gemini-flash-latest",            # модель на платном (можно сильнее, например Pro)
            "thinking_paid": False,             # «размышления» на платном: умнее, но дольше и дороже
            "monthly_token_limit": 0,           # платный: не больше стольких токенов в месяц (0 — без лимита)
            "proxy": "",                        # socks5h://host:port или http://host:port — из России нужен
            "base_url": "https://generativelanguage.googleapis.com",
            "system_prompt": SYSTEM_PROMPT,
            "temperature": 0.8,
            "max_output_tokens": 1500,
            "max_chars": 2500,                  # длиннее не озвучивать (обрезается по предложению)
            "history_turns": 6,                 # сколько последних реплик помнить
            "history_minutes": 5,               # ...и сколько минут
            "timeout": 40,
            "thinking_phrase": "Сейчас подумаю",   # если ответа нет дольше thinking_after_sec; "" — молчать
            "thinking_after_sec": 3,
        },
        "commands": {
            "спроси нейросеть|спроси у нейросети|спроси джемини|спроси у джемини|спроси гугл|нейросеть|джемини": ask_cmd,
            "забудь разговор|начни разговор заново|забудь что я говорил": forget_cmd,
            "расход нейросети|сколько потрачено на нейросеть|сколько токенов потрачено": usage_cmd,
        },
    }


def start_with_options(core: VACore, manifest: dict):
    _migrate(core, manifest["options"])
    core.unknown_command_handler = unknown_command


def _migrate(core, options):
    """Настройки из plugin_llm (была версия с Алисой) или из старого plugin_gemini.json.old — сюда, один раз."""
    opt_dir = os.path.join(ROOT, "options")
    for name in ("plugin_llm.json", "plugin_gemini.json.old"):
        old = os.path.join(opt_dir, name)
        if not os.path.isfile(old):
            continue
        try:
            with open(old, encoding="utf-8") as f:
                o = json.load(f)
            if not options.get("api_key"):
                for k in ("api_key", "model", "proxy", "base_url", "enabled", "temperature", "thinking_phrase",
                          "history_minutes", "system_prompt"):
                    if o.get(k) not in (None, ""):
                        options[k] = o[k]
                core.save_plugin_options(modname, options)
            os.replace(old, old + ".migrated")
            logger.info(f"Настройки {name} перенесены в plugin_gemini")
        except Exception as e:
            logger.warning(f"Не удалось перенести настройки {name}: {e}")


def _ready(o):
    return bool(o.get("api_key"))


# ---------------------------------------------------------------- команды
def _room_key(core):
    room = getattr(core, "current_room", None)
    return getattr(room, "id", None) or "local"


def unknown_command(core: VACore, text: str) -> bool:
    o = core.plugin_options(modname)
    text = (text or "").strip()
    if not o.get("enabled", True) or not _ready(o) or not core.isOnline:
        return False
    if len(text) < 3:
        return False                          # одно имя «Руслана» — пусть ядро ответит как обычно
    ask(core, text)
    return True


def ask_cmd(core: VACore, phrase: str):
    o = core.plugin_options(modname)
    if not _ready(o):
        core.say("Не задан ключ Gemini. Укажите его в настройках нейросети")
        return
    if not phrase.strip():
        core.say("Что спросить у нейросети?")
        return
    ask(core, phrase.strip())


def forget_cmd(core: VACore, phrase: str):
    with _lock:
        _history.pop(_room_key(core), None)
    core.say("Хорошо, начнём сначала")


# ---------------------------------------------------------------- запрос в фоне
def ask(core: VACore, question: str):
    """Спросить в фоне и ответить в той же комнате (поток команд не ждёт сеть)."""
    room = getattr(core, "current_room", None)
    key = _room_key(core)
    server = getattr(core, "rooms_server", None)
    o = dict(core.plugin_options(modname))
    title = "Gemini"
    done = threading.Event()
    order = threading.Lock()                  # «Сейчас подумаю» не должно прозвучать после ответа

    def say_in_room(text):
        if server is not None:
            server.run_in_commands(room, lambda: core.say(text))
        else:
            core.say(text)

    def thinking():
        phrase = (o.get("thinking_phrase") or "").strip()
        if phrase and not done.wait(float(o.get("thinking_after_sec", 3))):
            with order:
                if not done.is_set():
                    say_in_room(phrase)

    def work():
        started = time.time()
        try:
            answer = query(o, key, question)
        except LLMError as e:
            journal.warning(f"{title}: {e.log}")
            answer = e.spoken
        except Exception as e:
            logger.exception(e)
            journal.warning(f"{title}: {e}")
            answer = "Не получилось спросить нейросеть"
        finally:
            with order:
                done.set()
        journal.info(f"{title} ({time.time() - started:.1f} с): «{question}» -> «{answer[:120]}»")
        say_in_room(answer)

    journal.info(f"{title}: спрашиваю «{question}»")
    threading.Thread(target=thinking, name="gemini-wait", daemon=True).start()
    threading.Thread(target=work, name="gemini", daemon=True).start()


class LLMError(Exception):
    def __init__(self, spoken, log, code=None):
        super().__init__(log)
        self.spoken, self.log, self.code = spoken, log, code


def _history_of(o, key):
    with _lock:
        if time.time() - _history_time.get(key, 0) > float(o.get("history_minutes", 5)) * 60:
            _history.pop(key, None)
        return list(_history.get(key, []))


def _remember(o, key, question, answer):
    with _lock:
        h = _history.setdefault(key, collections.deque())
        h.extend([("user", question), ("model", answer)])
        while len(h) > max(0, int(o.get("history_turns", 6))):
            h.popleft()
        _history_time[key] = time.time()


def _post(o, body, model):
    url = f"{o.get('base_url', '').rstrip('/')}/v1beta/models/{model}:generateContent"
    proxy = (o.get("proxy") or "").strip()
    try:
        return requests.post(url, json=body, headers={"x-goog-api-key": o["api_key"]},
                             proxies={"http": proxy, "https": proxy} if proxy else None,
                             timeout=float(o.get("timeout", 40)))
    except requests.exceptions.InvalidSchema as e:
        raise LLMError("Для прокси socks нужен пакет PySocks", f"{e} — установите: .venv/bin/pip install pysocks")
    except requests.exceptions.ProxyError as e:
        raise LLMError("Прокси для нейросети не отвечает", f"прокси {proxy}: {e}")
    except requests.exceptions.Timeout:
        raise LLMError("Нейросеть слишком долго думает, попробуйте ещё раз", "таймаут")
    except requests.exceptions.ConnectionError as e:
        raise LLMError("Нет связи с нейросетью", str(e))


def _paid(o) -> bool:
    return str(o.get("tier", "free")).lower() == "paid"


def query(o: dict, key, question: str) -> str:
    paid = _paid(o)
    limit = int(o.get("monthly_token_limit") or 0)
    if paid and limit and usage()["tokens"] >= limit:
        raise LLMError("Месячный лимит нейросети исчерпан. Его можно поднять в настройках",
                       f"месячный лимит {limit} токенов исчерпан")
    model = (o.get("model_paid") if paid else o.get("model")) or "gemini-flash-latest"
    contents = [{"role": r, "parts": [{"text": t}]} for r, t in _history_of(o, key)]
    contents.append({"role": "user", "parts": [{"text": question}]})
    body = {"systemInstruction": {"parts": [{"text": o.get("system_prompt") or SYSTEM_PROMPT}]},
            "contents": contents}
    think = paid and bool(o.get("thinking_paid"))
    try:
        data = _generate(o, body, model, think)
    except LLMError as e:
        fallback = (o.get("fallback_model") or "").strip()
        if paid or e.code != 429 or not fallback or fallback == model:
            raise
        # бесплатный тариф: у облегчённой модели свой, больший дневной лимит
        journal.info(f"Gemini: лимит {model} исчерпан, спрашиваю {fallback}")
        data = _generate(o, body, fallback, False)
    meta = data.get("usageMetadata") or {}
    _count_usage(int(meta.get("totalTokenCount") or 0))
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = " ".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        reason = cand.get("finishReason") or (data.get("promptFeedback") or {}).get("blockReason") or "пусто"
        if reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "OTHER"):
            raise LLMError("На это я отвечать не буду", f"ответ заблокирован: {reason}")
        raise LLMError("Нейросеть ничего не ответила", f"пустой ответ: {reason}")
    text = for_speech(text, int(o.get("max_chars", 2500)))
    _remember(o, key, question, text)
    return text


def _generate(o, body, model, think):
    gen = {"temperature": float(o.get("temperature", 0.8)), "maxOutputTokens": int(o.get("max_output_tokens", 1500))}
    if think:
        # «размышления» съедают часть maxOutputTokens — добавим им места
        gen["maxOutputTokens"] += 4096
    elif _thinking_cfg_ok.get(model, True):
        # «размышления» добавляют секунды ожидания — голосовому помощнику обычно не нужны
        gen["thinkingConfig"] = {"thinkingBudget": 0}
    body = dict(body, generationConfig=gen)
    r = _post(o, body, model)
    if r.status_code == 400 and "thinking" in r.text.lower() and "thinkingConfig" in gen:
        gen.pop("thinkingConfig")           # модель не умеет отключать размышления — спросим без этого
        _thinking_cfg_ok[model] = False
        r = _post(o, body, model)
    if r.status_code != 200:
        _raise_http(r)
    return r.json()


# ---------------------------------------------------------------- учёт расхода (по месяцам)
def usage() -> dict:
    month = time.strftime("%Y-%m")
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            u = json.load(f)
    except Exception:
        u = {}
    if u.get("month") != month:
        u = {"month": month, "tokens": 0, "requests": 0}
    return u


def _count_usage(tokens: int):
    with _lock:
        u = usage()
        u["tokens"] += tokens
        u["requests"] += 1
        try:
            os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
            with open(USAGE_FILE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(u, f)
            os.replace(USAGE_FILE + ".tmp", USAGE_FILE)
        except OSError as e:
            logger.warning(f"Gemini: не сохранить расход: {e}")


def usage_cmd(core: VACore, phrase: str):
    from utils.num_to_text_ru import num2text
    o = core.plugin_options(modname)
    u = usage()
    req = num2text(u["requests"], (("запрос", "запроса", "запросов"), "m"))
    k = round(u["tokens"] / 1000)
    tok = (num2text(k, (("тысяча токенов", "тысячи токенов", "тысяч токенов"), "f")) if k
           else num2text(u["tokens"], (("токен", "токена", "токенов"), "m")))
    text = f"В этом месяце {req} к нейросети, {tok}"
    limit = int(o.get("monthly_token_limit") or 0)
    if _paid(o) and limit:
        text += f", это {min(100, round(u['tokens'] * 100 / limit))} процентов месячного лимита"
    core.say(text)


def _raise_http(r):
    try:
        err = r.json().get("error", {})
        msg, status = err.get("message", r.text[:200]), err.get("status", "")
    except Exception:
        msg, status = r.text[:200], ""
    log = f"HTTP {r.status_code} {status}: {msg}"
    if "location is not supported" in msg.lower():
        raise LLMError("Нейросеть недоступна из этой страны. Нужен прокси в настройках", log)
    if r.status_code in (401, 403) or "api key" in msg.lower():
        raise LLMError("Ключ Gemini не подходит. Проверьте его в настройках", log)
    if r.status_code == 429:
        raise LLMError("Лимит запросов к нейросети исчерпан, попробуйте позже", log, 429)
    if r.status_code == 404:
        raise LLMError("Такой модели Gemini нет. Проверьте название модели в настройках", log)
    raise LLMError("Нейросеть ответила ошибкой", log)


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")


def for_speech(text: str, max_chars: int = 2500) -> str:
    """Убрать разметку, которую модель всё же вставила, и обрезать слишком длинное по предложению."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)            # [текст](ссылка)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"^\s*(#+|[-*•]|\d+[.)])\s+", "", text, flags=re.M)  # заголовки и пункты списков
    text = text.replace("**", "").replace("__", "").replace("`", "").replace("*", "")
    text = _EMOJI.sub("", text)
    text = re.sub(r"\s*\n+\s*", "\n", text).strip()
    # строка без знака в конце (бывший пункт списка) — точка, чтобы синтез сделал паузу
    text = " ".join(ln if re.search(r"[.!?:;,…]$", ln) else ln + "." for ln in text.split("\n") if ln)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r" ([,.!?;:])", r"\1", text)
    if len(text) > max_chars:
        cut = max(text.rfind(c, 0, max_chars) for c in ".!?")
        text = text[:cut + 1] if cut > max_chars // 2 else text[:max_chars].rsplit(" ", 1)[0] + "."
    return text
