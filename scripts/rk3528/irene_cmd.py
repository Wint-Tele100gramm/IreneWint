#!/usr/bin/env python3
"""Текстовая команда Ирине — для проверки без микрофона и клиентов ESP32.

    .venv/bin/python scripts/rk3528/irene_cmd.py "включи свет на кухне"
    .venv/bin/python scripts/rk3528/irene_cmd.py --room Кухня "включи свет"     # как будто сказали на кухне
    .venv/bin/python scripts/rk3528/irene_cmd.py --mute "какие комнаты подключены"  # ответ только текстом в логе

Имя "ирина" в начале добавляется само. Ответ играет через динамик приставки (paplay).
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys

from websockets.asyncio.client import connect

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="+")
    ap.add_argument("--room", default="Консоль", help="название комнаты (как зона в Home Assistant)")
    ap.add_argument("--mute", action="store_true", help="не проигрывать ответ")
    args = ap.parse_args()

    with open(os.path.join(ROOT, "runva_rooms.json"), encoding="utf-8") as f:
        opts = json.load(f)
    text = " ".join(args.text).lower()
    if not text.startswith("ирин"):
        text = "ирина " + text

    async with connect(f"ws://127.0.0.1:{opts['port']}/", max_size=2 ** 22) as ws:
        await ws.send(json.dumps({"type": "hello", "room": "console", "name": args.room,
                                  "token": opts.get("token", ""), "rate": 16000, "fw": "cli"}))
        player = None
        await ws.send(json.dumps({"type": "text", "text": text}))
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=60)
                if isinstance(msg, bytes):
                    if player:
                        player.stdin.write(msg)
                    continue
                data = json.loads(msg)
                t = data.get("type")
                if t == "error":
                    sys.exit("Ошибка: " + data.get("message", ""))
                if t == "tts_start" and not args.mute:
                    player = subprocess.Popen(["paplay", "--raw", f"--rate={data['rate']}", "--channels=1",
                                               "--format=s16le"], stdin=subprocess.PIPE)
                elif t == "tts_end" and player:
                    player.stdin.close()
                    player.wait()
                    player = None
                elif t == "idle":
                    break
        except asyncio.TimeoutError:
            sys.exit("Ирина не ответила за 60 секунд — смотрите journalctl --user -u irene -f")
    print("Готово")


if __name__ == "__main__":
    asyncio.run(main())
