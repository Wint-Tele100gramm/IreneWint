#!/usr/bin/env python3
"""Получение токена Яндекс Музыки без браузера на приставке (OAuth Device Flow).

Запуск из корня проекта:  .venv/bin/python scripts/rk3528/yandex_token.py
Скрипт покажет код — откройте ya.ru/device на телефоне, введите код и подтвердите вход.
Токен сохранится в options/plugin_yandex_music.json.
"""
import json
import os
import sys

from yandex_music import Client

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OPTIONS = os.path.join(ROOT, "options", "plugin_yandex_music.json")


def on_code(code):
    print()
    print("  1. Откройте на телефоне или компьютере:", code.verification_url)
    print("  2. Введите код:", code.user_code)
    print("  3. Подтвердите вход в аккаунт с подпиской Плюс")
    print()
    print("Жду подтверждения...")


def main():
    client = Client()
    token = client.device_auth(on_code=on_code)
    os.makedirs(os.path.dirname(OPTIONS), exist_ok=True)
    data = {}
    if os.path.exists(OPTIONS):
        with open(OPTIONS, encoding="utf-8") as f:
            data = json.load(f)
    data.setdefault("v", "1.0")  # версия плагина, без неё Ирина не прочитает настройки
    data["token"] = token.access_token
    with open(OPTIONS, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.chmod(OPTIONS, 0o600)
    print("Готово, токен сохранён в", OPTIONS)
    print("Перезапустите Ирину: sudo systemctl restart irene")


if __name__ == "__main__":
    sys.exit(main())
