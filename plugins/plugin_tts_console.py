# «Озвучка» текстом в консоль — запасной вариант, если голос не загрузился
# author: Vladislav Janvarev

from termcolor import cprint

from vacore import VACore


def start(core: VACore):
    return {
        "name": "TTS console (for debug)",
        "version": "1.0",
        "require_online": False,
        "tts": {
            "console": (init, say),
        },
    }


def init(core: VACore):
    pass


def say(core: VACore, text_to_speech: str):
    cprint(f"TTS: {text_to_speech}", "blue")
