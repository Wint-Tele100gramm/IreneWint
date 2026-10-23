#!/usr/bin/env python3
"""Получение токена Яндекс Музыки без браузера на приставке (вход по коду).

Запуск из корня проекта:  .venv/bin/python scripts/rk3528/yandex_token.py [music|music_login|all]
    music        — только права Музыки (по умолчанию)
    music_login  — плюс доступ к имени аккаунта (Яндекс спросит, давать ли)
    all          — все права приложения «Яндекс.Музыка», как раньше
Скрипт покажет код — откройте ya.ru/device на телефоне, введите код и подтвердите вход.
Токен сохранится в options/plugin_yandex_music.json. То же самое есть в веб-интерфейсе: Настройки → Яндекс Музыка.
"""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
from utils import yandex_oauth as yo  # noqa: E402

OPTIONS = os.path.join(ROOT, "options", "plugin_yandex_music.json")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "music"
    if mode not in yo.MODES:
        sys.exit(__doc__)
    code = yo.request_code(mode)
    print()
    print("  1. Откройте на телефоне или компьютере:", code.get("verification_url"))
    print("  2. Введите код:", code.get("user_code"))
    print("  3. Проверьте список прав (запрошены:", yo.MODE_TITLES[mode] + ") и подтвердите вход")
    print()
    print("Жду подтверждения...")
    token = yo.wait_token(code)["access_token"]
    ok, text = yo.check_token(token)
    print(text)
    if not ok:
        sys.exit("Токен не сохранён. Попробуйте режим music_login или all.")
    os.makedirs(os.path.dirname(OPTIONS), exist_ok=True)
    data = {}
    if os.path.exists(OPTIONS):
        with open(OPTIONS, encoding="utf-8") as f:
            data = json.load(f)
    data.setdefault("v", "1.0")  # версия плагина, без неё Ирина не прочитает настройки
    data["token"] = token
    with open(OPTIONS, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    try:
        os.chmod(OPTIONS, 0o600)
    except OSError:
        pass
    print("Готово, токен сохранён в", OPTIONS)
    print("Перезапустите Ирину: systemctl --user restart irene")


if __name__ == "__main__":
    sys.exit(main())
