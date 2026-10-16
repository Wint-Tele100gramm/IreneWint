# Распознавание речи для сервера комнат: общий интерфейс для двух движков.
#
#  - sherpa: новая потоковая модель Vosk (Zipformer, 0.54+/0.56) через sherpa-onnx.
#            Одна модель в памяти на все комнаты, у каждой комнаты свой лёгкий "поток".
#            Малая int8-модель: ~100 МБ ОЗУ, ошибок ~11% (у старой small-0.22 ~30%).
#  - kaldi:  классический Vosk (KaldiRecognizer), модель в папке "model". ~250 МБ ОЗУ.
#
# Сессия распознавания (на комнату):
#   accept(pcm_s16le) -> (final_text | None, partial_text)
#   finish()          -> final_text   (конец фразы по сигналу VAD клиента)

import json
import os
import threading

import numpy as np


class KaldiBackend:
    name = "kaldi"

    def __init__(self, model_path="model"):
        import vosk
        vosk.SetLogLevel(-1)
        self.model = vosk.Model(model_path)

    def session(self, endpointing=True):
        return _KaldiSession(self.model)


class _KaldiSession:
    def __init__(self, model):
        from vosk import KaldiRecognizer
        self.rec = KaldiRecognizer(model, 16000)

    def accept(self, pcm: bytes):
        if self.rec.AcceptWaveform(pcm):
            return json.loads(self.rec.Result()).get("text", ""), ""
        return None, json.loads(self.rec.PartialResult()).get("partial", "")

    def finish(self):
        return json.loads(self.rec.FinalResult()).get("text", "")


class SherpaBackend:
    name = "sherpa"

    def __init__(self, model_dir, threads=2):
        import sherpa_onnx

        def pick(*names):
            for n in names:
                p = os.path.join(model_dir, n)
                if os.path.isfile(p):
                    return p
            raise FileNotFoundError(f"В {model_dir} нет ни одного из файлов: {', '.join(names)}")

        # поддерживаем обе раскладки: архив sherpa-onnx (всё в корне) и репозиторий HuggingFace (am-onnx/, lang/)
        kwargs = dict(
            tokens=pick("tokens.txt", "lang/tokens.txt"),
            encoder=pick("encoder.int8.onnx", "am-onnx/encoder.int8.onnx", "encoder.onnx", "am-onnx/encoder.onnx"),
            decoder=pick("decoder.onnx", "am-onnx/decoder.onnx", "decoder.int8.onnx", "am-onnx/decoder.int8.onnx"),
            joiner=pick("joiner.int8.onnx", "am-onnx/joiner.int8.onnx", "joiner.onnx", "am-onnx/joiner.onnx"),
            num_threads=threads,
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
        )
        # одна модель на все комнаты. Определение конца фразы нужно только микрофону приставки,
        # у клиентов ESP32 конец фразы сообщает их VAD — для них is_endpoint() просто не вызывается
        self.rec = sherpa_onnx.OnlineRecognizer.from_transducer(
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=2.4,
            rule2_min_trailing_silence=1.2,   # пауза после речи, после которой фраза считается законченной
            rule3_min_utterance_length=20,
            **kwargs)
        # декодируем по очереди: фразы из разных комнат редко совпадают по времени
        self.lock = threading.Lock()

    def session(self, endpointing=True):
        return _SherpaSession(self, self.rec, endpointing)


_TAIL = np.zeros(int(0.6 * 16000), dtype=np.float32)   # тишина в конце фразы, чтобы модель "дослушала" последнее слово


class _SherpaSession:
    def __init__(self, backend, rec, endpointing):
        self.backend = backend
        self.rec = rec
        self.endpointing = endpointing
        self.stream = rec.create_stream()

    def _decode(self):
        with self.backend.lock:
            while self.rec.is_ready(self.stream):
                self.rec.decode_stream(self.stream)

    def accept(self, pcm: bytes):
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        self.stream.accept_waveform(16000, samples)
        self._decode()
        text = self.rec.get_result(self.stream).strip().lower()
        # пустой "конец фразы" (долгая тишина) не сбрасываем: сброс в момент начала речи съедает первые слова
        if self.endpointing and text and self.rec.is_endpoint(self.stream):
            self.rec.reset(self.stream)
            return text, ""
        return None, text

    def finish(self):
        self.stream.accept_waveform(16000, _TAIL)
        self._decode()
        text = self.rec.get_result(self.stream).strip().lower()
        self.rec.reset(self.stream)
        return text


def create_backend(options):
    kind = options.get("asr", "sherpa")
    if kind == "sherpa":
        try:
            return SherpaBackend(options.get("sherpa_model", "model-sherpa"), int(options.get("asr_threads", 2)))
        except (ImportError, FileNotFoundError) as e:
            # нет sherpa-onnx или модели (например, установка до перехода на новую модель) — работаем на старой
            print(f"Новая модель Vosk недоступна ({e}). Использую классический Vosk из папки model.")
            print("Чтобы включить новую: bash scripts/rk3528/get_asr_model.sh")
    return KaldiBackend(options.get("model", "model"))
