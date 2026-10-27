// Клиент Ирины для комнаты.
//  - слушает I2S-микрофон, и когда в комнате говорят — отправляет звук на сервер (runva_rooms.py)
//  - проигрывает ответы сервера через I2S-усилитель
//  - настройка Wi-Fi, адреса сервера и имени комнаты — через портал WiFiManager
//
//  - 2.0: настройки микрофона с сервера (веб-интерфейс), живой индикатор уровня, автоусиление по речи
//  - 3.0: музыка по комнатам — клиент Snapcast (snap_proto.h): синхронно с другими комнатами, голос Ирины
//         поверх музыки, музыка притихает, пока Ирина слушает команду и отвечает
//  - 3.1: стерео 44100 Гц (S3), эхо- и шумоподавление микрофона на S3 (mic_aec.h, ESP-SR)
//
// Протокол описан в начале runva_rooms.py.
//
// Сборка в Arduino IDE (подробно — esp32_client/README.md):
//   ESP32-S3 N16R8: плата "ESP32S3 Dev Module", Flash 16MB, PSRAM "OPI PSRAM",
//                   Partition Scheme "16M Flash (3MB APP/9.9MB FATFS)", USB CDC On Boot "Disabled" (адаптер с CH9102);
//   ESP32:          плата "ESP32 Dev Module", Partition Scheme "Minimal SPIFFS (1.9MB APP with OTA/190KB SPIFFS)";
//   библиотеки: WiFiManager (tzapu), WebSockets (Markus Sattler), ArduinoJson (Benoit Blanchon).
//   Работает с ядром esp32 2.0.x и 3.x (в 3.x будет предупреждение о старом драйвере I2S — это нормально).

#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <ArduinoOTA.h>
#include <Preferences.h>
#include <WiFiManager.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <WiFiClient.h>
#include <new>
#include <freertos/stream_buffer.h>
#include "esp_timer.h"
#include "driver/i2s.h"
#include "driver/gpio.h"
#include "esp_idf_version.h"
#include "esp_system.h"
#include "esp_heap_caps.h"
#include "config.h"
#include "snap_proto.h"
#include "mic_aec.h"

// в ESP-IDF 5 (ядро Arduino 3.x) поля DMA переименованы, старые имена оставлены как устаревшие
#if ESP_IDF_VERSION_MAJOR >= 5
  #define I2S_DMA_COUNT dma_desc_num
  #define I2S_DMA_LEN   dma_frame_num
#else
  #define I2S_DMA_COUNT dma_buf_count
  #define I2S_DMA_LEN   dma_buf_len
#endif

#define MIC_PORT I2S_NUM_0
#define AMP_PORT I2S_NUM_1

static const int SAMPLE_RATE = 16000;
static const int FRAME = 320;                       // 20 мс при 16 кГц
static const int FRAMES_PER_PACKET = 4;             // отправляем пакетами по 80 мс
static const int PREROLL_FRAMES = VAD_PREROLL_MS / 20;
static const int AMP_CH = AMP_STEREO ? 2 : 1;       // каналов в очереди музыки (на выход всегда L и R)
static const int BLOCK = AMP_RATE >= 44100 ? 512 : 256;   // блок воспроизведения = один буфер DMA (11,6 мс)
static const int AMP_DMA_BUFS = 4;                  // буферов DMA: задержка ~46 мс, её учитывает синхронизация

// ---------------- настройки ----------------
struct Settings {
  String server = DEFAULT_SERVER;
  uint16_t port = DEFAULT_PORT;
  String roomId;
  String roomName;
  String token;
  float micGainDb = DEFAULT_MIC_GAIN_DB;   // при автоусилении — подобранное значение
  int vadLevel = DEFAULT_VAD_LEVEL;
  int hangMs = DEFAULT_VAD_HANG_MS;
  bool agc = DEFAULT_AGC;
  int agcMaxDb = DEFAULT_AGC_MAX_DB;
  bool aec = DEFAULT_AEC;
  bool ns = DEFAULT_NS;
} cfg;
float micGain = 4.0f;                    // линейное усиление из cfg.micGainDb
bool gainDirty = false;                  // автоусиление поменяло усиление — сохранить позже
uint32_t gainSavedAt = 0;

Preferences prefs;
WebSocketsClient ws;

// ---------------- состояние ----------------
volatile bool wsConnected = false;
volatile int volumePct = DEFAULT_VOLUME;
volatile uint32_t wakeUntil = 0;

// воспроизведение
StreamBufferHandle_t audioBuf;
volatile bool playing = false;       // идёт ответ (от tts_start до конца звука)
volatile bool ttsEnded = false;      // сервер прислал весь ответ
volatile int pendingRate = 22050;
volatile uint32_t playEndMs = 0;

// музыка (Snapcast): сеть -> snapSched -> задача воспроизведения
WiFiClient snapSock;
String snapHost;                         // адрес сервера (тот же, что у Ирины)
snap::Parser* snapParser = nullptr;
snap::TimeSync snapTime;
snap::Schedule snapSched;
snap::SyncStats syncStats;
volatile bool snapConnected = false;
volatile bool snapFormatOk = false;
snap::Resampler snapRes;                 // поток snapserver -> частота и каналы усилителя
volatile int snapVolume = 100;           // громкость музыки в этой комнате (Ирина меняет через snapserver)
volatile bool snapMuted = false;
volatile int snapBufferMs = 1000, snapLatencyMs = 0;
volatile uint32_t musicHeardAt = 0;      // когда последний раз звучала музыка (детектор речи учитывает)
bool speechWithMusic = false;            // фраза записана поверх музыки — автоусиление по ней не подстраиваем

// микрофон и детектор речи
int32_t micRaw[FRAME * 2];           // оба слота I2S (L и R): микрофон говорит только в одном из них
int micSlot = MIC_SLOT < 0 ? 0 : MIC_SLOT;   // какой слот читаем (выбирается автоматически)
float slotActivity[2] = {0, 0};      // «живость» слотов: средний модуль разности соседних отсчётов
int slotFrames = 0, slotVotes = 0;
static const int32_t GLITCH_MIN = 1 << 16;     // сбой — не меньше этого (в 24-битных единицах)
static const int GLITCH_RATIO = 16;            // ...и в 16 раз больше среднего шага сигнала в кадре
int32_t glitchTail[4] = {0, 0, 0, 0};          // последние отсчёты прошлого кадра (фильтру нужны соседи)
uint32_t glitchCount = 0;
int16_t frame16[FRAME];
int16_t preroll[PREROLL_FRAMES][FRAME];
int prerollHead = 0, prerollCount = 0;
int16_t packet[FRAME * FRAMES_PER_PACKET];
int packetFrames = 0;
float noiseFloor = 150.0f;
bool inSpeech = false;
int loudFrames = 0;
uint32_t silenceMs = 0, speechMs = 0;
uint64_t levelSum = 0;
uint32_t levelCount = 0;
uint32_t clipFrames = 0;             // кадры фразы с перегрузкой
uint64_t voicedSum = 0;              // уровень только по кадрам с голосом (без пауз и хвоста тишины)
uint32_t voicedCount = 0;
// живой индикатор для веб-интерфейса
volatile bool meterOn = false;
float meterPeak = 0;                 // максимум уровня за интервал
uint32_t meterSentAt = 0;
float hpPrevX = 0, hpPrevY = 0;      // фильтр постоянной составляющей

// ---------------- светодиод и усилитель ----------------
void led(bool on) {
  if (LED_PIN >= 0) digitalWrite(LED_PIN, on ? HIGH : LOW);
}

