import os
import re
import sys
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
import sounddevice as sd
from vosk import Model, KaldiRecognizer

PLAYBACK_RATE = 48000   # частота для захвата/вывода звука (полное качество для человека)
REC_RATE = 16000        # частота, которую требует Vosk для распознавания
BEEP_FREQ = 1000


def resample_linear(x, orig_sr, target_sr):
    if orig_sr == target_sr or len(x) == 0:
        return x.astype(np.float32)
    duration = len(x) / orig_sr
    target_len = max(1, int(duration * target_sr))
    orig_idx = np.linspace(0, len(x) - 1, num=len(x))
    target_idx = np.linspace(0, len(x) - 1, num=target_len)
    return np.interp(target_idx, orig_idx, x).astype(np.float32)


def resource_path(relative_path):
    if hasattr(sys, "_MEIPASS"):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

ROOT_CORES = [
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
    "долбаёб",
    "уёбак",
    "уебак",
    "шлюха",
    "хуйня",
    "блядина",
    "сучка",
    "сус",
    "голохуевка",
    "ахуй",
    "дохуя",
    "выблядок",
    "негр",
]

PREFIXES = ["", "по", "на", "рас", "раз", "разъ", "за", "вы", "от", "отъ", "у", "пере", "под", "подъ", "до", "при", "об", "объ", "недо", "съ"]

SWEAR_PATTERN = re.compile(
    "^(?:" + "|".join(PREFIXES) + ")(?:" + "|".join(ROOT_CORES) + r")\w*$",
    re.IGNORECASE,
)


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

    def stamp_beep(self, global_start_sample, global_end_sample, beep_volume):
        with self.lock:
            if global_end_sample <= self.read_pos:
                return False

            start = max(global_start_sample, self.read_pos)
            end = min(global_end_sample, self.write_pos)
            if end <= start:
                return False

            n = end - start
            t = (np.arange(n) + start) / self.sr
            beep = beep_volume * np.sin(2 * np.pi * BEEP_FREQ * t).astype(np.float32)

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

    def start(self):
        self.log("Загружаю модель Vosk...")
        self.model = Model(self.config["model_path"])
        self.recognizer = KaldiRecognizer(self.model, REC_RATE)
        self.recognizer.SetWords(True)

        self.delay_buffer = DelayBuffer(PLAYBACK_RATE, self.config["delay_sec"])
        self.running = True

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
                text = result.get("text", "")
                if text:
                    self.log(f"[РАСПОЗНАНО] {text}")
                self._process_words(result.get("result", []))
            else:
                json.loads(self.recognizer.PartialResult())

    def _process_words(self, words):
        for w in words:
            word = w.get("word", "")
            word_normalized = word.replace("ё", "е").replace("Ё", "Е")
            if SWEAR_PATTERN.match(word_normalized):
                start_sample = int((w["start"] - self.config["pad_before"]) * PLAYBACK_RATE)
                end_sample = int((w["end"] + self.config["pad_after"]) * PLAYBACK_RATE)
                start_sample = max(0, start_sample)
                ok = self.delay_buffer.stamp_beep(start_sample, end_sample, self.config["beep_volume"])
                tag = "OK" if ok else "ПОЗДНО"
                self.log(f"[МАТ] '{word}' [{w['start']:.2f}s - {w['end']:.2f}s] -> бип: {tag}")


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Swear Beeper")
        self.root.geometry("560x520")
        self.engine = None
        self.log_queue = queue.Queue()

        self.devices = sd.query_devices()

        self._build_ui()
        self._poll_log_queue()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        frame = ttk.Frame(self.root)
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

        ttk.Button(frame, text="Обновить список устройств", command=self._refresh_devices).grid(row=3, column=1, sticky="w", pady=(4, 8))

        ttk.Separator(self.root, orient="horizontal").pack(fill="x", pady=6)

        sliders = ttk.Frame(self.root)
        sliders.pack(fill="x", **pad)

        self.delay_var = tk.DoubleVar(value=1.5)
        self._add_slider(sliders, 0, "Задержка (сек):", self.delay_var, 0.3, 4.0)

        self.beep_volume_var = tk.DoubleVar(value=0.12)
        self._add_slider(sliders, 1, "Громкость бипа:", self.beep_volume_var, 0.0, 1.0)

        self.pad_before_var = tk.DoubleVar(value=-0.12)
        self._add_slider(sliders, 2, "Паддинг ДО слова (сек):", self.pad_before_var, -0.3, 0.3)

        self.pad_after_var = tk.DoubleVar(value=0.12)
        self._add_slider(sliders, 3, "Паддинг ПОСЛЕ слова (сек):", self.pad_after_var, 0.0, 0.5)

        ttk.Separator(self.root, orient="horizontal").pack(fill="x", pady=6)

        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btn_frame, text="Старт", command=self._on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btn_frame, text="Стоп", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill="both", expand=True, **pad)
        ttk.Label(log_frame, text="Лог:").pack(anchor="w")
        self.log_text = tk.Text(log_frame, height=15, state="disabled")
        self.log_text.pack(fill="both", expand=True)

        self._refresh_devices()

    def _add_slider(self, parent, row, label, var, frm, to):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        scale = ttk.Scale(parent, from_=frm, to=to, variable=var, orient="horizontal", length=250)
        scale.grid(row=row, column=1, sticky="we", padx=6)
        value_label = ttk.Label(parent, text=f"{var.get():.2f}")
        value_label.grid(row=row, column=2, sticky="w")

        def update_label(_event=None, v=var, l=value_label):
            l.config(text=f"{v.get():.2f}")

        scale.config(command=lambda _v, v=var, l=value_label: l.config(text=f"{v.get():.2f}"))

    def _browse_model(self):
        path = filedialog.askdirectory(title="Выбери папку модели Vosk")
        if path:
            self.model_path_var.set(path)

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

        config = {
            "model_path": model_path,
            "input_device": self._parse_device_index(self.input_device_var.get()),
            "output_device": self._parse_device_index(self.output_device_var.get()),
            "delay_sec": self.delay_var.get(),
            "beep_volume": self.beep_volume_var.get(),
            "pad_before": self.pad_before_var.get(),
            "pad_after": self.pad_after_var.get(),
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
