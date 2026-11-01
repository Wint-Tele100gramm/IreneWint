#!/usr/bin/env bash
# Ставит Silero TTS: PyTorch (CPU) в окружение Ирины, модель в models/silero, проверяет скорость.
#     bash scripts/rk3528/get_tts_silero.sh            # поставить и проверить скорость
#     bash scripts/rk3528/get_tts_silero.sh --use      # то же и включить Silero (ttsEngineId = silero)
#     bash scripts/rk3528/get_tts_silero.sh --vosk     # вернуть прежний голос vosk-tts
# После --use / --vosk: systemctl --user restart irene

set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$REPO/.venv/bin/python"
CORE="$REPO/options/core.json"

set_engine() {
  "$PY" - "$CORE" "$1" <<'PYEOF'
import json, sys
path, engine = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    core = json.load(f)
core["ttsEngineId"] = engine
with open(path, "w", encoding="utf-8") as f:
    json.dump(core, f, ensure_ascii=False, indent=4)
print(f"ttsEngineId = {engine} (options/core.json)")
PYEOF
}

if [ "${1:-}" = "--vosk" ]; then
  set_engine vosk
  echo "Перезапустите Ирину: systemctl --user restart irene"
  exit 0
fi

[ -x "$PY" ] || { echo "!! Нет $PY — сначала установите Ирину (scripts/rk3528/install.sh)"; exit 1; }

# pip и SOCKS-прокси уживаются плохо — как в install.sh: если PyPI доступен напрямую, ставим без прокси
PIP=("$REPO/.venv/bin/pip" --timeout 120 --retries 5)
if env | grep -qiE '^(all|https?)_proxy=socks' && \
   env -u ALL_PROXY -u all_proxy -u HTTPS_PROXY -u https_proxy curl -sS -o /dev/null --max-time 15 https://pypi.org/simple/pip/; then
  PIP=(env -u ALL_PROXY -u all_proxy -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy "${PIP[@]}")
fi

# Память: PyTorch с моделью занимает 300–500 МБ. На приставке с 1 ГБ вместе с распознаванием речи это слишком много.
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$MEM_MB" -lt 1800 ] && [ "${FORCE:-0}" != 1 ]; then
  echo "!! На этой приставке ${MEM_MB} МБ памяти — Silero (PyTorch) с распознаванием речи в неё не поместится."
  echo "   Оставьте vosk-tts. Поставить всё равно: FORCE=1 bash $0"
  exit 1
fi

if "$PY" -c "import torch" 2>/dev/null; then
  echo "==> PyTorch уже есть: $("$PY" -c 'import torch; print(torch.__version__)')"
else
  # Сборка без видеокарты — с сайта PyTorch: на PyPI для aarch64 теперь лежит сборка с CUDA (почти 500 МБ
  # плюс библиотеки NVIDIA). Временные файлы pip — на диск, а не в /tmp (в Armbian /tmp в памяти, ~450 МБ).
  echo "==> Ставлю PyTorch (сборка для процессора, около 150–250 МБ)"
  mkdir -p "$REPO/temp/pip"
  TMPDIR="$REPO/temp/pip" "${PIP[@]}" install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
  rm -rf "$REPO/temp/pip"
fi

MODEL="$("$PY" -c "import json; print(json.load(open('$REPO/options/plugin_tts_silero.json')).get('modelId','v5_5_ru'))" 2>/dev/null || echo v5_5_ru)"
DIR="$REPO/models/silero"
mkdir -p "$DIR"
if [ -s "$DIR/$MODEL.pt" ]; then
  echo "==> Модель $MODEL уже есть"
else
  echo "==> Скачиваю модель $MODEL (прерванную загрузку можно продолжить повторным запуском)"
  curl -fL -C - --retry 5 --retry-delay 5 --connect-timeout 20 -o "$DIR/$MODEL.pt.part" \
       "https://models.silero.ai/models/tts/ru/$MODEL.pt" || {
    echo "!! Не скачалось. Запустите ещё раз; если сайт недоступен — скачайте файл"
    echo "   https://models.silero.ai/models/tts/ru/$MODEL.pt на компьютере и положите в $DIR/"; exit 1; }
  mv "$DIR/$MODEL.pt.part" "$DIR/$MODEL.pt"
fi
du -sh "$DIR/$MODEL.pt"

echo "==> Проверка скорости (Silero и для сравнения vosk-tts)"
"$PY" "$REPO/scripts/tts_bench.py" || echo "!! Проверка не прошла — пришлите вывод выше"

if [ "${1:-}" = "--use" ]; then
  set_engine silero
  echo "Перезапустите Ирину: systemctl --user restart irene"
else
  echo
  echo "Послушать образец: aplay $REPO/temp/tts_bench_silero_*.wav"
  echo "Включить Silero:   bash $0 --use && systemctl --user restart irene"
fi
