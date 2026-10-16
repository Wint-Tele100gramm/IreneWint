#!/usr/bin/env bash
# Установка Ирины на ТВ-приставку RK3528 (Armbian, aarch64).
# Запускать от обычного пользователя (не root) из корня репозитория:
#     bash scripts/rk3528/install.sh            # всё: Ирина, AirPlay, Bluetooth, DLNA
#     bash scripts/rk3528/install.sh --no-bt    # без Bluetooth
#     bash scripts/rk3528/install.sh --aec      # + эхоподавление PipeWire (опционально)
#     bash scripts/rk3528/install.sh --lite     # приставка с 1 ГБ ОЗУ: без DLNA и Bluetooth, zram, жёсткий лимит памяти
#                                               (включается сам, если ОЗУ меньше 1,5 ГБ; --full — отключить)
#     bash scripts/rk3528/install.sh --asr kaldi  # старое распознавание Vosk вместо новой потоковой модели
#
# Переменные: PYTHON=/usr/local/bin/python3.13 (по умолчанию python3.13 из PATH)

set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPTS="$REPO/scripts/rk3528"
PYTHON="${PYTHON:-python3.13}"
WITH_BT=1; WITH_AIRPLAY=1; WITH_DLNA=1; WITH_AEC=0; ASR=sherpa
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
LITE=0; [ "$MEM_MB" -lt 1500 ] && LITE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-bt) WITH_BT=0 ;;
    --no-airplay) WITH_AIRPLAY=0 ;;
    --no-dlna) WITH_DLNA=0 ;;
    --aec) WITH_AEC=1 ;;
    --lite) LITE=1 ;;
    --full) LITE=0 ;;
    --asr) ASR="$2"; shift ;;
    *) echo "Неизвестный параметр $1"; exit 1 ;;
  esac
  shift
done
if [ $LITE = 1 ]; then
  echo "==> Облегчённый режим (ОЗУ ${MEM_MB} МБ): без DLNA и Bluetooth, zram, лимит памяти Ирины 800 МБ"
  WITH_BT=0; WITH_DLNA=0
fi

if [ "$(id -u)" -eq 0 ]; then
  echo "Запустите от обычного пользователя (sudo будет вызван сам)"; exit 1
fi

echo "==> Системные пакеты"
PKGS="python3-venv bzip2 mpv socat alsa-utils libportaudio2 libsndfile1 pipewire pipewire-pulse pipewire-alsa wireplumber pulseaudio-utils"
[ $WITH_AIRPLAY = 1 ] && PKGS="$PKGS shairport-sync avahi-daemon"
[ $WITH_DLNA = 1 ] && PKGS="$PKGS gmediarender gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-pulseaudio"
[ $WITH_BT = 1 ] && PKGS="$PKGS bluez bluez-tools libspa-0.2-bluetooth"
[ $LITE = 1 ] && PKGS="$PKGS zram-tools"
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
if [ ! -x "$REPO/.venv/bin/pip" ]; then
  rm -rf "$REPO/.venv"   # недоделанное окружение (например, без python3-venv) — создаём заново
  "$PYTHON" -m venv "$REPO/.venv" || {
    echo "!! Не удалось создать venv. Для системного Python: sudo apt install python3-venv"
    echo "!! Для собранного из исходников: при сборке нужен модуль ensurepip (configure --with-ensurepip=install)"
    exit 1; }
fi
# pip и SOCKS-прокси уживаются плохо (нужен PySocks, а в pip 26.x есть ошибка с SOCKS).
# Если PyPI доступен напрямую — ставим пакеты в обход прокси, иначе через прокси с PySocks.
PIP=("$REPO/.venv/bin/pip" --timeout 60 --retries 5)
NOPROXY=(env -u ALL_PROXY -u all_proxy -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy)
if env | grep -qiE '^(all|https?)_proxy=socks'; then
  if "${NOPROXY[@]}" curl -sS -o /dev/null --max-time 15 https://pypi.org/simple/pip/; then
    echo "    PyPI доступен напрямую — ставлю пакеты без прокси"
    PIP=("${NOPROXY[@]}" "${PIP[@]}")
  else
    echo "    PyPI доступен только через прокси — нужен PySocks"
    "$REPO/.venv/bin/python" -c "import socks" 2>/dev/null || {
      sudo apt-get install -y python3-socks
      cp /usr/lib/python3/dist-packages/socks.py /usr/lib/python3/dist-packages/sockshandler.py \
         "$("$REPO/.venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/" 2>/dev/null || true
    }
  fi
fi
# pip не обновляем: версия из Debian работает, а в свежих бывают ошибки с прокси
"${PIP[@]}" install wheel
"${PIP[@]}" install -r "$REPO/requirements-rk3528.txt"

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
    "asr": "$ASR",
    "sherpa_model": "model-sherpa",
    "asr_threads": 2,
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

if [ "$ASR" = sherpa ] && [ ! -s "$REPO/model-sherpa/tokens.txt" ] && [ ! -s "$REPO/model-sherpa/lang/tokens.txt" ]; then
  echo "==> Модель распознавания речи (потоковая Vosk, малая int8)"
  bash "$SCRIPTS/get_asr_model.sh" small || echo "!! Не скачалась — Ирина будет работать на старой модели Vosk из папки model"
fi

if [ $LITE = 1 ]; then
  echo "==> zram: сжатая подкачка в памяти (без неё при 1 ГБ систему может прибить OOM)"
  printf 'ALGO=zstd\nPERCENT=60\nPRIORITY=100\n' | sudo tee /etc/default/zramswap >/dev/null
  sudo systemctl enable zramswap >/dev/null 2>&1 || true
  sudo systemctl restart zramswap || echo "!! zramswap не запустился: journalctl -u zramswap"
  # если в Armbian уже есть своя zram-подкачка (armbian-zram-config) — она тоже подойдёт
fi

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
  sudo systemctl enable --now bluetooth || true
  sleep 2
  if bluetoothctl list 2>/dev/null | grep -q Controller; then
    sudo systemctl enable irene-bt-agent
    sudo systemctl restart irene-bt-agent || echo "!! Bluetooth-агент не запустился: journalctl -u irene-bt-agent -n 30"
  else
    sudo systemctl disable irene-bt-agent 2>/dev/null || true
    echo "!! Bluetooth-адаптер не найден — Bluetooth пропущен, остальное работает."
    echo "!! Встроенный модуль приставки может не поддерживаться ядром — подойдёт USB-донгл (RTL8761B),"
    echo "!! после подключения: sudo systemctl enable --now irene-bt-agent"
  fi
fi

echo "==> Сервис Ирины"
sed "$UNIT_SED" "$SCRIPTS/irene.service" > ~/.config/systemd/user/irene.service
if [ $LITE = 1 ]; then
  sed -i 's/^MemoryHigh=.*/MemoryHigh=650M/; s/^MemoryMax=.*/MemoryMax=800M/' ~/.config/systemd/user/irene.service
fi
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
