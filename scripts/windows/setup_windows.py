"""Подготовка Ирины на Windows: настройки, ключ клиентов, модели распознавания и голоса.

Запускается из install.bat (можно и повторно — уже сделанное не повторяется).
"""
import json
import os
import secrets
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OPTIONS = os.path.join(ROOT, "options")
ASR_NAME = "sherpa-onnx-streaming-zipformer-small-ru-vosk-int8-2025-08-16"
ASR_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{ASR_NAME}.tar.bz2"
TTS_NAME = "vosk-model-tts-ru-0.7-multi"
TTS_URL = f"https://alphacephei.com/vosk/models/{TTS_NAME}.zip"


def ask(question, default=""):
    try:
        answer = input(f"{question} ").strip()
    except EOFError:
        answer = ""
    return answer or default


def download(url, dest):
    print(f"    {url}")
    part = dest + ".part"

    shown = [-1]

    def hook(blocks, block_size, total):
        if total > 0:
            done = min(blocks * block_size, total)
            pct = done * 100 // total
            if pct != shown[0]:
                shown[0] = pct
                print(f"\r    {pct:3d}%  {done / 1e6:6.1f} из {total / 1e6:.1f} МБ", end="", flush=True)
    urllib.request.urlretrieve(url, part, hook)
    print()
    os.replace(part, dest)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def setup_options():
    print("==> Настройки")
    os.makedirs(OPTIONS, exist_ok=True)
    src = os.path.join(ROOT, "options_rk3528")
    for name in os.listdir(src):
        if name.endswith(".json") and not os.path.exists(os.path.join(OPTIONS, name)):
            shutil.copy(os.path.join(src, name), OPTIONS)

    core = read_json(os.path.join(OPTIONS, "core.json"))
    core["playWavEngineId"] = "sounddevice"          # aplay есть только в Linux
    write_json(os.path.join(OPTIONS, "core.json"), core)

    music_path = os.path.join(OPTIONS, "plugin_yandex_music.json")
    music = read_json(music_path)
    if music:
        music["mpv_args"] = ["--ao=wasapi"]
        write_json(music_path, music)

    rooms_path = os.path.join(ROOT, "runva_rooms.json")
    rooms = read_json(rooms_path)
    if not rooms.get("token"):
        print()
        print("    Ключ клиентов — тот же, что вписан в клиентах ESP32. Если помните ключ с приставки —")
        print("    вставьте его, и клиентам останется поменять только адрес сервера.")
        token = ask("    Ключ клиентов (Enter — создать новый):", secrets.token_hex(8))
        rooms["token"] = token
        print(f"    Ключ: {token}")
    if "local_room" not in rooms:
        print()
        name = ask("    Слушать микрофон ноутбука как комнату? Название комнаты (Enter — нет):", "")
        rooms["local_room"] = name
    write_json(rooms_path, rooms)


def setup_asr():
    target = os.path.join(ROOT, "model-sherpa")
    if os.path.isfile(os.path.join(target, "tokens.txt")):
        print("==> Модель распознавания уже есть")
        return
    print("==> Модель распознавания (~25 МБ)")
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "model.tar.bz2")
        download(ASR_URL, archive)
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data") if sys.version_info >= (3, 12) else tar.extractall(tmp)
        shutil.rmtree(os.path.join(tmp, ASR_NAME, "test_wavs"), ignore_errors=True)
        shutil.rmtree(target, ignore_errors=True)
        shutil.move(os.path.join(tmp, ASR_NAME), target)


def setup_tts():
    cache = os.path.join(os.path.expanduser("~"), ".cache", "vosk")
    target = os.path.join(cache, TTS_NAME)
    if os.path.isfile(os.path.join(target, "model.onnx")):
        print("==> Модель голоса уже есть")
        return
    print("==> Модель голоса (~130 МБ)")
    os.makedirs(cache, exist_ok=True)
    archive = os.path.join(cache, TTS_NAME + ".zip")
    download(TTS_URL, archive)
    with zipfile.ZipFile(archive) as z:
        z.extractall(cache)
    os.remove(archive)


def main():
    setup_options()
    setup_asr()
    setup_tts()
    print()
    print("Готово. Запуск Ирины: run.bat")


if __name__ == "__main__":
    main()
