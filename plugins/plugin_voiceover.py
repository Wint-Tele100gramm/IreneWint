# Озвучивание текста: «ирина скажи привет всем»
# author: Vladislav Janvarev

from vacore import VACore


def start(core: VACore):
    return {
        "name": "Озвучивание текста",
        "version": "2.0",
        "require_online": False,
        "description": "Голосовая команда: скажи|озвучь <текст>",
        "commands": {
            "озвучь|скажи": say,
        },
    }


def start_with_options(core: VACore, manifest: dict):
    pass


def say(core: VACore, phrase: str):
    core.say(phrase if phrase else "Нечего сказать")
