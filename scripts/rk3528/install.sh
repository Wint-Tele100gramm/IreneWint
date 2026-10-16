#!/usr/bin/env bash
# Установка Ирины на ТВ-приставку RK3528 (Armbian, aarch64).
# Запускать от обычного пользователя (не root) из корня репозитория:
#     bash scripts/rk3528/install.sh            # всё: Ирина, AirPlay, Bluetooth, DLNA
#     bash scripts/rk3528/install.sh --no-bt    # без Bluetooth
#     bash scripts/rk3528/install.sh --aec      # + эхоподавление PipeWire (опционально)
#
# Переменные: PYTHON=/usr/local/bin/python3.13 (по умолчанию python3.13 из PATH)

set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPTS="$REPO/scripts/rk3528"
PYTHON="${PYTHON:-python3.13}"
WITH_BT=1; WITH_AIRPLAY=1; WITH_DLNA=1; WITH_AEC=0
for a in "$@"; do
  case "$a" in
    --no-bt) WITH_BT=0 ;;
    --no-airplay) WITH_AIRPLAY=0 ;;
    --no-dlna) WITH_DLNA=0 ;;
    --aec) WITH_AEC=1 ;;
    *) echo "Неизвестный параметр $a"; exit 1 ;;
  esac
done

if [ "$(id -u)" -eq 0 ]; then
  echo "Запустите от обычного пользователя (sudo будет вызван сам)"; exit 1
fi

echo "==> Системные пакеты"
PKGS="mpv socat alsa-utils libportaudio2 libsndfile1 pipewire pipewire-pulse pipewire-alsa wireplumber pulseaudio-utils"
[ $WITH_AIRPLAY = 1 ] && PKGS="$PKGS shairport-sync avahi-daemon"
[ $WITH_DLNA = 1 ] && PKGS="$PKGS gmediarender gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-pulseaudio"
[ $WITH_BT = 1 ] && PKGS="$PKGS bluez bluez-tools libspa-0.2-bluetooth"
sudo apt-get update || echo "!! apt-get update завершился с ошибкой — проверьте зеркала в /etc/apt/sources.list*"
# ставим только те пакеты, что есть в репозиториях: иначе apt откажется ставить и все остальные
AVAILABLE=""; MISSING=""
for p in $PKGS; do
  if apt-cache show "$p" >/dev/null 2>&1; then AVAILABLE="$AVAILABLE $p"; else MISSING="$MISSING $p"; fi
done
# shellcheck disable=SC2086
sudo apt-get install -y --no-install-recommends $AVAILABLE || {
  echo "!! Часть пакетов не скачалась. Повторяю по одному..."
  FAILED=""
  for p in $AVAILABLE; do
    sudo apt-get install -y --no-install-recommends "$p" >/dev/null 2>&1 || FAILED="$FAILED $p"
  done
  MISSING="$MISSING$FAILED"
}
if [ -n "$MISSING" ]; then
  echo
  echo "!! Не удалось установить:$MISSING"
  case "$MISSING" in *mpv*|*libportaudio2*|*pipewire-pulse*)
    echo "!! Без этих пакетов Ирина не заработает. Пришлите вывод: sudo apt-get install$MISSING"; exit 1 ;;
  esac
  echo "!! Остальное необязательно — соответствующая функция (AirPlay/DLNA/Bluetooth) не будет работать."
  echo
fi

echo "==> Python-окружение ($PYTHON)"
"$PYTHON" -c "import ssl, ctypes, sqlite3" || {
  echo "Ваш $PYTHON собран без ssl/ctypes. Пересоберите с libssl-dev libffi-dev libsqlite3-dev"; exit 1; }
[ -d "$REPO/.venv" ] || "$PYTHON" -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install --upgrade pip wheel
"$REPO/.venv/bin/pip" install -r "$REPO/requirements-rk3528.txt"

