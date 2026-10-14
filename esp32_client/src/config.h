// Распиновка и параметры клиента. Wi-Fi, адрес сервера и имя комнаты задаются
// через веб-портал при первом запуске (точка доступа "Irene-XXXXXX"), а не здесь.
#pragma once

// ---------------- распиновка ----------------
#if defined(CONFIG_IDF_TARGET_ESP32S3)
  // ESP32-S3 DevKitC
  #define MIC_SCK   4    // INMP441 SCK
  #define MIC_WS    5    // INMP441 WS
  #define MIC_SD    6    // INMP441 SD
  #define AMP_BCLK  15   // MAX98357A BCLK
  #define AMP_LRC   16   // MAX98357A LRC
  #define AMP_DIN   7    // MAX98357A DIN
  #define AMP_SD    -1   // MAX98357A SD (выключение усилителя в тишине), -1 — не подключён
  #define LED_PIN   -1   // светодиод "слушаю", -1 — нет
#else
  // ESP32 DevKit (WROOM-32)
  #define MIC_SCK   32
  #define MIC_WS    25
  #define MIC_SD    33
  #define AMP_BCLK  26
  #define AMP_LRC   27
  #define AMP_DIN   22
  #define AMP_SD    -1
  #define LED_PIN   2    // встроенный светодиод
#endif

#define RESET_BUTTON 0     // кнопка BOOT: удерживать 5 секунд — сброс настроек Wi-Fi и комнаты

// ---------------- микрофон ----------------
// INMP441 с L/R на GND отдаёт левый канал. На некоторых платах ESP32 каналы перепутаны —
// если в логе уровень всегда ~0, поменяйте на I2S_CHANNEL_FMT_ONLY_RIGHT.
#define MIC_CHANNEL_FMT   I2S_CHANNEL_FMT_ONLY_LEFT
#define MIC_SHIFT         14     // 32-битный отсчёт >> MIC_SHIFT = 16 бит. Меньше — громче (13 = +6 дБ)

// ---------------- детектор речи (VAD) ----------------
// Звук уходит на сервер только когда в комнате говорят — так сервер не тратит CPU на тишину.
#define VAD_START_RATIO   3.0f   // во сколько раз громче фонового шума считается речью
#define VAD_MIN_LEVEL     250    // минимальный уровень (RMS) речи
#define VAD_START_FRAMES  3      // столько кадров по 20 мс подряд, чтобы начать
#define VAD_HANG_MS       800    // сколько тишины ждать до конца фразы
#define VAD_HANG_WAKE_MS  1600   // ... если сервер уже услышал "Ирина" (ждём команду)
#define VAD_PREROLL_MS    400    // сколько звука до начала речи отправить (чтобы не съесть начало слова)
#define VAD_MAX_MS        12000  // максимальная длина фразы
#define MIC_MUTE_AFTER_TTS_MS 350 // не слушать микрофон сразу после ответа (эхо)

#define DEFAULT_SERVER   "irene.local"
#define DEFAULT_PORT     8765
#define DEFAULT_VOLUME   70
#define FW_VERSION       "1.0"
