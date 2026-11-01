#!/usr/bin/env python3
"""Скорость голоса Silero на этой машине.

    .venv/bin/python scripts/tts_bench.py                 # настройки из options/plugin_tts_silero.json
    .venv/bin/python scripts/tts_bench.py --threads 1 2 4 # с разным числом ядер

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, nargs="+", default=None)
    args = ap.parse_args()
    os.makedirs("temp", exist_ok=True)
    threads = args.threads
    if not threads:
        try:
            threads = [int(json.load(open("options/plugin_tts_silero.json")).get("threads", 2))]
        except (OSError, ValueError):
            threads = [2]
    bench_silero(threads)


if __name__ == "__main__":
    main()
