import os
import re
import sys
import json
import wave
import queue
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
import sounddevice as sd
from vosk import Model, KaldiRecognizer

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
]

PREFIXES = ["", "по", "на", "рас", "раз", "разъ", "за", "вы", "от", "отъ", "у", "пере", "под", "подъ", "до", "при", "об", "объ", "недо", "съ"]

VB_CABLE_URL = "https://vb-audio.com/Cable/"


def resource_path(relative_path):
    if hasattr(sys, "_MEIPASS"):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


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
        return re.compile(r"(?!x)x")  # никогда не совпадёт, если список пуст
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
    def __init__(self, config, log_callback):
        self.config = config
        self.log = log_callback
        self.running = False
        self.model = None
        self.recognizer = None
        self.delay_buffer = None
        self.mic_queue = queue.Queue()
        self.rec_thread = None
        self.input_stream = None
        self.output_stream = None
        self.custom_beep = None
        self._partial_processed_count = 0

    def start(self):
        self.log("Загружаю модель Vosk...")
        self.model = Model(self.config["model_path"])
        self.recognizer = KaldiRecognizer(self.model, REC_RATE)
        self.recognizer.SetWords(True)
        if hasattr(self.recognizer, "SetPartialWords"):
            self.recognizer.SetPartialWords(True)

        if self.config.get("custom_beep_path"):
            try:
                self.custom_beep = load_wav_mono_float(self.config["custom_beep_path"], PLAYBACK_RATE)
                self.log(f"Кастомный звук бипа загружен: {len(self.custom_beep)} сэмплов")
            except Exception as e:
                self.log(f"Не удалось загрузить кастомный звук: {e}. Использую стандартный тон.")
                self.custom_beep = None
        else:
            self.custom_beep = None

        self.delay_buffer = DelayBuffer(PLAYBACK_RATE, self.config["delay_sec"])
        self.running = True
        self._partial_processed_count = 0

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
        self.delay_buffer.write(mono)
        mono_16k = resample_linear(mono, PLAYBACK_RATE, REC_RATE)
        self.mic_queue.put(mono_16k)

    def _audio_out_callback(self, outdata, frames, time_info, status):
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

    def _beep_segment_custom(self, positions, original_start):
        idx = (positions - original_start) % len(self.custom_beep)
        volume = self.config.get("beep_volume", 0.12)
        return self.custom_beep[idx] * (volume / 0.12)

    def _process_words(self, words):
        pattern = self.config["swear_pattern"]
        for w in words:
            word = w.get("word", "")
            word_normalized = word.replace("ё", "е").replace("Ё", "Е")
            if pattern.match(word_normalized):
                start_sample = int((w["start"] - self.config["pad_before"]) * PLAYBACK_RATE)
                end_sample = int((w["end"] + self.config["pad_after"]) * PLAYBACK_RATE)
                start_sample = max(0, start_sample)

                if self.custom_beep is not None:
                    seg_fn = lambda positions, os_=start_sample: self._beep_segment_custom(positions, os_)
                else:
                    seg_fn = self._beep_segment_sine

                ok = self.delay_buffer.stamp_beep(start_sample, end_sample, seg_fn)
                tag = "OK" if ok else "ПОЗДНО"
                self.log(f"[МАТ] '{word}' [{w['start']:.2f}s - {w['end']:.2f}s] -> бип: {tag}")


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Swear Beeper")
        self.root.geometry("620x700")
        self.engine = None
        self.log_queue = queue.Queue()
        self.root_words = list(DEFAULT_ROOT_CORES)
        self.custom_beep_path_var = tk.StringVar(value="")

        self.devices = sd.query_devices()

        self._build_ui()
        self._poll_log_queue()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True)

        main_tab = ttk.Frame(notebook)
        words_tab = ttk.Frame(notebook)
        notebook.add(main_tab, text="Основное")
        notebook.add(words_tab, text="Запрещённые слова")

        # ---------- Основная вкладка ----------
        frame = ttk.Frame(main_tab)
        frame.pack(fill="x", **pad)

        ttk.Label(frame, text="Модель Vosk (папка):").grid(row=0, column=0, sticky="w")
        self.model_path_var = tk.StringVar(value=resource_path("model_ru"))
        ttk.Entry(frame, textvariable=self.model_path_var, width=35).grid(row=0, column=1, sticky="we")
        ttk.Button(frame, text="Обзор...", command=self._browse_model).grid(row=0, column=2)

        ttk.Label(frame, text="Микрофон (вход):").grid(row=1, column=0, sticky="w")
        self.input_device_var = tk.StringVar()
        self.input_combo = ttk.Combobox(frame, textvariable=self.input_device_var, state="readonly", width=45)
        self.input_combo.grid(row=1, column=1, columnspan=2, sticky="we")

        ttk.Label(frame, text="Выход (динамики / кабель):").grid(row=2, column=0, sticky="w")
        self.output_device_var = tk.StringVar()
        self.output_combo = ttk.Combobox(frame, textvariable=self.output_device_var, state="readonly", width=45)
        self.output_combo.grid(row=2, column=1, columnspan=2, sticky="we")

        ttk.Button(frame, text="Обновить список устройств", command=self._refresh_devices).grid(row=3, column=1, sticky="w", pady=(4, 4))
        ttk.Button(frame, text="Скачать VB-CABLE (для Discord)", command=self._open_vbcable).grid(row=3, column=2, sticky="w", pady=(4, 4))

        ttk.Separator(main_tab, orient="horizontal").pack(fill="x", pady=6)

        sliders = ttk.Frame(main_tab)
        sliders.pack(fill="x", **pad)

        self.delay_var = tk.DoubleVar(value=1.5)
        self._add_slider(sliders, 0, "Задержка (сек):", self.delay_var, 0.3, 4.0)

        self.beep_volume_var = tk.DoubleVar(value=0.12)
        self._add_slider(sliders, 1, "Громкость бипа:", self.beep_volume_var, 0.0, 1.0)

        self.pad_before_var = tk.DoubleVar(value=-0.12)
        self._add_slider(sliders, 2, "Паддинг ДО слова (сек):", self.pad_before_var, -0.3, 0.3)

        self.pad_after_var = tk.DoubleVar(value=0.12)
        self._add_slider(sliders, 3, "Паддинг ПОСЛЕ слова (сек):", self.pad_after_var, 0.0, 0.5)

        self.mic_gain_var = tk.DoubleVar(value=1.0)
        self._add_slider(sliders, 4, "Усиление микрофона (x):", self.mic_gain_var, 0.5, 5.0)

        ttk.Separator(main_tab, orient="horizontal").pack(fill="x", pady=6)

        beep_sound_frame = ttk.Frame(main_tab)
        beep_sound_frame.pack(fill="x", **pad)
        ttk.Label(beep_sound_frame, text="Кастомный звук бипа (.wav):").grid(row=0, column=0, sticky="w")
        ttk.Entry(beep_sound_frame, textvariable=self.custom_beep_path_var, width=35, state="readonly").grid(row=0, column=1, sticky="we")
        ttk.Button(beep_sound_frame, text="Обзор...", command=self._browse_beep_sound).grid(row=0, column=2)
        ttk.Button(beep_sound_frame, text="Сбросить (стандартный тон)", command=self._reset_beep_sound).grid(row=1, column=1, sticky="w", pady=(4, 0))

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
        self.log_text = tk.Text(log_frame, height=12, state="disabled")
        self.log_text.pack(fill="both", expand=True)

        # ---------- Вкладка со словами ----------
        words_frame = ttk.Frame(words_tab)
        words_frame.pack(fill="both", expand=True, **pad)

        ttk.Label(words_frame, text="Список слов/корней, которые нужно запикивать:").pack(anchor="w")

        list_container = ttk.Frame(words_frame)
        list_container.pack(fill="both", expand=True, pady=4)

        scrollbar = ttk.Scrollbar(list_container, orient="vertical")
        self.words_listbox = tk.Listbox(list_container, yscrollcommand=scrollbar.set, height=12)
        scrollbar.config(command=self.words_listbox.yview)
        self.words_listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for w in self.root_words:
            self.words_listbox.insert("end", w)

        add_frame = ttk.Frame(words_frame)
        add_frame.pack(fill="x", pady=6)
        self.new_word_var = tk.StringVar()
        ttk.Entry(add_frame, textvariable=self.new_word_var, width=30).pack(side="left", padx=4)
        ttk.Button(add_frame, text="Добавить", command=self._add_word).pack(side="left", padx=4)
        ttk.Button(add_frame, text="Удалить выбранное", command=self._remove_word).pack(side="left", padx=4)

        ttk.Label(
            words_frame,
            text="Подсказка: вводи корень слова без окончания (например 'хуй', а не 'хуйня') —\n"
                 "приложение само учитывает типичные приставки и окончания.",
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        self._refresh_devices()

    def _add_slider(self, parent, row, label, var, frm, to):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        scale = ttk.Scale(parent, from_=frm, to=to, variable=var, orient="horizontal", length=250)
        scale.grid(row=row, column=1, sticky="we", padx=6)
        value_label = ttk.Label(parent, text=f"{var.get():.2f}")
        value_label.grid(row=row, column=2, sticky="w")
        scale.config(command=lambda _v, v=var, l=value_label: l.config(text=f"{v.get():.2f}"))

    def _browse_model(self):
        path = filedialog.askdirectory(title="Выбери папку модели Vosk")
        if path:
            self.model_path_var.set(path)

    def _browse_beep_sound(self):
        path = filedialog.askopenfilename(title="Выбери .wav файл для бипа", filetypes=[("WAV files", "*.wav")])
        if path:
            self.custom_beep_path_var.set(path)

    def _reset_beep_sound(self):
        self.custom_beep_path_var.set("")

    def _open_vbcable(self):
        webbrowser.open(VB_CABLE_URL)

    def _add_word(self):
        word = self.new_word_var.get().strip().lower()
        if not word:
            return
        if word in self.root_words:
            messagebox.showinfo("Инфо", "Это слово уже есть в списке.")
            return
        self.root_words.append(word)
        self.words_listbox.insert("end", word)
        self.new_word_var.set("")

    def _remove_word(self):
        selection = self.words_listbox.curselection()
        if not selection:
            return
        index = selection[0]
        word = self.words_listbox.get(index)
        self.words_listbox.delete(index)
        if word in self.root_words:
            self.root_words.remove(word)

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
        if input_options and not self.input_device_var.get():
            self.input_combo.current(0)
        if output_options and not self.output_device_var.get():
            self.output_combo.current(0)

    def _parse_device_index(self, combo_value):
        return int(combo_value.split(":")[0])

    def _log(self, message):
        self.log_queue.put(message)

    def _poll_log_queue(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get_nowait()
            self.log_text.config(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.root.after(100, self._poll_log_queue)

    def _on_start(self):
        if not self.input_device_var.get() or not self.output_device_var.get():
            messagebox.showerror("Ошибка", "Выбери микрофон и устройство вывода.")
            return

        model_path = self.model_path_var.get()
        if not os.path.isdir(model_path):
            messagebox.showerror("Ошибка", f"Папка модели не найдена: {model_path}")
            return

        if not self.root_words:
            messagebox.showerror("Ошибка", "Список запрещённых слов пуст — добавь хотя бы одно слово.")
            return

        config = {
            "model_path": model_path,
            "input_device": self._parse_device_index(self.input_device_var.get()),
            "output_device": self._parse_device_index(self.output_device_var.get()),
            "delay_sec": self.delay_var.get(),
            "beep_volume": self.beep_volume_var.get(),
            "pad_before": self.pad_before_var.get(),
            "pad_after": self.pad_after_var.get(),
            "mic_gain": self.mic_gain_var.get(),
            "custom_beep_path": self.custom_beep_path_var.get() or None,
            "swear_pattern": build_swear_pattern(self.root_words),
            "block_ms": 50,
        }

        self.engine = SwearBeeperEngine(config, self._log)

        def run_engine():
            try:
                self.engine.start()
            except Exception as e:
                self._log(f"Ошибка запуска: {e}")

        threading.Thread(target=run_engine, daemon=True).start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

    def _on_stop(self):
        if self.engine:
            self.engine.stop()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")


def main():
    root = tk.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
