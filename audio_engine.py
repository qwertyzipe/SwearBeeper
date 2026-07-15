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
        self.custom_beeps = []
        self.playback_rate = PLAYBACK_RATE
        self._partial_processed_count = 0
        self.level = 0.0
        self.manual_mute = False
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

        try:
            native_rate = int(sd.query_devices(self.config["input_device"])["default_samplerate"])
        except Exception:
            native_rate = PLAYBACK_RATE
        self.playback_rate = native_rate
        self.log(f"Родная частота микрофона: {self.playback_rate} Гц — захват идёт без лишнего ресемплинга.")

        self.custom_beeps = []
        for path in self.config.get("custom_beep_paths", []) or []:
            try:
                data = load_wav_mono_float(path, self.playback_rate)
                self.custom_beeps.append(data)
                self.log(f"Звук загружен: {os.path.basename(path)} ({len(data)} сэмплов)")
            except Exception as e:
                self.log(f"Не удалось загрузить звук '{path}': {e}. Пропускаю.")

        if not self.custom_beeps:
            self.log("Кастомные звуки не заданы — использую стандартный тон.")

        self.delay_buffer = DelayBuffer(self.playback_rate, self.config["delay_sec"])
        self.running = True
        self._partial_processed_count = 0
        self.stats = {"total": 0, "per_word": {}}

        self.rec_thread = threading.Thread(target=self._recognition_loop, daemon=True)
        self.rec_thread.start()

        blocksize = int(self.playback_rate * self.config["block_ms"] / 1000)

        self.input_stream = sd.InputStream(
            samplerate=self.playback_rate, channels=1, dtype="float32",
            blocksize=blocksize, callback=self._audio_in_callback,
            device=self.config["input_device"], latency="high",
        )
        self.output_stream = sd.OutputStream(
            samplerate=self.playback_rate, channels=1, dtype="float32",
            blocksize=blocksize, callback=self._audio_out_callback,
            device=self.config["output_device"], latency="high",
        )
        self.input_stream.start()
        self.output_stream.start()
        self.log(f"Запущено. Задержка вывода: {self.config['delay_sec']} сек.")

    def stop(self):
        self.running = False
        if self.input_stream:
            self.input_stream.stop()
            self.input_stream.close()
        if self.output_stream:
            self.output_stream.stop()
            self.output_stream.close()
        self.log("Остановлено.")

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

    def _process_words(self, words):
        pattern = self.config["swear_pattern"]
        whitelist = self.config.get("whitelist", set())
        for w in words:
            word = w.get("word", "")
            word_normalized = normalize_word(word)
            if word_normalized in whitelist:
                continue
            if pattern.match(word_normalized):
                start_sample = int((w["start"] - self.config["pad_before"]) * self.playback_rate)
                end_sample = int((w["end"] + self.config["pad_after"]) * self.playback_rate)
                start_sample = max(0, start_sample)

                if self.custom_beeps:
                    chosen_sound = random.choice(self.custom_beeps)
                    seg_fn = lambda positions, os_=start_sample, snd=chosen_sound: self._beep_segment_custom(positions, os_, snd)
                else:
                    seg_fn = self._beep_segment_sine

                ok = self.delay_buffer.stamp_beep(start_sample, end_sample, seg_fn)
                tag = "OK" if ok else "ПОЗДНО"
                self.log(f"[МАТ] '{word}' [{w['start']:.2f}s - {w['end']:.2f}s] -> бип: {tag}")
                if ok:
                    self.stats["total"] += 1
                    self.stats["per_word"][word_normalized] = self.stats["per_word"].get(word_normalized, 0) + 1
                    if self.journal_callback:
                        self.journal_callback(word_normalized)
