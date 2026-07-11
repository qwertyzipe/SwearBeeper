import os
import re
import sys
import time
import random
import json
import wave
import math
import queue
import socket
import threading
import webbrowser
import datetime
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
import sounddevice as sd
from vosk import Model, KaldiRecognizer

try:
    import pystray
    from PIL import Image, ImageDraw
    PYSTRAY_AVAILABLE = True
except ImportError:
    PYSTRAY_AVAILABLE = False

try:
    import keyboard
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False

PLAYBACK_RATE = 48000
REC_RATE = 16000
BEEP_FREQ = 1000

DEFAULT_ROOT_CORES = [
    "бля",
    "хуй",
    "хуе",
    "пизд",
    "еб",
    "ебан",
    "сук",
    "гандон",
    "мудак",
    "пидор",
    "хуесос",
    "писька",
    "еблан",
    "долбаеб",
    "уебак",
    "шлюха",
    "хуйня",
    "блядина",
    "сучка",
    "голохуевка",
    "ахуй",
    "дохуя",
    "выблядок",
    "негр",
]

PREFIXES = ["", "по", "на", "рас", "раз", "разъ", "за", "вы", "от", "отъ", "у", "пере", "под", "подъ", "до", "при", "об", "объ", "недо", "съ"]

VB_CABLE_URL = "https://vb-audio.com/Cable/"
SETTINGS_FILENAME = "swear_beeper_settings.json"
JOURNAL_FILENAME = "swear_beeper_journal.log"


def journal_path():
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, JOURNAL_FILENAME)


def append_journal_entry(word):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(journal_path(), "a", encoding="utf-8") as f:
            f.write(f"{timestamp}\t{word}\n")
    except Exception:
        pass
    return timestamp


def load_journal():
    path = journal_path()
    entries = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if "\t" in line:
                        ts, word = line.split("\t", 1)
                        entries.append((ts, word))
        except Exception:
            pass
    return entries


def clear_journal_file():
    path = journal_path()
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass

DEFAULT_DELAY = 1.5
DEFAULT_BEEP_VOLUME = 0.12
DEFAULT_PAD_BEFORE = -0.12
DEFAULT_PAD_AFTER = 0.12
DEFAULT_MIC_GAIN = 1.0
DEFAULT_HOTKEY = "ctrl+alt+m"
SINGLE_INSTANCE_PORT = 47821
OBS_BRIDGE_PORT = 47823

SCANCODE_TO_ENGLISH_KEY = {
    30: "a", 48: "b", 46: "c", 32: "d", 18: "e", 33: "f", 34: "g", 35: "h",
    23: "i", 36: "j", 37: "k", 38: "l", 50: "m", 49: "n", 24: "o", 25: "p",
    16: "q", 19: "r", 31: "s", 20: "t", 22: "u", 47: "v", 17: "w", 45: "x",
    21: "y", 44: "z",
    2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8", 10: "9", 11: "0",
    59: "f1", 60: "f2", 61: "f3", 62: "f4", 63: "f5", 64: "f6",
    65: "f7", 66: "f8", 67: "f9", 68: "f10", 87: "f11", 88: "f12",
}

MODIFIER_KEY_NAMES = {
    "ctrl": {"ctrl", "left ctrl", "right ctrl"},
    "alt": {"alt", "left alt", "right alt", "alt gr"},
    "shift": {"shift", "left shift", "right shift"},
    "windows": {"windows", "left windows", "right windows"},
}

APP_VERSION = "1.2.0"
GITHUB_REPO = "qwertyzipe/SwearBeeper"


def parse_version(v):
    parts = []
    for chunk in v.strip().lstrip("v").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check_for_updates():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SwearBeeper-UpdateCheck"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest_tag = data.get("tag_name", "")
        html_url = data.get("html_url", f"https://github.com/{GITHUB_REPO}/releases")
        if not latest_tag:
            return None, None
        return latest_tag, html_url
    except Exception:
        return None, None


def try_acquire_single_instance():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        s.listen(5)
        return s
    except OSError:
        s.close()
        return None


def signal_existing_instance():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(("127.0.0.1", SINGLE_INSTANCE_PORT))
        s.sendall(b"show")
        s.close()
    except Exception:
        pass


class ObsBridgeServer:
    """Локальный TCP-сервер: рассылает JSON-события подключённому OBS-скрипту
    (счётчик матов, момент цензуры) построчно (JSON Lines)."""

    def __init__(self, port):
        self.port = port
        self.server_socket = None
        self.clients = []
        self.lock = threading.Lock()
        self.running = False

    def start(self):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.server_socket.bind(("127.0.0.1", self.port))
            self.server_socket.listen(5)
        except OSError:
            return False
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return True

    def _accept_loop(self):
        while self.running:
            try:
                conn, _ = self.server_socket.accept()
                with self.lock:
                    self.clients.append(conn)
            except Exception:
                break

    def broadcast(self, data):
        line = (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            dead = []
            for c in self.clients:
                try:
                    c.sendall(line)
                except Exception:
                    dead.append(c)
            for d in dead:
                self.clients.remove(d)

    def stop(self):
        self.running = False
        with self.lock:
            for c in self.clients:
                try:
                    c.close()
                except Exception:
                    pass
            self.clients.clear()
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass


def resource_path(relative_path):
    if hasattr(sys, "_MEIPASS"):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def settings_path():
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, SETTINGS_FILENAME)


