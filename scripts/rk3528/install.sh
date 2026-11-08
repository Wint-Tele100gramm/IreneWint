#!/usr/bin/env bash
# Установка Русланы на ТВ-приставку (Armbian/Debian, aarch64): RK3528, S905 и другие.
# Запускать от обычного пользователя (не root) из корня репозитория:
#     bash scripts/rk3528/install.sh            # всё: Руслана, мультирум Snapcast, Bluetooth, DLNA
#     bash scripts/rk3528/install.sh --no-bt    # без Bluetooth
#     bash scripts/rk3528/install.sh --no-multiroom   # музыка только на динамике приставки (без Snapcast)
#     bash scripts/rk3528/install.sh --aec      # + эхоподавление PipeWire (опционально)
#     bash scripts/rk3528/install.sh --lite     # приставка с 1 ГБ ОЗУ: без DLNA и Bluetooth, zram, жёсткий лимит памяти
#                                               (включается сам, если ОЗУ меньше 1,5 ГБ; --full — отключить)
#
# Переменные: PYTHON=/usr/local/bin/python3.13 (по умолчанию: Python уже созданного окружения .venv,
#             иначе самый новый из python3.13 / 3.12 / 3.11 / python3 в системе)

set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPTS="$REPO/scripts/rk3528"
if [ -z "${PYTHON:-}" ]; then
  if [ -x "$REPO/.venv/bin/python" ]; then
    PYTHON="$REPO/.venv/bin/python"          # повторная установка: окружение уже есть
  else
    for p in python3.13 /usr/local/bin/python3.13 python3.12 /usr/local/bin/python3.12 python3.11 python3; do
      if command -v "$p" >/dev/null 2>&1; then PYTHON="$p"; break; fi
    done
  fi
fi
PYTHON="${PYTHON:-python3}"
WITH_BT=1; WITH_SNAP=1; WITH_DLNA=1; WITH_AEC=0
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
LITE=0; [ "$MEM_MB" -lt 1500 ] && LITE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-bt) WITH_BT=0 ;;
    --no-multiroom) WITH_SNAP=0 ;;
    --no-airplay) ;;   # AirPlay больше не поддерживается — параметр оставлен для старых инструкций
    --no-dlna) WITH_DLNA=0 ;;
    --aec) WITH_AEC=1 ;;
    --lite) LITE=1 ;;
    --full) LITE=0 ;;
    *) echo "Неизвестный параметр $1"; exit 1 ;;
  esac
  shift
done
if [ $LITE = 1 ]; then
  echo "==> Облегчённый режим (ОЗУ ${MEM_MB} МБ): без DLNA и Bluetooth, zram, лимит памяти Русланы 800 МБ"
  WITH_BT=0; WITH_DLNA=0
fi

if [ "$(id -u)" -eq 0 ]; then
  echo "Запустите от обычного пользователя (sudo будет вызван сам)"; exit 1
fi

echo "==> Системные пакеты"
PKGS="python3-venv bzip2 mpv socat alsa-utils libportaudio2 libsndfile1 pipewire pipewire-pulse pipewire-alsa wireplumber pulseaudio-utils"
[ $WITH_SNAP = 1 ] && PKGS="$PKGS snapserver snapclient"
[ $WITH_DLNA = 1 ] && PKGS="$PKGS gmediarender gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-pulseaudio"
[ $WITH_BT = 1 ] && PKGS="$PKGS bluez bluez-tools libspa-0.2-bluetooth"
[ $LITE = 1 ] && PKGS="$PKGS zram-tools"
sudo apt-get update || {
  echo "!! apt-get update завершился с ошибкой — проверьте зеркала в /etc/apt/sources.list*"
  echo "!! Если выше «пока не действителен» / «not valid yet» — на приставке неверное время: $(date '+%d.%m.%Y %H:%M')."
  echo "!!   Исправить: sudo timedatectl set-ntp true  (или вручную: sudo date -s '2026-10-27 22:00')"
}
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
    echo "!! Без этих пакетов Руслана не заработает. Пришлите вывод: sudo apt-get install$MISSING"; exit 1 ;;
  esac
  echo "!! Остальное необязательно — соответствующая функция (мультирум/DLNA/Bluetooth) не будет работать."
  echo
fi

echo "==> Python-окружение ($PYTHON)"
command -v "$PYTHON" >/dev/null 2>&1 || {
  echo "Не найден $PYTHON. Поставьте python3 (sudo apt install python3 python3-venv) или укажите PYTHON=/путь/к/python3"; exit 1; }
"$PYTHON" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" || {
  echo "Нужен Python 3.10 или новее, а $PYTHON — $("$PYTHON" -V 2>&1)"; exit 1; }
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
"${PIP[@]}" install -r "$REPO/requirements.txt"
# остатки прежних версий, больше не нужны
"${PIP[@]}" uninstall -y vosk >/dev/null 2>&1 || true

