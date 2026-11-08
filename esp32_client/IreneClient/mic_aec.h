// Эхоподавление и шумоподавление микрофона (ESP32-S3, библиотека ESP-SR от Espressif — входит в ядро Arduino).
//
// Эхоподавление (AEC) вычитает из микрофона то, что сейчас играет свой динамик: музыку и голос Русланы.
// Для этого задача воспроизведения записывает «опорный» сигнал — ровно то, что ушло на усилитель, — в кольцо
// с привязкой ко времени, когда он прозвучит (refWrite). Микрофон знает время каждого своего отсчёта (micTime)
// и берёт опорный сигнал за то же время. Оставшиеся несколько миллисекунд расхождения AEC подстраивает сам.
// Шумоподавление (NS) после AEC убирает ровный фон: вентилятор, гул, остатки эха.
//
// Два микрофона (MIC_COUNT 2): вместо этой цепочки работает «передний край» ESP-SR (AFE) в режиме «MMR» —
// эхоподавление на каждом микрофоне -> разделение источников (BSS) -> выбор канала, где голос чище (MISO) ->
// шумоподавление. Голос выделяется на фоне телевизора, музыки из другого угла и отражений от стен.
#pragma once

#if MIC_AEC
#include "esp_aec.h"
#include "esp_ns.h"
#include "esp_heap_caps.h"

