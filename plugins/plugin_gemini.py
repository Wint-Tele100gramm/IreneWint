# Собеседник на Google Gemini: на всё, что не является командой Ирины, отвечает нейросеть.
# «ирина расскажи сказку про ёжика», «ирина почему небо голубое», «ирина а что было дальше?».
# Явно: «ирина спроси нейросеть …», «ирина спроси у джемини …».
#
# Нужен ключ Gemini API: https://aistudio.google.com/apikey (бесплатный уровень с ограничением запросов).
# Из России Google API не отвечает («User location is not supported») — нужен прокси за границей
# ("proxy": "socks5h://адрес:порт" или "http://адрес:порт") или свой ретранслятор ("base_url").
# Настройки — options/plugin_gemini.json или веб-интерфейс, раздел «Нейросеть (Gemini)».
#
# Ответ говорится той комнате, где спросили; пока нейросеть думает, Ирина выполняет другие команды.
# Последние реплики помнятся несколько минут — можно переспросить или попросить продолжить.

import collections
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
        "name": "Нейросеть Gemini",
        "version": "1.0",
        "require_online": True,
        "description": "Отвечает на всё, что не является командой Ирины: сказки, вопросы, разговор (Google Gemini).",
        "default_options": {
            "enabled": True,
            "api_key": "",                      # https://aistudio.google.com/apikey
            "model": "gemini-flash-latest",     # или конкретная модель, например gemini-3.5-flash-lite
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
            "спроси нейросеть|спроси у нейросети|спроси джемини|спроси у джемини|спроси гугл|нейросеть": ask_cmd,
            "забудь разговор|начни разговор заново|забудь что я говорил": forget_cmd,
        },
    }


def start_with_options(core: VACore, manifest: dict):
    core.unknown_command_handler = unknown_command


# ---------------------------------------------------------------- команды
def _room_key(core):
    room = getattr(core, "current_room", None)
    return getattr(room, "id", None) or "local"


def unknown_command(core: VACore, text: str) -> bool:
    o = core.plugin_options(modname)
    text = (text or "").strip()
    if not o.get("enabled", True) or not o.get("api_key") or not core.isOnline:
        return False
    if len(text) < 3:
        return False                          # одно имя «Ирина» — пусть ядро ответит как обычно
    ask(core, text)
    return True


def ask_cmd(core: VACore, phrase: str):
    o = core.plugin_options(modname)
    if not o.get("api_key"):
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
        except GeminiError as e:
            journal.warning(f"Gemini: {e.log}")
            answer = e.spoken
        except Exception as e:
            logger.exception(e)
            journal.warning(f"Gemini: {e}")
            answer = "Не получилось спросить нейросеть"
        finally:
            with order:
                done.set()
        journal.info(f"Gemini ({time.time() - started:.1f} с): «{question}» -> «{answer[:120]}»")
        say_in_room(answer)

    journal.info(f"Gemini: спрашиваю «{question}»")
    threading.Thread(target=thinking, name="gemini-wait", daemon=True).start()
    threading.Thread(target=work, name="gemini", daemon=True).start()


class GeminiError(Exception):
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
        raise GeminiError("Для прокси socks нужен пакет PySocks", f"{e} — установите: .venv/bin/pip install pysocks")
    except requests.exceptions.ProxyError as e:
        raise GeminiError("Прокси для нейросети не отвечает", f"прокси {proxy}: {e}")
    except requests.exceptions.Timeout:
        raise GeminiError("Нейросеть слишком долго думает, попробуйте ещё раз", "таймаут")
    except requests.exceptions.ConnectionError as e:
        raise GeminiError("Нет связи с нейросетью", str(e))


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
            raise GeminiError("На это я отвечать не буду", f"ответ заблокирован: {reason}")
        raise GeminiError("Нейросеть ничего не ответила", f"пустой ответ: {reason}")
    text = for_speech(text, int(o.get("max_chars", 2500)))
    _remember(o, key, question, text)
    return text


def _raise_http(r):
    try:
        err = r.json().get("error", {})
        msg, status = err.get("message", r.text[:200]), err.get("status", "")
    except Exception:
        msg, status = r.text[:200], ""
    log = f"HTTP {r.status_code} {status}: {msg}"
    if "location is not supported" in msg.lower():
        raise GeminiError("Нейросеть недоступна из этой страны. Нужен прокси в настройках", log)
    if r.status_code in (401, 403) or "api key" in msg.lower():
        raise GeminiError("Ключ Gemini не подходит. Проверьте его в настройках", log)
    if r.status_code == 429:
        raise GeminiError("Лимит запросов к нейросети исчерпан, попробуйте позже", log)
    if r.status_code == 404:
        raise GeminiError("Такой модели Gemini нет. Проверьте название модели в настройках", log)
    raise GeminiError("Нейросеть ответила ошибкой", log)


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
