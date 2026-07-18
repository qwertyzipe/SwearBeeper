import os
import re
import json
import wave
import math
import random
import queue
import threading

import numpy as np
import sounddevice as sd
from vosk import Model, KaldiRecognizer

from config import PLAYBACK_RATE, REC_RATE, BEEP_FREQ, PREFIXES


def normalize_word(w):
    return w.strip().lower().replace("ё", "е").replace("Ё", "Е")


def resample_linear(x, orig_sr, target_sr):
    if orig_sr == target_sr or len(x) == 0:
        return x.astype(np.float32)
    duration = len(x) / orig_sr
    target_len = max(1, int(duration * target_sr))
    orig_idx = np.linspace(0, len(x) - 1, num=len(x))
    target_idx = np.linspace(0, len(x) - 1, num=target_len)
    return np.interp(target_idx, orig_idx, x).astype(np.float32)


def build_swear_pattern(root_words):
    escaped_roots = [re.escape(r) for r in root_words if r.strip()]
    if not escaped_roots:
        return re.compile(r"(?!x)x")
    return re.compile(
        "^(?:" + "|".join(PREFIXES) + ")(?:" + "|".join(escaped_roots) + r")\w*$",
        re.IGNORECASE,
    )


def load_wav_mono_float(path, target_sr):
    with wave.open(path, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if sampwidth != 2:
        raise ValueError("Поддерживаются только 16-bit PCM WAV файлы")

    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels > 1:
        data = data.reshape(-1, n_channels).mean(axis=1)
    if framerate != target_sr:
        data = resample_linear(data, framerate, target_sr)
    return data.astype(np.float32)


def level_to_percent(level):
    if level <= 1e-6:
        return 0
    db = 20 * math.log10(level)
    db = max(-60.0, min(0.0, db))
    return (db + 60.0) / 60.0 * 100.0


class DelayBuffer:
    def __init__(self, sample_rate, delay_sec):
        self.sr = sample_rate
        self.delay_samples = int(sample_rate * delay_sec)
        self.buffer = np.zeros(max(self.delay_samples * 2, sample_rate), dtype=np.float32)
        self.write_pos = 0
        self.read_pos = 0
        self.lock = threading.Lock()

        self._w = 0
        self._r = 0
        self.capacity = len(self.buffer)

    def write(self, chunk: np.ndarray):
        with self.lock:
            n = len(chunk)
            end = self._w + n
            if end <= self.capacity:
                self.buffer[self._w:end] = chunk
            else:
                first = self.capacity - self._w
                self.buffer[self._w:] = chunk[:first]
                self.buffer[:end - self.capacity] = chunk[first:]
            self._w = end % self.capacity
            self.write_pos += n

    def read_ready(self):
        with self.lock:
            return max(0, (self.write_pos - self.delay_samples) - self.read_pos)

    def read(self, n):
        with self.lock:
            out = np.zeros(n, dtype=np.float32)
            end = self._r + n
            if end <= self.capacity:
                out[:] = self.buffer[self._r:end]
            else:
                first = self.capacity - self._r
                out[:first] = self.buffer[self._r:]
                out[first:] = self.buffer[:end - self.capacity]
            self._r = end % self.capacity
            self.read_pos += n
            return out

    def stamp_beep(self, global_start_sample, global_end_sample, segment_fn):
        with self.lock:
            if global_end_sample <= self.read_pos:
                return False

            start = max(global_start_sample, self.read_pos)
            end = min(global_end_sample, self.write_pos)
            if end <= start:
                return False

            positions = np.arange(start, end)
            beep = segment_fn(positions).astype(np.float32)

            n = end - start
            offset_from_read = start - self.read_pos
            idx = (self._r + offset_from_read) % self.capacity
            end_idx = idx + n
            if end_idx <= self.capacity:
                self.buffer[idx:end_idx] = beep
            else:
                first = self.capacity - idx
                self.buffer[idx:] = beep[:first]
                self.buffer[:end_idx - self.capacity] = beep[first:]
            return True


class SwearBeeperEngine:
    def __init__(self, config, log_callback, journal_callback=None, crash_callback=None):
        self.config = config
        self.log = log_callback
        self.journal_callback = journal_callback
        self.crash_callback = crash_callback
        self.crashed = False
        self.running = False
        self.model = None
        self.recognizer = None
        self.delay_buffer = None
        self.mic_queue = queue.Queue()
        self.rec_thread = None
        self.input_stream = None
        self.output_stream = None
        self.sound_mappings = []
        self.playback_rate = PLAYBACK_RATE
        self._partial_processed_count = 0
        self.level = 0.0
        self.manual_mute = False
        self.noise_profile = None
        self.stats = {"total": 0, "per_word": {}}

    def _handle_crash(self, exc):
        if self.crashed:
            return
        self.crashed = True
        self.manual_mute = True
        self.log(f"КРИТИЧЕСКАЯ ОШИБКА в движке: {exc}. Микрофон заглушен для безопасности.")
        if self.crash_callback:
            try:
                self.crash_callback(exc)
            except Exception:
                pass

    def start(self):
        self.log("Загружаю модель Vosk...")
        self.model = Model(self.config["model_path"])
        self.recognizer = KaldiRecognizer(self.model, REC_RATE)
        self.recognizer.SetWords(True)
        if hasattr(self.recognizer, "SetPartialWords"):
            self.recognizer.SetPartialWords(True)

        self.running = True
        self._partial_processed_count = 0
        self.stats = {"total": 0, "per_word": {}}

        candidates = self._build_samplerate_candidates()
        self._open_audio_streams(candidates)

        self.rec_thread = threading.Thread(target=self._recognition_loop, daemon=True)
        self.rec_thread.start()

        self.log(f"Запущено. Задержка вывода: {self.config['delay_sec']} сек.")

    def _build_samplerate_candidates(self):
        candidates = []

        try:
            input_rate = int(sd.query_devices(self.config["input_device"])["default_samplerate"])
            candidates.append(input_rate)
        except Exception:
            pass

        try:
            output_rate = int(sd.query_devices(self.config["output_device"])["default_samplerate"])
            if output_rate not in candidates:
                candidates.append(output_rate)
        except Exception:
            pass

        # Стандартный набор частот, которые почти всегда поддерживаются железом/драйверами
        for r in (PLAYBACK_RATE, 44100, 32000, 22050, 16000):
            if r not in candidates:
                candidates.append(r)

        return candidates

    def _load_sound_mappings(self):
        self.sound_mappings = []

        # Новый формат: список {"path": ..., "words": [...]}  (words пусто = "любой мат")
        for entry in self.config.get("custom_sound_mappings", []) or []:
            path = entry.get("path")
            words = {normalize_word(w) for w in entry.get("words", []) if w.strip()}
            try:
                data = load_wav_mono_float(path, self.playback_rate)
                self.sound_mappings.append((words if words else None, data))
                label = ", ".join(sorted(words)) if words else "любой мат"
                self.log(f"Звук загружен: {os.path.basename(path)} -> {label} ({len(data)} сэмплов)")
            except Exception as e:
                self.log(f"Не удалось загрузить звук '{path}': {e}. Пропускаю.")

        # Старый формат (плоский список путей) - для обратной совместимости со старыми настройками
        for path in self.config.get("custom_beep_paths", []) or []:
            try:
                data = load_wav_mono_float(path, self.playback_rate)
                self.sound_mappings.append((None, data))
                self.log(f"Звук загружен (старый формат): {os.path.basename(path)} -> любой мат ({len(data)} сэмплов)")
            except Exception as e:
                self.log(f"Не удалось загрузить звук '{path}': {e}. Пропускаю.")

        if not self.sound_mappings:
            self.log("Кастомные звуки не заданы — использую стандартный тон.")

    def _open_audio_streams(self, candidates):
        last_exception = None

        for i, rate in enumerate(candidates):
            self.playback_rate = rate
            blocksize = int(rate * self.config["block_ms"] / 1000)

            try:
                self.input_stream = sd.InputStream(
                    samplerate=rate, channels=1, dtype="float32",
                    blocksize=blocksize, callback=self._audio_in_callback,
                    device=self.config["input_device"], latency="high",
                )
                self.output_stream = sd.OutputStream(
                    samplerate=rate, channels=1, dtype="float32",
                    blocksize=blocksize, callback=self._audio_out_callback,
                    device=self.config["output_device"], latency="high",
                )
                self.input_stream.start()
                self.output_stream.start()

                if i == 0:
                    self.log(f"Аудио-поток открыт на родной частоте {rate} Гц.")
                else:
                    self.log(f"Аудио-поток открыт на частоте {rate} Гц (запасной вариант №{i + 1}, предыдущие частоты не подошли).")

                # Буфер и звуки создаём/грузим ПОСЛЕ того как узнали реально рабочую частоту
                self._load_sound_mappings()
                self.delay_buffer = DelayBuffer(self.playback_rate, self.config["delay_sec"])
                return

            except Exception as e:
                last_exception = e
                for stream_attr in ("input_stream", "output_stream"):
                    stream = getattr(self, stream_attr, None)
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                self.input_stream = None
                self.output_stream = None
                self.log(f"Частота {rate} Гц не подошла ({e}), пробую следующий вариант...")

        raise last_exception if last_exception else RuntimeError("Не удалось открыть аудио-поток ни на одной из частот.")

    def stop(self):
        self.running = False
        if self.input_stream:
            self.input_stream.stop()
            self.input_stream.close()
        if self.output_stream:
            self.output_stream.stop()
            self.output_stream.close()
        self.log("Остановлено.")

    def _apply_noise_suppression(self, mono):
        """Простой спектральный шумодав (spectral gating) на FFT, без внешних библиотек.
        Постоянно отслеживает 'пол' шума по спектру и вычитает его с небольшим запасом (oversubtraction)."""
        n = len(mono)
        if n < 4:
            return mono

        window = np.hanning(n).astype(np.float32)
        spectrum = np.fft.rfft(mono * window)
        mag = np.abs(spectrum)
        phase = np.angle(spectrum)

        if self.noise_profile is None or len(self.noise_profile) != len(mag):
            self.noise_profile = mag.copy()
        else:
            # Медленно тянем профиль шума к "полу" сигнала (минимум с утечкой вверх/вниз)
            self.noise_profile = np.where(
                mag < self.noise_profile,
                0.85 * self.noise_profile + 0.15 * mag,
                0.995 * self.noise_profile + 0.005 * mag,
            )

        oversubtraction = 1.5
        clean_mag = np.maximum(mag - oversubtraction * self.noise_profile, 0.0)
        clean_spectrum = clean_mag * np.exp(1j * phase)
        cleaned = np.fft.irfft(clean_spectrum, n=n).astype(np.float32)
        return np.clip(cleaned, -1.0, 1.0)

    def _audio_in_callback(self, indata, frames, time_info, status):
        if self.crashed:
            return
        try:
            if status:
                self.log(str(status))
            mono = indata[:, 0].copy()
            gain = self.config.get("mic_gain", 1.0)
            if gain != 1.0:
                mono = np.clip(mono * gain, -1.0, 1.0).astype(np.float32)
            if self.config.get("noise_suppression", False):
                mono = self._apply_noise_suppression(mono)
            self.level = float(np.sqrt(np.mean(mono ** 2))) if len(mono) else 0.0
            self.delay_buffer.write(mono)
            mono_16k = resample_linear(mono, self.playback_rate, REC_RATE)
            self.mic_queue.put(mono_16k)
        except Exception as e:
            self._handle_crash(e)

    def _audio_out_callback(self, outdata, frames, time_info, status):
        if self.crashed or self.manual_mute:
            outdata[:, 0] = 0
            return
        try:
            ready = self.delay_buffer.read_ready()
            if ready >= frames:
                outdata[:, 0] = self.delay_buffer.read(frames)
            else:
                outdata[:, 0] = 0
        except Exception as e:
            outdata[:, 0] = 0
            self._handle_crash(e)

    def _recognition_loop(self):
        while self.running and not self.crashed:
            try:
                chunk = self.mic_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                pcm16 = (chunk * 32767).astype(np.int16).tobytes()

                if self.recognizer.AcceptWaveform(pcm16):
                    result = json.loads(self.recognizer.Result())
                    words = result.get("result", [])
                    new_words = words[self._partial_processed_count:]
                    self._process_words(new_words)
                    text = result.get("text", "")
                    if text:
                        self.log(f"[РАСПОЗНАНО] {text}")
                    self._partial_processed_count = 0
                else:
                    partial = json.loads(self.recognizer.PartialResult())
                    words = partial.get("result", [])
                    if len(words) > self._partial_processed_count:
                        new_words = words[self._partial_processed_count:]
                        self._process_words(new_words)
                        self._partial_processed_count = len(words)
            except Exception as e:
                self._handle_crash(e)
                break

    def _beep_segment_sine(self, positions):
        t = positions / self.playback_rate
        volume = self.config.get("beep_volume", 0.12)
        return volume * np.sin(2 * np.pi * BEEP_FREQ * t)

    def _beep_segment_custom(self, positions, original_start, sound_array):
        idx = (positions - original_start) % len(sound_array)
        volume = self.config.get("beep_volume", 0.12)
        return sound_array[idx] * (volume / 0.12)

    def _mute_segment(self, positions):
        return np.zeros(len(positions), dtype=np.float32)

    def _pick_sound_for_word(self, word_normalized):
        """Возвращает np.array звука для конкретного слова: сначала ищем специфичный
        маппинг (слово -> конкретные звуки), если нет - берём любой 'общий' звук (без слов)."""
        specific = [arr for (words, arr) in self.sound_mappings if words and word_normalized in words]
        if specific:
            return random.choice(specific)
        generic = [arr for (words, arr) in self.sound_mappings if not words]
        if generic:
            return random.choice(generic)
        return None

    def _process_words(self, words):
        pattern = self.config["swear_pattern"]
        whitelist = self.config.get("whitelist", set())
        censor_mode = self.config.get("censor_mode", "beep")

        for w in words:
            word = w.get("word", "")
            word_normalized = normalize_word(word)
            if word_normalized in whitelist:
                continue
            if pattern.match(word_normalized):
                start_sample = int((w["start"] - self.config["pad_before"]) * self.playback_rate)
                end_sample = int((w["end"] + self.config["pad_after"]) * self.playback_rate)
                start_sample = max(0, start_sample)

                if censor_mode == "mute":
                    seg_fn = self._mute_segment
                else:
                    chosen_sound = self._pick_sound_for_word(word_normalized)
                    if chosen_sound is not None:
                        seg_fn = lambda positions, os_=start_sample, snd=chosen_sound: self._beep_segment_custom(positions, os_, snd)
                    else:
                        seg_fn = self._beep_segment_sine

                ok = self.delay_buffer.stamp_beep(start_sample, end_sample, seg_fn)
                tag = "OK" if ok else "ПОЗДНО"
                self.log(f"[МАТ] '{word}' [{w['start']:.2f}s - {w['end']:.2f}s] -> цензура: {tag}")

                # Считаем в статистику/журнал/OBS ВСЕГДА, даже если бип не успел (ПОЗДНО) -
                # мат всё равно был сказан и распознан, это должно быть учтено.
                self.stats["total"] += 1
                self.stats["per_word"][word_normalized] = self.stats["per_word"].get(word_normalized, 0) + 1
                if self.journal_callback:
                    self.journal_callback(word_normalized)