echo "==> Настройки Русланы (все — в папке options)"
mkdir -p "$REPO/options" "$REPO/temp"
# значения по умолчанию — в коде; в options/ лежат только ваши настройки. Переезд со старой раскладки:
[ -e "$REPO/runva_rooms.json" ] && [ ! -e "$REPO/options/rooms.json" ] && mv "$REPO/runva_rooms.json" "$REPO/options/rooms.json"

echo "==> Сервер комнат (клиенты ESP32)"
ROOMS="$REPO/options/rooms.json"
if [ ! -e "$ROOMS" ]; then
  ROOMS_TOKEN="$("$REPO/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(12))')"
  printf '{\n    "token": "%s"\n}\n' "$ROOMS_TOKEN" > "$ROOMS"   # остальное Руслана допишет сама при запуске
fi
chmod 600 "$ROOMS"
ROOMS_TOKEN="$("$REPO/.venv/bin/python" -c "import json; print(json.load(open('$ROOMS'))['token'])")"

if [ ! -s "$REPO/model-sherpa/tokens.txt" ] && [ ! -s "$REPO/model-sherpa/lang/tokens.txt" ]; then
  echo "==> Модель распознавания речи (потоковая Vosk, малая int8)"
  bash "$SCRIPTS/get_asr_model.sh" small || echo "!! Не скачалась — без неё Руслана не запустится. Повторите: bash $SCRIPTS/get_asr_model.sh"
fi
rm -rf "$REPO/model"   # старая модель Kaldi из прежних версий (~90 МБ) больше не используется

echo "==> Голос Silero (PyTorch и модель)"
bash "$SCRIPTS/get_tts_silero.sh" --no-bench || echo "!! Голос не установился — повторите: bash $SCRIPTS/get_tts_silero.sh"
# прежний голос vosk-tts больше не нужен: пакет и его модель (~130 МБ)
"${PIP[@]}" uninstall -y vosk-tts >/dev/null 2>&1 || true
rm -rf "$HOME"/.cache/vosk/vosk-model-tts-*
sed -i 's/"ttsEngineId": *"vosk"/"ttsEngineId": "silero"/' "$REPO/options/core.json" 2>/dev/null || true
rm -f "$REPO/options/plugin_tts_vosk.json"

echo "==> Разрешение Руслане включать максимальную частоту процессора на время речи"
if ls /sys/devices/system/cpu/cpufreq/policy*/scaling_governor >/dev/null 2>&1; then
  echo "z /sys/devices/system/cpu/cpufreq/policy*/scaling_governor 0664 root $(id -gn) -" \
    | sudo tee /etc/tmpfiles.d/irene-cpufreq.conf >/dev/null
  sudo systemd-tmpfiles --create /etc/tmpfiles.d/irene-cpufreq.conf || true
else
  echo "   регулятора частоты (cpufreq) нет — пропускаю"
fi

if [ ! -s "$REPO/data/jokes.json" ]; then
  echo "==> Анекдоты (Hugging Face igorktech/anekdots, несколько минут)"
  "$REPO/.venv/bin/python" "$REPO/scripts/get_jokes.py" || echo "!! Анекдоты не скачались — повторите: .venv/bin/python scripts/get_jokes.py"
fi

if [ $LITE = 1 ]; then
  echo "==> zram: сжатая подкачка в памяти (без неё при 1 ГБ систему может прибить OOM)"
  if swapon --show=NAME --noheadings 2>/dev/null | grep -q zram; then
    # в Armbian zram-подкачку обычно уже включает armbian-zram-config — второй zram не нужен
    echo "    zram-подкачка уже работает: $(swapon --show=NAME,SIZE --noheadings | grep zram | tr -s ' ' | paste -sd ',')"
    sudo systemctl disable --now zramswap >/dev/null 2>&1 || true
  else
    printf 'ALGO=zstd\nPERCENT=60\nPRIORITY=100\n' | sudo tee /etc/default/zramswap >/dev/null
    sudo systemctl enable zramswap >/dev/null 2>&1 || true
    sudo systemctl restart zramswap || echo "!! zramswap не запустился: journalctl -u zramswap -n 20"
  fi
fi

echo "==> Звук: PipeWire в пользовательской сессии (работает и без входа в систему)"
sudo loginctl enable-linger "$USER"
sudo usermod -aG audio "$USER"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user enable --now pipewire pipewire-pulse wireplumber || true

if [ $WITH_AEC = 1 ]; then
  echo "==> Эхоподавление: включится после установки — bash $SCRIPTS/enable_aec.sh"
fi

mkdir -p ~/.config/systemd/user ~/.config/irene
UNIT_SED="s#@REPO@#$REPO#g"

# прежние версии ставили AirPlay (shairport-sync) — убираем
if [ -e ~/.config/systemd/user/irene-airplay.service ]; then
  echo "==> Убираю AirPlay из прежней установки"
  systemctl --user disable --now irene-airplay 2>/dev/null || true
  rm -f ~/.config/systemd/user/irene-airplay.service ~/.config/irene/shairport-sync.conf
