#!/usr/bin/with-contenv bashio
# Запуск Русланы в дополнении Home Assistant OS. Код — в /app, всё, что надо сохранять (настройки, модели,
# кэш озвучки, анекдоты, прошивки клиентов), — в /data: оно переживает обновление дополнения и попадает в бэкап.
set -e
APP=/app
DATA=/data
PY=$APP/.venv/bin/python

for d in options data models model-sherpa temp tts_cache firmware; do
    mkdir -p "$DATA/$d"
    rm -rf "${APP:?}/$d"
    ln -s "$DATA/$d" "$APP/$d"
done

# Ключ клиентов ESP32 (он же пароль веб-интерфейса вне панели Home Assistant) — один раз
ROOMS=$DATA/options/rooms.json
if [ ! -s "$ROOMS" ]; then
    TOKEN=$($PY -c 'import secrets; print(secrets.token_urlsafe(12))')
    printf '{\n    "token": "%s"\n}\n' "$TOKEN" > "$ROOMS"
fi
bashio::log.info "Ключ клиентов ESP32: $($PY -c "import json; print(json.load(open('$ROOMS'))['token'])")"
bashio::log.info "Адрес сервера для клиентов: IP Home Assistant, порт 8765"

# Модель распознавания речи (~25–70 МБ): скачивается при первом запуске и при смене параметра asr_model
ASR=$(bashio::config 'asr_model')
if [ "$(cat "$DATA/model-sherpa/.kind" 2>/dev/null)" != "$ASR" ]; then
    bashio::log.info "Скачиваю модель распознавания речи ($ASR)"
    ASR_DIR="$DATA/model-sherpa" bash $APP/scripts/rk3528/get_asr_model.sh "$ASR"
    echo "$ASR" > "$DATA/model-sherpa/.kind"
fi

# Голос Silero (модель ~60 МБ, один раз)
bash $APP/scripts/rk3528/get_tts_silero.sh --no-bench || bashio::log.warning "Голос Silero не скачался — ответы будут только текстом"

# Анекдоты — в фоне, это несколько минут
if bashio::config.true 'download_jokes' && [ ! -s "$DATA/data/jokes.json" ]; then
    bashio::log.info "Скачиваю анекдоты в фоне"
    ($PY $APP/scripts/get_jokes.py > /dev/null 2>&1 || bashio::log.warning "Анекдоты не скачались") &
fi

# Мультирум: snapserver с двумя линиями музыки (клиенты ESP32 3.0+ подключаются к порту 1704)
if bashio::config.true 'multiroom'; then
    SNAP_RUN=/tmp/ruslana-snap
    mkdir -p "$SNAP_RUN" "$DATA/snapserver"
    sed "s#@RUNDIR@#$SNAP_RUN#g; s#@DATADIR@#$DATA/snapserver#g" $APP/scripts/rk3528/snapserver.conf > /tmp/snapserver.conf
    bashio::log.info "Мультирум: snapserver на портах 1704 и 1705"
    snapserver -c /tmp/snapserver.conf &
    sleep 1
fi

export PYTHONUNBUFFERED=1 MALLOC_ARENA_MAX=2
cd $APP
# код 3 — «перезапуск» из веб-интерфейса: запускаем заново, не перезапуская контейнер
while true; do
    set +e
    $PY runva_rooms.py
    code=$?
    set -e
    [ $code -eq 3 ] || exit $code
    bashio::log.info "Перезапуск Русланы"
done
