# Собеседник на нейросети: на всё, что не является командой Ирины, отвечает Алиса (Yandex AI Studio) или Gemini.
# «ирина расскажи сказку про ёжика», «ирина почему небо голубое», «ирина а что было дальше?».
# Явно: «ирина спроси алису …», «ирина спроси джемини …», «ирина спроси нейросеть …».
#
# provider = "yandex" — модель Alice AI LLM в Yandex AI Studio: работает из России, платно (по токенам, есть
#   стартовый грант). Нужны API-ключ сервисного аккаунта (роль ai.languageModels.user) и ID каталога.
# provider = "gemini" — Google Gemini: бесплатный уровень, но из России нужен прокси или свой адрес API.
# Настройки — options/plugin_llm.json или веб-интерфейс, раздел «Нейросеть».
#
# Ответ говорится той комнате, где спросили; пока нейросеть думает, Ирина выполняет другие команды.
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

SYSTEM_PROMPT = (
    "Ты — Ирина, голосовой помощник в умном доме. Твой ответ будет озвучен синтезатором речи, поэтому:\n"
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
        "name": "Нейросеть (Алиса / Gemini)",
        "version": "2.0",
        "require_online": True,
        "description": "Отвечает на всё, что не является командой Ирины: сказки, вопросы, разговор.",
        "default_options": {
            "enabled": True,
            "provider": "yandex",               # yandex (Алиса, Yandex AI Studio) | gemini
            # --- Yandex AI Studio
            "yandex_api_key": "",
            "yandex_folder_id": "",
            "yandex_model": "aliceai-llm",      # или aliceai-llm-flash — быстрее и дешевле
            "yandex_url": "https://ai.api.cloud.yandex.net/v1/responses",
            # --- Google Gemini
            "api_key": "",                      # https://aistudio.google.com/apikey
            "model": "gemini-flash-latest",
            "proxy": "",                        # socks5h://host:port или http://host:port — из России нужен
            "base_url": "https://generativelanguage.googleapis.com",
            # --- общее
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
            "спроси нейросеть|спроси у нейросети|нейросеть": ask_cmd,
            "спроси алису|спроси у алисы|алиса": (ask_cmd, "yandex"),
            "спроси джемини|спроси у джемини|спроси гугл|джемини": (ask_cmd, "gemini"),
            "забудь разговор|начни разговор заново|забудь что я говорил": forget_cmd,
        },
    }


def start_with_options(core: VACore, manifest: dict):
    _migrate(core, manifest["options"])
    core.unknown_command_handler = unknown_command


def _migrate(core, options):
    """Настройки прежнего plugin_gemini (ключ, модель, прокси) — сюда, один раз."""
    old = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "options", "plugin_gemini.json")
    if not os.path.isfile(old):
        return
    try:
        with open(old, encoding="utf-8") as f:
            o = json.load(f)
        for k in ("api_key", "model", "proxy", "base_url", "enabled", "temperature", "thinking_phrase",
                  "history_minutes", "system_prompt"):
            if o.get(k) not in (None, ""):
                options[k] = o[k]
        if o.get("api_key") and not options.get("yandex_api_key"):
            options["provider"] = "gemini"      # Gemini уже был настроен — остаётся им
        core.save_plugin_options(modname, options)
        os.replace(old, old + ".old")
        logger.info("Настройки plugin_gemini перенесены в plugin_llm")
    except Exception as e:
        logger.warning(f"Не удалось перенести настройки plugin_gemini: {e}")


def _ready(o, provider):
    if provider == "yandex":
        return bool(o.get("yandex_api_key") and o.get("yandex_folder_id"))
    return bool(o.get("api_key"))


# ---------------------------------------------------------------- команды
def _room_key(core):
    room = getattr(core, "current_room", None)
    return getattr(room, "id", None) or "local"


def unknown_command(core: VACore, text: str) -> bool:
    o = core.plugin_options(modname)
    text = (text or "").strip()
    if not o.get("enabled", True) or not _ready(o, o.get("provider", "yandex")) or not core.isOnline:
        return False
    if len(text) < 3:
        return False                          # одно имя «Ирина» — пусть ядро ответит как обычно
    ask(core, text)
    return True


def ask_cmd(core: VACore, phrase: str, provider: str = None):
    o = core.plugin_options(modname)
    provider = provider or o.get("provider", "yandex")
    if not _ready(o, provider):
        core.say("Не заданы ключ и каталог Яндекса в настройках нейросети" if provider == "yandex"
                 else "Не задан ключ Gemini. Укажите его в настройках нейросети")
        return
    if not phrase.strip():
        core.say("Что спросить у нейросети?")
        return
    ask(core, phrase.strip(), provider)


def forget_cmd(core: VACore, phrase: str):
    with _lock:
        _history.pop(_room_key(core), None)
    core.say("Хорошо, начнём сначала")