namespace micproc {

static const int RATE = 16000;
static const int NS_FRAME_MS = 10;
static const int NS_FRAME = RATE * NS_FRAME_MS / 1000;   // 160 отсчётов
static const uint32_t REF_N = 16384;          // ~1 с опорного сигнала при 16 кГц
static int16_t* refRing = nullptr;
static uint32_t* refStamp = nullptr;          // номер отсчёта (по времени), который лежит в ячейке

static aec_handle_t* aec = nullptr;
static ns_handle_t ns = nullptr;
static int chunk = 0;                         // размер куска AEC (обычно 256 отсчётов = 16 мс)

// очереди между кусками разного размера: кадры микрофона по 320, AEC по chunk, NS по 320
static const int FIFO_N = 2048;
static int16_t* inFifo = nullptr;  static int inCount = 0;
static int16_t* midFifo = nullptr; static int midCount = 0;
static int16_t* chunkIn = nullptr; static int16_t* chunkRef = nullptr; static int16_t* chunkOut = nullptr;
static uint64_t inHeadSample = 0;             // номер (с начала работы микрофона) первого отсчёта в inFifo

// Время отсчётов микрофона. Кадр не мог начаться позже «момент чтения − длина кадра»; задержки чтения случайны
// (Wi-Fi, сеть), поэтому берём нижнюю огибающую: минимум за каждую секунду, и по последним 8 минимумам —
// прямую (часы микрофона и процессора чуть расходятся, наклон это учитывает).
static uint64_t micSamples = 0;               // прочитано отсчётов до текущего кадра
static const int WIN_FRAMES = 50;             // окно минимума — 1 с
static const int WINS = 8;
static double winS[WINS], winC[WINS];         // номер отсчёта и «начало отсчёта №0» в минимуме окна
static int winCount = 0, winPos = 0, winFrames = 0;
static double curMinC = 1e300, curMinS = 0;
static double fitA = 0, fitB = 0;             // начало отсчёта №0 = fitA + fitB * номер
static bool fitSet = false;

static volatile bool ready = false;

inline int64_t sampleTimeUs(uint64_t s) {
  return (int64_t)(fitA + fitB * (double)s) + (int64_t)(s * 1000000ULL / RATE);
}

bool init(int nsMode) {
  uint32_t caps = MALLOC_CAP_SPIRAM;
  refRing = (int16_t*)heap_caps_calloc(REF_N, sizeof(int16_t), caps);
  refStamp = (uint32_t*)heap_caps_calloc(REF_N, sizeof(uint32_t), caps);
  inFifo = (int16_t*)heap_caps_aligned_alloc(16, FIFO_N * 2, caps);
  midFifo = (int16_t*)heap_caps_aligned_alloc(16, FIFO_N * 2, caps);
  if (!refRing || !refStamp || !inFifo || !midFifo) return false;
  for (uint32_t i = 0; i < REF_N; i++) refStamp[i] = 0xFFFFFFFF;
  aec = aec_create(RATE, 4, 1, AEC_MODE_SR_LOW_COST);
  if (!aec) return false;
  chunk = aec_get_chunksize(aec);
  if (chunk <= 0 || chunk > 1024) return false;
  chunkIn = (int16_t*)heap_caps_aligned_alloc(16, chunk * 2, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  chunkRef = (int16_t*)heap_caps_aligned_alloc(16, chunk * 2, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  chunkOut = (int16_t*)heap_caps_aligned_alloc(16, chunk * 2, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  if (!chunkIn || !chunkRef || !chunkOut) return false;
  ns = ns_pro_create(NS_FRAME_MS, nsMode, RATE);   // библиотека принимает только кадры по 10 мс
  ready = true;
  return true;
}

// Задача воспроизведения: блок out (стерео, n кадров с частотой rate) зазвучит в outAtUs
void refWrite(const int16_t* out, int n, int rate, int64_t outAtUs) {
  if (!ready) return;
  int64_t k0 = (outAtUs * RATE + 999999) / 1000000;                 // первый отсчёт 16 кГц не раньше начала блока
  int64_t k1 = ((outAtUs + (int64_t)n * 1000000 / rate) * RATE) / 1000000;
  for (int64_t k = k0; k < k1; k++) {
    int64_t t = k * 1000000 / RATE;
    int i = (int)((t - outAtUs) * rate / 1000000);
    if (i < 0) i = 0;
    if (i >= n) i = n - 1;
    // моно и простое сглаживание [1 2 1]: выше 8 кГц микрофон всё равно не слышит
    int ia = i > 0 ? i - 1 : i, ib = i < n - 1 ? i + 1 : i;
    int32_t a = out[2 * ia] + out[2 * ia + 1], b = out[2 * i] + out[2 * i + 1], c = out[2 * ib] + out[2 * ib + 1];
    int32_t v = (a + 2 * b + c) / 8;
    uint32_t slot = (uint32_t)k % REF_N;
    refRing[slot] = (int16_t)v;
    refStamp[slot] = (uint32_t)k;
  }
}

// Микрофон: кадр из n отсчётов только что прочитан (readUs — момент, когда i2s_read вернул его)
void noteFrame(int n, int64_t readUs) {
  int64_t latest = readUs - (int64_t)n * 1000000 / RATE;   // кадр начался не позже этого
  double c = (double)(latest - (int64_t)(micSamples * 1000000ULL / RATE));
  double sNow = (double)micSamples;
  if (!fitSet || c < fitA + fitB * sNow) {   // раньше, чем считали, — значит, так и есть
    if (winCount < 2) { fitA = c; fitB = 0; }
    else fitA = c - fitB * sNow;
    fitSet = true;
  }
  if (c < curMinC) { curMinC = c; curMinS = sNow; }
  if (++winFrames >= WIN_FRAMES) {
    winS[winPos] = curMinS; winC[winPos] = curMinC;
    winPos = (winPos + 1) % WINS;
    if (winCount < WINS) winCount++;
    curMinC = 1e300; winFrames = 0;
    if (winCount >= 3) {                     // прямая по минимумам (метод наименьших квадратов)
      double ms = 0, mc = 0;
      for (int i = 0; i < winCount; i++) { ms += winS[i]; mc += winC[i]; }
      ms /= winCount; mc /= winCount;
      double sxy = 0, sxx = 0;
      for (int i = 0; i < winCount; i++) { sxy += (winS[i] - ms) * (winC[i] - mc); sxx += (winS[i] - ms) * (winS[i] - ms); }
      fitB = sxx > 0 ? sxy / sxx : 0;
      fitA = mc - fitB * ms;
      double below = 0;                      // прямую — под все минимумы (это нижняя граница)
      for (int i = 0; i < winCount; i++) {
        double d = fitA + fitB * winS[i] - winC[i];
        if (d > below) below = d;
      }
      fitA -= below;
    }
  }
  micSamples += n;
}

// Обработать кадр микрофона на месте (n = 320). doAec/doNs — включены ли. Задержка — до одного куска AEC.
void process(int16_t* frame, int n, bool doAec, bool doNs) {
  if (!ready || (!doAec && !doNs)) return;
  if (inCount + n > FIFO_N) inCount = 0;           // не бывает; на всякий случай
  if (inCount == 0) inHeadSample = micSamples - n;  // номер первого отсчёта этого кадра
  memcpy(inFifo + inCount, frame, n * 2);
  inCount += n;
  while (inCount >= chunk) {
    memcpy(chunkIn, inFifo, chunk * 2);
    if (doAec) {
      int64_t t0 = sampleTimeUs(inHeadSample) + (int64_t)MIC_REF_DELAY_US;
      int64_t k0 = (t0 * RATE) / 1000000;
      for (int j = 0; j < chunk; j++) {
        uint32_t k = (uint32_t)(k0 + j), slot = k % REF_N;
        chunkRef[j] = refStamp[slot] == k ? refRing[slot] : 0;
      }
      aec_process(aec, chunkIn, chunkRef, chunkOut);
    } else {
      memcpy(chunkOut, chunkIn, chunk * 2);
    }
    if (midCount + chunk <= FIFO_N) {
      memcpy(midFifo + midCount, chunkOut, chunk * 2);
      midCount += chunk;
    }
    memmove(inFifo, inFifo + chunk, (inCount - chunk) * 2);
    inCount -= chunk;
    inHeadSample += chunk;
  }
  if (midCount >= n) {
    if (doNs && ns) {
      for (int off = 0; off + NS_FRAME <= n; off += NS_FRAME)   // 20 мс = два куска по 10 мс
        ns_process(ns, midFifo + off, frame + off);
    }
    else memcpy(frame, midFifo, n * 2);
    memmove(midFifo, midFifo + n, (midCount - n) * 2);
    midCount -= n;
  } else {
    memset(frame, 0, n * 2);                         // первые миллисекунды после включения
  }
}

// ---------------- два микрофона: AFE «MMR» ----------------
#if MIC_COUNT >= 2
}  // namespace micproc
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_afe_config.h"
namespace micproc {

static const esp_afe_sr_iface_t* afe = nullptr;
static esp_afe_sr_data_t* afeData = nullptr;
static int feedChunk = 0, feedCh = 0;
static int16_t* dualFifo = nullptr; static int dualCount = 0;   // кадры [мик1, мик2, опорный]
static int16_t* feedBuf = nullptr;
static int16_t* outFifo = nullptr; static int outCount = 0;
static uint64_t dualHead = 0;
static bool afeAec = true, afeNs = true;
static volatile bool dualReady = false;

bool initDual(bool aecOn, bool nsOn) {
  if (!ready) return false;                       // кольцо опорного сигнала и часы микрофона — из init()
  afe_config_t* c = afe_config_init("MMR", NULL, AFE_TYPE_SR, AFE_MODE_LOW_COST);
  if (!c) return false;
  c->aec_init = true;
  c->se_init = true;                              // разделение источников по двум микрофонам
  c->ns_init = true;
  c->afe_ns_mode = AFE_NS_MODE_WEBRTC;            // без моделей во флеш-памяти
  c->vad_init = false;                            // детектор речи — свой
  c->wakenet_init = false;
  c->agc_init = false;                            // автоусиление — своё
  c->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;
  c->afe_perferred_core = 0;
  c->afe_perferred_priority = 4;
  c->afe_linear_gain = 1.0f;
  c = afe_config_check(c);
  if (!c) return false;
  afe = esp_afe_handle_from_config(c);
  if (!afe) return false;
  afeData = afe->create_from_config(c);
  if (!afeData) return false;
  feedChunk = afe->get_feed_chunksize(afeData);
  feedCh = afe->get_feed_channel_num(afeData);
  if (feedChunk <= 0 || feedChunk > 1024 || feedCh != 3) return false;
  dualFifo = (int16_t*)heap_caps_malloc(FIFO_N * 3 * 2, MALLOC_CAP_SPIRAM);
  feedBuf = (int16_t*)heap_caps_aligned_alloc(16, feedChunk * 3 * 2, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  outFifo = (int16_t*)heap_caps_malloc(FIFO_N * 2 * 2, MALLOC_CAP_SPIRAM);
  if (!dualFifo || !feedBuf || !outFifo) return false;
  afeAec = afeNs = true;
  if (!aecOn) { afe->disable_aec(afeData); afeAec = false; }
  if (!nsOn) { afe->disable_ns(afeData); afeNs = false; }
  dualReady = true;
  return true;
}

// Два кадра микрофонов (n отсчётов, только что прочитаны) -> очищенный кадр out той же длины (с задержкой).
// false — выход ещё не набрался (первые десятки миллисекунд).
bool processDual(const int16_t* a, const int16_t* b, int n, bool aecOn, bool nsOn, int16_t* out) {
  if (aecOn != afeAec) { aecOn ? afe->enable_aec(afeData) : afe->disable_aec(afeData); afeAec = aecOn; }
  if (nsOn != afeNs) { nsOn ? afe->enable_ns(afeData) : afe->disable_ns(afeData); afeNs = nsOn; }
  if (dualCount + n > FIFO_N) dualCount = 0;
  if (dualCount == 0) dualHead = micSamples - n;
  for (int i = 0; i < n; i++) {
    int16_t* f = dualFifo + (dualCount + i) * 3;
    f[0] = a[i];
    f[1] = b[i];
    f[2] = 0;                                     // опорный — ниже, по времени отсчёта
  }
  dualCount += n;
  while (dualCount >= feedChunk) {
    int64_t t0 = sampleTimeUs(dualHead) + (int64_t)MIC_REF_DELAY_US;
    int64_t k0 = (t0 * RATE) / 1000000;
    for (int j = 0; j < feedChunk; j++) {
      uint32_t k = (uint32_t)(k0 + j), slot = k % REF_N;
      dualFifo[j * 3 + 2] = refStamp[slot] == k ? refRing[slot] : 0;
    }
    memcpy(feedBuf, dualFifo, feedChunk * 3 * 2);
    afe->feed(afeData, feedBuf);
    memmove(dualFifo, dualFifo + feedChunk * 3, (dualCount - feedChunk) * 3 * 2);
    dualCount -= feedChunk;
    dualHead += feedChunk;
  }
  for (;;) {                                      // забрать всё готовое, не дожидаясь
    afe_fetch_result_t* r = afe->fetch_with_delay(afeData, 0);
    if (!r || r->ret_value != 0 || !r->data || r->data_size <= 0) break;
    int m = r->data_size / 2;
    if (outCount + m > FIFO_N * 2) outCount = 0;
    memcpy(outFifo + outCount, r->data, m * 2);
    outCount += m;
  }
  if (outCount < n) return false;
  memcpy(out, outFifo, n * 2);
  memmove(outFifo, outFifo + n, (outCount - n) * 2);
  outCount -= n;
  return true;
}
#endif

}  // namespace micproc
#endif
