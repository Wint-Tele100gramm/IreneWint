#!/usr/bin/env bash
# Включает эхоподавление для микрофона приставки и "перебивание" Ирины голосом.
#     bash scripts/rk3528/enable_aec.sh          # включить
#     bash scripts/rk3528/enable_aec.sh off      # выключить
#
# Перед включением подключите микрофон и динамик, которыми будет пользоваться Ирина, и выберите их
# устройствами по умолчанию — эхоподавление встаёт между ними:
#     pactl list short sinks / sources;  pactl set-default-sink ... ; pactl set-default-source ...

set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CONF_DIR=~/.config/pipewire/pipewire.conf.d
CONF="$CONF_DIR/50-irene-aec.conf"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

set_barge_in() {
  "$REPO/.venv/bin/python" - "$REPO/runva_rooms.json" "$1" <<'PYEOF'
import json, sys
path, value = sys.argv[1], sys.argv[2] == "1"
with open(path, encoding="utf-8") as f:
    data = json.load(f)
data["barge_in"] = value
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=4)
PYEOF
}

if [ "${1:-on}" = off ]; then
  rm -f "$CONF"
  set_barge_in 0
  systemctl --user restart pipewire pipewire-pulse wireplumber
  sleep 3
  systemctl --user restart irene 2>/dev/null || true
  echo "Эхоподавление выключено, микрофон снова отключается на время ответа."
  exit 0
fi

if ! find /usr/lib -name 'libspa-aec-webrtc.so' 2>/dev/null | grep -q .; then
  echo "==> Ставлю модуль эхоподавления WebRTC"
  sudo apt-get install -y libspa-0.2-modules
fi

# реальные динамик и микрофон: к ним явно привязываем эхоподавление
# (если AEC уже включён и выбран по умолчанию — берём сохранённые при первом включении)
SAVED=~/.config/irene/aec-devices
REAL_SINK="$(pactl get-default-sink)"
REAL_SOURCE="$(pactl get-default-source)"
if [[ "$REAL_SINK$REAL_SOURCE" == *irene_aec* ]]; then
  [ -f "$SAVED" ] || { echo "!! Сначала выберите реальные устройства: pactl set-default-sink/source ..."; exit 1; }
  # shellcheck disable=SC1090
  . "$SAVED"
fi
mkdir -p ~/.config/irene
printf 'REAL_SINK=%q\nREAL_SOURCE=%q\n' "$REAL_SINK" "$REAL_SOURCE" > "$SAVED"
echo "    динамик:  $REAL_SINK"
echo "    микрофон: $REAL_SOURCE"

mkdir -p "$CONF_DIR"
sed -e "s#capture.props = { #capture.props = { target.object = \"$REAL_SOURCE\" #" \
    -e "s#playback.props = { #playback.props = { target.object = \"$REAL_SINK\" #" \
    "$REPO/scripts/rk3528/50-irene-aec.conf" > "$CONF"
systemctl --user restart pipewire pipewire-pulse wireplumber
sleep 3

if ! pactl list short sinks | grep -q irene_aec_sink; then
  echo "!! Узлы эхоподавления не появились. Смотрите: journalctl --user-unit pipewire -n 30"
  rm -f "$CONF"
  systemctl --user restart pipewire pipewire-pulse wireplumber
  exit 1
fi

# весь звук — через эхоподавление (WirePlumber запомнит выбор и после перезагрузки)
pactl set-default-sink irene_aec_sink
pactl set-default-source irene_aec_source
set_barge_in 1
systemctl --user restart irene irene-snapclient 2>/dev/null || systemctl --user restart irene 2>/dev/null || true

cat <<EOF
Готово: эхоподавление включено, Ирину можно перебивать.
  Проверка: включите музыку ("Ирина, включи музыку") и скажите "Ирина, стоп" или "Ирина, что играет".
  Во время ответа Ирины можно сказать "стоп" / "хватит" — она замолчит.
Выключить: bash scripts/rk3528/enable_aec.sh off
EOF