void ampEnable(bool on) {
  if (AMP_SD >= 0) digitalWrite(AMP_SD, on ? HIGH : LOW);
}

// ---------------- настройки: хранение ----------------
void setGain(float db) {
  cfg.micGainDb = constrain(db, 0.0f, 30.0f);
  micGain = powf(10.0f, cfg.micGainDb / 20.0f);
}

String defaultRoomId() {
  char buf[16];
  snprintf(buf, sizeof(buf), "room-%06x", (unsigned)((ESP.getEfuseMac() >> 24) & 0xFFFFFF));
  return String(buf);
}

void loadSettings() {
  prefs.begin("irene", true);
  cfg.server = prefs.getString("server", DEFAULT_SERVER);
  cfg.port = prefs.getUShort("port", DEFAULT_PORT);
  cfg.roomId = prefs.getString("room", defaultRoomId());
  cfg.roomName = prefs.getString("name", cfg.roomId);
  cfg.token = prefs.getString("token", "");
  volumePct = prefs.getUChar("volume", DEFAULT_VOLUME);
  cfg.micGainDb = prefs.isKey("gainf") ? prefs.getFloat("gainf", DEFAULT_MIC_GAIN_DB)
                                       : (float)prefs.getInt("gain", DEFAULT_MIC_GAIN_DB);
  cfg.vadLevel = prefs.getInt("vadlvl", DEFAULT_VAD_LEVEL);
  cfg.hangMs = prefs.getInt("hang", DEFAULT_VAD_HANG_MS);
  cfg.agc = prefs.getBool("agc", DEFAULT_AGC);
  cfg.agcMaxDb = prefs.getInt("agcmax", DEFAULT_AGC_MAX_DB);
  cfg.aec = prefs.getBool("aec", DEFAULT_AEC);
  cfg.ns = prefs.getBool("ns", DEFAULT_NS);
  prefs.end();
  setGain(cfg.micGainDb);
}

void saveSettings() {
  prefs.begin("irene", false);
  prefs.putString("server", cfg.server);
  prefs.putUShort("port", cfg.port);
  prefs.putString("room", cfg.roomId);
  prefs.putString("name", cfg.roomName);
  prefs.putString("token", cfg.token);
  prefs.putFloat("gainf", cfg.micGainDb);
  prefs.putInt("vadlvl", cfg.vadLevel);
  prefs.putInt("hang", cfg.hangMs);
  prefs.putBool("agc", cfg.agc);
  prefs.putInt("agcmax", cfg.agcMaxDb);
  prefs.putBool("aec", cfg.aec);
  prefs.putBool("ns", cfg.ns);
  prefs.end();
  gainDirty = false;
  gainSavedAt = millis();
}

void saveVolume() {
  prefs.begin("irene", false);
  prefs.putUChar("volume", (uint8_t)volumePct);
  prefs.end();
}

// ---------------- диагностика Wi-Fi и питания ----------------
volatile uint32_t wifiDrops = 0;          // сколько раз терялся Wi-Fi с момента включения
volatile uint8_t lastDropReason = 0;      // код причины последнего обрыва (см. wifiReasonText)
String resetReasonText;

const char* wifiReasonText(uint8_t r) {
  switch (r) {
    case 2: case 15: case 204: return "истекло время авторизации (слабый сигнал или неверный пароль)";
    case 4: case 200: return "роутер перестал видеть клиента (слабый сигнал/помехи)";
    case 8: return "клиент сам отключился";
    case 201: return "сеть не найдена";
    case 202: case 203: return "ошибка авторизации";
    case 34: case 205: return "роутер разорвал соединение";
    default: return "другая причина";
  }
}

String describeResetReason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "включение питания";
    case ESP_RST_BROWNOUT: return "ПРОСАДКА ПИТАНИЯ (brownout)";
    case ESP_RST_PANIC: return "сбой прошивки";
    case ESP_RST_INT_WDT: case ESP_RST_TASK_WDT: case ESP_RST_WDT: return "сторожевой таймер";
    case ESP_RST_SW: return "перезагрузка программой";
    default: return "другая";
  }
}

void onWifiEvent(arduino_event_id_t event, arduino_event_info_t info) {
  if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
    wifiDrops = wifiDrops + 1;
    lastDropReason = info.wifi_sta_disconnected.reason;
    Serial.printf("Wi-Fi потерян (%u-й раз): код %u — %s\n", (unsigned)wifiDrops, lastDropReason,
                  wifiReasonText(lastDropReason));
  } else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
    Serial.printf("Wi-Fi есть: IP %s, сигнал %d дБм\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  }
}

// ---------------- Wi-Fi и страница настройки ----------------
// Первый запуск: точка доступа "Irene-XXXXXX", страница настройки на 192.168.4.1.
// После подключения к домашней сети та же страница доступна по IP клиента (http://<IP>/ -> "Setup").
WiFiManager wm;
WiFiManagerParameter *pServer, *pPort, *pRoom, *pName, *pToken, *pGain, *pVad, *pHang, *pAgc, *pAgcMax;
char portStr[8], gainStr[8], vadStr[8], hangStr[8], agcStr[4], agcMaxStr[8];
volatile bool portalSaved = false;      // сохранили настройки при первой настройке (AP)
volatile bool liveSaved = false;        // сохранили настройки на странице в домашней сети

void applyParams() {
  cfg.server = pServer->getValue();
  cfg.port = (uint16_t)atoi(pPort->getValue());
  if (cfg.port == 0) cfg.port = DEFAULT_PORT;
  cfg.roomId = pRoom->getValue();
  cfg.roomName = pName->getValue();
  String token = pToken->getValue();
  cfg.server.trim(); cfg.roomId.trim(); cfg.roomName.trim(); token.trim();
  if (token.length()) cfg.token = token;     // пустое поле — ключ не меняем
  if (cfg.roomId.isEmpty()) cfg.roomId = defaultRoomId();
  if (cfg.roomName.isEmpty()) cfg.roomName = cfg.roomId;
  setGain(atof(pGain->getValue()));
  cfg.vadLevel = constrain(atoi(pVad->getValue()), 30, 5000);
  cfg.hangMs = constrain(atoi(pHang->getValue()), 300, 3000);
  cfg.agc = atoi(pAgc->getValue()) != 0;
  cfg.agcMaxDb = constrain(atoi(pAgcMax->getValue()), 0, 30);
  saveSettings();
}

