ARG BUILD_FROM
FROM $BUILD_FROM

# Системные пакеты: Python, плеер mpv (Яндекс Музыка), Snapcast (музыка по комнатам), звуковые библиотеки
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv git curl bzip2 ca-certificates \
        mpv snapserver libportaudio2 libsndfile1 alsa-utils \
    && rm -rf /var/lib/apt/lists/*

# Код Русланы — из репозитория (сборка идёт на самой машине с Home Assistant).
# BUILD_VERSION меняется с версией дополнения — тогда код скачивается заново, а не берётся из кэша сборки.
# Bluetooth и DLNA работают через PipeWire приставки — в дополнении их нет (plugin_audio_sources убираем).
ARG RUSLANA_REPO
ARG RUSLANA_BRANCH
ARG BUILD_VERSION
RUN echo "Руслана ${BUILD_VERSION}" \
    && git clone --depth 1 -b "${RUSLANA_BRANCH}" "${RUSLANA_REPO}" /app \
    && rm -rf /app/.git /app/hardware /app/esp32_client /app/ruslana \
    && rm -f /app/plugins/plugin_audio_sources.py

# Python-окружение; PyTorch (голос Silero) — сборка для процессора, без CUDA
RUN python3 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir wheel \
    && /app/.venv/bin/pip install --no-cache-dir -r /app/requirements.txt \
    && /app/.venv/bin/pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY run.sh /run.sh
RUN chmod a+x /run.sh
WORKDIR /app
CMD ["/run.sh"]
