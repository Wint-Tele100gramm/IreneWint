// Клиент Ирины для комнаты.
//  - слушает I2S-микрофон, и когда в комнате говорят — отправляет звук на сервер (runva_rooms.py)
//  - проигрывает ответы сервера через I2S-усилитель
//  - настройка Wi-Fi, адреса сервера и имени комнаты — через портал WiFiManager
//
// Протокол описан в начале runva_rooms.py.
//
// Сборка в Arduino IDE (подробно — esp32_client/README.md):
//   плата "ESP32 Dev Module" (или "ESP32S3 Dev Module"),
//   Partition Scheme: "Minimal SPIFFS (1.9MB APP with OTA/190KB SPIFFS)",
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
#include <freertos/stream_buffer.h>
#include "driver/i2s.h"
#include "esp_idf_version.h"
#include "esp_system.h"
#include "config.h"

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

// ---------------- настройки ----------------
struct Settings {
  String server = DEFAULT_SERVER;
  uint16_t port = DEFAULT_PORT;
  String roomId;
  String roomName;
  String token;
  int micGainDb = DEFAULT_MIC_GAIN_DB;
  int vadLevel = DEFAULT_VAD_LEVEL;
  int hangMs = DEFAULT_VAD_HANG_MS;
} cfg;
float micGain = 4.0f;                    // линейное усиление из cfg.micGainDb

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

// микрофон и детектор речи
int32_t micRaw[FRAME];
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
float hpPrevX = 0, hpPrevY = 0;      // фильтр постоянной составляющей

// ---------------- светодиод и усилитель ----------------
void led(bool on) {
  if (LED_PIN >= 0) digitalWrite(LED_PIN, on ? HIGH : LOW);
}

void ampEnable(bool on) {
  if (AMP_SD >= 0) digitalWrite(AMP_SD, on ? HIGH : LOW);
}

// ---------------- настройки: хранение ----------------
String defaultRoomId() {
  char buf[16];
  snprintf(buf, sizeof(buf), "room-%06x", (uint32_t)(ESP.getEfuseMac() >> 24) & 0xFFFFFF);
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
  cfg.micGainDb = prefs.getInt("gain", DEFAULT_MIC_GAIN_DB);
  cfg.vadLevel = prefs.getInt("vadlvl", DEFAULT_VAD_LEVEL);
  cfg.hangMs = prefs.getInt("hang", DEFAULT_VAD_HANG_MS);
  prefs.end();
  micGain = powf(10.0f, cfg.micGainDb / 20.0f);
}

void saveSettings() {
  prefs.begin("irene", false);
  prefs.putString("server", cfg.server);
  prefs.putUShort("port", cfg.port);
  prefs.putString("room", cfg.roomId);
  prefs.putString("name", cfg.roomName);
  prefs.putString("token", cfg.token);
  prefs.putInt("gain", cfg.micGainDb);
  prefs.putInt("vadlvl", cfg.vadLevel);
  prefs.putInt("hang", cfg.hangMs);
  prefs.end();
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
    wifiDrops++;
    lastDropReason = info.wifi_sta_disconnected.reason;
    Serial.printf("Wi-Fi потерян (%u-й раз): код %u — %s\n", wifiDrops, lastDropReason,
                  wifiReasonText(lastDropReason));
  } else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
    Serial.printf("Wi-Fi есть: IP %s, сигнал %d дБм\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  }
}