void setupWifi() {
  snprintf(portStr, sizeof(portStr), "%u", cfg.port);
  pServer = new WiFiManagerParameter("server", "Адрес сервера Ирины (IP приставки)", cfg.server.c_str(), 64);
  pPort = new WiFiManagerParameter("port", "Порт", portStr, 6);
  pRoom = new WiFiManagerParameter("room", "ID комнаты латиницей (kitchen)", cfg.roomId.c_str(), 32);
  pName = new WiFiManagerParameter("name", "Название комнаты как в Home Assistant (Кухня)", cfg.roomName.c_str(), 64);
  // ключ не показываем на странице: пустое поле при сохранении оставляет прежний
  pToken = new WiFiManagerParameter("token", cfg.token.length() ? "Ключ (оставьте пустым, чтобы не менять)"
                                                                 : "Ключ (token из runva_rooms.json)", "", 64);
  wm.addParameter(pServer);
  wm.addParameter(pPort);
  wm.addParameter(pRoom);
  wm.addParameter(pName);
  wm.addParameter(pToken);
  snprintf(gainStr, sizeof(gainStr), "%d", (int)roundf(cfg.micGainDb));
  snprintf(vadStr, sizeof(vadStr), "%d", cfg.vadLevel);
  snprintf(hangStr, sizeof(hangStr), "%d", cfg.hangMs);
  snprintf(agcStr, sizeof(agcStr), "%d", cfg.agc ? 1 : 0);
  snprintf(agcMaxStr, sizeof(agcMaxStr), "%d", cfg.agcMaxDb);
  pGain = new WiFiManagerParameter("gain", "Усиление микрофона, дБ (0-30; при автоусилении — начальное)", gainStr, 4);
  pAgc = new WiFiManagerParameter("agc", "Автоусиление: 1 — включено, 0 — выключено", agcStr, 2);
  pAgcMax = new WiFiManagerParameter("agcmax", "Автоусиление не больше, дБ (0-30)", agcMaxStr, 3);
  pVad = new WiFiManagerParameter("vad", "Порог речи (30-5000; меньше — чувствительнее)", vadStr, 5);
  pHang = new WiFiManagerParameter("hang", "Пауза конца фразы, мс (300-3000)", hangStr, 5);
  wm.addParameter(pGain);
  wm.addParameter(pAgc);
  wm.addParameter(pAgcMax);
  wm.addParameter(pVad);
  wm.addParameter(pHang);
  wm.setSaveConfigCallback([]() { portalSaved = true; });
  wm.setSaveParamsCallback([]() { if (WiFi.status() == WL_CONNECTED && !wm.getConfigPortalActive()) liveSaved = true;
                                  else portalSaved = true; });
  wm.setTitle("Ирина");
  wm.setConfigPortalTimeout(300);
  wm.setConnectTimeout(20);

  String ap = "Irene-" + cfg.roomId.substring(cfg.roomId.length() > 6 ? cfg.roomId.length() - 6 : 0);
  led(true);
  if (!wm.autoConnect(ap.c_str())) {
    Serial.println("Wi-Fi не настроен, перезагрузка");
    delay(1000);
    ESP.restart();
  }
  led(false);
  if (portalSaved) applyParams();

  WiFi.setSleep(false);   // меньше задержка и пропуски звука
  WiFi.setAutoReconnect(true);
  WiFi.setTxPower(WIFI_TX_POWER);
  WiFi.onEvent(onWifiEvent);
  int rssi = WiFi.RSSI();
  Serial.printf("Сигнал Wi-Fi: %d дБм — %s\n", rssi, rssi > -60 ? "отличный" : rssi > -70 ? "нормальный" :
                rssi > -78 ? "слабый, возможны обрывы" : "очень слабый — переставьте клиент или роутер");
  Serial.printf("Wi-Fi: %s, IP %s\n", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());

  // страница настройки в домашней сети: отдельная страница "Setup" с параметрами
  static const char* menu[] = {"param", "wifi", "info", "restart"};
  wm.setParamsPage(true);
  wm.setMenu(menu, 4);
  wm.setConfigPortalBlocking(false);
  wm.startWebPortal();
  Serial.printf("Настройки клиента: http://%s/  (кнопка Setup)\n", WiFi.localIP().toString().c_str());
}

void handleWebPortal() {
  wm.process();
  if (liveSaved) {
    liveSaved = false;
    applyParams();
    Serial.println("Настройки сохранены, перезагрузка");
    delay(500);
    ESP.restart();
  }
}

void checkResetButton() {
  static uint32_t pressedAt = 0;
  if (digitalRead(RESET_BUTTON) == LOW) {
    if (pressedAt == 0) pressedAt = millis();
    if (millis() - pressedAt > 5000) {
      Serial.println("Сброс настроек");
      for (int i = 0; i < 6; i++) { led(i % 2); delay(150); }
      prefs.begin("irene", false);
      prefs.clear();
      prefs.end();
      WiFiManager wm;
      wm.resetSettings();
      ESP.restart();
    }
  } else {
    pressedAt = 0;
  }
}

// ---------------- I2S ----------------
void setupMic() {
  i2s_config_t c = {};
  c.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  c.sample_rate = SAMPLE_RATE;
  c.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  // читаем оба слота: на ESP32 и ESP32-S3 «левый» канал старого драйвера оказывается разным слотом,
  // а пустой слот даёт тишину с редкими щелчками — живой выбирается автоматически
  c.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  c.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  c.intr_alloc_flags = 0;
  c.I2S_DMA_COUNT = 8;
  c.I2S_DMA_LEN = FRAME;
  c.use_apll = false;
  ESP_ERROR_CHECK(i2s_driver_install(MIC_PORT, &c, 0, NULL));
  i2s_pin_config_t p = {};
  p.mck_io_num = I2S_PIN_NO_CHANGE;
  p.bck_io_num = MIC_SCK;
  p.ws_io_num = MIC_WS;
  p.data_out_num = I2S_PIN_NO_CHANGE;
  p.data_in_num = MIC_SD;
  ESP_ERROR_CHECK(i2s_set_pin(MIC_PORT, &p));
  // Помехоустойчивость шины микрофона:
  //  - SD подтягиваем к земле (так требует даташит INMP441: в чужом слоте выход микрофона отключён, и
  //    «висящая» линия ловит наводки);
  //  - тактовые линии — с минимальной силой выхода: фронты мягче, меньше «звона» на проводах,
  //    из-за которого микрофон видит лишние такты и отдаёт битые отсчёты (щелчки).
  gpio_pulldown_en((gpio_num_t)MIC_SD);
  gpio_set_drive_capability((gpio_num_t)MIC_SCK, GPIO_DRIVE_CAP_0);
  gpio_set_drive_capability((gpio_num_t)MIC_WS, GPIO_DRIVE_CAP_0);
}

void setupAmp() {
  i2s_config_t c = {};
  c.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
  c.sample_rate = AMP_RATE;
  c.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  c.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;   // моно дублируем в оба канала
  c.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  c.intr_alloc_flags = 0;
  // небольшие буферы: задержка вывода известна точно (синхронизация музыки между комнатами)
  c.I2S_DMA_COUNT = AMP_DMA_BUFS;
  c.I2S_DMA_LEN = BLOCK;
  c.use_apll = false;
  c.tx_desc_auto_clear = true;                     // при нехватке данных — тишина, а не треск
  ESP_ERROR_CHECK(i2s_driver_install(AMP_PORT, &c, 0, NULL));
  i2s_pin_config_t p = {};
  p.mck_io_num = I2S_PIN_NO_CHANGE;
  p.bck_io_num = AMP_BCLK;
  p.ws_io_num = AMP_LRC;
  p.data_out_num = AMP_DIN;
  p.data_in_num = I2S_PIN_NO_CHANGE;
  ESP_ERROR_CHECK(i2s_set_pin(AMP_PORT, &p));
  // мягкие фронты на линиях усилителя: меньше помех соседним проводам микрофона
  gpio_set_drive_capability((gpio_num_t)AMP_BCLK, GPIO_DRIVE_CAP_0);
  gpio_set_drive_capability((gpio_num_t)AMP_LRC, GPIO_DRIVE_CAP_0);
  gpio_set_drive_capability((gpio_num_t)AMP_DIN, GPIO_DRIVE_CAP_0);
  i2s_zero_dma_buffer(AMP_PORT);
}

