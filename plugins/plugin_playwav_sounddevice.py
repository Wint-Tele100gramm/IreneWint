# Проигрывание WAV через sounddevice (Windows и любые системы без aplay)
# author: Vladislav Janvarev

import os

import numpy
import sounddevice
import soundfile

from vacore import VACore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def start(core: VACore):
    return {
        "name": "PlayWav through sounddevice",
        "version": "1.1",
        "require_online": False,
        "playwav": {
            "sounddevice": (init, playwav),
        },
    }


def start_with_options(core: VACore, manifest: dict):
    pass


def init(core: VACore):
    pass


def playwav(core: VACore, wavfile: str):
    path = wavfile if os.path.isabs(wavfile) else os.path.join(ROOT, wavfile)
    data, rate = soundfile.read(path, dtype="float32")
    # тишина в конце, иначе sounddevice иногда «проглатывает» конец фразы
    # https://github.com/spatialaudio/python-sounddevice/issues/283
    pad = numpy.zeros((5000,) + data.shape[1:], dtype=data.dtype)
    sounddevice.play(numpy.concatenate((data, pad)), rate)
    sounddevice.wait()
