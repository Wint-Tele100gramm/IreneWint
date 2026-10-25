// Протокол Snapcast (поток музыки для мультирума) — разбор сообщений, синхронизация часов, расписание звука.
// Без зависимостей от Arduino: тот же код проверяется на компьютере против настоящего snapserver.
//
// Сообщение: заголовок 26 байт (little-endian) + данные:
//   uint16 type, uint16 id, uint16 refersTo, int32 sent.sec, int32 sent.usec, int32 recv.sec, int32 recv.usec, uint32 size
// Типы: 1 CodecHeader, 2 WireChunk, 3 ServerSettings, 4 Time, 5 Hello.
#pragma once
#include <stdint.h>
#include <string.h>

namespace snap {

enum MsgType : uint16_t { kBase = 0, kCodecHeader = 1, kWireChunk = 2, kServerSettings = 3, kTime = 4, kHello = 5 };

static const int HEADER_SIZE = 26;

struct Header {
  uint16_t type, id, refersTo;
  int32_t sentSec, sentUsec, recvSec, recvUsec;
  uint32_t size;
};

inline uint16_t rd16(const uint8_t* p) { return p[0] | (p[1] << 8); }
inline uint32_t rd32(const uint8_t* p) { return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24); }
inline void wr16(uint8_t* p, uint16_t v) { p[0] = v; p[1] = v >> 8; }
inline void wr32(uint8_t* p, uint32_t v) { p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24; }

inline void parseHeader(const uint8_t* p, Header& h) {
  h.type = rd16(p); h.id = rd16(p + 2); h.refersTo = rd16(p + 4);
  h.sentSec = (int32_t)rd32(p + 6); h.sentUsec = (int32_t)rd32(p + 10);
  h.recvSec = (int32_t)rd32(p + 14); h.recvUsec = (int32_t)rd32(p + 18);
  h.size = rd32(p + 22);
}

// Заголовок исходящего сообщения; nowUs — местное время отправки (мкс)
inline void writeHeader(uint8_t* p, uint16_t type, uint16_t id, int64_t nowUs, uint32_t size) {
  wr16(p, type); wr16(p + 2, id); wr16(p + 4, 0);
  wr32(p + 6, (uint32_t)(int32_t)(nowUs / 1000000)); wr32(p + 10, (uint32_t)(int32_t)(nowUs % 1000000));
  wr32(p + 14, 0); wr32(p + 18, 0); wr32(p + 22, size);
}

// Hello: JSON с описанием клиента. Возвращает длину сообщения в out (out должен вместить 26 + 4 + len(json)).
inline int buildHello(uint8_t* out, uint16_t id, int64_t nowUs, const char* json) {
  uint32_t n = strlen(json);
  writeHeader(out, kHello, id, nowUs, 4 + n);
  wr32(out + HEADER_SIZE, n);
  memcpy(out + HEADER_SIZE + 4, json, n);
  return HEADER_SIZE + 4 + n;
}

// Time: запрос синхронизации часов (8 байт данных: латентность, клиент шлёт нули)
inline int buildTime(uint8_t* out, uint16_t id, int64_t nowUs) {
  writeHeader(out, kTime, id, nowUs, 8);
  memset(out + HEADER_SIZE, 0, 8);
  return HEADER_SIZE + 8;
}

// Сборка сообщений из потока байт TCP
class Parser {
 public:
  static const uint32_t MAX_PAYLOAD = 8192;   // кусок 20 мс PCM 48 кГц стерео — 3,8 КБ
  uint8_t buf[HEADER_SIZE + MAX_PAYLOAD];
  uint32_t have = 0;
  Header hdr;
  bool skipping = false;      // слишком большое сообщение — пропускаем
  uint32_t skipLeft = 0;

  void reset() { have = 0; skipping = false; skipLeft = 0; }