# ---------------------------------------------------------------- запрос в фоне
def ask(core: VACore, question: str, provider: str = None):
    """Спросить в фоне и ответить в той же комнате (поток команд не ждёт сеть)."""
    room = getattr(core, "current_room", None)
    key = _room_key(core)
    server = getattr(core, "rooms_server", None)
    o = dict(core.plugin_options(modname))
    provider = provider or o.get("provider", "yandex")
    title = "Алиса" if provider == "yandex" else "Gemini"
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
            answer = (query_yandex if provider == "yandex" else query)(o, key, question)
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
    def __init__(self, spoken, log):
        super().__init__(log)
        self.spoken, self.log = spoken, log


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


def _post(o, body):
    url = f"{o.get('base_url', '').rstrip('/')}/v1beta/models/{o.get('model')}:generateContent"
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


def query(o: dict, key, question: str) -> str:
    contents = [{"role": r, "parts": [{"text": t}]} for r, t in _history_of(o, key)]
    contents.append({"role": "user", "parts": [{"text": question}]})
    gen = {"temperature": float(o.get("temperature", 0.8)), "maxOutputTokens": int(o.get("max_output_tokens", 1500)),
           # «размышления» моделей 2.5 / 3.x добавляют секунды ожидания — голосовому помощнику не нужны
           "thinkingConfig": {"thinkingBudget": 0}}
    if not _thinking_cfg_ok.get(o.get("model"), True):
        gen.pop("thinkingConfig")
    body = {"systemInstruction": {"parts": [{"text": o.get("system_prompt") or SYSTEM_PROMPT}]},
            "contents": contents, "generationConfig": gen}
    r = _post(o, body)
    if r.status_code == 400 and "thinking" in r.text.lower() and "thinkingConfig" in gen:
        gen.pop("thinkingConfig")           # модель не умеет отключать размышления — спросим без этого
        _thinking_cfg_ok[o.get("model")] = False
        r = _post(o, body)
    if r.status_code != 200:
        _raise_http(r)
    data = r.json()
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


# ---------------------------------------------------------------- Yandex AI Studio (Alice AI LLM)
def query_yandex(o: dict, key, question: str) -> str:
    """OpenAI-совместимый Responses API Yandex AI Studio."""
    msgs = [{"role": "assistant" if r == "model" else "user", "content": t} for r, t in _history_of(o, key)]
    msgs.append({"role": "user", "content": question})
    model = (o.get("yandex_model") or "aliceai-llm").strip()
    if not model.startswith("gpt://"):
        model = f"gpt://{o['yandex_folder_id'].strip()}/{model}"
    body = {"model": model, "instructions": o.get("system_prompt") or SYSTEM_PROMPT, "input": msgs,
            "temperature": float(o.get("temperature", 0.8)), "max_output_tokens": int(o.get("max_output_tokens", 1500))}
    try:
        r = requests.post(o.get("yandex_url") or "https://ai.api.cloud.yandex.net/v1/responses", json=body,
                          headers={"Authorization": "Api-Key " + o["yandex_api_key"].strip(),
                                   "OpenAI-Project": o["yandex_folder_id"].strip()},
                          timeout=float(o.get("timeout", 40)))
    except requests.exceptions.Timeout:
        raise LLMError("Алиса слишком долго думает, попробуйте ещё раз", "таймаут")
    except requests.exceptions.ConnectionError as e:
        raise LLMError("Нет связи с Яндексом", str(e))
    if r.status_code != 200:
        _raise_http_yandex(r)
    data = r.json()
    text = data.get("output_text") or ""
    if not text:
        for item in data.get("output") or []:
            if item.get("type") == "message":
                text += " ".join(c.get("text", "") for c in item.get("content") or []
                                 if c.get("type") in ("output_text", "text"))
    if not text.strip():
        raise LLMError("Алиса ничего не ответила", f"пустой ответ: {str(data)[:300]}")
    text = for_speech(text, int(o.get("max_chars", 2500)))
    _remember(o, key, question, text)
    return text


def _raise_http_yandex(r):
    try:
        d = r.json()
        err = d.get("error") or d
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        msg = r.text[:300]
    log = f"HTTP {r.status_code}: {msg}"
    if r.status_code in (401, 403):
        raise LLMError("Яндекс не принял ключ. Проверьте ключ, каталог и роль сервисного аккаунта", log)
    if r.status_code == 429:
        raise LLMError("Слишком много запросов к Алисе, попробуйте позже", log)
    low = (msg or "").lower()
    if r.status_code == 404 or ("model" in low and ("not found" in low or "unknown" in low or "не найден" in low)):
        raise LLMError("Такой модели в Яндексе нет. Проверьте название модели в настройках", log)
    if r.status_code == 402 or "billing" in (msg or "").lower() or "balance" in (msg or "").lower():
        raise LLMError("В Яндекс Облаке закончились деньги или не привязан платёжный аккаунт", log)
    raise LLMError("Яндекс ответил ошибкой", log)


# ---------------------------------------------------------------- Google Gemini
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
        raise LLMError("Лимит запросов к нейросети исчерпан, попробуйте позже", log)
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