// ---------------- воспроизведение (отдельная задача) ----------------
// Каждые 11,6 мс — блок: ответ Ирины (из audioBuf) + музыка (из snapSched по расписанию) -> усилитель.
// Блок, записанный сейчас, зазвучит через AMP_DMA_BUFS буферов DMA — по этому времени выбирается музыка
// и по нему же эхоподавление сопоставляет микрофон с тем, что играет динамик.
void playerTask(void*) {
  static int16_t tts[BLOCK], mus[BLOCK * 2], out[BLOCK * 2];
  static int16_t ttsFifo[BLOCK * 2], ttsIn[512];
  static snap::Resampler ttsRes;  // голос Ирины (16–22 кГц моно) -> частота усилителя
  int ttsHave = 0;
  bool ttsReady = false;          // ответ накопил запас и играет
  bool active = false;            // усилитель включён, блоки идут
  uint32_t quietSince = 0, statsAt = 0;
  int64_t lastWriteUs = 0;
  float musGain = 0;              // текущее усиление музыки (плавно идёт к цели: без щелчков)
  for (;;) {
    bool music = snapSched.used() > 0;
    if (!playing && !music) {
      ttsReady = false;
      if (active && millis() - quietSince > 3000) {   // тишина 3 с — выключаем усилитель
        ampEnable(false);
        active = false;
      }
      if (!active) {
        snapSched.checkFlush();
        vTaskDelay(pdMS_TO_TICKS(5));
        continue;
      }
    }
    if (!active) {
      active = true;
      quietSince = millis();
      lastWriteUs = esp_timer_get_time();
      ampEnable(true);
    }

    // ответ Ирины: пересчитываем в частоту усилителя
    memset(tts, 0, sizeof(tts));
    int nt = 0;
    if (playing) {
      size_t avail = xStreamBufferBytesAvailable(audioBuf);
      if (!ttsReady && (avail >= (size_t)pendingRate * 2 * 150 / 1000 || ttsEnded)) {   // запас 150 мс
        ttsReady = true;
        ttsRes.setup(pendingRate, 1, AMP_RATE, 1);
        ttsHave = 0;
      }
      if (ttsReady) {
        while (ttsHave < BLOCK) {
          size_t bytes = xStreamBufferBytesAvailable(audioBuf);
          if (bytes < 2) break;
          int space = BLOCK * 2 - ttsHave;
          int want = (int)((int64_t)(space - 2) * pendingRate / AMP_RATE);
          if (want < 1) break;
          if (want > (int)(sizeof(ttsIn) / 2)) want = sizeof(ttsIn) / 2;
          if ((size_t)want * 2 > bytes) want = bytes / 2;
          int got = xStreamBufferReceive(audioBuf, ttsIn, want * 2, 0) / 2;
          if (got <= 0) break;
          ttsHave += ttsRes.process(ttsIn, got, ttsFifo + ttsHave, space);
        }
        if (ttsHave >= BLOCK || (ttsEnded && ttsHave > 0)) {
          nt = ttsHave < BLOCK ? ttsHave : BLOCK;
          memcpy(tts, ttsFifo, nt * 2);
          memmove(ttsFifo, ttsFifo + nt, (ttsHave - nt) * 2);
          ttsHave -= nt;
        } else if (ttsEnded && ttsHave == 0 && xStreamBufferIsEmpty(audioBuf)) {
          playEndMs = millis();
          playing = false;
          ttsReady = false;
          led(false);
        } else if (!music) {
          vTaskDelay(pdMS_TO_TICKS(2));    // ждём звук ответа; без музыки блоки писать незачем
          continue;
        }
      } else if (!music) {
        vTaskDelay(pdMS_TO_TICKS(2));
        continue;
      }
    }

    // музыка
    int nm = 0;
    int64_t outAt = lastWriteUs + (int64_t)AMP_DMA_BUFS * BLOCK * 1000000 / AMP_RATE;
    if (snapFormatOk && snapTime.ready()) {
      nm = snap::fillBlock(snapSched, mus, BLOCK, outAt, syncStats);
    } else {
      memset(mus, 0, sizeof(mus));
      snapSched.checkFlush();
    }

    // громкость: голос — volumePct, музыка — громкость комнаты в snapserver; пока Ирина слушает и отвечает — тише
    int vol = volumePct * VOLUME_LIMIT_PCT / 100;
    int32_t kt = vol * vol;                       // квадратичная шкала, 0..10000
    float target = 0;
    if (!snapMuted) {
      float mv = snapVolume * VOLUME_LIMIT_PCT / 100.0f;
      target = mv * mv / 10000.0f;
    }
    if (playing || millis() < wakeUntil) target *= (MUSIC_DUCK_PCT / 100.0f) * (MUSIC_DUCK_PCT / 100.0f);
    float g0 = musGain, g1 = musGain + (target - musGain) * 0.25f;   // ~50 мс до новой громкости
    if (fabsf(g1 - target) < 0.0005f) g1 = target;
    musGain = g1;
    for (int i = 0; i < BLOCK; i++) {
      float g = g0 + (g1 - g0) * i / BLOCK;
      int32_t t = (int32_t)tts[i] * kt / 10000;
      int32_t l, r;
      if (AMP_CH == 2) {
        l = t + (int32_t)(mus[2 * i] * g);
        r = t + (int32_t)(mus[2 * i + 1] * g);
      } else {
        l = r = t + (int32_t)(mus[i] * g);
      }
      out[2 * i] = (int16_t)constrain(l, -32768, 32767);
      out[2 * i + 1] = (int16_t)constrain(r, -32768, 32767);
    }
#if MIC_AEC
    micproc::refWrite(out, BLOCK, AMP_RATE, outAt);   // опорный сигнал для эхоподавления
#endif
    size_t written;
    i2s_write(AMP_PORT, out, sizeof(out), &written, portMAX_DELAY);
    lastWriteUs = esp_timer_get_time();           // очередь DMA снова полна: следующий блок — через AMP_DMA_BUFS
    if (nt > 0 || (nm > 0 && target > 0)) quietSince = millis();
    if (nm > 0 && target > 0) musicHeardAt = millis();

    if (nm > 0 && millis() - statsAt > 60000) {
      statsAt = millis();
      Serial.printf("Музыка: расхождение %.1f мс, поправок %u, пропущено %u кадров, громкость %d%s\n",
                    syncStats.lastErrUs / 1000.0f, (unsigned)syncStats.adjusts, (unsigned)syncStats.skipped,
                    (int)snapVolume, snapMuted ? " (выкл)" : "");
    }
  }
}

// ---------------- Snapcast (отдельная задача на ядре 0) ----------------
int64_t snapRxUs = 0;

void onSnapMessage(const snap::Header& h, const uint8_t* p, uint32_t len) {
  switch (h.type) {
    case snap::kServerSettings: {
      snap::Settings st;
      st.bufferMs = snapBufferMs; st.latencyMs = snapLatencyMs; st.volume = snapVolume; st.muted = snapMuted;
      if (snap::parseServerSettings(p, len, st)) {
        if (st.volume != snapVolume || st.muted != snapMuted)
          Serial.printf("Музыка: громкость %d%s\n", st.volume, st.muted ? " (выкл)" : "");
        snapBufferMs = st.bufferMs; snapLatencyMs = st.latencyMs;
        snapVolume = constrain(st.volume, 0, 100); snapMuted = st.muted;
      }
      break;
    }
    case snap::kCodecHeader: {
      char codec[16];
      snap::Format f;
      bool ok = snap::parseCodecHeader(p, len, codec, sizeof(codec), f);
      snapSched.requestFlush();                   // новый поток (Ирина переключила линию)
      snapFormatOk = false;
      if (ok && f.channels <= 2 && f.rate >= 8000 && f.rate <= 48000) {
        snapRes.setup(f.rate, f.channels, AMP_RATE, AMP_CH);
        snapFormatOk = true;
        Serial.printf("Музыка: поток %s %u Гц, каналов %u -> %d Гц, %s\n", codec, (unsigned)f.rate, (unsigned)f.channels, AMP_RATE,
                      AMP_CH == 2 ? "стерео" : "моно");
      } else {
        Serial.printf("Музыка: формат %s %u Гц %u бит не поддерживается — нужен pcm 44100:16:2 (snapserver.conf)\n",
                      codec, (unsigned)f.rate, (unsigned)f.bits);
      }
      break;
    }
    case snap::kTime: {
      if (len < 8) break;
      int64_t latency = (int64_t)(int32_t)snap::rd32(p) * 1000000 + (int32_t)snap::rd32(p + 4);
      snapTime.onTimeReply(latency, (int64_t)h.sentSec * 1000000 + h.sentUsec, snapRxUs);
      break;
    }
    case snap::kWireChunk: {
      if (!snapFormatOk || !snapTime.ready()) break;
      int64_t ts;
      const uint8_t* d;
      uint32_t dl;
      if (!snap::parseWireChunk(p, len, ts, d, dl)) break;
      static int16_t conv[2400 * 2];             // 20 мс 48 кГц -> 44,1 кГц стерео с запасом
      uint32_t n = snapRes.process((const int16_t*)d, dl / 2 / snapRes.inCh, conv, 2400);
      int64_t playAt = snapTime.serverToLocal(ts) + (int64_t)(snapBufferMs - snapLatencyMs) * 1000;
      static uint32_t fullLogged = 0;
      if (!snapSched.push(conv, n, playAt) && millis() - fullLogged > 10000) {
        fullLogged = millis();
        Serial.println("Музыка: очередь полна — кусок пропущен");
      }
      break;
    }
    default:
      break;
  }
}

