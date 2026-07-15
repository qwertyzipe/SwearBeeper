import os
import sys
import time
import json
import queue
import random
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
import sounddevice as sd

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

from config import (
    PLAYBACK_RATE, BEEP_FREQ, DEFAULT_ROOT_CORES, VB_CABLE_URL,
    DEFAULT_DELAY, DEFAULT_BEEP_VOLUME, DEFAULT_PAD_BEFORE, DEFAULT_PAD_AFTER,
    DEFAULT_MIC_GAIN, DEFAULT_HOTKEY, OBS_BRIDGE_PORT,
    SCANCODE_TO_ENGLISH_KEY, MODIFIER_KEY_NAMES, APP_VERSION,
    resource_path, load_settings, save_settings,
    append_journal_entry, load_journal, clear_journal_file,
)
from updater import parse_version, check_for_updates
from single_instance import try_acquire_single_instance, signal_existing_instance
from obs_bridge import ObsBridgeServer
from audio_engine import (
    normalize_word, build_swear_pattern, load_wav_mono_float,
    level_to_percent, SwearBeeperEngine,
)
from ui_widgets import Tooltip, add_info_icon


class App:
    def __init__(self, root, single_instance_lock=None):
        self.single_instance_lock = single_instance_lock
        self.root = root
        self.root.title("Swear Beeper")
        self.root.geometry("660x780")
        self.root.minsize(560, 600)
        self.root.maxsize(1100, 1000)
        self._set_window_icon()
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
            if self.obs_bridge.port != OBS_BRIDGE_PORT:
                self._log(f"OBS-мост запущен на порту {self.obs_bridge.port} (порт {OBS_BRIDGE_PORT} был занят). Впиши {self.obs_bridge.port} в настройках OBS-скрипта!")
            else:
                self._log(f"OBS-мост запущен на порту {self.obs_bridge.port}.")
        else:
            self._log(f"Не удалось запустить OBS-мост (перепробовал порты {OBS_BRIDGE_PORT}-{OBS_BRIDGE_PORT+9}, все заняты).")

        if not self.saved.get("onboarding_dismissed", False):
            self.root.after(300, self._show_onboarding)


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
        self._add_slider(sliders, 0, "Задержка (сек):", self.delay_var, 0.3, 4.0, DEFAULT_DELAY,
                          "Насколько звук отстаёт от реального времени. Больше задержка = больше времени на распознавание "
                          "и меньше риск, что бип не успеет наложиться. Меньше задержка = более 'живой' звук, но выше риск пропуска мата.")

        self.beep_volume_var = tk.DoubleVar(value=self.saved.get("beep_volume", DEFAULT_BEEP_VOLUME))
        self._add_slider(sliders, 1, "Громкость бипа:", self.beep_volume_var, 0.0, 1.0, DEFAULT_BEEP_VOLUME,
                          "Громкость звука-заменителя (тона или кастомного звука). 0 = тишина вместо мата, 1 = максимальная громкость.")

        self.pad_before_var = tk.DoubleVar(value=self.saved.get("pad_before", DEFAULT_PAD_BEFORE))
        self._add_slider(sliders, 2, "Паддинг ДО слова (сек):", self.pad_before_var, -0.3, 0.3, DEFAULT_PAD_BEFORE,
                          "Сдвиг начала бипа относительно начала слова. Отрицательное значение = бип стартует ПОЗЖЕ, "
                          "и слышно чуть-чуть начала слова перед запиком.")

        self.pad_after_var = tk.DoubleVar(value=self.saved.get("pad_after", DEFAULT_PAD_AFTER))
        self._add_slider(sliders, 3, "Паддинг ПОСЛЕ слова (сек):", self.pad_after_var, 0.0, 0.5, DEFAULT_PAD_AFTER,
                          "Запас времени после конца слова, чтобы бип точно перекрыл 'хвост' мата целиком (окончания, шипящие).")

        self.mic_gain_var = tk.DoubleVar(value=self.saved.get("mic_gain", DEFAULT_MIC_GAIN))
        self._add_slider(sliders, 4, "Усиление микрофона (x):", self.mic_gain_var, 0.5, 5.0, DEFAULT_MIC_GAIN,
                          "Множитель громкости голоса перед обработкой. Полезно, если микрофон слишком тихий. "
                          "Слишком большое значение может исказить звук (защита от перегруза встроена).")

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
        self.status_indicator = tk.Label(btn_frame, text="● ОСТАНОВЛЕНО", fg="gray", font=("", 12, "bold"))
        self.status_indicator.pack(side="left", padx=(16, 0))

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

    def _add_slider(self, parent, row, label, var, frm, to, default_value, tooltip_text=None):
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

        if tooltip_text:
            add_info_icon(parent, row, 4, tooltip_text)


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

        try:
            default_input_idx, default_output_idx = sd.default.device
        except Exception:
            default_input_idx, default_output_idx = -1, -1

        input_options = []
        output_options = []
        cable_output_option = None
        default_input_option = None
        default_output_option = None

        for i, d in enumerate(self.devices):
            name = d["name"]

            if d["max_input_channels"] > 0:
                display = f"⭐ Windows Default - {name}" if i == default_input_idx else name
                label = f"{i}: {display}"
                input_options.append(label)
                if i == default_input_idx:
                    default_input_option = label

            if d["max_output_channels"] > 0:
                if "cable input" in name.lower():
                    display = f"🔌 CABLE Input (рекомендуется для Discord) - {name}"
                elif i == default_output_idx:
                    display = f"⭐ Windows Default - {name}"
                else:
                    display = name
                label = f"{i}: {display}"
                output_options.append(label)
                if "cable input" in name.lower():
                    cable_output_option = label
                if i == default_output_idx:
                    default_output_option = label

        self.input_combo["values"] = input_options
        self.output_combo["values"] = output_options
        self.test_output_combo["values"] = output_options

        if input_options and not self.input_device_var.get():
            self.input_device_var.set(default_input_option or input_options[0])

        if output_options and not self.output_device_var.get():
            self.output_device_var.set(cable_output_option or default_output_option or output_options[0])

        if output_options and not self.test_output_device_var.get():
            self.test_output_device_var.set(default_output_option or output_options[0])

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

        self.mic_test_engine = SwearBeeperEngine(config, self._log, journal_callback=self._on_swear_journal, crash_callback=self._on_engine_crash)
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

    def _on_engine_crash(self, exc):
        self.root.after(0, lambda: self._show_crash_dialog(exc))

    def _show_crash_dialog(self, exc):
        self.status_indicator.config(text="● ОШИБКА - ПЕРЕЗАПУСТИ", fg="red")
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="normal")
        messagebox.showerror(
            "Ошибка движка",
            "Внутри движка произошла непредвиденная ошибка:\n\n"
            f"{exc}\n\n"
            "Микрофон автоматически заглушен для безопасности (звук больше никуда не выводится).\n\n"
            "Нажми 'Стоп', затем снова 'Старт'. Если ошибка повторится — перезапусти приложение полностью.",
        )

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
            "onboarding_dismissed": self.saved.get("onboarding_dismissed", False),
        }

    def _show_onboarding(self):
        win = tk.Toplevel(self.root)
        win.title("Быстрый старт")
        win.geometry("560x520")
        win.minsize(480, 420)
        win.maxsize(800, 700)
        win.transient(self.root)
        win.grab_set()

        text = (
            "Добро пожаловать в Swear Beeper!\n\n"
            "Чтобы всё заработало правильно:\n\n"
            "1. Скачай и установи VB-CABLE (кнопка 'Скачать VB-CABLE' на главном экране)\n"
            "   — понадобится перезагрузка компьютера после установки.\n\n"
            "2. На главном экране в поле 'Микрофон (вход)' выбери СВОЙ настоящий физический микрофон.\n\n"
            "3. В поле 'Выход (динамики / кабель)' выбери 'CABLE Input'\n"
            "   — именно туда приложение будет отправлять уже очищенный звук.\n\n"
            "4. В Discord (или другой программе) в качестве микрофона выбери 'CABLE Output'.\n\n"
            "5. Перед стартом можешь проверить всё через кнопку 'Тест микрофона' —\n"
            "   там отдельно выбирается выход (твои настоящие наушники/колонки), чтобы услышать результат.\n\n"
            "Подробности и troubleshooting — в README на GitHub."
        )

        label = tk.Label(win, text=text, justify="left", anchor="w", padx=12, pady=12, wraplength=450)
        label.pack(fill="both", expand=True)

        dont_show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(win, text="Больше не показывать", variable=dont_show_var).pack(anchor="w", padx=12)

        def close():
            if dont_show_var.get():
                self.saved["onboarding_dismissed"] = True
                self._autosave()
            win.destroy()

        ttk.Button(win, text="Понятно, поехали", command=close).pack(pady=10)
        win.protocol("WM_DELETE_WINDOW", close)

    def _autosave(self, *args):
        if getattr(self, "_suppress_autosave", False):
            return
        save_settings(self._collect_settings())


    def _set_window_icon(self):
        icon_path = resource_path("icon.ico")
        if os.path.isfile(icon_path):
            try:
                self.root.iconbitmap(icon_path)
            except Exception:
                pass

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

        if not (PYSTRAY_AVAILABLE and self.tray_icon):
            self._full_exit()
            return

        win = tk.Toplevel(self.root)
        win.title("Закрыть приложение?")
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)

        tk.Label(
            win,
            text="Приложение может продолжать работать в фоне (в трее),\nдаже если микрофон/детект сейчас активны.\n\nЧто сделать?",
            justify="left", padx=16, pady=16,
        ).pack()

        btn_frame = ttk.Frame(win)
        btn_frame.pack(pady=(0, 16))

        def minimize():
            win.destroy()
            self.root.withdraw()
            self._log("Свернуто в трей. Для полного выхода используй меню трея (правый клик по иконке → Выход).")

        def full_close():
            win.destroy()
            self._full_exit()

        ttk.Button(btn_frame, text="Свернуть в трей", command=minimize).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Закрыть полностью", command=full_close).pack(side="left", padx=6)
        win.protocol("WM_DELETE_WINDOW", minimize)

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
        tray_reminder = "\n\nЕсли захочешь закрыть приложение — используй иконку в трее (правый клик → Выход), а не просто крестик окна: иначе оно останется работать в фоне."

        output_device_combo_value = override_output_device or self.output_device_var.get()

        if not self.input_device_var.get() or not output_device_combo_value:
            messagebox.showerror("Ошибка", "Выбери микрофон и устройство вывода." + tray_reminder)
            return None

        model_path = self.model_path_var.get()
        if not os.path.isdir(model_path):
            messagebox.showerror("Ошибка", f"Папка модели не найдена: {model_path}\n\nПроверь, что модель Vosk скачана и лежит в указанной папке (или нажми 'Сбросить путь' рядом с полем модели)." + tray_reminder)
            return None

        if not self.root_words:
            messagebox.showerror("Ошибка", "Список запрещённых слов пуст — добавь хотя бы одно слово." + tray_reminder)
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

        self.engine = SwearBeeperEngine(config, self._log, journal_callback=self._on_swear_journal, crash_callback=self._on_engine_crash)

        def run_engine():
            try:
                self.engine.start()
            except Exception as e:
                self._log(f"Ошибка запуска: {e}")

        threading.Thread(target=run_engine, daemon=True).start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.mute_indicator.config(text="Микрофон: активен", foreground="green")
        self.status_indicator.config(text="● ЗАПУЩЕНО", fg="green")

    def _on_stop(self):
        self._commit_session_stats_to_alltime()
        self._autosave()
        if self.engine:
            self.engine.stop()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_indicator.config(text="● ОСТАНОВЛЕНО", fg="gray")


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