echo "==> Настройки Ирины"
mkdir -p "$REPO/options" "$REPO/temp"
for f in "$REPO"/options_rk3528/*.json; do
  [ -e "$REPO/options/$(basename "$f")" ] || cp "$f" "$REPO/options/"
done

echo "==> Сервер комнат (клиенты ESP32)"
if [ ! -e "$REPO/runva_rooms.json" ]; then
  ROOMS_TOKEN="$("$REPO/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(12))')"
  cat > "$REPO/runva_rooms.json" <<JSON
{
    "host": "0.0.0.0",
    "port": 8765,
    "token": "$ROOMS_TOKEN",
    "model": "model",
    "local_room": "",
    "local_device": null,
    "arbitration_ms": 450,
    "max_segment_sec": 12,
    "default_volume": 70,
    "tts_chunk_bytes": 4096
}
JSON
  chmod 600 "$REPO/runva_rooms.json"
fi
ROOMS_TOKEN="$("$REPO/.venv/bin/python" -c "import json; print(json.load(open('$REPO/runva_rooms.json'))['token'])")"

echo "==> Звук: PipeWire в пользовательской сессии (работает и без входа в систему)"
sudo loginctl enable-linger "$USER"
sudo usermod -aG audio "$USER"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user enable --now pipewire pipewire-pulse wireplumber || true

if [ $WITH_AEC = 1 ]; then
  mkdir -p ~/.config/pipewire/pipewire.conf.d
  cp "$SCRIPTS/50-irene-aec.conf" ~/.config/pipewire/pipewire.conf.d/
  systemctl --user restart pipewire pipewire-pulse wireplumber
  sleep 2
  pactl set-default-sink irene_aec_sink || true
  pactl set-default-source irene_aec_source || true
fi

mkdir -p ~/.config/systemd/user ~/.config/irene
UNIT_SED="s#@REPO@#$REPO#g"

if [ $WITH_AIRPLAY = 1 ]; then
  echo "==> AirPlay (iPhone/iPad/Mac): shairport-sync"
  sudo systemctl disable --now shairport-sync 2>/dev/null || true   # системный вариант не видит PipeWire пользователя
  sed "$UNIT_SED" "$SCRIPTS/shairport-sync.conf" > ~/.config/irene/shairport-sync.conf
  sed "$UNIT_SED" "$SCRIPTS/irene-airplay.service" > ~/.config/systemd/user/irene-airplay.service
  sudo systemctl enable --now avahi-daemon
fi

if [ $WITH_DLNA = 1 ]; then
  echo "==> DLNA/UPnP (Android): gmediarender"
  sed "$UNIT_SED" "$SCRIPTS/irene-dlna.service" > ~/.config/systemd/user/irene-dlna.service
fi

if [ $WITH_BT = 1 ]; then
  echo "==> Bluetooth-колонка (Android и iOS)"
  sudo usermod -aG bluetooth "$USER"
  sudo sed -i -E 's/^#?\s*Name\s*=.*/Name = Irene/; s/^#?\s*Class\s*=.*/Class = 0x200414/; s/^#?\s*DiscoverableTimeout\s*=.*/DiscoverableTimeout = 180/' /etc/bluetooth/main.conf
  sudo cp "$SCRIPTS/irene-bt-agent.service" /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now bluetooth irene-bt-agent
  if ! bluetoothctl list | grep -q Controller; then
    echo "!! Bluetooth-адаптер не найден. Встроенный модуль приставки может не поддерживаться ядром — подойдёт USB-донгл."
  fi
fi

echo "==> Сервис Ирины"
sed "$UNIT_SED" "$SCRIPTS/irene.service" > ~/.config/systemd/user/irene.service
chmod +x "$SCRIPTS"/*.sh
systemctl --user daemon-reload
UNITS="irene"
[ $WITH_AIRPLAY = 1 ] && UNITS="$UNITS irene-airplay"
[ $WITH_DLNA = 1 ] && UNITS="$UNITS irene-dlna"
# shellcheck disable=SC2086
systemctl --user enable $UNITS

cat <<EOF

Готово. Дальше:
  1. Home Assistant: впишите url и token в $REPO/options/plugin_homeassistant.json
     (файл появится после первого запуска; токен: Профиль HA -> Безопасность -> Долгосрочные токены)
  2. Яндекс Музыка:  $REPO/.venv/bin/python $SCRIPTS/yandex_token.py
  3. Клиенты ESP32 (esp32_client/README.md): сервер $(hostname -I | awk '{print $1}'), порт 8765,
     ключ: $ROOMS_TOKEN
     Микрофон самой приставки как комната: "local_room": "Гостиная" в $REPO/runva_rooms.json
  4. Запуск:          systemctl --user start $UNITS
     Логи:            journalctl --user -u irene -f
  5. Проверьте микрофон: arecord -l   и   pactl list short sources

Перезайдите по SSH, чтобы применились группы audio/bluetooth.
EOF
