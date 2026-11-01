# Голос Ирины: Silero TTS (офлайн, PyTorch).
# Модель — один файл models/silero/<modelId>.pt; PyTorch и модель ставит scripts/rk3528/get_tts_silero.sh
# (его вызывает install.sh), он же показывает скорость синтеза на этой машине.
#
# Лицензия русских моделей Silero — CC BY-NC 4.0: для дома можно, для продажи — нет.

import logging
import os
import re
import threading
import time
import wave

from vacore import VACore

modname = os.path.basename(__file__)[:-3]
logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(ROOT, "models", "silero")
SPEAKERS = ["xenia", "baya", "kseniya", "aidar", "eugene"]
MAX_CHUNK = 800          # символов за один вызов модели: длинный ответ режется по предложениям

_model = None
_lock = threading.Lock()
_extra_ok = True         # модель понимает put_stress_homo / put_yo_homo (v5); у старых — нет


def start(core: VACore):
    return {
        "name": "TTS Silero",
        "version": "1.0",
        "require_online": False,
        "description": "Озвучка Silero (v5). ID: silero. Голоса: xenia, baya, kseniya, aidar, eugene.",
        "default_options": {
            "modelId": "v5_5_ru",
            "speaker": "xenia",
            "sampleRate": 24000,    # 8000 / 24000 / 48000; клиентам всё равно уходит 22 050 Гц — 24000 хватает
            "threads": max(1, min(3, (os.cpu_count() or 2) - 1)),   # ядер на синтез: одно — распознаванию и музыке
            "putAccent": True,      # ударения
            "putYo": True,          # буква «ё»
            "homographs": True,     # омографы («замок» / «замок») — v5
        },
        "tts": {
            "silero": (init, None, towavfile),
        },
    }


def start_with_options(core: VACore, manifest: dict):
    pass


def model_path(model_id: str) -> str:
    return os.path.join(MODEL_DIR, model_id + ".pt")


def load_model(model_id: str, threads: int):
    """Загрузить модель (без ядра Ирины — для теста скорости)."""
    import torch
    torch.set_num_threads(max(1, int(threads)))
    path = model_path(model_id)
    if not os.path.isfile(path):
        # сама не качаем: без прогресса это выглядит как зависание при запуске
        raise FileNotFoundError(f"нет модели {path} — запустите bash scripts/rk3528/get_tts_silero.sh")
    import warnings
    with warnings.catch_warnings():
        # код внутри модели написан до Python 3.12: на «'\^'» в регулярном выражении он ругается SyntaxWarning,
        # но работает так же — предупреждение не нужно в журнале
        warnings.simplefilter("ignore", SyntaxWarning)
        model = torch.package.PackageImporter(path).load_pickle("tts_models", "model")
    model.to(torch.device("cpu"))
    return model


def init(core: VACore):
    global _model
    options = core.plugin_options(modname)
    started = time.time()
    _model = load_model(options["modelId"], options.get("threads", 2))
    # первый вызов у PyTorch долгий (прогрев) — делаем его сразу, а не на первом ответе
    synth_pcm(options, "Привет.")
    logger.info(f"Модель Silero {options['modelId']} загружена за {time.time() - started:.1f} с")


def clean_text(text: str) -> str:
    text = (text or "").replace("—", ",").replace("–", ",").replace("…", ".")
    # «+» — ручное ударение Silero («зам+ок»), остальное, чего нет в русском алфавите, модели не нужно
    text = re.sub(r"[^ЁА-Яа-яё+,.?!;:()\- ]", " ", text)   # числа к этому моменту уже словами (нормализатор)
    text = re.sub(r"\s+", " ", text)
    return re.sub(r" ([,.?!;:)])", r"\1", text).strip()


def split_text(text: str, limit: int = MAX_CHUNK):
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.!?;])\s+", text):
        while len(sent) > limit:                 # очень длинное предложение — по запятым / пробелам
            cut = max(sent.rfind(",", 0, limit), sent.rfind(" ", 0, limit))
            cut = cut if cut > 0 else limit
            parts.append(sent[:cut + 1].strip())
            sent = sent[cut + 1:].strip()
        if len(cur) + len(sent) + 1 > limit and cur:
            parts.append(cur)
            cur = sent
        else:
            cur = (cur + " " + sent).strip()
    if cur:
        parts.append(cur)
    return [p for p in parts if p]


def synth_pcm(options: dict, text: str, model=None) -> bytes:
    """Текст -> PCM 16 бит моно с частотой options['sampleRate']."""
    global _extra_ok
    import numpy as np
    model = model or _model
    rate = int(options.get("sampleRate", 24000))
    speaker = options.get("speaker", "xenia")
    out = []
    for chunk in split_text(text):
        if not re.search(r"[ЁА-Яа-яё]", chunk):
            continue
        kw = dict(text=chunk, speaker=speaker, sample_rate=rate,
                  put_accent=bool(options.get("putAccent", True)), put_yo=bool(options.get("putYo", True)))
        if options.get("homographs", True) and _extra_ok:
            kw.update(put_stress_homo=True, put_yo_homo=True)
        with _lock:
            try:
                audio = model.apply_tts(**kw)
            except TypeError:
                # модель старше v5 не знает про омографы
                _extra_ok = False
                kw.pop("put_stress_homo", None)
                kw.pop("put_yo_homo", None)
                audio = model.apply_tts(**kw)
        a = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
        out.append(np.clip(a * 32767.0, -32768, 32767).astype("<i2").tobytes())
        out.append(b"\x00\x00" * int(rate * 0.12))   # короткая пауза между кусками длинного ответа
    if not out:
        out.append(b"\x00\x00" * int(rate * 0.2))
    return b"".join(out[:-1] if len(out) > 1 else out)


def towavfile(core: VACore, text_to_speech: str, wavfile: str):
    options = core.plugin_options(modname)
    text = clean_text(core.normalize(text_to_speech))
    started = time.time()
    pcm = synth_pcm(options, text or ".")
    rate = int(options.get("sampleRate", 24000))
    with wave.open(wavfile, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    took = time.time() - started
    dur = len(pcm) / 2 / rate
    logger.info(f"Silero: {dur:.1f} с речи за {took:.2f} с (в {dur / max(took, 1e-3):.1f} раза быстрее речи)")
