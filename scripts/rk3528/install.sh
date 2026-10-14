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
sudo apt-get update
# shellcheck disable=SC2086
sudo apt-get install -y --no-install-recommends $PKGS

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
  3. Запуск:          systemctl --user start $UNITS
     Логи:            journalctl --user -u irene -f
  4. Проверьте микрофон: arecord -l   и   pactl list short sources

Перезайдите по SSH, чтобы применились группы audio/bluetooth.
EOF