  // Подаёт байты; на каждое полное сообщение вызывает cb(hdr, payload, len). Возвращает число разобранных сообщений.
  template <typename CB>
  int feed(const uint8_t* data, uint32_t len, CB cb) {
    int count = 0;
    while (len > 0) {
      if (skipping) {
        uint32_t n = len < skipLeft ? len : skipLeft;
        data += n; len -= n; skipLeft -= n;
        if (skipLeft == 0) { skipping = false; have = 0; }
        continue;
      }
      if (have < (uint32_t)HEADER_SIZE) {
        uint32_t n = HEADER_SIZE - have; if (n > len) n = len;
        memcpy(buf + have, data, n); have += n; data += n; len -= n;
        if (have < (uint32_t)HEADER_SIZE) break;
        parseHeader(buf, hdr);
        if (hdr.size > MAX_PAYLOAD) { skipping = true; skipLeft = hdr.size; continue; }
      }
      uint32_t need = HEADER_SIZE + hdr.size - have;
      uint32_t n = need < len ? need : len;
      memcpy(buf + have, data, n); have += n; data += n; len -= n;
      if (have == HEADER_SIZE + hdr.size) {
        cb(hdr, buf + HEADER_SIZE, hdr.size);
        count++;
        have = 0;
      }
    }
    return count;
  }
};

// Синхронизация часов: сервер_время = местное_время + diff. Медиана последних замеров — устойчиво к задержкам Wi-Fi.
class TimeSync {
 public:
  static const int N = 31;
  int64_t diffs[N];
  int count = 0, pos = 0;
  int64_t diffUs = 0;

  void reset() { count = 0; pos = 0; diffUs = 0; }
  bool ready() const { return count >= 3; }

  // Ответ сервера на Time: latency = (сервер_получил - клиент_отправил), serverSent — время отправки ответа сервером,
  // localRecv — местное время получения ответа
  void onTimeReply(int64_t latencyUs, int64_t serverSentUs, int64_t localRecvUs) {
    int64_t c2s = latencyUs;
    int64_t s2c = localRecvUs - serverSentUs;
    int64_t d = (c2s - s2c) / 2;
    diffs[pos] = d; pos = (pos + 1) % N; if (count < N) count++;
    int64_t tmp[N];
    memcpy(tmp, diffs, sizeof(int64_t) * count);
    for (int i = 1; i < count; i++) {               // сортировка вставками: N маленькое
      int64_t k = tmp[i]; int j = i - 1;
      while (j >= 0 && tmp[j] > k) { tmp[j + 1] = tmp[j]; j--; }
      tmp[j + 1] = k;
    }
    diffUs = tmp[count / 2];
  }
  int64_t serverToLocal(int64_t serverUs) const { return serverUs - diffUs; }
};

// Формат звука из CodecHeader (pcm: WAV-заголовок RIFF)
struct Format {
  uint32_t rate = 0;
  uint16_t channels = 0, bits = 0;
  bool ok() const { return rate > 0 && channels > 0 && bits == 16; }
};

// CodecHeader: string codec (uint32 len + байты) + uint32 size + заголовок кодека. Возвращает true для pcm.
inline bool parseCodecHeader(const uint8_t* p, uint32_t len, char* codecOut, int codecMax, Format& f) {
  if (len < 8) return false;
  uint32_t n = rd32(p);
  if (4 + n + 4 > len) return false;
  int c = n < (uint32_t)(codecMax - 1) ? n : codecMax - 1;
  memcpy(codecOut, p + 4, c); codecOut[c] = 0;
  uint32_t hl = rd32(p + 4 + n);
  const uint8_t* h = p + 8 + n;
  if (strcmp(codecOut, "pcm") != 0 || hl < 36 || 8 + n + hl > len) return false;
  // RIFF....WAVEfmt : каналы (22), частота (24), биты (34)
  f.channels = rd16(h + 22); f.rate = rd32(h + 24); f.bits = rd16(h + 34);
  return f.ok();
}

// WireChunk: int32 sec, int32 usec (время сервера), uint32 size + данные
inline bool parseWireChunk(const uint8_t* p, uint32_t len, int64_t& tsUs, const uint8_t*& data, uint32_t& dataLen) {
  if (len < 12) return false;
  tsUs = (int64_t)(int32_t)rd32(p) * 1000000 + (int32_t)rd32(p + 4);
  dataLen = rd32(p + 8);
  if (12 + dataLen > len) return false;
  data = p + 12;
  return true;
}