// ---------------- Wi-Fi и страница настройки ----------------
// Первый запуск: точка доступа "Irene-XXXXXX", страница настройки на 192.168.4.1.
// После подключения к домашней сети та же страница доступна по IP клиента (http://<IP>/ -> "Setup").
WiFiManager wm;
WiFiManagerParameter *pServer, *pPort, *pRoom, *pName, *pToken, *pGain, *pVad, *pHang;
char portStr[8], gainStr[8], vadStr[8], hangStr[8];
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
  cfg.micGainDb = constrain(atoi(pGain->getValue()), 0, 30);
  cfg.vadLevel = constrain(atoi(pVad->getValue()), 30, 5000);
  cfg.hangMs = constrain(atoi(pHang->getValue()), 300, 3000);
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
  snprintf(gainStr, sizeof(gainStr), "%d", cfg.micGainDb);
  snprintf(vadStr, sizeof(vadStr), "%d", cfg.vadLevel);
  snprintf(hangStr, sizeof(hangStr), "%d", cfg.hangMs);
  pGain = new WiFiManagerParameter("gain", "Усиление микрофона, дБ (0-30; тихо/далеко — больше)", gainStr, 4);
  pVad = new WiFiManagerParameter("vad", "Порог речи (30-5000; меньше — чувствительнее)", vadStr, 5);
  pHang = new WiFiManagerParameter("hang", "Пауза конца фразы, мс (300-3000)", hangStr, 5);
  wm.addParameter(pGain);
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
  c.channel_format = MIC_CHANNEL_FMT;
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
}

void setupAmp() {
  i2s_config_t c = {};
  c.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
  c.sample_rate = 22050;
  c.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  c.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;   // моно дублируем в оба канала
  c.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  c.intr_alloc_flags = 0;
  c.I2S_DMA_COUNT = 8;
  c.I2S_DMA_LEN = 512;
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
  i2s_zero_dma_buffer(AMP_PORT);
}

// ---------------- воспроизведение (отдельная задача) ----------------
void playerTask(void*) {
  static int16_t in[512];
  static int16_t out[1024];
  static int16_t silence[1024] = {0};
  bool prebuffering = true;
  int curRate = 22050;
  for (;;) {
    if (!playing) {
      prebuffering = true;
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }
    if (prebuffering) {
      // копим ~150 мс, чтобы пережить неровности Wi-Fi
      size_t need = (size_t)pendingRate * 2 * 150 / 1000;
      if (xStreamBufferBytesAvailable(audioBuf) < need && !ttsEnded) {
        vTaskDelay(pdMS_TO_TICKS(5));
        continue;
      }
      prebuffering = false;
      if (curRate != pendingRate) {
        curRate = pendingRate;
        i2s_set_sample_rates(AMP_PORT, curRate);
      }
      ampEnable(true);
    }
    size_t n = xStreamBufferReceive(audioBuf, in, sizeof(in), pdMS_TO_TICKS(30));
    if (n >= 2) {
      int samples = n / 2;
      int vol = volumePct;
      int32_t k = vol * vol;                        // квадратичная шкала громкости, 0..10000
      for (int i = 0; i < samples; i++) {
        int32_t s = (int32_t)in[i] * k / 10000;
        out[2 * i] = out[2 * i + 1] = (int16_t)s;
      }
      size_t written;
      i2s_write(AMP_PORT, out, samples * 4, &written, portMAX_DELAY);
    } else if (ttsEnded && xStreamBufferIsEmpty(audioBuf)) {
      size_t written;
      i2s_write(AMP_PORT, silence, sizeof(silence), &written, portMAX_DELAY);   // доиграть хвост DMA
      i2s_write(AMP_PORT, silence, sizeof(silence), &written, portMAX_DELAY);
      ampEnable(false);
      playEndMs = millis();
      playing = false;
      led(false);
    }
  }
}

// ---------------- WebSocket ----------------
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
  String s;
  serializeJson(doc, s);
  ws.sendTXT(s);
}