void snapSendHello() {
  JsonDocument doc;
  doc["Arch"] = "xtensa";
  doc["ClientName"] = "IreneClient";
  doc["HostName"] = cfg.roomName;
  doc["ID"] = cfg.roomId;                  // ID комнаты: по нему Ирина узнаёт, где играет музыка
  doc["Instance"] = 1;
  doc["MAC"] = WiFi.macAddress();
  doc["OS"] = ESP.getChipModel();
  doc["SnapStreamProtocolVersion"] = 2;
  doc["Version"] = "0.31.0";
  String js;
  serializeJson(doc, js);
  static uint8_t buf[600];
  if (js.length() > sizeof(buf) - 40) return;
  int n = snap::buildHello(buf, 0, esp_timer_get_time(), js.c_str());
  snapSock.write(buf, n);
}

void snapTask(void*) {
  static uint8_t rx[2048];
  uint8_t tx[64];
  uint16_t msgId = 1;
  uint32_t nextTry = 0, lastTimeReq = 0, failures = 0;
  for (;;) {
    if (WiFi.status() != WL_CONNECTED || snapHost.isEmpty()) {
      vTaskDelay(pdMS_TO_TICKS(500));
      continue;
    }
    if (!snapSock.connected()) {
      if (snapConnected) {
        snapConnected = false;
        snapFormatOk = false;
        snapSched.requestFlush();
        Serial.println("Музыка: связь с snapserver потеряна");
      }
      if ((int32_t)(millis() - nextTry) < 0) {
        vTaskDelay(pdMS_TO_TICKS(200));
        continue;
      }
      if (!snapSock.connect(snapHost.c_str(), SNAP_PORT, 3000)) {
        failures++;
        if (failures == 1)
          Serial.printf("Музыка: нет snapserver на %s:%d — музыки в этой комнате не будет (пробую снова)\n",
                        snapHost.c_str(), SNAP_PORT);
        nextTry = millis() + min<uint32_t>(30000, 3000 * failures);
        continue;
      }
      failures = 0;
      snapSock.setNoDelay(true);
      snapParser->reset();
      snapTime.reset();
      snapSched.requestFlush();
      snapSendHello();
      snapConnected = true;
      lastTimeReq = 0;
      Serial.printf("Музыка: подключено к snapserver %s:%d\n", snapHost.c_str(), SNAP_PORT);
    }
    // синхронизация часов: сначала часто, потом раз в секунду
    if (millis() - lastTimeReq >= (uint32_t)(snapTime.count < 10 ? 100 : 1000)) {
      lastTimeReq = millis();
      int n = snap::buildTime(tx, msgId++, esp_timer_get_time());
      snapSock.write(tx, n);
    }
    int avail = snapSock.available();
    if (avail <= 0) {
      vTaskDelay(1);                       // 1 мс: время получения ответа на Time нужно точно
      continue;
    }
    int n = snapSock.read(rx, min(avail, (int)sizeof(rx)));
    snapRxUs = esp_timer_get_time();
    if (n > 0) snapParser->feed(rx, n, onSnapMessage);
  }
}

bool setupSnap() {
  // очередь музыки и сборщик сообщений — в PSRAM, если есть
  uint32_t frames = (uint32_t)AMP_RATE * SNAP_RING_MS / 1000;
  uint32_t caps = psramFound() ? MALLOC_CAP_SPIRAM : MALLOC_CAP_8BIT;
  int16_t* ring = (int16_t*)heap_caps_malloc(frames * 2 * AMP_CH, caps);
  void* pm = heap_caps_malloc(sizeof(snap::Parser), caps);
  if (!ring || !pm) {
    Serial.println("!! Не хватило памяти под музыку — клиент работает без неё");
    if (ring) heap_caps_free(ring);
    if (pm) heap_caps_free(pm);
    return false;
  }
  snapParser = new (pm) snap::Parser();
  snapSched.init(ring, frames, AMP_CH, AMP_RATE);
  xTaskCreatePinnedToCore(snapTask, "snap", 6144, NULL, 3, NULL, 0);
  return true;
}

// ---------------- WebSocket ----------------
void fillMicState(JsonDocument& doc) {
  doc["gain"] = roundf(cfg.micGainDb * 10) / 10;
  doc["vad"] = cfg.vadLevel;
  doc["hang"] = cfg.hangMs;
  doc["agc"] = cfg.agc;
  doc["agc_max"] = cfg.agcMaxDb;
#if MIC_AEC
  if (micproc::ready) {
    doc["aec"] = cfg.aec;
    doc["ns"] = cfg.ns;
  }
#endif
}

void sendHello() {
  JsonDocument doc;
  doc["type"] = "hello";
  doc["room"] = cfg.roomId;
  doc["name"] = cfg.roomName;
  doc["token"] = cfg.token;
  doc["rate"] = SAMPLE_RATE;
  doc["fw"] = FW_VERSION;
  doc["volume"] = (int)volumePct;
  doc["rssi"] = WiFi.RSSI();                 // для лога сервера: качество связи и причина перезагрузки
  doc["reset"] = resetReasonText;
  doc["drops"] = (uint32_t)wifiDrops;
  doc["chip"] = ESP.getChipModel();
  doc["psram"] = (uint32_t)(ESP.getPsramSize() / 1024);
  doc["mic_slot"] = micSlot;
  JsonArray caps = doc["caps"].to<JsonArray>();   // что умеет прошивка: сервер покажет нужные настройки
  caps.add("config");
  caps.add("meter");
  caps.add("agc");
  if (snapParser) caps.add("snap");        // музыка через Snapcast: сервер шлёт голос на 22050 Гц
#if MIC_AEC
  if (micproc::ready) caps.add("aec");     // эхо- и шумоподавление: переключатели в веб-интерфейсе
#endif
  fillMicState(doc);
  String s;
  serializeJson(doc, s);
  ws.sendTXT(s);
}


void sendMicState() {
  JsonDocument doc;
  doc["type"] = "state";
  fillMicState(doc);
  String s;
  serializeJson(doc, s);
  ws.sendTXT(s);
}