// ServerSettings: uint32 len + JSON {"bufferMs":1000,"latency":0,"muted":false,"volume":100}. Простой разбор без библиотек.
inline long jsonNumber(const char* js, const char* key, long def) {
  const char* k = strstr(js, key);
  if (!k) return def;
  k = strchr(k, ':');
  if (!k) return def;
  k++;
  while (*k == ' ') k++;
  if (!strncmp(k, "true", 4)) return 1;
  if (!strncmp(k, "false", 5)) return 0;
  long sign = 1, v = 0;
  if (*k == '-') { sign = -1; k++; }
  if (*k < '0' || *k > '9') return def;
  while (*k >= '0' && *k <= '9') { v = v * 10 + (*k - '0'); k++; }
  return sign * v;
}

struct Settings {
  int bufferMs = 1000, latencyMs = 0, volume = 100;
  bool muted = false;
};

inline bool parseServerSettings(const uint8_t* p, uint32_t len, Settings& s) {
  if (len < 4) return false;
  uint32_t n = rd32(p);
  if (4 + n > len || n > 512) return false;
  char js[513];
  memcpy(js, p + 4, n); js[n] = 0;
  s.bufferMs = (int)jsonNumber(js, "\"bufferMs\"", s.bufferMs);
  s.latencyMs = (int)jsonNumber(js, "\"latency\"", s.latencyMs);
  s.volume = (int)jsonNumber(js, "\"volume\"", s.volume);
  s.muted = jsonNumber(js, "\"muted\"", s.muted) != 0;
  return true;
}

// Очередь звука с расписанием: кольцо отсчётов + метки «когда (по местным часам) играть начало куска».
// Один производитель (задача сети) и один потребитель (задача воспроизведения), без блокировок.
#if defined(__XTENSA__) || defined(__riscv) || defined(__GNUC__)
  #define SNAP_BARRIER() __sync_synchronize()
#else
  #define SNAP_BARRIER()
#endif

class Schedule {
 public:
  int16_t* ring = nullptr;    // отсчёты моно 16 бит
  uint32_t cap = 0;           // ёмкость кольца в отсчётах
  volatile uint32_t wr = 0, rd = 0;
  struct Mark { int64_t localUs; uint32_t at; };   // отсчёт с позиции at играть в localUs
  static const int MARKS = 256;
  Mark marks[MARKS];
  volatile uint32_t mWr = 0, mRd = 0;
  volatile uint32_t flushGen = 0;   // производитель просит очистить очередь (новый поток, обрыв связи)
  uint32_t seenGen = 0;
  uint32_t rate = 22050;

  void init(int16_t* mem, uint32_t samples) { ring = mem; cap = samples; wr = 0; rd = 0; mWr = 0; mRd = 0; }
  // вызывает производитель: всё, что уже в очереди, потребитель выбросит
  void requestFlush() { flushGen = flushGen + 1; }
  uint32_t used() const { return wr - rd; }
  uint32_t space() const { return cap - used(); }

  // производитель: добавить кусок, который надо начать играть в localUs. false — нет места (кусок пропущен)
  bool push(const int16_t* s, uint32_t n, int64_t localUs) {
    if (!ring || n > space() || mWr - mRd >= (uint32_t)MARKS) return false;
    uint32_t w = wr;
    for (uint32_t i = 0; i < n; i++) ring[(w + i) % cap] = s[i];
    Mark& m = marks[mWr % MARKS]; m.localUs = localUs; m.at = w;
    SNAP_BARRIER();
    mWr = mWr + 1;
    wr = w + n;
    return true;
  }

  // потребитель: обработать просьбу об очистке
  void checkFlush() {
    if (seenGen != flushGen) { seenGen = flushGen; SNAP_BARRIER(); mRd = mWr; rd = wr; }
  }

