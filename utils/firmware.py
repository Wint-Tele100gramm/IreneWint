# Прошивки клиентов ESP32 для обновления по воздуху через сервер Ирины.
#
# Файл .bin — то, что даёт Arduino IDE: «Скетч → Экспорт скомпилированного бинарного файла» (IreneClient.ino.bin,
# без bootloader и merged). По заголовку образа определяется чип (ESP32 или ESP32-S3), по метке
# "IRENE_FW:<версия>:<чип>:" в теле — версия прошивки Ирины. Чужие прошивки и прошивки для другого чипа не принимаются.

import hashlib
import json
import os
import re
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FW_DIR = os.path.join(ROOT, "firmware")
KEEP_PER_CHIP = 5
MAX_SIZE = 4 * 1024 * 1024

# chip_id в заголовке образа ESP-IDF (esp_image_header_t, смещение 12)
CHIP_IDS = {0: "esp32", 2: "esp32s2", 5: "esp32c3", 9: "esp32s3", 12: "esp32c2", 13: "esp32c6", 16: "esp32h2"}
CHIP_TITLES = {"esp32": "ESP32", "esp32s3": "ESP32-S3"}


class FirmwareError(ValueError):
    pass


def chip_of_model(model: str) -> str:
    """ESP.getChipModel() клиента ("ESP32-S3", "ESP32-D0WD-V3") -> "esp32s3" / "esp32"."""
    m = (model or "").upper().replace("-", "")
    for key in ("ESP32S3", "ESP32S2", "ESP32C3", "ESP32C6"):
        if m.startswith(key):
            return key.lower()
    return "esp32" if m.startswith("ESP32") else ""


def version_tuple(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")) or (0,)


def inspect(data: bytes) -> dict:
    """Проверить образ и вытащить чип и версию. FirmwareError — если это не прошивка клиента Ирины."""
    if len(data) < 1024 or data[0] != 0xE9:
        raise FirmwareError("Это не прошивка ESP32 (нужен файл .bin из «Экспорт скомпилированного бинарного файла»)")
    chip = CHIP_IDS.get(int.from_bytes(data[12:14], "little"), "")
    # длина образа по заголовкам сегментов: у объединённого файла (merged: загрузчик + разметка + программа)
    # после первого образа (загрузчика) идёт ещё много данных — такой файл записывать в раздел программы нельзя
    pos, segs = 24, data[1]
    try:
        for _ in range(segs):
            pos += 8 + int.from_bytes(data[pos + 4:pos + 8], "little")
        img_len = (pos + 16) // 16 * 16 + (32 if data[23] == 1 else 0)
    except IndexError:
        img_len = 0
    if not img_len or img_len > len(data):
        raise FirmwareError("Файл повреждён или обрезан")
    if len(data) - img_len > 4096:
        raise FirmwareError("Это объединённый файл (.merged.bin) — нужен IreneClient.ino.bin без «merged» и «bootloader»")
    if len(data) > MAX_SIZE:
        raise FirmwareError("Файл слишком большой для прошивки клиента")
    m = re.search(rb"IRENE_FW:([0-9][0-9A-Za-z.\-]{0,15}):([a-z0-9]{3,10}):", data)
    if not m:
        raise FirmwareError("В файле нет метки прошивки Ирины — это другая прошивка или версия старше 3.2")
    version, tag_chip = m.group(1).decode(), m.group(2).decode()
    if chip and tag_chip != chip:
        raise FirmwareError(f"Метка {tag_chip} не совпадает с чипом образа {chip}")
    return {"version": version, "chip": tag_chip, "size": len(data), "md5": hashlib.md5(data).hexdigest()}


def save(data: bytes, original_name: str = "") -> dict:
    info = inspect(data)
    os.makedirs(FW_DIR, exist_ok=True)
    name = f"irene-{info['chip']}-{info['version']}-{time.strftime('%Y%m%d-%H%M%S')}.bin"
    with open(os.path.join(FW_DIR, name), "wb") as f:
        f.write(data)
    info.update({"file": name, "uploaded": time.time(), "original": os.path.basename(original_name or "")})
    with open(os.path.join(FW_DIR, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False)
    _cleanup(info["chip"])
    return info


def list_all() -> list:
    result = []
    if not os.path.isdir(FW_DIR):
        return result
    for fn in os.listdir(FW_DIR):
        if not fn.endswith(".bin.json"):
            continue
        try:
            with open(os.path.join(FW_DIR, fn), encoding="utf-8") as f:
                info = json.load(f)
            if os.path.isfile(os.path.join(FW_DIR, info["file"])):
                result.append(info)
        except (OSError, ValueError, KeyError):
            continue
    result.sort(key=lambda i: (i["chip"], version_tuple(i["version"]), i["uploaded"]), reverse=True)
    return result


def get(name: str):
    name = os.path.basename(name or "")
    return next((i for i in list_all() if i["file"] == name), None)


def path_of(name: str):
    name = os.path.basename(name or "")
    p = os.path.join(FW_DIR, name)
    return p if name.endswith(".bin") and os.path.isfile(p) else None


def delete(name: str):
    p = path_of(name)
    if p:
        os.unlink(p)
        try:
            os.unlink(p + ".json")
        except OSError:
            pass


def latest_for(chip: str):
    items = [i for i in list_all() if i["chip"] == chip]
    return items[0] if items else None


def _cleanup(chip):
    items = [i for i in list_all() if i["chip"] == chip]
    items.sort(key=lambda i: i["uploaded"], reverse=True)
    for old in items[KEEP_PER_CHIP:]:
        delete(old["file"])