// настройки микрофона с сервера (веб-интерфейс): применяются сразу, без перезагрузки
void applyServerConfig(JsonDocument& doc) {
  if (doc["gain"].is<float>()) setGain(doc["gain"].as<float>());
  if (doc["vad"].is<int>()) cfg.vadLevel = constrain(doc["vad"].as<int>(), 30, 5000);
  if (doc["hang"].is<int>()) cfg.hangMs = constrain(doc["hang"].as<int>(), 300, 3000);
  if (doc["agc"].is<bool>()) cfg.agc = doc["agc"].as<bool>();
  if (doc["agc_max"].is<int>()) cfg.agcMaxDb = constrain(doc["agc_max"].as<int>(), 0, 30);
  if (doc["aec"].is<bool>()) cfg.aec = doc["aec"].as<bool>();
  if (doc["ns"].is<bool>()) cfg.ns = doc["ns"].as<bool>();
  if (cfg.agc && cfg.micGainDb > cfg.agcMaxDb) setGain(cfg.agcMaxDb);
  saveSettings();
  Serial.printf("Настройки с сервера: эхоподавление %s, шумоподавление %s\n", cfg.aec ? "вкл" : "выкл",
                cfg.ns ? "вкл" : "выкл");
  Serial.printf("Настройки с сервера: усиление %.1f дБ%s, порог %d, пауза %d мс\n", cfg.micGainDb,
                cfg.agc ? " (авто)" : "", cfg.vadLevel, cfg.hangMs);
  sendMicState();
}

void sendMeter(float level) {
  char buf[128];
  snprintf(buf, sizeof(buf), "{\"type\":\"meter\",\"level\":%d,\"noise\":%d,\"gain\":%.1f,\"speech\":%s}",
           (int)level, (int)noiseFloor, cfg.micGainDb, inSpeech ? "true" : "false");
  ws.sendTXT(buf);
}

void sendVad(bool start, uint32_t level) {
  char buf[64];
  if (start) snprintf(buf, sizeof(buf), "{\"type\":\"vad\",\"state\":\"start\"}");
  else snprintf(buf, sizeof(buf), "{\"type\":\"vad\",\"state\":\"end\",\"level\":%u}", (unsigned)level);
  ws.sendTXT(buf);
}

void onText(uint8_t* payload, size_t length) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, length)) return;
  const char* t = doc["type"] | "";
  if (!strcmp(t, "tts_start")) {
    if (!playing) xStreamBufferReset(audioBuf);
    pendingRate = doc["rate"] | 22050;
    ttsEnded = false;
    playing = true;
    led(true);
  } else if (!strcmp(t, "tts_end")) {
    ttsEnded = true;
  } else if (!strcmp(t, "wake")) {
    wakeUntil = millis() + 6000;
    led(true);
  } else if (!strcmp(t, "idle")) {
    wakeUntil = 0;
    if (!playing) led(false);
  } else if (!strcmp(t, "volume") || !strcmp(t, "welcome")) {
    int v = doc[!strcmp(t, "volume") ? "value" : "volume"] | (int)volumePct;
    if (!strcmp(t, "volume")) {
      volumePct = constrain(v, 0, 100);
      saveVolume();
    }
    Serial.printf("Сервер: %s, громкость %d\n", t, (int)volumePct);
  } else if (!strcmp(t, "config")) {
    applyServerConfig(doc);
  } else if (!strcmp(t, "meter")) {
    meterOn = doc["enable"] | false;          // сервер включает индикатор, пока открыта страница
  } else if (!strcmp(t, "error")) {
    Serial.printf("Ошибка от сервера: %s\n", (const char*)(doc["message"] | ""));
  }
}

void onWsEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      wsConnected = true;
      Serial.println("Подключено к серверу Ирины");
      sendHello();
      break;
    case WStype_DISCONNECTED:
      if (wsConnected) Serial.println("Отключено от сервера");
      wsConnected = false;
      inSpeech = false;
      meterOn = false;
      break;
    case WStype_TEXT:
      onText(payload, length);
      break;
    case WStype_BIN:
      if (playing) {
        // если буфер полон — ждём: TCP притормозит сервер, и звук не потеряется
        // сервер шлёт звук в темпе воспроизведения, поэтому буфер не переполняется;
        // долго не ждём, чтобы не перестать отвечать на пинги и не потерять соединение
        if (xStreamBufferSend(audioBuf, payload, length, pdMS_TO_TICKS(100)) < length)
          Serial.println("Буфер звука переполнен — кусок ответа пропущен");
      }
      break;
    default:
      break;
  }
}

String resolveServer() {
  String host = cfg.server;
  if (host.endsWith(".local")) {
    String name = host.substring(0, host.length() - 6);
    IPAddress ip = MDNS.queryHost((char*)name.c_str(), 3000);
    if (ip[0] != 0) return ip.toString();
    Serial.printf("Не удалось найти %s через mDNS\n", host.c_str());
  }
  return host;
}

void setupWebSocket() {
  String host = resolveServer();
  snapHost = host;                       // snapserver — на той же приставке
  Serial.printf("Сервер: ws://%s:%u/  комната: %s (%s)\n", host.c_str(), cfg.port,
                cfg.roomName.c_str(), cfg.roomId.c_str());
  ws.begin(host.c_str(), cfg.port, "/");
  ws.onEvent(onWsEvent);
  ws.setReconnectInterval(3000);
  ws.enableHeartbeat(15000, 3000, 2);
}

// ---------------- микрофон и детектор речи ----------------
void flushPacket() {
  if (packetFrames > 0 && wsConnected) {
    ws.sendBIN((uint8_t*)packet, packetFrames * FRAME * sizeof(int16_t));
  }
  packetFrames = 0;
}

void queueFrame(const int16_t* f) {
  memcpy(packet + packetFrames * FRAME, f, FRAME * sizeof(int16_t));
  if (++packetFrames >= FRAMES_PER_PACKET) flushPacket();
}

// Автоусиление: после фразы сдвигаем усиление к цели. Посреди фразы не трогаем (мешает распознаванию),
// короткие щелчки не учитываем, шум не поднимаем выше порога, при перегрузке снижаем сразу.
void agcUpdate(uint32_t level) {
  if (!cfg.agc || level == 0) return;
  if (speechMs < 400 || voicedCount < 10) return;             // щелчок, хлопок, стук — не повод менять
  float before = cfg.micGainDb;
  float step;
  if (level > AGC_CLIP_LEVEL || (clipFrames >= AGC_CLIP_FRAMES && level > AGC_TARGET)) {
    step = -AGC_STEP_MAX_DB;                                  // речь с искажениями — вниз без раздумий
  } else {
    float err = 20.0f * log10f((float)AGC_TARGET / (float)level);
    // вверх — быстрее (тихую речь важнее не пропустить), вниз — осторожнее
    step = constrain(err > 0 ? err / 2.0f : err / 3.0f, -AGC_STEP_MAX_DB, AGC_STEP_MAX_DB);
    if (fabsf(step) < 0.5f) return;                           // и так хорошо
  }
  float target = before + step;
  if (step > 0) {
    // потолок по шуму: порог речи = шум × VAD_START_RATIO должен остаться заметно ниже уровня речи,
    // иначе тихую речь перестанет быть слышно на фоне шума
    float noiseCeil = before + 20.0f * log10f((float)AGC_NOISE_MAX / max(noiseFloor, 1.0f));
    target = min(target, max(noiseCeil, before));
    target = min(target, (float)cfg.agcMaxDb);
  }
  target = constrain(target, (float)AGC_MIN_DB, 30.0f);
  if (fabsf(target - before) < 0.1f) return;
  setGain(target);
  noiseFloor *= powf(10.0f, (target - before) / 20.0f);       // шум в новом масштабе
  gainDirty = true;
  Serial.printf("Автоусиление: %.1f -> %.1f дБ (уровень фразы %u, цель %d)\n", before, target, (unsigned)level, AGC_TARGET);
}