def load_settings():
    path = settings_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_settings(data):
    path = settings_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


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
    def __init__(self, config, log_callback, journal_callback=None):
        self.config = config
        self.log = log_callback
        self.journal_callback = journal_callback
        self.running = False
        self.model = None
        self.recognizer = None
        self.delay_buffer = None
        self.mic_queue = queue.Queue()
        self.rec_thread = None
        self.input_stream = None
        self.output_stream = None
        self.custom_beeps = []
        self._partial_processed_count = 0
        self.level = 0.0
        self.manual_mute = False
        self.stats = {"total": 0, "per_word": {}}

    def start(self):
        self.log("Загружаю модель Vosk...")
        self.model = Model(self.config["model_path"])
        self.recognizer = KaldiRecognizer(self.model, REC_RATE)
        self.recognizer.SetWords(True)
        if hasattr(self.recognizer, "SetPartialWords"):
            self.recognizer.SetPartialWords(True)

        self.custom_beeps = []
        for path in self.config.get("custom_beep_paths", []) or []:
            try:
                data = load_wav_mono_float(path, PLAYBACK_RATE)
                self.custom_beeps.append(data)
                self.log(f"Звук загружен: {os.path.basename(path)} ({len(data)} сэмплов)")
            except Exception as e:
                self.log(f"Не удалось загрузить звук '{path}': {e}. Пропускаю.")

        if not self.custom_beeps:
            self.log("Кастомные звуки не заданы — использую стандартный тон.")

        self.delay_buffer = DelayBuffer(PLAYBACK_RATE, self.config["delay_sec"])
        self.running = True
        self._partial_processed_count = 0
        self.stats = {"total": 0, "per_word": {}}

        self.rec_thread = threading.Thread(target=self._recognition_loop, daemon=True)
        self.rec_thread.start()

        blocksize = int(PLAYBACK_RATE * self.config["block_ms"] / 1000)

        self.input_stream = sd.InputStream(
            samplerate=PLAYBACK_RATE, channels=1, dtype="float32",
            blocksize=blocksize, callback=self._audio_in_callback,
            device=self.config["input_device"], latency="high",
        )
        self.output_stream = sd.OutputStream(
            samplerate=PLAYBACK_RATE, channels=1, dtype="float32",
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
        if status:
            self.log(str(status))
        mono = indata[:, 0].copy()
        gain = self.config.get("mic_gain", 1.0)
        if gain != 1.0:
            mono = np.clip(mono * gain, -1.0, 1.0).astype(np.float32)
        self.level = float(np.sqrt(np.mean(mono ** 2))) if len(mono) else 0.0
        self.delay_buffer.write(mono)
        mono_16k = resample_linear(mono, PLAYBACK_RATE, REC_RATE)
        self.mic_queue.put(mono_16k)

    def _audio_out_callback(self, outdata, frames, time_info, status):
        if self.manual_mute:
            outdata[:, 0] = 0
            return
        ready = self.delay_buffer.read_ready()
        if ready >= frames:
            outdata[:, 0] = self.delay_buffer.read(frames)
        else:
            outdata[:, 0] = 0

    def _recognition_loop(self):
        while self.running:
            try:
                chunk = self.mic_queue.get(timeout=0.5)
            except queue.Empty:
                continue

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

    def _beep_segment_sine(self, positions):
        t = positions / PLAYBACK_RATE
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
                start_sample = int((w["start"] - self.config["pad_before"]) * PLAYBACK_RATE)
                end_sample = int((w["end"] + self.config["pad_after"]) * PLAYBACK_RATE)
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


class App:
    def __init__(self, root, single_instance_lock=None):
        self.single_instance_lock = single_instance_lock
        self.root = root
        self.root.title("Swear Beeper")
        self.root.geometry("660x780")
        self.engine = None
        self.mic_test_engine = None
        self.log_queue = queue.Queue()
        self.journal_queue = queue.Queue()
        self.journal_entries = load_journal()
        self.current_level = 0.0
        self.level_display = 0.0
        self.tray_icon = None
        self.current_hotkey = None
        self.alltime_stats = None

        self.saved = load_settings()
        self.root_words = list(self.saved.get("root_words", DEFAULT_ROOT_CORES))
        self.whitelist_words = list(self.saved.get("whitelist_words", []))
        self.custom_beep_paths = list(self.saved.get("custom_beep_paths", []) or [])
        self.alltime_stats = {
            "total": self.saved.get("alltime_total", 0),
            "per_word": dict(self.saved.get("alltime_per_word", {})),
        }

        self.devices = sd.query_devices()

        self._suppress_autosave = True
        self._build_ui()
        self._suppress_autosave = False

        self._poll_log_queue()
        self._poll_vu_meter()
        self._poll_stats()

        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        self._setup_hotkey(self.saved.get("hotkey", DEFAULT_HOTKEY))
        self._setup_tray()
        if getattr(self, "single_instance_lock", None):
            threading.Thread(target=self._listen_single_instance, daemon=True).start()

        self._check_updates_on_startup()

        self.obs_bridge = ObsBridgeServer(OBS_BRIDGE_PORT)
        if self.obs_bridge.start():
            self._log(f"OBS-мост запущен на порту {OBS_BRIDGE_PORT}.")
        else:
            self._log(f"Не удалось запустить OBS-мост на порту {OBS_BRIDGE_PORT} (порт занят?).")


    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True)

        main_tab = ttk.Frame(notebook)
        words_tab = ttk.Frame(notebook)
        stats_tab = ttk.Frame(notebook)
        journal_tab = ttk.Frame(notebook)
        notebook.add(main_tab, text="Основное")
        notebook.add(words_tab, text="Слова")
        notebook.add(stats_tab, text="Статистика")
        notebook.add(journal_tab, text="Журнал")

        self._build_main_tab(main_tab, pad)
        self._build_words_tab(words_tab, pad)
        self._build_stats_tab(stats_tab, pad)
        self._build_journal_tab(journal_tab, pad)

        self._refresh_devices()
        self._restore_device_selection()

    def _build_main_tab(self, main_tab, pad):
        frame = ttk.Frame(main_tab)
        frame.pack(fill="x", **pad)

        ttk.Label(frame, text="Модель Vosk (папка):").grid(row=0, column=0, sticky="w")
        default_model_path = self.saved.get("model_path") or resource_path("model_ru")
        self.model_path_var = tk.StringVar(value=default_model_path)
        self.model_path_var.trace_add("write", self._autosave)
        ttk.Entry(frame, textvariable=self.model_path_var, width=35).grid(row=0, column=1, sticky="we")
        ttk.Button(frame, text="Обзор...", command=self._browse_model).grid(row=0, column=2)
        ttk.Button(frame, text="Сбросить путь", command=self._reset_model_path).grid(row=0, column=3, padx=(4, 0))

        ttk.Label(frame, text="Микрофон (вход):").grid(row=1, column=0, sticky="w")
        self.input_device_var = tk.StringVar()
        self.input_combo = ttk.Combobox(frame, textvariable=self.input_device_var, state="readonly", width=42)
        self.input_combo.grid(row=1, column=1, columnspan=2, sticky="we")
        self.input_combo.bind("<<ComboboxSelected>>", lambda e: self._autosave())

        ttk.Label(frame, text="Выход (динамики / кабель):").grid(row=2, column=0, sticky="w")
        self.output_device_var = tk.StringVar()
        self.output_combo = ttk.Combobox(frame, textvariable=self.output_device_var, state="readonly", width=42)
        self.output_combo.grid(row=2, column=1, columnspan=2, sticky="we")
        self.output_combo.bind("<<ComboboxSelected>>", lambda e: self._autosave())

        ttk.Button(frame, text="Обновить устройства", command=self._refresh_devices).grid(row=3, column=1, sticky="w", pady=(4, 4))
        ttk.Button(frame, text="Скачать VB-CABLE", command=self._open_vbcable).grid(row=3, column=2, sticky="w", pady=(4, 4))
        ttk.Button(frame, text="Проверить обновления", command=self._check_updates_clicked).grid(row=3, column=3, sticky="w", pady=(4, 4))

        vu_frame = ttk.Frame(main_tab)
        vu_frame.pack(fill="x", **pad)
        ttk.Label(vu_frame, text="Уровень микрофона:").pack(side="left")
        self.vu_bar = ttk.Progressbar(vu_frame, orient="horizontal", mode="determinate", maximum=100, length=300)
        self.vu_bar.pack(side="left", padx=8, fill="x", expand=True)

        test_frame = ttk.Frame(main_tab)
        test_frame.pack(fill="x", **pad)
        ttk.Label(test_frame, text="Выход для теста (твои наушники/колонки, НЕ CABLE):").grid(row=0, column=0, sticky="w")
        self.test_output_device_var = tk.StringVar()
        self.test_output_combo = ttk.Combobox(test_frame, textvariable=self.test_output_device_var, state="readonly", width=42)
        self.test_output_combo.grid(row=0, column=1, sticky="we", padx=(4, 0))
        self.test_output_combo.bind("<<ComboboxSelected>>", lambda e: self._autosave())
        self.mic_test_btn = ttk.Button(test_frame, text="Тест микрофона (с цензурой мата)", command=self._toggle_mic_test)
        self.mic_test_btn.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Separator(main_tab, orient="horizontal").pack(fill="x", pady=6)

        sliders = ttk.Frame(main_tab)
        sliders.pack(fill="x", **pad)

        self.delay_var = tk.DoubleVar(value=self.saved.get("delay", DEFAULT_DELAY))
        self._add_slider(sliders, 0, "Задержка (сек):", self.delay_var, 0.3, 4.0, DEFAULT_DELAY)

        self.beep_volume_var = tk.DoubleVar(value=self.saved.get("beep_volume", DEFAULT_BEEP_VOLUME))
        self._add_slider(sliders, 1, "Громкость бипа:", self.beep_volume_var, 0.0, 1.0, DEFAULT_BEEP_VOLUME)

        self.pad_before_var = tk.DoubleVar(value=self.saved.get("pad_before", DEFAULT_PAD_BEFORE))
        self._add_slider(sliders, 2, "Паддинг ДО слова (сек):", self.pad_before_var, -0.3, 0.3, DEFAULT_PAD_BEFORE)

        self.pad_after_var = tk.DoubleVar(value=self.saved.get("pad_after", DEFAULT_PAD_AFTER))
        self._add_slider(sliders, 3, "Паддинг ПОСЛЕ слова (сек):", self.pad_after_var, 0.0, 0.5, DEFAULT_PAD_AFTER)

        self.mic_gain_var = tk.DoubleVar(value=self.saved.get("mic_gain", DEFAULT_MIC_GAIN))
        self._add_slider(sliders, 4, "Усиление микрофона (x):", self.mic_gain_var, 0.5, 5.0, DEFAULT_MIC_GAIN)

        ttk.Separator(main_tab, orient="horizontal").pack(fill="x", pady=6)

        beep_sound_frame = ttk.Frame(main_tab)
        beep_sound_frame.pack(fill="both", **pad)
        ttk.Label(beep_sound_frame, text="Звуки вместо бипа (.wav) — при мате выбирается случайный из списка:").pack(anchor="w")

        beep_list_container = ttk.Frame(beep_sound_frame)
        beep_list_container.pack(fill="x", pady=4)
        beep_scrollbar = ttk.Scrollbar(beep_list_container, orient="vertical")
        self.beep_sounds_listbox = tk.Listbox(beep_list_container, yscrollcommand=beep_scrollbar.set, height=4, selectmode="extended")
        beep_scrollbar.config(command=self.beep_sounds_listbox.yview)
        self.beep_sounds_listbox.pack(side="left", fill="x", expand=True)
        beep_scrollbar.pack(side="right", fill="y")
        for p in self.custom_beep_paths:
            self.beep_sounds_listbox.insert("end", p)

        beep_btn_row = ttk.Frame(beep_sound_frame)
        beep_btn_row.pack(fill="x", pady=(4, 0))
        ttk.Button(beep_btn_row, text="Добавить звук(и)...", command=self._add_beep_sounds).pack(side="left", padx=(0, 4))
        ttk.Button(beep_btn_row, text="Удалить выбранное", command=self._remove_beep_sound).pack(side="left", padx=(0, 4))
        ttk.Button(beep_btn_row, text="Очистить (вернуть тон)", command=self._reset_beep_sound).pack(side="left", padx=(0, 4))
        ttk.Button(beep_btn_row, text="Прослушать случайный", command=self._preview_beep_sound).pack(side="left")

        ttk.Separator(main_tab, orient="horizontal").pack(fill="x", pady=6)

        hotkey_frame = ttk.Frame(main_tab)
        hotkey_frame.pack(fill="x", **pad)
        ttk.Label(hotkey_frame, text="Хоткей мьют/анмьют:").grid(row=0, column=0, sticky="w")
        self.hotkey_var = tk.StringVar(value=self.saved.get("hotkey", DEFAULT_HOTKEY))
        self.hotkey_entry = ttk.Entry(hotkey_frame, textvariable=self.hotkey_var, width=20)
        self.hotkey_entry.grid(row=0, column=1, sticky="w")
        ttk.Button(hotkey_frame, text="Записать", command=self._record_hotkey).grid(row=0, column=2, padx=(4, 0))
        ttk.Button(hotkey_frame, text="Применить", command=self._apply_hotkey).grid(row=0, column=3, padx=(4, 0))
        self.mute_indicator = ttk.Label(hotkey_frame, text="Микрофон: активен", foreground="green")
        self.mute_indicator.grid(row=0, column=4, padx=(12, 0))
        if not KEYBOARD_AVAILABLE:
            ttk.Label(hotkey_frame, text="(модуль 'keyboard' не установлен — хоткей недоступен)", foreground="gray").grid(row=1, column=0, columnspan=5, sticky="w")
        else:
            ttk.Label(hotkey_frame, text="Нажми 'Записать', затем зажми нужную комбинацию клавиш", foreground="gray").grid(row=1, column=0, columnspan=5, sticky="w")

        ttk.Separator(main_tab, orient="horizontal").pack(fill="x", pady=6)

        btn_frame = ttk.Frame(main_tab)
        btn_frame.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btn_frame, text="Старт", command=self._on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btn_frame, text="Стоп", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        log_frame = ttk.Frame(main_tab)
        log_frame.pack(fill="both", expand=True, **pad)
        ttk.Label(log_frame, text="Лог:").pack(anchor="w")
        self.log_text = tk.Text(log_frame, height=10, state="disabled")
        self.log_text.pack(fill="both", expand=True)

    def _build_words_tab(self, words_tab, pad):
        top_frame = ttk.Frame(words_tab)
        top_frame.pack(fill="x", **pad)
        ttk.Button(top_frame, text="Импорт списка слов...", command=self._import_words).pack(side="left", padx=4)
        ttk.Button(top_frame, text="Экспорт списка слов...", command=self._export_words).pack(side="left", padx=4)

        ttk.Label(words_tab, text="Запрещённые слова/корни (будут запикиваться):").pack(anchor="w", padx=8)

        list_container = ttk.Frame(words_tab)
        list_container.pack(fill="both", expand=True, padx=8, pady=4)

        scrollbar = ttk.Scrollbar(list_container, orient="vertical")
        self.words_listbox = tk.Listbox(list_container, yscrollcommand=scrollbar.set, height=8, selectmode="extended")
        scrollbar.config(command=self.words_listbox.yview)
        self.words_listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for w in self.root_words:
            self.words_listbox.insert("end", w)

        add_frame = ttk.Frame(words_tab)
        add_frame.pack(fill="x", padx=8, pady=6)
        self.new_word_var = tk.StringVar()
        ttk.Entry(add_frame, textvariable=self.new_word_var, width=30).pack(side="left", padx=4)
        ttk.Button(add_frame, text="Добавить", command=self._add_word).pack(side="left", padx=4)
        ttk.Button(add_frame, text="Удалить выбранное", command=self._remove_word).pack(side="left", padx=4)
        ttk.Button(add_frame, text="Очистить весь список", command=self._clear_words).pack(side="left", padx=4)

        ttk.Separator(words_tab, orient="horizontal").pack(fill="x", pady=8)

        ttk.Label(words_tab, text="Белый список (эти слова НЕ пикать, даже если похожи на мат):").pack(anchor="w", padx=8)

        wl_container = ttk.Frame(words_tab)
        wl_container.pack(fill="both", expand=True, padx=8, pady=4)

        wl_scrollbar = ttk.Scrollbar(wl_container, orient="vertical")
        self.whitelist_listbox = tk.Listbox(wl_container, yscrollcommand=wl_scrollbar.set, height=6, selectmode="extended")
        wl_scrollbar.config(command=self.whitelist_listbox.yview)
        self.whitelist_listbox.pack(side="left", fill="both", expand=True)
        wl_scrollbar.pack(side="right", fill="y")

        for w in self.whitelist_words:
            self.whitelist_listbox.insert("end", w)

        wl_add_frame = ttk.Frame(words_tab)
        wl_add_frame.pack(fill="x", padx=8, pady=6)
        self.new_whitelist_word_var = tk.StringVar()
        ttk.Entry(wl_add_frame, textvariable=self.new_whitelist_word_var, width=30).pack(side="left", padx=4)
        ttk.Button(wl_add_frame, text="Добавить в белый список", command=self._add_whitelist_word).pack(side="left", padx=4)
        ttk.Button(wl_add_frame, text="Удалить выбранное", command=self._remove_whitelist_word).pack(side="left", padx=4)
        ttk.Button(wl_add_frame, text="Очистить весь список", command=self._clear_whitelist).pack(side="left", padx=4)

    def _build_stats_tab(self, stats_tab, pad):
        frame = ttk.Frame(stats_tab)
        frame.pack(fill="both", expand=True, **pad)

        self.stats_session_label = ttk.Label(frame, text="Матов за сессию: 0", font=("", 13, "bold"))
        self.stats_session_label.pack(anchor="w", pady=(0, 2))

        self.stats_alltime_label = ttk.Label(frame, text="Матов за всё время: 0", font=("", 13, "bold"))
        self.stats_alltime_label.pack(anchor="w", pady=(0, 8))

        ttk.Label(frame, text="Таблица-рейтинг (кто чаще всего — тот выше):").pack(anchor="w")
        self.stats_text = tk.Text(frame, height=15, state="disabled", font=("Consolas", 10))
        self.stats_text.pack(fill="both", expand=True, pady=4)

        btn_row = ttk.Frame(frame)
        btn_row.pack(anchor="w", pady=(6, 0))
        ttk.Button(btn_row, text="Сбросить статистику сессии", command=self._reset_session_stats).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Сбросить статистику за всё время", command=self._reset_alltime_stats).pack(side="left")

    def _build_journal_tab(self, journal_tab, pad):
        frame = ttk.Frame(journal_tab)
        frame.pack(fill="both", expand=True, **pad)

        ttk.Label(frame, text="Журнал матов — хронологический список (что и когда было сказано):").pack(anchor="w")

        text_container = ttk.Frame(frame)
        text_container.pack(fill="both", expand=True, pady=4)
        scrollbar = ttk.Scrollbar(text_container, orient="vertical")
        self.journal_text = tk.Text(text_container, height=20, state="disabled", yscrollcommand=scrollbar.set, font=("Consolas", 10))
        scrollbar.config(command=self.journal_text.yview)
        self.journal_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for ts, word in self.journal_entries:
            self._append_journal_line(ts, word)

        ttk.Button(frame, text="Очистить журнал", command=self._clear_journal).pack(anchor="w", pady=(6, 0))

    def _append_journal_line(self, timestamp, word):
        self.journal_text.config(state="normal")
        self.journal_text.insert("end", f"{timestamp}\t{word}\n")
        self.journal_text.see("end")
        self.journal_text.config(state="disabled")

    def _clear_journal(self):
        if not messagebox.askyesno("Подтверждение", "Удалить весь журнал матов (всю историю)?"):
            return
        clear_journal_file()
        self.journal_entries = []
        self.journal_text.config(state="normal")
        self.journal_text.delete("1.0", "end")
        self.journal_text.config(state="disabled")
        self._log("Журнал матов очищен.")

    def _add_slider(self, parent, row, label, var, frm, to, default_value):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        scale = ttk.Scale(parent, from_=frm, to=to, variable=var, orient="horizontal", length=220)
        scale.grid(row=row, column=1, sticky="we", padx=6)
        value_label = ttk.Label(parent, text=f"{var.get():.2f}", width=6)
        value_label.grid(row=row, column=2, sticky="w")

        def on_change(_v, v=var, l=value_label):
            l.config(text=f"{v.get():.2f}")
            self._autosave()

        scale.config(command=on_change)

        def reset(v=var, l=value_label, d=default_value):
            v.set(d)
            l.config(text=f"{d:.2f}")
            self._autosave()

        reset_btn = ttk.Button(parent, text="↺", width=3, command=reset)
        reset_btn.grid(row=row, column=3, sticky="w", padx=(4, 0))


    def _reset_model_path(self):
        self.model_path_var.set(resource_path("model_ru"))
        self._autosave()

    def _browse_model(self):
        path = filedialog.askdirectory(title="Выбери папку модели Vosk")
        if path:
            self.model_path_var.set(path)

    def _add_beep_sounds(self):
        paths = filedialog.askopenfilenames(title="Выбери .wav файл(ы) для замены бипа", filetypes=[("WAV files", "*.wav")])
        if not paths:
            return
        for p in paths:
            if p not in self.custom_beep_paths:
                self.custom_beep_paths.append(p)
                self.beep_sounds_listbox.insert("end", p)
        self._autosave()

    def _remove_beep_sound(self):
        selection = self.beep_sounds_listbox.curselection()
        if not selection:
            return
        for index in sorted(selection, reverse=True):
            path = self.beep_sounds_listbox.get(index)
            self.beep_sounds_listbox.delete(index)
            if path in self.custom_beep_paths:
                self.custom_beep_paths.remove(path)
        self._autosave()

    def _reset_beep_sound(self):
        self.beep_sounds_listbox.delete(0, "end")
        self.custom_beep_paths.clear()
        self._autosave()

    def _preview_beep_sound(self):
        try:
            out_val = self.test_output_device_var.get() or self.output_device_var.get()
            device_idx = self._parse_device_index(out_val) if out_val else None
            if self.custom_beep_paths:
                path = random.choice(self.custom_beep_paths)
                data = load_wav_mono_float(path, PLAYBACK_RATE)
                self._log(f"Превью случайного звука: {os.path.basename(path)}")
            else:
                t = np.arange(int(PLAYBACK_RATE * 0.3)) / PLAYBACK_RATE
                data = (self.beep_volume_var.get() * np.sin(2 * np.pi * BEEP_FREQ * t)).astype(np.float32)
            sd.play(data, PLAYBACK_RATE, device=device_idx)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось воспроизвести звук: {e}")

    def _open_vbcable(self):
        webbrowser.open(VB_CABLE_URL)

    def _check_updates_on_startup(self):
        def worker():
            latest_tag, html_url = check_for_updates()
            self.root.after(0, lambda: self._on_startup_update_check_result(latest_tag, html_url))

        threading.Thread(target=worker, daemon=True).start()

    def _on_startup_update_check_result(self, latest_tag, html_url):
        if latest_tag is None:
            return

        current = parse_version(APP_VERSION)
        latest = parse_version(latest_tag)

        if latest > current:
            self._log(f"Доступна новая версия: {latest_tag} (у тебя {APP_VERSION})")
            if messagebox.askyesno("Доступно обновление", f"Вышла новая версия {latest_tag} (у тебя {APP_VERSION}). Открыть страницу релиза?"):
                webbrowser.open(html_url)

    def _check_updates_clicked(self):
        self._log("Проверяю обновления на GitHub...")

        def worker():
            latest_tag, html_url = check_for_updates()
            self.root.after(0, lambda: self._on_update_check_result(latest_tag, html_url))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_check_result(self, latest_tag, html_url):
        if latest_tag is None:
            self._log("Не удалось проверить обновления (нет интернета или репозиторий недоступен).")
            return

        current = parse_version(APP_VERSION)
        latest = parse_version(latest_tag)

        if latest > current:
            self._log(f"Доступна новая версия: {latest_tag} (у тебя {APP_VERSION})")
            if messagebox.askyesno("Доступно обновление", f"Вышла новая версия {latest_tag} (у тебя {APP_VERSION}). Открыть страницу релиза?"):
                webbrowser.open(html_url)
        else:
            self._log(f"У тебя последняя версия ({APP_VERSION}).")
            messagebox.showinfo("Обновления", f"У тебя установлена последняя версия ({APP_VERSION}).")

    def _refresh_devices(self):
        self.devices = sd.query_devices()
        input_options = []
        output_options = []
        for i, d in enumerate(self.devices):
            label = f"{i}: {d['name']}"
            if d["max_input_channels"] > 0:
                input_options.append(label)
            if d["max_output_channels"] > 0:
                output_options.append(label)

        self.input_combo["values"] = input_options
        self.output_combo["values"] = output_options
        self.test_output_combo["values"] = output_options

        if input_options and not self.input_device_var.get():
            self.input_combo.current(0)
        if output_options and not self.output_device_var.get():
            self.output_combo.current(0)

        if output_options and not self.test_output_device_var.get():
            non_cable = [o for o in output_options if "cable" not in o.lower()]
            self.test_output_device_var.set(non_cable[0] if non_cable else output_options[0])

    def _restore_device_selection(self):
        saved_input_name = self.saved.get("input_device_name")
        saved_output_name = self.saved.get("output_device_name")
        saved_test_output_name = self.saved.get("test_output_device_name")

        if saved_input_name:
            for item in self.input_combo["values"]:
                name = item.split(":", 1)[1].strip() if ":" in item else item
                if name == saved_input_name:
                    self.input_device_var.set(item)
                    break

        if saved_output_name:
            for item in self.output_combo["values"]:
                name = item.split(":", 1)[1].strip() if ":" in item else item
                if name == saved_output_name:
                    self.output_device_var.set(item)
                    break

        if saved_test_output_name:
            for item in self.test_output_combo["values"]:
                name = item.split(":", 1)[1].strip() if ":" in item else item
                if name == saved_test_output_name:
                    self.test_output_device_var.set(item)
                    break

    def _parse_device_index(self, combo_value):
        return int(combo_value.split(":")[0])

    def _parse_device_name(self, combo_value):
        return combo_value.split(":", 1)[1].strip() if ":" in combo_value else combo_value


    def _add_word(self):
        word = normalize_word(self.new_word_var.get())
        if not word:
            return
        if word in self.root_words:
            messagebox.showinfo("Инфо", "Это слово уже есть в списке.")
            return
        self.root_words.append(word)
        self.words_listbox.insert("end", word)
        self.new_word_var.set("")
        self._autosave()

    def _remove_word(self):
        selection = self.words_listbox.curselection()
        if not selection:
            return
        for index in sorted(selection, reverse=True):
            word = self.words_listbox.get(index)
            self.words_listbox.delete(index)
            if word in self.root_words:
                self.root_words.remove(word)
        self._autosave()

    def _clear_words(self):
        if not self.root_words:
            return
        if not messagebox.askyesno("Подтверждение", "Удалить ВСЕ запрещённые слова из списка?"):
            return
        self.words_listbox.delete(0, "end")
        self.root_words.clear()
        self._autosave()

    def _add_whitelist_word(self):
        word = normalize_word(self.new_whitelist_word_var.get())
        if not word:
            return
        if word in self.whitelist_words:
            messagebox.showinfo("Инфо", "Это слово уже есть в белом списке.")
            return
        self.whitelist_words.append(word)
        self.whitelist_listbox.insert("end", word)
        self.new_whitelist_word_var.set("")
        self._autosave()

    def _remove_whitelist_word(self):
        selection = self.whitelist_listbox.curselection()
        if not selection:
            return
        for index in sorted(selection, reverse=True):
            word = self.whitelist_listbox.get(index)
            self.whitelist_listbox.delete(index)
            if word in self.whitelist_words:
                self.whitelist_words.remove(word)
        self._autosave()

    def _clear_whitelist(self):
        if not self.whitelist_words:
            return
        if not messagebox.askyesno("Подтверждение", "Удалить ВЕСЬ белый список?"):
            return
        self.whitelist_listbox.delete(0, "end")
        self.whitelist_words.clear()
        self._autosave()

    def _export_words(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON files", "*.json")])
        if not path:
            return
        data = {"root_words": self.root_words, "whitelist_words": self.whitelist_words}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._log(f"Список слов экспортирован: {path}")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить файл: {e}")

    def _import_words(self):
        path = filedialog.askopenfilename(filetypes=[("JSON files", "*.json")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось прочитать файл: {e}")
            return

        imported_roots = data.get("root_words", [])
        imported_whitelist = data.get("whitelist_words", [])
        added_roots = 0
        added_whitelist = 0

        for w in imported_roots:
            wn = normalize_word(w)
            if wn and wn not in self.root_words:
                self.root_words.append(wn)
                self.words_listbox.insert("end", wn)
                added_roots += 1

        for w in imported_whitelist:
            wn = normalize_word(w)
            if wn and wn not in self.whitelist_words:
                self.whitelist_words.append(wn)
                self.whitelist_listbox.insert("end", wn)
                added_whitelist += 1

        self._autosave()
        self._log(f"Импортировано: {added_roots} новых слов, {added_whitelist} в белый список")


    def _toggle_mute(self):
        if self.engine and self.engine.running:
            self.engine.manual_mute = not self.engine.manual_mute
            state_text = "ЗАМЬЮЧЕН" if self.engine.manual_mute else "активен"
            color = "red" if self.engine.manual_mute else "green"
            self.mute_indicator.config(text=f"Микрофон: {state_text}", foreground=color)
            self._log(f"Микрофон {state_text} (хоткей/трей)")
        else:
            self._log("Хоткей нажат, но приложение не запущено (нажми Старт).")

    def _record_hotkey(self):
        if not KEYBOARD_AVAILABLE:
            messagebox.showerror("Ошибка", "Модуль 'keyboard' не установлен.")
            return

        self.hotkey_entry.config(state="disabled")
        self.hotkey_var.set("Нажми комбинацию...")

        def worker():
            modifiers = set()
            result_holder = {}

            def on_event(event):
                if event.event_type != "down":
                    return
                name = (event.name or "").lower()

                matched_modifier = None
                for mod_label, variants in MODIFIER_KEY_NAMES.items():
                    if name in variants:
                        matched_modifier = mod_label
                        break

                if matched_modifier:
                    modifiers.add(matched_modifier)
                else:
                    key_name = SCANCODE_TO_ENGLISH_KEY.get(event.scan_code, name)
                    result_holder["key"] = key_name

            hook = keyboard.hook(on_event)
            start_time = time.time()
            while "key" not in result_holder and time.time() - start_time < 15:
                time.sleep(0.05)
            keyboard.unhook(hook)

            if "key" in result_holder:
                combo_parts = sorted(modifiers) + [result_holder["key"]]
                combo = "+".join(combo_parts)
            else:
                combo = None

            self.root.after(0, lambda: self._on_hotkey_recorded(combo))

        threading.Thread(target=worker, daemon=True).start()

    def _on_hotkey_recorded(self, combo):
        self.hotkey_entry.config(state="normal")
        if combo:
            self.hotkey_var.set(combo)
            self._setup_hotkey(combo)
            self._autosave()
        else:
            self.hotkey_var.set(self.current_hotkey or DEFAULT_HOTKEY)

    def _apply_hotkey(self):
        new_combo = self.hotkey_var.get().strip()
        if not new_combo:
            return
        self._setup_hotkey(new_combo)
        self._autosave()

    def _setup_hotkey(self, combo):
        if not KEYBOARD_AVAILABLE:
            return
        try:
            if self.current_hotkey:
                keyboard.remove_hotkey(self.current_hotkey)
        except Exception:
            pass
        try:
            keyboard.add_hotkey(combo, lambda: self.root.after(0, self._toggle_mute))
            self.current_hotkey = combo
            self._log(f"Хоткей установлен: {combo}")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось установить хоткей '{combo}': {e}")


    def _toggle_mic_test(self):
        if self.mic_test_engine and self.mic_test_engine.running:
            self.mic_test_engine.stop()
            self.mic_test_engine = None
            self.mic_test_btn.config(text="Тест микрофона (с цензурой мата)")
            self._log("Тест микрофона остановлен.")
            return

        if self.engine and self.engine.running:
            messagebox.showerror("Ошибка", "Сначала останови основной движок (кнопка Стоп).")
            return

        config = self._validate_and_build_config(override_output_device=self.test_output_device_var.get())
        if config is None:
            return

        self.mic_test_engine = SwearBeeperEngine(config, self._log, journal_callback=self._on_swear_journal)
        try:
            self.mic_test_engine.start()
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось запустить тест микрофона: {e}")
            self.mic_test_engine = None
            return

        self.mic_test_btn.config(text="Остановить тест")
        self._log("Тест запущен — говори маты и слушай через выбранный выход (цензура применяется по-настоящему, как при Старт).")


    def _get_current_totals(self):
        active_engine = self.engine if (self.engine and self.engine.running) else (
            self.mic_test_engine if (self.mic_test_engine and self.mic_test_engine.running) else None
        )
        session_total = active_engine.stats.get("total", 0) if active_engine else 0
        alltime_total_display = self.alltime_stats["total"] + session_total
        return session_total, alltime_total_display

    def _on_swear_journal(self, word):
        timestamp = append_journal_entry(word)
        self.journal_queue.put((timestamp, word))

        if getattr(self, "obs_bridge", None):
            session_total, alltime_total = self._get_current_totals()
            self.obs_bridge.broadcast({
                "type": "censor_event",
                "session_total": session_total,
                "alltime_total": alltime_total,
                "ts": timestamp,
            })

    def _log(self, message):
        self.log_queue.put(message)

    def _poll_log_queue(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get_nowait()
            self.log_text.config(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")

        while not self.journal_queue.empty():
            ts, word = self.journal_queue.get_nowait()
            self.journal_entries.append((ts, word))
            self._append_journal_line(ts, word)

        self.root.after(100, self._poll_log_queue)

    def _poll_vu_meter(self):
        level = 0.0
        if self.engine and self.engine.running:
            level = getattr(self.engine, "level", 0.0)
        elif self.mic_test_engine and self.mic_test_engine.running:
            level = getattr(self.mic_test_engine, "level", 0.0)

        self.level_display = 0.6 * self.level_display + 0.4 * level
        percent = level_to_percent(self.level_display)
        self.vu_bar["value"] = percent
        self.root.after(80, self._poll_vu_meter)

    def _poll_stats(self):
        active_engine = self.engine if (self.engine and self.engine.running) else (
            self.mic_test_engine if (self.mic_test_engine and self.mic_test_engine.running) else None
        )

        session_total = active_engine.stats.get("total", 0) if active_engine else 0
        session_per_word = active_engine.stats.get("per_word", {}) if active_engine else {}

        alltime_total_display = self.alltime_stats["total"] + session_total
        combined_per_word = dict(self.alltime_stats["per_word"])
        for w, c in session_per_word.items():
            combined_per_word[w] = combined_per_word.get(w, 0) + c

        self.stats_session_label.config(text=f"Матов за сессию: {session_total}")
        self.stats_alltime_label.config(text=f"Матов за всё время: {alltime_total_display}")

        ranked = sorted(combined_per_word.items(), key=lambda x: -x[1])
        lines = [f"{i}. {w:<20} {c}" for i, (w, c) in enumerate(ranked, start=1)]
        content = "\n".join(lines) if lines else "(пока пусто)"

        self.stats_text.config(state="normal")
        self.stats_text.delete("1.0", "end")
        self.stats_text.insert("1.0", content)
        self.stats_text.config(state="disabled")

        if getattr(self, "obs_bridge", None):
            self.obs_bridge.broadcast({
                "type": "snapshot",
                "session_total": session_total,
                "alltime_total": alltime_total_display,
            })

        self.root.after(1000, self._poll_stats)

    def _commit_session_stats_to_alltime(self):
        for eng in (self.engine, self.mic_test_engine):
            if not eng:
                continue
            session_total = eng.stats.get("total", 0)
            self.alltime_stats["total"] += session_total
            for w, c in eng.stats.get("per_word", {}).items():
                self.alltime_stats["per_word"][w] = self.alltime_stats["per_word"].get(w, 0) + c
            eng.stats = {"total": 0, "per_word": {}}

    def _reset_session_stats(self):
        for eng in (self.engine, self.mic_test_engine):
            if eng:
                eng.stats = {"total": 0, "per_word": {}}
        self._log("Статистика сессии сброшена.")

    def _reset_alltime_stats(self):
        self.alltime_stats = {"total": 0, "per_word": {}}
        self._autosave()
        self._log("Статистика за всё время сброшена.")


    def _collect_settings(self):
        current_model_path = self.model_path_var.get()
        default_model_path = resource_path("model_ru")
        model_path_to_save = None if current_model_path == default_model_path else current_model_path

        return {
            "delay": self.delay_var.get(),
            "beep_volume": self.beep_volume_var.get(),
            "pad_before": self.pad_before_var.get(),
            "pad_after": self.pad_after_var.get(),
            "mic_gain": self.mic_gain_var.get(),
            "model_path": model_path_to_save,
            "custom_beep_paths": list(self.custom_beep_paths),
            "root_words": list(self.root_words),
            "whitelist_words": list(self.whitelist_words),
            "hotkey": self.hotkey_var.get() if hasattr(self, "hotkey_var") else DEFAULT_HOTKEY,
            "alltime_total": self.alltime_stats["total"],
            "alltime_per_word": self.alltime_stats["per_word"],
            "input_device_name": self._parse_device_name(self.input_device_var.get()) if self.input_device_var.get() else None,
            "output_device_name": self._parse_device_name(self.output_device_var.get()) if self.output_device_var.get() else None,
            "test_output_device_name": self._parse_device_name(self.test_output_device_var.get()) if self.test_output_device_var.get() else None,
        }

    def _autosave(self, *args):
        if getattr(self, "_suppress_autosave", False):
            return
        save_settings(self._collect_settings())


    def _load_tray_icon_image(self):
        icon_path = resource_path("icon.ico")
        if os.path.isfile(icon_path):
            try:
                return Image.open(icon_path)
            except Exception:
                pass
        img = Image.new("RGB", (64, 64), color=(30, 30, 30))
        d = ImageDraw.Draw(img)
        d.ellipse((8, 8, 56, 56), fill=(200, 50, 50))
        d.text((18, 24), "SB", fill=(255, 255, 255))
        return img

    def _setup_tray(self):
        if not PYSTRAY_AVAILABLE:
            self._log("pystray/Pillow не установлены — трей недоступен (pip install pystray pillow)")
            return

        image = self._load_tray_icon_image()
        menu = pystray.Menu(
            pystray.MenuItem("Показать окно", self._tray_show, default=True),
            pystray.MenuItem("Мьют/Анмьют микро", self._tray_toggle_mute),
            pystray.MenuItem("Выход", self._tray_exit),
        )
        self.tray_icon = pystray.Icon("SwearBeeper", image, "Swear Beeper", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _listen_single_instance(self):
        while True:
            try:
                conn, _ = self.single_instance_lock.accept()
                data = conn.recv(1024)
                conn.close()
                if data:
                    self.root.after(0, self._show_from_other_instance)
            except Exception:
                break

    def _show_from_other_instance(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _tray_show(self, icon=None, item=None):
        self.root.after(0, self.root.deiconify)

    def _tray_toggle_mute(self, icon=None, item=None):
        self.root.after(0, self._toggle_mute)

    def _tray_exit(self, icon=None, item=None):
        self.root.after(0, self._full_exit)

    def _on_window_close(self):
        self._autosave()
        if PYSTRAY_AVAILABLE and self.tray_icon:
            self.root.withdraw()
            self._log("Свернуто в трей. Для полного выхода используй меню трея.")
        else:
            self._full_exit()

    def _full_exit(self):
        self._commit_session_stats_to_alltime()
        self._autosave()
        if self.engine:
            self.engine.stop()
        if self.mic_test_engine:
            self.mic_test_engine.stop()
        if KEYBOARD_AVAILABLE:
            try:
                keyboard.unhook_all_hotkeys()
            except Exception:
                pass
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        if getattr(self, "obs_bridge", None):
            self.obs_bridge.stop()
        self.root.destroy()
        sys.exit(0)


    def _validate_and_build_config(self, override_output_device=None):
        output_device_combo_value = override_output_device or self.output_device_var.get()

        if not self.input_device_var.get() or not output_device_combo_value:
            messagebox.showerror("Ошибка", "Выбери микрофон и устройство вывода.")
            return None

        model_path = self.model_path_var.get()
        if not os.path.isdir(model_path):
            messagebox.showerror("Ошибка", f"Папка модели не найдена: {model_path}")
            return None

        if not self.root_words:
            messagebox.showerror("Ошибка", "Список запрещённых слов пуст — добавь хотя бы одно слово.")
            return None

        self._autosave()

        return {
            "model_path": model_path,
            "input_device": self._parse_device_index(self.input_device_var.get()),
            "output_device": self._parse_device_index(output_device_combo_value),
            "delay_sec": self.delay_var.get(),
            "beep_volume": self.beep_volume_var.get(),
            "pad_before": self.pad_before_var.get(),
            "pad_after": self.pad_after_var.get(),
            "mic_gain": self.mic_gain_var.get(),
            "custom_beep_paths": list(self.custom_beep_paths),
            "swear_pattern": build_swear_pattern(self.root_words),
            "whitelist": set(self.whitelist_words),
            "block_ms": 50,
        }

    def _on_start(self):
        if self.mic_test_engine and self.mic_test_engine.running:
            messagebox.showerror("Ошибка", "Сначала останови тест микрофона.")
            return

        config = self._validate_and_build_config()
        if config is None:
            return

        self.engine = SwearBeeperEngine(config, self._log, journal_callback=self._on_swear_journal)

        def run_engine():
            try:
                self.engine.start()
            except Exception as e:
                self._log(f"Ошибка запуска: {e}")

        threading.Thread(target=run_engine, daemon=True).start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.mute_indicator.config(text="Микрофон: активен", foreground="green")

    def _on_stop(self):
        self._commit_session_stats_to_alltime()
        self._autosave()
        if self.engine:
            self.engine.stop()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")


def main():
    lock_socket = try_acquire_single_instance()
    if lock_socket is None:
        signal_existing_instance()
        sys.exit(0)

    root = tk.Tk()
    app = App(root, single_instance_lock=lock_socket)
    root.mainloop()


if __name__ == "__main__":
    main()
