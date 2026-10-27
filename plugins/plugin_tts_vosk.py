# Озвучка через vosk-tts (офлайн)
# author: Vladislav Janvarev
# Модель скачивается в ~/.cache/vosk (scripts/rk3528/get_tts_model.sh или install_windows.bat).

import logging
import os
import re
import time

from vacore import VACore

modname = os.path.basename(__file__)[:-3]
logger = logging.getLogger(__name__)


def start(core: VACore):
    return {
        "name": "TTS vosk",
        "version": "1.4",
        "require_online": False,
        "description": "Озвучка vosk-tts. ID: vosk. Голоса 0–4 (2 — Ирина).",
        "default_options": {
            "modelId": "vosk-model-tts-ru-0.7-multi",
            "speakerId": 2,
            "useGPU": False,     # не используется: на приставке и ноутбуке хватает процессора
        },
        "tts": {
            "vosk": (init, None, towavfile),   # инициализация, (говорить сразу — нет), синтез в WAV
        },
    }


def start_with_options(core: VACore, manifest: dict):
    pass


def init(core: VACore):
    from vosk_tts.model import Model
    from vosk_tts.synth import Synth

    started = time.time()
    options = core.plugin_options(modname)
    core.ttsModel = Model(model_name=options["modelId"])
    core.ttsSynth = Synth(core.ttsModel)
    logger.info(f"Модель голоса загружена за {time.time() - started:.1f} с")


def towavfile(core: VACore, text_to_speech: str, wavfile: str):
    options = core.plugin_options(modname)
    text = core.normalize(text_to_speech)
    # последняя защита: vosk-tts падает на любом символе, которого нет в его словаре (латиница, эмодзи...)
    text = (text or "").replace("—", ",").replace("–", ",")
    text = re.sub(r"[^ЁА-Яа-яё,.?!;:()\- ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip() or "."
    core.ttsSynth.synth(text, wavfile, speaker_id=options["speakerId"])
