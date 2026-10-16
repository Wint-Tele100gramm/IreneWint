#!/usr/bin/env bash
# Скачивает потоковую модель Vosk для sherpa-onnx (новое распознавание) в папку model-sherpa.
#     bash scripts/rk3528/get_asr_model.sh          # малая int8 (~27 МБ, ~100 МБ ОЗУ) — для 1 ГБ
#     bash scripts/rk3528/get_asr_model.sh full     # полная 0.56 int8 (~70 МБ, ~250 МБ ОЗУ) — для 2 ГБ и больше
#
# Малая — из релизов sherpa-onnx на GitHub, полная — с HuggingFace (alphacep/vosk-model-streaming-ru).

set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
KIND="${1:-small}"
BASE="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

case "$KIND" in
  small)
    # малая модель есть в релизах sherpa-onnx на GitHub — одним архивом
    NAME="sherpa-onnx-streaming-zipformer-small-ru-vosk-int8-2025-08-16"
    echo "==> Скачиваю $NAME (~23 МБ)"
    curl -fL --retry 3 -o "$TMP/model.tar.bz2" "$BASE/$NAME.tar.bz2"
    tar xjf "$TMP/model.tar.bz2" -C "$TMP"
    rm -rf "$TMP/$NAME/test_wavs"
    SRC="$TMP/$NAME"
    ;;
  full)
    # полная модель 0.56 — только на HuggingFace; если он недоступен, пробуем зеркало hf-mirror.com
    SRC="$TMP/full"
    mkdir -p "$SRC/am-onnx" "$SRC/lang"
    for f in am-onnx/encoder.int8.onnx am-onnx/decoder.onnx am-onnx/joiner.int8.onnx lang/tokens.txt; do
      echo "==> $f"
      curl -fL --retry 3 -o "$SRC/$f" "https://huggingface.co/alphacep/vosk-model-streaming-ru/resolve/main/$f" || \
        curl -fL --retry 3 -o "$SRC/$f" "https://hf-mirror.com/alphacep/vosk-model-streaming-ru/resolve/main/$f"
    done
    ;;
  *) echo "Использование: $0 [small|full]"; exit 1 ;;
esac

rm -rf "$REPO/model-sherpa"
mv "$SRC" "$REPO/model-sherpa"
du -sh "$REPO/model-sherpa"
echo "Готово. Перезапустите Ирину: systemctl --user restart irene"
