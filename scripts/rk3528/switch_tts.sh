#!/usr/bin/env bash
# Переключение голоса Ирины.
#     bash scripts/rk3528/switch_tts.sh silero    # Silero v4 (лучше звучит, медленнее, нужен PyTorch ~400 МБ)
#     bash scripts/rk3528/switch_tts.sh vosk      # vosk-tts (быстрее, легче)
#
# Голос Silero меняется в options/plugin_tts_silero_v4.json -> "speaker": aidar, baya, kseniya, xenia, eugene

set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$REPO/.venv/bin/python"
ENGINE="${1:-}"

# как и в install.sh: если PyPI доступен напрямую — ставим в обход SOCKS-прокси
PIP=("$REPO/.venv/bin/pip" --timeout 120 --retries 5)
NOPROXY=(env -u ALL_PROXY -u all_proxy -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy)
if env | grep -qiE '^(all|https?)_proxy=socks' && \
   "${NOPROXY[@]}" curl -sS -o /dev/null --max-time 15 https://pypi.org/simple/pip/; then
  PIP=("${NOPROXY[@]}" "${PIP[@]}")
fi

set_engine() {
  "$PY" - "$REPO/options/core.json" "$1" <<'PYEOF'
import json, sys
path, engine = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
data["ttsEngineId"] = engine
data["useTTSCache"] = True
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=4)
PYEOF
}

case "$ENGINE" in
  silero)
    echo "==> PyTorch (CPU) — около 200 МБ загрузки, может занять время"
    if ! "$PY" -c "import torch" 2>/dev/null; then
      # сначала официальный CPU-индекс PyTorch (без CUDA), если он недоступен — PyPI
      "${PIP[@]}" install "torch>=2.5,<2.8" --index-url https://download.pytorch.org/whl/cpu || \
        "${PIP[@]}" install "torch>=2.5,<2.8"
    fi
    "$PY" -c "import torch; print('    torch', torch.__version__)"

    echo "==> Модель Silero v4 (~40 МБ)"
    if [ ! -s "$REPO/silero_model_v4.pt" ]; then
      curl -fL --retry 3 -o "$REPO/silero_model_v4.pt.part" https://models.silero.ai/models/tts/ru/v4_ru.pt
      mv "$REPO/silero_model_v4.pt.part" "$REPO/silero_model_v4.pt"
    fi

    [ -e "$REPO/options/plugin_tts_silero_v4.json" ] || \
      cp "$REPO/options_rk3528/plugin_tts_silero_v4.json" "$REPO/options/"
    set_engine silero_v4

    # PyTorch + модель занимают больше памяти — поднимаем лимит сервиса Ирины
    UNIT=~/.config/systemd/user/irene.service
    if [ -f "$UNIT" ]; then
      sed -i 's/^MemoryHigh=.*/MemoryHigh=1300M/; s/^MemoryMax=.*/MemoryMax=1600M/' "$UNIT"
      systemctl --user daemon-reload
    fi
    ;;
  vosk)
    set_engine vosk
    UNIT=~/.config/systemd/user/irene.service
    if [ -f "$UNIT" ]; then
      sed -i 's/^MemoryHigh=.*/MemoryHigh=900M/; s/^MemoryMax=.*/MemoryMax=1200M/' "$UNIT"
      systemctl --user daemon-reload
    fi
    echo "    PyTorch можно удалить, чтобы освободить место: .venv/bin/pip uninstall torch"
    ;;
  *)
    echo "Использование: $0 silero|vosk"; exit 1 ;;
esac

echo "==> Перезапуск Ирины"
systemctl --user restart irene
echo "Готово. Первый запуск с новым голосом — до минуты. Лог: journalctl --user -u irene -f"