void sendVad(bool start, uint32_t level) {
  char buf[64];
  if (start) snprintf(buf, sizeof(buf), "{\"type\":\"vad\",\"state\":\"start\"}");
  else snprintf(buf, sizeof(buf), "{\"type\":\"vad\",\"state\":\"end\",\"level\":%u}", level);
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

void endSpeech() {
  flushPacket();
  uint32_t level = levelCount ? (uint32_t)(levelSum / levelCount) : 0;
  if (wsConnected) sendVad(false, level);
  // для настройки чувствительности: хорошая речь — уровень 1500–5000, шум — в разы меньше порога
  Serial.printf("Фраза %.1f с: уровень речи %u, шум %.0f, порог %d, усиление %d дБ\n",
                speechMs / 1000.0f, level, noiseFloor, cfg.vadLevel, cfg.micGainDb);
  inSpeech = false;
  loudFrames = 0;
  if (millis() > wakeUntil) led(false);
}

void processMic() {
  size_t bytesRead = 0;
  if (i2s_read(MIC_PORT, micRaw, sizeof(micRaw), &bytesRead, pdMS_TO_TICKS(100)) != ESP_OK) return;
  if (bytesRead < sizeof(micRaw)) return;

  // пока звучит ответ (и чуть после) — микрофон не слушаем, иначе Ирина услышит саму себя
  if (playing || millis() - playEndMs < MIC_MUTE_AFTER_TTS_MS) {
    if (inSpeech) endSpeech();
    prerollCount = 0;
    return;
  }

  // 32 бита -> 16 бит, убираем постоянную составляющую, считаем уровень
  uint64_t sq = 0;
  for (int i = 0; i < FRAME; i++) {
    float x = (float)(micRaw[i] >> 8) * (micGain / 256.0f);   // 24 бита -> 16 бит с усилением
    float y = x - hpPrevX + 0.995f * hpPrevY;
    hpPrevX = x;
    hpPrevY = y;
    int32_t s = (int32_t)y;
    if (s > 32767) s = 32767;
    if (s < -32768) s = -32768;
    frame16[i] = (int16_t)s;
    sq += (int64_t)s * s;
  }
  float rms = sqrtf((float)sq / FRAME);

  if (!wsConnected) return;

  if (!inSpeech) {
    static uint32_t lastNoiseLog = 0;
    if (millis() - lastNoiseLog > 30000) {
      lastNoiseLog = millis();
      Serial.printf("Тишина: шум %.0f, речь начнётся с уровня %.0f; Wi-Fi %d дБм, обрывов %u\n", noiseFloor,
                    max(noiseFloor * VAD_START_RATIO, (float)cfg.vadLevel), WiFi.RSSI(), wifiDrops);
    }
    // фоновый шум медленно подстраивается
    if (rms < noiseFloor * 2.0f || noiseFloor < 50) noiseFloor = 0.97f * noiseFloor + 0.03f * rms;
    if (noiseFloor < 30) noiseFloor = 30;

    memcpy(preroll[prerollHead], frame16, sizeof(frame16));
    prerollHead = (prerollHead + 1) % PREROLL_FRAMES;
    if (prerollCount < PREROLL_FRAMES) prerollCount++;

    float threshold = max(noiseFloor * VAD_START_RATIO, (float)cfg.vadLevel);
    loudFrames = (rms > threshold) ? loudFrames + 1 : 0;
    if (loudFrames >= VAD_START_FRAMES) {
      inSpeech = true;
      silenceMs = 0;
      speechMs = 0;
      levelSum = 0;
      levelCount = 0;
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
  queueFrame(frame16);
  speechMs += 20;
  levelSum += (uint32_t)rms;
  levelCount++;
  if (rms < max(noiseFloor * 2.0f, (float)cfg.vadLevel * 0.6f)) silenceMs += 20;
  else silenceMs = 0;
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

  audioBuf = xStreamBufferCreate(24 * 1024, 1);   // ~0,55 с при 22 кГц (сервер держит запас 0,3 с)
  if (!audioBuf) Serial.println("!! Не хватило памяти под буфер звука");
  setupMic();
  setupAmp();
  xTaskCreatePinnedToCore(playerTask, "player", 4096, NULL, 5, NULL, 1);

  setupWebSocket();
}

void loop() {
  ws.loop();
  ArduinoOTA.handle();
  handleWebPortal();
  checkResetButton();
  processMic();      // блокируется на ~20 мс, пока не накопится кадр — это и есть ритм цикла
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