  // потребитель: местное время, когда должен прозвучать отсчёт с позиции rd. false — очередь пуста
  bool headTime(int64_t& t) {
    checkFlush();
    while (mWr != mRd && rd != wr) {
      SNAP_BARRIER();
      Mark& m = marks[mRd % MARKS];
      if (mWr - mRd > 1) {
        Mark& next = marks[(mRd + 1) % MARKS];
        if ((int32_t)(rd - next.at) >= 0) { mRd = mRd + 1; continue; }   // этот кусок уже проигран
      }
      t = m.localUs + (int64_t)(int32_t)(rd - m.at) * 1000000 / (int64_t)rate;
      return true;
    }
    return false;
  }

  // потребитель: взять до n отсчётов
  uint32_t pop(int16_t* out, uint32_t n) {
    uint32_t avail = used();
    if (n > avail) n = avail;
    SNAP_BARRIER();
    for (uint32_t i = 0; i < n; i++) out[i] = ring[(rd + i) % cap];
    SNAP_BARRIER();
    rd = rd + n;
    return n;
  }
  void skip(uint32_t n) { uint32_t a = used(); rd = rd + (n > a ? a : n); }
};

// Статистика синхронизации (для журнала)
struct SyncStats {
  uint32_t blocks = 0, adjusts = 0, skipped = 0, waits = 0;
  int64_t lastErrUs = 0;
};

// Заполнить блок музыки, который зазвучит в outAtUs (местное время первого отсчёта блока).
// Мягкая синхронизация: расхождение до SYNC_HARD_US выправляется выбрасыванием/повтором одного отсчёта на блок
// (незаметно на слух), больше — пропуском или тишиной (так бывает при старте и после сбоев Wi-Fi).
// Возвращает число отсчётов с музыкой (0 — блок тишины).
static const int64_t SYNC_TOL_US = 400;       // точнее не нужно: соседние комнаты, а не стереопара
static const int64_t SYNC_HARD_US = 3000;     // дальше — сразу перескок
inline int fillBlock(Schedule& q, int16_t* out, int n, int64_t outAtUs, SyncStats& st) {
  memset(out, 0, sizeof(int16_t) * n);
  int64_t head;
  if (!q.headTime(head)) return 0;
  st.blocks++;
  int64_t err = head - outAtUs;          // > 0 — музыке ещё рано, < 0 — опаздываем
  int64_t blockUs = (int64_t)n * 1000000 / q.rate;
  if (err < -SYNC_HARD_US) {             // отстали (старт, Wi-Fi, перегрузка) — догоняем пропуском
    uint32_t drop = (uint32_t)((-err) * (int64_t)q.rate / 1000000);
    q.skip(drop);
    st.skipped += drop;
    if (!q.headTime(head)) return 0;
    err = head - outAtUs;
  }
  st.lastErrUs = err;
  if (err >= blockUs) { st.waits++; return 0; }
  int lead = 0, adj = 0;
  if (err > SYNC_HARD_US) {
    lead = (int)(err * (int64_t)q.rate / 1000000);   // музыка начинается посреди блока — до неё тишина
  } else if (err > SYNC_TOL_US) {
    adj = -1;                            // чуть рано: растягиваем на отсчёт
  } else if (err < -SYNC_TOL_US) {
    adj = 1;                             // чуть поздно: выбрасываем отсчёт
  }
  if (lead >= n) return 0;
  int need = n - lead;
  int take = need + adj;
  static int16_t tmp[2048 + 1];
  if (take > 2048) take = 2048;
  int got = (int)q.pop(tmp, take);
  if (adj != 0) st.adjusts++;
  if (adj == 1 && got == take) {         // убрать отсчёт посередине
    int mid = take / 2;
    memcpy(out + lead, tmp, sizeof(int16_t) * mid);
    memcpy(out + lead + mid, tmp + mid + 1, sizeof(int16_t) * (take - mid - 1));
    return need;
  }
  if (adj == -1 && got == take) {        // повторить отсчёт посередине
    int mid = take / 2;
    memcpy(out + lead, tmp, sizeof(int16_t) * (mid + 1));
    memcpy(out + lead + mid + 1, tmp + mid, sizeof(int16_t) * (take - mid));
    return need;
  }
  if (got > need) got = need;
  memcpy(out + lead, tmp, sizeof(int16_t) * got);
  return got;
}

}  // namespace snap