void endSpeech() {
  flushPacket();
  // уровень фразы — по кадрам с голосом: паузы между словами и хвост тишины его не занижают
  uint32_t level = voicedCount ? (uint32_t)(voicedSum / voicedCount)
                               : (levelCount ? (uint32_t)(levelSum / levelCount) : 0);
  if (wsConnected) sendVad(false, level);
  // для настройки чувствительности: хорошая речь — уровень 1500–5000, шум — в разы меньше порога
  Serial.printf("Фраза %.1f с: уровень речи %u, шум %.0f, порог %d, усиление %.1f дБ%s, убрано щелчков %u\n",
                speechMs / 1000.0f, (unsigned)level, noiseFloor, cfg.vadLevel, cfg.micGainDb, cfg.agc ? " (авто)" : "",
                (unsigned)glitchCount);
  glitchCount = 0;
  if (!speechWithMusic) agcUpdate(level);   // поверх музыки уровень фразы ничего не говорит о голосе
  speechWithMusic = false;
  inSpeech = false;
  loudFrames = 0;
  if (millis() > wakeUntil) led(false);
}

// Какой слот I2S живой. У микрофона даже в тишине есть собственный шум — соседние отсчёты почти всегда различаются;
// пустой слот стоит на месте (0 / -1) и изредка даёт выбросы на всю шкалу. Считаем долю «живых» отсчётов (0..1).
void trackMicSlot() {
  if (MIC_SLOT >= 0) return;                         // слот задан вручную в config.h
  for (int ch = 0; ch < 2; ch++) {
    float acc = 0;
    int32_t prev = micRaw[ch] >> 8;
    for (int i = 1; i < FRAME; i++) {
      int32_t cur = micRaw[2 * i + ch] >> 8;
      int32_t d = cur - prev;
      if (d < 0) d = -d;
      if (d > 0 && d < (1 << 20)) acc += 1;    // «живой» отсчёт; выбросы почти на всю шкалу не считаем
      prev = cur;
    }
    slotActivity[ch] += acc / FRAME;
  }
  if (++slotFrames < 50) return;                     // оцениваем по секунде звука
  int other = 1 - micSlot;
  bool switchIt = slotActivity[other] > slotActivity[micSlot] * 3 + 1;   // в сумме за 50 кадров
  slotVotes = switchIt ? slotVotes + 1 : 0;
  static bool reported = false;
  if (!reported || slotVotes >= 2) {
    if (slotVotes >= 2 || (!reported && switchIt)) { micSlot = other; slotVotes = 0; noiseFloor = 150; }
    Serial.printf("Микрофон: слот %d (живых отсчётов: %.0f%% / %.0f%%)%s\n", micSlot, slotActivity[0] * 2,
                  slotActivity[1] * 2, max(slotActivity[0], slotActivity[1]) / 50 < 0.1f
                  ? " — оба слота молчат: проверьте провода SD/WS/SCK и питание микрофона" : "");
    reported = true;
  }
  slotActivity[0] = slotActivity[1] = 0;
  slotFrames = 0;
}

// Одиночные сбои шины I2S: изредка отсчёт (или два подряд) читается с ошибкой в старших битах — на записи это
// щелчок на всю шкалу. Отсчёт заменяется медианой пяти соседних, если отскочил от неё в 16 раз дальше среднего
// шага сигнала в этом кадре: речь (даже громкая и с шипящими) так не прыгает, сбой — один-два отсчёта на фоне
// плавного сигнала. Порог следует за громкостью, поэтому громкую речь фильтр не трогает. Задержка — 2 отсчёта.

static int32_t median5(int32_t a, int32_t b, int32_t c, int32_t d, int32_t e) {
  int32_t v[5] = {a, b, c, d, e};
  for (int i = 1; i < 5; i++) {                      // вставками: 5 чисел
    int32_t k = v[i];
    int j = i - 1;
    while (j >= 0 && v[j] > k) { v[j + 1] = v[j]; j--; }
    v[j + 1] = k;
  }
  return v[2];
}

int deglitch(int32_t* out) {
  int32_t ext[FRAME + 4];
  memcpy(ext, glitchTail, sizeof(glitchTail));
  for (int i = 0; i < FRAME; i++) ext[i + 4] = micRaw[2 * i + micSlot] >> 8;
  int fixed = 0;
  int64_t steps = 0;
  for (int i = 1; i < FRAME + 4; i++) steps += abs(ext[i] - ext[i - 1]);
  int64_t thr = max((int64_t)GLITCH_MIN, steps * GLITCH_RATIO / (FRAME + 3));
  // меньше этого (≈300 на выходе при текущем усилении) отскок не считается сбоем — это обычный шум
  int32_t minDev = (int32_t)(300.0f * 256.0f / max(micGain, 0.1f));
  // выход i соответствует ext[i + 2]: у каждого отсчёта есть по два соседа с каждой стороны
  for (int i = 0; i < FRAME; i++) {
    int32_t x = ext[i + 2];
    int32_t m = median5(ext[i], ext[i + 1], x, ext[i + 3], ext[i + 4]);
    int32_t dev = abs(x - m);
    bool bad = (int64_t)dev > thr;                   // сбой на всю шкалу
    if (!bad && dev > minDev) {
      // битый отсчёт посреди речи: соседи меняются плавно, а он один отскочил в разы дальше их шага
      int32_t s1 = abs(ext[i + 1] - ext[i + 3]), s2 = abs(ext[i + 1] - ext[i]), s3 = abs(ext[i + 4] - ext[i + 3]);
      int32_t smooth = max(max(s1, s2), max(s3, (int32_t)1));
      bad = dev > 4 * smooth;
    }
    if (bad) { x = m; ext[i + 2] = m; fixed++; }
    out[i] = x;
  }
  memcpy(glitchTail, ext + FRAME, sizeof(glitchTail));
  return fixed;
}

