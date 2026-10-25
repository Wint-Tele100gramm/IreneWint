# Приём звука с телефонов: Bluetooth-сопряжение голосом и подсказки по DLNA.
# Сами приёмники (gmediarender, bt-agent) ставит scripts/rk3528/install.sh.
#
# Примеры: "ирина режим сопряжения", "ирина подключи телефон", "ирина блютуз выключи"

import os
import subprocess
import logging

from vacore import VACore

modname = os.path.basename(__file__)[:-3]
logger = logging.getLogger(__name__)


def start(core: VACore):
    manifest = {
        "name": "Звук с телефона (Bluetooth / DLNA)",
        "version": "1.0",
        "require_online": False,
        "description": "Bluetooth-сопряжение голосом. DLNA (Android) работает всегда, приёмник называется 'Ирина'.",
        "commands": {
            "режим сопряжения|подключи телефон|включи блютуз|блютуз|сопряжение": bt_pair,
            "выключи блютуз|отключи блютуз|блютуз выключи": bt_off,
            "как подключить телефон|как включить музыку с телефона": howto,
        },
    }
    return manifest


def start_with_options(core: VACore, manifest: dict):
    pass


def _btctl(*args) -> bool:
    try:
        r = subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=10)
        logger.info(r.stdout)
        return r.returncode == 0
    except Exception as e:
        logger.warning(f"bluetoothctl: {e}")
        return False


def bt_pair(core: VACore, phrase: str):
    if phrase.strip().startswith("выключ"):
        return bt_off(core, phrase)
    ok = _btctl("power", "on") and _btctl("discoverable", "on") and _btctl("pairable", "on")
    if ok:
        core.say("Блютуз включён на три минуты. Найдите на телефоне устройство Irene")
    else:
        core.say("Не получилось включить блютуз. Проверьте адаптер")


def bt_off(core: VACore, phrase: str):
    if not _btctl("discoverable", "off"):
        core.say("Не получилось управлять блютузом. Проверьте адаптер")
        return
    try:
        out = subprocess.run(["bluetoothctl", "devices", "Connected"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception as e:
        logger.warning(f"bluetoothctl: {e}")
        out = ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            _btctl("disconnect", parts[1])
    core.say("Блютуз отключён")


def howto(core: VACore, phrase: str):
    core.say("Скажите: режим сопряжения, и подключите телефон по блютузу к Irene. "
             "С андроида можно ещё выбрать Ирину в приложении с поддержкой DLNA")
