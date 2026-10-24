# Как написать плагин

Плагин — один файл `plugins/plugin_<имя>.py`. Его подхватывают при запуске; удалите файл — плагина не будет.
Плагины загружаются по алфавиту, `core.py` — первым (основные настройки).

## Простейший плагин

```python
from vacore import VACore


def start(core: VACore):
    return {
        "name": "Привет",
        "version": "1.0",
        "require_online": False,          # True — команда отключается, если в настройках выключен онлайн
        "commands": {                     # фразы через |; после имени помощника: «ирина привет»
            "привет|доброе утро": hello,
            "подбрось|брось": {           # дерево команд: «подбрось монетку», «брось кубик»
                "монету|монетку": (coin, "монета"),   # (функция, параметр) -> coin(core, phrase, "монета")
            },
        },
    }


def hello(core: VACore, phrase: str):     # phrase — то, что сказано после команды
    core.say("И тебе привет!")


def coin(core: VACore, phrase: str, what: str):
    core.say(f"Подбрасываю {what}")
```

Ответ звучит в той комнате, откуда пришла команда: комната доступна как `core.current_room` (`.name` — зона HA).

## Настройки

```python
def start(core):
    return {..., "default_options": {"url": "", "token": ""}}

def start_with_options(core, manifest):
    options = manifest["options"]          # options/plugin_<имя>.json, создаётся при первом запуске

def anywhere(core, phrase):
    options = core.plugin_options("plugin_<имя>")
    core.save_plugin_options("plugin_<имя>", options)   # сохранить изменения
```
При смене `version` файл настроек дополняется новыми ключами (старые значения сохраняются).
Команды можно строить из настроек: добавьте их в `manifest["commands"]` в `start_with_options` (см. `plugin_scenes.py`).

## Диалог: вопрос и ответ без имени

Функция команды может вернуть генератор — каждый `yield` произносит вопрос и ждёт ответа:

```python
from vacore import DialogTimeout

def order(core, phrase):
    try:
        what = yield "Что заказать?"              # ответ — без «Ирина», в той же комнате
        size = yield ("Какой размер?", 15)        # своё время ожидания, секунд
    except DialogTimeout:                         # не ответили вовремя
        return "Ладно, в другой раз"
    return f"Заказываю {what}, размер {size}"     # return со строкой — последний ответ
```

Старый способ тоже работает: `core.context_set(функция_или_словарь_команд, секунд)` — следующая фраза без имени уйдёт
туда (один раз; чтобы продолжить — установить снова). Пример: `plugin_gamemoreless.py`.

## Таймеры и отложенные события

```python
core.set_timer(60, (after, "чайник"))      # через 60 с: after(core, "чайник")

def after(core, what):
    core.play_wav("media/timer.wav")
    core.say(f"Пора: {what}")              # прозвучит в комнате, где таймер поставили
```

## Прочее

- `core.say(text)` — сказать; `core.play_wav(path)` — проиграть WAV (тоже в комнату).
- `core.say_in_all_rooms(text)` — во всех комнатах.
- `core.find_best_cmd_with_fuzzy(phrase, {"вариант|другой": ...}, False, 0.7)` — выбрать ближайший вариант.
- `utils.ru_words_to_num.extract_number("пятьдесят пять")` -> 55; `utils/ru_speech` — числа и время с падежами.
- Логи, видимые в «Журнале» веб-интерфейса: `logging.getLogger("rooms").info(...)`.
- Озвучка, проигрывание WAV и нормализация текста — тоже плагины: ключи `tts`, `playwav`, `normalizer` в манифесте
  (см. `plugin_tts_vosk.py`, `plugin_playwav_aplay.py`, `plugin_normalizer_prepare.py`).