void processMic() {
  size_t bytesRead = 0;
  if (i2s_read(MIC_PORT, micRaw, sizeof(micRaw), &bytesRead, pdMS_TO_TICKS(100)) != ESP_OK) return;
  if (bytesRead < sizeof(micRaw)) return;
#if MIC_AEC
  micproc::noteFrame(FRAME, esp_timer_get_time());   // время кадра — для сопоставления с динамиком
#endif
  trackMicSlot();

  // 24-битные отсчёты живого слота + убираем одиночные сбои шины I2S (щелчки)
  int32_t clean[FRAME];
  int glitches = deglitch(clean);
  if (glitches) glitchCount += glitches;

  // 24 бита -> 16 бит, убираем постоянную составляющую, считаем уровень
  uint64_t sq = 0;
  int clipped = 0;
  for (int i = 0; i < FRAME; i++) {
    float x = (float)clean[i] * (micGain / 256.0f);                         // 24 бита -> 16 бит с усилением
    float y = x - hpPrevX + 0.995f * hpPrevY;
    hpPrevX = x;
    hpPrevY = y;
    int32_t s = (int32_t)y;
    if (s > 32767) { s = 32767; clipped++; }
    if (s < -32768) { s = -32768; clipped++; }
    frame16[i] = (int16_t)s;
    sq += (int64_t)s * s;
  }
#if MIC_AEC
  // эхоподавление (музыка и голос своего динамика) и шумоподавление; уровень — уже по очищенному звуку
  if (cfg.aec || cfg.ns) {
    micproc::process(frame16, FRAME, cfg.aec, cfg.ns);
    sq = 0;
    for (int i = 0; i < FRAME; i++) sq += (int64_t)frame16[i] * frame16[i];
  }
#endif
  float rms = sqrtf((float)sq / FRAME);

  bool muted = playing || millis() - playEndMs < MIC_MUTE_AFTER_TTS_MS;
  if (!muted && rms > meterPeak) meterPeak = rms;

  // пока звучит ответ (и чуть после) — микрофон не слушаем, иначе Ирина услышит саму себя
  if (muted) {
    if (inSpeech) endSpeech();
    prerollCount = 0;
    return;
  }

  if (!wsConnected) return;

  if (!inSpeech) {
    static uint32_t lastNoiseLog = 0;
    if (millis() - lastNoiseLog > 30000) {
      lastNoiseLog = millis();
      Serial.printf("Тишина: шум %.0f, речь начнётся с уровня %.0f; Wi-Fi %d дБм, обрывов %u\n", noiseFloor,
                    max(noiseFloor * VAD_START_RATIO, (float)cfg.vadLevel), WiFi.RSSI(), (unsigned)wifiDrops);
    }
    // фоновый шум медленно подстраивается; пока играет музыка, она и есть фон — следуем за ней в обе стороны
    bool musicNow = millis() - musicHeardAt < 600;
    if (musicNow) noiseFloor = 0.95f * noiseFloor + 0.05f * rms;
    else if (rms < noiseFloor * 2.0f || noiseFloor < 50) noiseFloor = 0.97f * noiseFloor + 0.03f * rms;
    if (noiseFloor < 30) noiseFloor = 30;

    memcpy(preroll[prerollHead], frame16, sizeof(frame16));
    prerollHead = (prerollHead + 1) % PREROLL_FRAMES;
    if (prerollCount < PREROLL_FRAMES) prerollCount++;

    // с эхоподавлением музыка в микрофоне почти не слышна — порог обычный
    bool echoLeft = musicNow && !(MIC_AEC && cfg.aec);
    float threshold = max(noiseFloor * (echoLeft ? MUSIC_VAD_RATIO : VAD_START_RATIO), (float)cfg.vadLevel);
    loudFrames = (rms > threshold) ? loudFrames + 1 : 0;
    if (loudFrames >= VAD_START_FRAMES) {
      inSpeech = true;
      speechWithMusic = musicNow;
      silenceMs = 0;
      speechMs = 0;
      levelSum = 0;
      levelCount = 0;
      clipFrames = 0;
      voicedSum = 0;
      voicedCount = 0;
      packetFrames = 0;
      sendVad(true, 0);
      // отправляем звук, записанный до начала речи (включая текущий кадр)
      int start = (prerollHead - prerollCount + PREROLL_FRAMES) % PREROLL_FRAMES;
      for (int i = 0; i < prerollCount; i++) queueFrame(preroll[(start + i) % PREROLL_FRAMES]);
      prerollCount = 0;
    }
    return;
  }

  // идёт речь
  if (millis() - musicHeardAt < 600) speechWithMusic = true;
  queueFrame(frame16);
  speechMs += 20;
  levelSum += (uint32_t)rms;
  levelCount++;
  if (clipped > 4) clipFrames++;
  if (rms < max(noiseFloor * 2.0f, (float)cfg.vadLevel * 0.6f)) {
    silenceMs += 20;
  } else {
    silenceMs = 0;
    voicedSum += (uint32_t)rms;
    voicedCount++;
  }
  uint32_t hang = (millis() < wakeUntil) ? cfg.hangMs * 2 : cfg.hangMs;
  if (silenceMs >= hang || speechMs >= VAD_MAX_MS) endSpeech();
}

// ---------------- setup / loop ----------------
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.printf("\nКлиент Ирины %s\n", FW_VERSION);
  if (LED_PIN >= 0) pinMode(LED_PIN, OUTPUT);
  if (AMP_SD >= 0) pinMode(AMP_SD, OUTPUT);
  ampEnable(false);
  pinMode(RESET_BUTTON, INPUT_PULLUP);

  resetReasonText = describeResetReason();
  Serial.printf("Причина запуска: %s\n", resetReasonText.c_str());
  if (esp_reset_reason() == ESP_RST_BROWNOUT)
    Serial.println("!! Просела линия питания. Нужен блок питания 5 В от 1 А и конденсатор 470–1000 мкФ у усилителя");
  loadSettings();
  setupWifi();

  String host = "irene-" + cfg.roomId;
  MDNS.begin(host.c_str());
  ArduinoOTA.setHostname(host.c_str());
  if (cfg.token.length()) ArduinoOTA.setPassword(cfg.token.c_str());
  ArduinoOTA.onStart([]() { ws.disconnect(); });
  ArduinoOTA.begin();

  Serial.printf("Чип %s, PSRAM %u КБ, свободно ОЗУ %u КБ\n", ESP.getChipModel(),
                (unsigned)(ESP.getPsramSize() / 1024), (unsigned)(ESP.getFreeHeap() / 1024));
  // буфер ответа: в PSRAM, если она есть (S3 N16R8), иначе во внутренней памяти
  static StaticStreamBuffer_t audioBufCtl;
  uint8_t* storage = nullptr;
  if (psramFound()) storage = (uint8_t*)heap_caps_malloc(AUDIO_BUF_BYTES + 1, MALLOC_CAP_SPIRAM);
  if (!storage) storage = (uint8_t*)heap_caps_malloc(AUDIO_BUF_BYTES + 1, MALLOC_CAP_8BIT);
  if (storage) audioBuf = xStreamBufferCreateStatic(AUDIO_BUF_BYTES, 1, storage, &audioBufCtl);
  if (!audioBuf) Serial.println("!! Не хватило памяти под буфер звука");
  setupMic();
  setupAmp();
#if MIC_AEC
  if (psramFound() && micproc::init(NS_MODE))
    Serial.printf("Эхо- и шумоподавление готово (кусок %d отсчётов): %s / %s\n", micproc::chunk,
                  cfg.aec ? "вкл" : "выкл", cfg.ns ? "вкл" : "выкл");
  else
    Serial.println("!! Эхоподавление не запустилось (нужна PSRAM) — микрофон без него");
#endif
  xTaskCreatePinnedToCore(playerTask, "player", 4096, NULL, 5, NULL, 1);

  setupWebSocket();
#if SNAP_ENABLED
  setupSnap();
#endif
}

void loop() {
  ws.loop();
  ArduinoOTA.handle();
  handleWebPortal();
  checkResetButton();
  processMic();      // блокируется на ~20 мс, пока не накопится кадр — это и есть ритм цикла
  if (meterOn && wsConnected && millis() - meterSentAt >= METER_INTERVAL_MS) {
    meterSentAt = millis();
    sendMeter(meterPeak);
    meterPeak = 0;
  }
  // подобранное автоусиление сохраняем нечасто — флеш-память не любит частую запись
  if (gainDirty && !inSpeech && millis() - gainSavedAt > AGC_SAVE_MS) {
    saveSettings();
    if (wsConnected) sendMicState();
  }
  // Wi-Fi пропал: пробуем переподключиться каждые 5 с, через 2 минуты — перезагрузка
  static uint32_t wifiLostAt = 0, lastReconnect = 0;
  if (WiFi.status() != WL_CONNECTED) {
    if (!wifiLostAt) wifiLostAt = millis();
    if (millis() - lastReconnect > 5000) {
      lastReconnect = millis();
      WiFi.reconnect();
    }
    if (millis() - wifiLostAt > 120000) ESP.restart();
  } else {
    wifiLostAt = 0;
  }
}