fi

if [ $WITH_SNAP = 1 ] && command -v snapserver >/dev/null; then
  echo "==> Мультирум: snapserver (две линии музыки) и snapclient для динамика приставки"
  # системный snapserver из пакета не нужен: наш работает от пользователя, чтобы плеер писал в его fifo
  sudo systemctl disable --now snapserver snapclient 2>/dev/null || true
  SNAP_RUN="/run/user/$(id -u)/irene-snap"
  SNAP_DATA="$HOME/.local/share/irene-snap"
  SNAP_SED="s#@RUNDIR@#$SNAP_RUN#g; s#@DATADIR@#$SNAP_DATA#g"
  sed "$SNAP_SED" "$SCRIPTS/snapserver.conf" > ~/.config/irene/snapserver.conf
  sed "$SNAP_SED" "$SCRIPTS/irene-snapserver.service" > ~/.config/systemd/user/irene-snapserver.service
  cp "$SCRIPTS/irene-snapclient.service" ~/.config/systemd/user/
  echo "    snapserver $(snapserver --version 2>/dev/null | head -1 | grep -o '[0-9][0-9.]*' | head -1): порт 1704 (клиенты), 1705 (управление)"
elif [ $WITH_SNAP = 1 ]; then
  echo "!! snapserver не установился — мультирума не будет, музыка играет на динамике приставки"
  WITH_SNAP=0
fi

if [ $WITH_DLNA = 1 ]; then
  echo "==> DLNA/UPnP (Android): gmediarender"
  sed "$UNIT_SED" "$SCRIPTS/irene-dlna.service" > ~/.config/systemd/user/irene-dlna.service
fi

if [ $WITH_BT = 1 ]; then
  echo "==> Bluetooth-колонка (Android и iOS)"
  sudo usermod -aG bluetooth "$USER"
  sudo sed -i -E 's/^#?\s*Name\s*=.*/Name = Ruslana/; s/^#?\s*Class\s*=.*/Class = 0x200414/; s/^#?\s*DiscoverableTimeout\s*=.*/DiscoverableTimeout = 180/' /etc/bluetooth/main.conf
  # Приставка без монитора: WirePlumber по умолчанию включает Bluetooth-звук только при «активном сеансе за экраном»,
  # а через SSH такого нет — телефон видит Ruslana, но подключиться не может («Не удалось подключиться»).
  mkdir -p ~/.config/wireplumber/bluetooth.lua.d ~/.config/wireplumber/wireplumber.conf.d
  echo 'bluez_monitor.properties["with-logind"] = false' > ~/.config/wireplumber/bluetooth.lua.d/80-irene-headless.lua
  printf 'wireplumber.profiles = {\n  main = {\n    monitor.bluez.seat-monitoring = disabled\n  }\n}\n' \
    > ~/.config/wireplumber/wireplumber.conf.d/80-irene-headless.conf
  systemctl --user restart wireplumber 2>/dev/null || true
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

echo "==> Сервис Русланы"
sed "$UNIT_SED" "$SCRIPTS/irene.service" > ~/.config/systemd/user/irene.service
if [ $LITE = 1 ]; then
  # 1 ГБ: голос Silero (~450 МБ) + распознавание + mpv — около 700 МБ. Ниже MemoryHigh ядро постоянно выдавливает
  # Руслану в подкачку и притормаживает её на каждом выделении памяти (заметно как паузы в ответах).
  sed -i 's/^MemoryHigh=.*/MemoryHigh=780M/; s/^MemoryMax=.*/MemoryMax=870M/' ~/.config/systemd/user/irene.service
fi
chmod +x "$SCRIPTS"/*.sh
systemctl --user daemon-reload
UNITS="irene"
[ $WITH_SNAP = 1 ] && UNITS="irene-snapserver irene-snapclient $UNITS"
[ $WITH_DLNA = 1 ] && UNITS="$UNITS irene-dlna"
# shellcheck disable=SC2086
systemctl --user enable $UNITS
# при повторной установке перезапускаем уже работающие сервисы, чтобы подхватить новый код
for u in $UNITS; do
  systemctl --user is-active --quiet "$u" && systemctl --user restart "$u" && echo "    перезапущен $u"
done

cat <<EOF

Готово. Дальше:
  1. Веб-интерфейс:   http://$(hostname -I | awk '{print $1}'):8765/  (пароль — ключ клиентов ниже)
     Там: Home Assistant (адрес и токен), вход в Яндекс Музыку, комнаты, сценарии, музыка по комнатам.
  2. Клиенты ESP32 (esp32_client/README.md): сервер $(hostname -I | awk '{print $1}'), порт 8765,
     ключ: $ROOMS_TOKEN
  3. Запуск:          systemctl --user start $UNITS
     Логи:            journalctl --user-unit irene -f
  4. Микрофон приставки (если нужен): arecord -l, pactl list short sources

Перезайдите по SSH, чтобы применились группы audio/bluetooth.
EOF
