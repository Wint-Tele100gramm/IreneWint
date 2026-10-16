#!/usr/bin/env bash
# Скачивает модель голоса vosk-tts в ~/.cache/vosk (там её ищет vosk-tts), с прогрессом и докачкой.
#     bash scripts/rk3528/get_tts_model.sh                         # модель из options/plugin_tts_vosk.json
#     bash scripts/rk3528/get_tts_model.sh vosk-model-tts-ru-0.7-multi
#
# Сама Ирина при первом запуске качает модель молча и без таймаута — на медленном канале
# это выглядит как зависание. Поэтому модель лучше скачать заранее этим скриптом.

set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
NAME="${1:-}"
if [ -z "$NAME" ]; then
  NAME="$(python3 -c "import json; print(json.load(open('$REPO/options/plugin_tts_vosk.json')).get('modelId','vosk-model-tts-ru-0.7-multi'))" 2>/dev/null || echo vosk-model-tts-ru-0.7-multi)"
fi
DIR="${VOSK_MODEL_PATH:-$HOME/.cache/vosk}"
mkdir -p "$DIR"

if [ -s "$DIR/$NAME/model.onnx" ] && [ -s "$DIR/$NAME/config.json" ]; then
  echo "Модель $NAME уже есть: $DIR/$NAME"
  exit 0
fi
# недораспакованная модель и недокачанный архив от самой Ирины — убираем, иначе vosk-tts примет их за готовые
rm -rf "${DIR:?}/$NAME" "$DIR/$NAME.zip"

echo "==> Скачиваю $NAME (около 130 МБ для 0.7-multi), прерванную загрузку можно продолжить повторным запуском"
if ! curl -fL -C - --retry 5 --retry-delay 5 --connect-timeout 20 -o "$DIR/$NAME.zip.part" \
     "https://alphacephei.com/vosk/models/$NAME.zip"; then
  # если архив уже был докачан целиком, сервер отвечает на докачку ошибкой — проверим сам архив
  python3 -m zipfile -t "$DIR/$NAME.zip.part" >/dev/null 2>&1 || {
    echo "!! Не скачалось. Запустите скрипт ещё раз — загрузка продолжится с того же места."; exit 1; }
fi
mv "$DIR/$NAME.zip.part" "$DIR/$NAME.zip"

echo "==> Распаковываю"
python3 -m zipfile -e "$DIR/$NAME.zip" "$DIR/"
rm -f "$DIR/$NAME.zip"
[ -s "$DIR/$NAME/model.onnx" ] || { echo "!! В архиве нет model.onnx — скачайте заново"; exit 1; }
du -sh "$DIR/$NAME"
echo "Готово. Перезапустите Ирину: systemctl --user restart irene"
