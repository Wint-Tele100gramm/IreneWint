#!/usr/bin/env bash
# Голос Русланы — Silero TTS: ставит PyTorch (сборка для процессора) в окружение Русланы, модель в models/silero
# и показывает скорость синтеза на этой машине. Вызывается из install.sh; можно запускать и отдельно.
#     bash scripts/rk3528/get_tts_silero.sh             # поставить и проверить скорость
#     bash scripts/rk3528/get_tts_silero.sh --no-bench  # без проверки скорости

set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$REPO/.venv/bin/python"

[ -x "$PY" ] || { echo "!! Нет $PY — сначала установите Руслану (scripts/rk3528/install.sh)"; exit 1; }

# pip и SOCKS-прокси уживаются плохо — как в install.sh: если PyPI доступен напрямую, ставим без прокси
PIP=("$REPO/.venv/bin/pip" --timeout 120 --retries 5)
if env | grep -qiE '^(all|https?)_proxy=socks' && \
   env -u ALL_PROXY -u all_proxy -u HTTPS_PROXY -u https_proxy curl -sS -o /dev/null --max-time 15 https://pypi.org/simple/pip/; then
  PIP=(env -u ALL_PROXY -u all_proxy -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy "${PIP[@]}")
fi

# Память: PyTorch с моделью занимает 450–650 МБ. На 1 ГБ работает впритык (проверено на S905: с музыкой
# и распознаванием доступно ~120 МБ и ~190 МБ сжатой подкачки zram) — предупреждаем.
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$MEM_MB" -lt 1800 ]; then
  echo "!! Памяти ${MEM_MB} МБ: голос Silero займёт 450–650 МБ. Нужна подкачка zram (install.sh включает её сам);"
  echo "   число ядер на озвучку можно уменьшить на странице Русланы (Голос)."
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

if [ "${1:-}" != "--no-bench" ]; then
  echo "==> Проверка скорости (Руслану на время проверки лучше остановить: systemctl --user stop irene)"
  "$PY" "$REPO/scripts/tts_bench.py" || echo "!! Проверка не прошла — пришлите вывод выше"
  echo "Послушать образец: aplay $REPO/temp/tts_bench_silero_*.wav"
fi
