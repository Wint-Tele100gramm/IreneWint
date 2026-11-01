#!/usr/bin/env python3
"""Сравнение скорости озвучки на этой машине: Silero и vosk-tts на одних и тех же фразах.

    .venv/bin/python scripts/tts_bench.py                 # Silero из options/plugin_tts_silero.json + vosk
    .venv/bin/python scripts/tts_bench.py --threads 1 2 4 # Silero с разным числом ядер

Образцы голоса сохраняются в temp/tts_bench_*.wav — их можно послушать (aplay temp/tts_bench_silero_xenia.wav).
"""

import argparse
import json
import os
import resource
import sys
import time
import wave

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

PHRASES = [
    "Включаю свет на кухне.",
    "Сейчас без четверти восемь вечера. На улице плюс двенадцать, небольшой дождь.",
    "В какой комнате включить музыку? Скажите: на кухне, в спальне или везде. "
    "Если хотите, могу продолжить ту же подборку, что играла вчера в гостиной, или поставить что-нибудь спокойное.",
]


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def save(path, rate, pcm):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def bench_silero(threads_list):
    from plugins import plugin_tts_silero as ps
    opts = dict(ps.start(None)["default_options"])
    try:
        opts.update(json.load(open("options/plugin_tts_silero.json", encoding="utf-8")))
    except (OSError, ValueError):
        pass
    import torch
    print(f"Silero {opts['modelId']}, голос {opts['speaker']}, {opts['sampleRate']} Гц, PyTorch {torch.__version__}")
    t = time.time()
    model = ps.load_model(opts["modelId"], threads_list[0])
    print(f"  загрузка модели: {time.time() - t:.1f} с, память процесса: {rss_mb():.0f} МБ")
    t = time.time()
    ps.synth_pcm(opts, "Привет.", model)
    print(f"  прогрев (первая фраза): {time.time() - t:.1f} с")
    for th in threads_list:
        torch.set_num_threads(th)
        total_t = total_d = 0
        for i, text in enumerate(PHRASES):
            t = time.time()
            pcm = ps.synth_pcm(opts, ps.clean_text(text), model)
            took = time.time() - t
            dur = len(pcm) / 2 / opts["sampleRate"]
            total_t += took
            total_d += dur
            print(f"  ядер {th}: фраза {i + 1}: {dur:4.1f} с речи за {took:4.2f} с")
            if i == len(PHRASES) - 1 and th == threads_list[0]:
                save(f"temp/tts_bench_silero_{opts['speaker']}.wav", opts["sampleRate"], pcm)
        print(f"  ядер {th}: в среднем в {total_d / total_t:.1f} раза быстрее речи")
    print(f"  память процесса (пик): {rss_mb():.0f} МБ")


def bench_vosk():
    try:
        from vosk_tts.model import Model
        from vosk_tts.synth import Synth
    except ImportError:
        print("vosk-tts не установлен — сравнения не будет")
        return
    opts = {"modelId": "vosk-model-tts-ru-0.7-multi", "speakerId": 2}
    try:
        opts.update(json.load(open("options/plugin_tts_vosk.json", encoding="utf-8")))
    except (OSError, ValueError):
        pass
    print(f"vosk-tts {opts['modelId']}, голос {opts['speakerId']}")
    try:
        synth = Synth(Model(model_name=opts["modelId"]))
    except Exception as e:
        print(f"  модель не загрузилась: {e}")
        return
    synth.synth("Привет.", "temp/tts_bench_warm.wav", speaker_id=opts["speakerId"])
    total_t = total_d = 0
    for i, text in enumerate(PHRASES):
        path = f"temp/tts_bench_vosk_{i}.wav"
        t = time.time()
        synth.synth(text, path, speaker_id=opts["speakerId"])
        took = time.time() - t
        with wave.open(path) as w:
            dur = w.getnframes() / w.getframerate()
        total_t += took
        total_d += dur
        print(f"  фраза {i + 1}: {dur:4.1f} с речи за {took:4.2f} с")
    print(f"  в среднем в {total_d / total_t:.1f} раза быстрее речи")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, nargs="+", default=None)
    ap.add_argument("--no-vosk", action="store_true")
    args = ap.parse_args()
    os.makedirs("temp", exist_ok=True)
    threads = args.threads
    if not threads:
        try:
            threads = [int(json.load(open("options/plugin_tts_silero.json")).get("threads", 2))]
        except (OSError, ValueError):
            threads = [2]
    bench_silero(threads)
    if not args.no_vosk:
        print()
        bench_vosk()


if __name__ == "__main__":
    main()
