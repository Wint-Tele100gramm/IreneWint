# Команды для режима "сервер комнат" (runva_rooms.py): объявления во все комнаты,
# громкость ответов в комнате, информация о подключённых комнатах.
#
# Примеры: "ирина объяви ужин готов", "ирина громкость голоса тридцать",
#          "ирина какая это комната", "ирина какие комнаты подключены"

import os

from vacore import VACore
from utils.ru_words_to_num import extract_number

modname = os.path.basename(__file__)[:-3]


def start(core: VACore):
    manifest = {
        "name": "Комнаты (клиенты ESP32)",
        "version": "1.0",
        "require_online": False,
        "description": "Работает с сервером комнат runva_rooms.py.\n"
                       "Команды: объяви <текст>, громкость голоса <число>, какая это комната, какие комнаты подключены",
        "commands": {
            "объяви|объявление|сообщи всем|скажи во всех комнатах": announce,
            "громкость голоса|громкость ответов|голос громкость": voice_volume,
            "голос громче|говори громче": (voice_volume_step, 15),
            "голос тише|говори тише": (voice_volume_step, -15),
            "какая это комната|где я|в какой я комнате": which_room,
            "какие комнаты подключены|список комнат|сколько комнат": list_rooms,
        },
    }
    return manifest


def start_with_options(core: VACore, manifest: dict):
    pass


def _server(core):
    return getattr(core, "rooms_server", None)


def announce(core: VACore, phrase: str):
    phrase = phrase.strip()
    if not phrase:
        core.say("Что объявить?")
        return
    if hasattr(core, "say_in_all_rooms"):
        core.say_in_all_rooms(phrase)
    else:
        core.say(phrase)


def _set_room_volume(core, value):
    room = getattr(core, "current_room", None)
    if room is None or room.local:
        core.say("Громкость голоса меняется только на клиентах в комнатах")
        return
    room.volume = max(0, min(100, value))
    room.send_json({"type": "volume", "value": room.volume})
    core.say(f"Громкость голоса {room.volume}")


def voice_volume(core: VACore, phrase: str):
    value = extract_number(phrase)
    if value is None:
        core.say("Скажите громкость от нуля до ста")
        return
    _set_room_volume(core, value)


def voice_volume_step(core: VACore, phrase: str, step: int):
    room = getattr(core, "current_room", None)
    _set_room_volume(core, (getattr(room, "volume", 70) or 0) + step)


def which_room(core: VACore, phrase: str):
    room = getattr(core, "current_room", None)
    if room is None:
        core.say("Я работаю без комнат")
    else:
        core.say(f"Это {room.name}")


def list_rooms(core: VACore, phrase: str):
    srv = _server(core)
    if srv is None:
        core.say("Сервер комнат не запущен")
        return
    names = [r.name for r in srv.rooms.values() if r.connected]
    if not names:
        core.say("Нет подключённых комнат")
    else:
        core.say(f"Подключено {len(names)}: " + ", ".join(names))
