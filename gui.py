import os
import sys
import time
import json
import queue
import random
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog, colorchooser

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
    PLAYBACK_RATE, BEEP_FREQ, DEFAULT_ROOT_CORES, VB_CABLE_URL, GITHUB_ISSUES_URL,
    DEFAULT_DELAY, DEFAULT_BEEP_VOLUME, DEFAULT_PAD_BEFORE, DEFAULT_PAD_AFTER,
    DEFAULT_MIC_GAIN, DEFAULT_HOTKEY, OBS_BRIDGE_PORT, OBS_OVERLAY_PORT,
    SCANCODE_TO_ENGLISH_KEY, MODIFIER_KEY_NAMES, APP_VERSION,
    resource_path, load_settings, save_settings,
    append_journal_entry, load_journal, clear_journal_file,
    load_profiles, save_profiles,
)
from updater import parse_version, check_for_updates
from single_instance import try_acquire_single_instance, signal_existing_instance
from obs_bridge import ObsBridgeServer
from overlay_server import OverlayServer
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
        self.root.geometry("680x640")
        self.root.minsize(560, 480)
        self.root.maxsize(1200, 1300)
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

        self.custom_sound_mappings = list(self.saved.get("custom_sound_mappings", []) or [])
        self.profiles = load_profiles()
        for old_path in self.saved.get("custom_beep_paths", []) or []:
            self.custom_sound_mappings.append({"path": old_path, "words": []})

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
        self._set_window_icon()

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

        self.overlay_server = OverlayServer(OBS_OVERLAY_PORT)
        if self.overlay_server.start():
            overlay_url = self.overlay_server.url()
            if self.overlay_server.port != OBS_OVERLAY_PORT:
                self._log(f"Веб-виджет для OBS запущен на {overlay_url} (порт {OBS_OVERLAY_PORT} был занят).")
            else:
                self._log(f"Веб-виджет для OBS запущен: {overlay_url}")
            if hasattr(self, "overlay_link_var"):
                self.overlay_link_var.set(overlay_url)
            self.overlay_server.set_counter_label(self.saved.get("obs_counter_label", "Матов:"))
            self.overlay_server.set_counter_colors(
                label_color=self.saved.get("obs_counter_label_color", "#cfcfcf"),
                value_color=self.saved.get("obs_counter_value_color", "#ff5b5b"),
            )
            self.overlay_server.set_counter_font_size(self.saved.get("obs_counter_font_size", 22))
            self.overlay_server.set_timer_enabled(self.saved.get("obs_timer_enabled", True))
            self.overlay_server.set_timer_format(self.saved.get("obs_timer_format", "Без мата: {time}"))
            self.overlay_server.set_timer_color(self.saved.get("obs_timer_color", "#cfcfcf"))
            self.overlay_server.set_timer_font_size(self.saved.get("obs_timer_font_size", 16))
            self.overlay_server.set_event_enabled(self.saved.get("obs_event_enabled", True))
            self.overlay_server.set_event_color(self.saved.get("obs_event_color", "#ffffff"))
            self.overlay_server.set_event_font_size(self.saved.get("obs_event_font_size", 20))
            saved_banner_image = self.saved.get("obs_banner_image_path")
            if saved_banner_image and os.path.isfile(saved_banner_image):
                try:
                    self.overlay_server.set_banner_image(saved_banner_image)
                except Exception as e:
                    self._log(f"Не удалось восстановить картинку баннера '{saved_banner_image}': {e}")
        else:
            self._log(f"Не удалось запустить веб-виджет для OBS (перепробовал порты {OBS_OVERLAY_PORT}-{OBS_OVERLAY_PORT+9}, все заняты).")
            if hasattr(self, "overlay_link_var"):
                self.overlay_link_var.set("не удалось запустить")

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
        canvas = tk.Canvas(main_tab, highlightthickness=0)
        vscroll = ttk.Scrollbar(main_tab, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vscroll.set)

        def _on_canvas_resize(event):
            canvas.itemconfig(canvas_window, width=event.width)

        canvas.bind("<Configure>", _on_canvas_resize)
        canvas.pack(side="left", fill="both", expand=True)
        vscroll.pack(side="right", fill="y")

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        main_tab = inner

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
        ttk.Button(frame, text="Нашёл баг? Создай Issue", command=self._open_github_issues).grid(row=4, column=1, sticky="w", pady=(4, 4))

        ttk.Separator(main_tab, orient="horizontal").pack(fill="x", pady=6)

        profile_frame = ttk.Frame(main_tab)
        profile_frame.pack(fill="x", **pad)
        ttk.Label(profile_frame, text="Набор (профиль слов/звуков/настроек):").pack(side="left")
        self.profile_var = tk.StringVar(value="")
        self.profile_combo = ttk.Combobox(profile_frame, textvariable=self.profile_var, state="readonly", width=22)
        self.profile_combo["values"] = list(self.profiles.keys())
        self.profile_combo.pack(side="left", padx=(6, 6))
        ttk.Button(profile_frame, text="Загрузить", command=self._load_profile).pack(side="left", padx=2)
        ttk.Button(profile_frame, text="Сохранить как...", command=self._save_profile_as).pack(side="left", padx=2)
        ttk.Button(profile_frame, text="Удалить", command=self._delete_profile).pack(side="left", padx=2)

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

        mode_frame = ttk.Frame(main_tab)
        mode_frame.pack(fill="x", **pad)
        ttk.Label(mode_frame, text="Способ цензуры:").pack(side="left")
        self.censor_mode_var = tk.StringVar(value=self.saved.get("censor_mode", "beep"))
        ttk.Radiobutton(mode_frame, text="Бип/звук", variable=self.censor_mode_var, value="beep", command=self._autosave).pack(side="left", padx=(8, 4))
        ttk.Radiobutton(mode_frame, text="Тишина (мьют)", variable=self.censor_mode_var, value="mute", command=self._autosave).pack(side="left", padx=(0, 4))
        ttk.Radiobutton(mode_frame, text="Инверсия (задом наперёд)", variable=self.censor_mode_var, value="reverse", command=self._autosave).pack(side="left")
        mode_info_icon = tk.Label(mode_frame, text="\u2753", fg="gray", cursor="question_arrow")
        mode_info_icon.pack(side="left", padx=(6, 0))
        Tooltip(
            mode_info_icon,
            "Тишина - просто вырезает мат без звука вместо него.\n"
            "Инверсия - переворачивает само слово задом наперёд (эффект 'бэкмаскинга'), "
            "смысл теряется, но факт речи остаётся слышен - забавнее, чем тупой бип.",
        )

        noise_frame = ttk.Frame(main_tab)
        noise_frame.pack(fill="x", **pad)
        self.noise_suppression_var = tk.BooleanVar(value=self.saved.get("noise_suppression", False))
        ttk.Checkbutton(
            noise_frame, text="Шумоподавление микрофона (эксперимент)",
            variable=self.noise_suppression_var, command=self._autosave,
        ).pack(side="left")
        noise_info_icon = tk.Label(noise_frame, text="\u2753", fg="gray", cursor="question_arrow")
        noise_info_icon.pack(side="left", padx=(6, 0))
        Tooltip(
            noise_info_icon,
            "Приглушает фоновый шум ДО распознавания и цензуры. По умолчанию выключено, "
            "т.к. может немного снижать точность детекта. Включай, если у тебя шумный микрофон/помещение.",
        )

        ttk.Separator(main_tab, orient="horizontal").pack(fill="x", pady=6)

        beep_sound_frame = ttk.Frame(main_tab)
        beep_sound_frame.pack(fill="both", **pad)
        ttk.Label(beep_sound_frame, text="Звуки вместо бипа (.wav) — можно привязать к конкретным словам:").pack(anchor="w")

        beep_list_container = ttk.Frame(beep_sound_frame)
        beep_list_container.pack(fill="x", pady=4)
        beep_scrollbar = ttk.Scrollbar(beep_list_container, orient="vertical")
        self.beep_sounds_listbox = tk.Listbox(beep_list_container, yscrollcommand=beep_scrollbar.set, height=5, selectmode="extended")
        beep_scrollbar.config(command=self.beep_sounds_listbox.yview)
        self.beep_sounds_listbox.pack(side="left", fill="x", expand=True)
        beep_scrollbar.pack(side="right", fill="y")
        for entry in self.custom_sound_mappings:
            self.beep_sounds_listbox.insert("end", self._format_sound_mapping_row(entry))

        beep_btn_row = ttk.Frame(beep_sound_frame)
        beep_btn_row.pack(fill="x", pady=(4, 0))
        ttk.Button(beep_btn_row, text="Добавить звук...", command=self._add_sound_mapping).pack(side="left", padx=(0, 4))
        ttk.Button(beep_btn_row, text="Изменить слова у выбранного", command=self._edit_sound_mapping_words).pack(side="left", padx=(0, 4))
        ttk.Button(beep_btn_row, text="Удалить выбранное", command=self._remove_beep_sound).pack(side="left", padx=(0, 4))
        ttk.Button(beep_btn_row, text="Очистить всё", command=self._reset_beep_sound).pack(side="left", padx=(0, 4))
        ttk.Button(beep_btn_row, text="Прослушать выбранное/случайное", command=self._preview_beep_sound).pack(side="left")

        ttk.Label(
            beep_sound_frame,
            text="Пустой список слов у звука = звук для ЛЮБОГО мата (общий пул). Если у слова есть свой звук — используется он.",
            foreground="gray",
        ).pack(anchor="w", pady=(4, 0))

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

        overlay_frame = ttk.Frame(main_tab)
        overlay_frame.pack(fill="x", **pad)
        ttk.Label(overlay_frame, text="Веб-виджет для OBS:").grid(row=0, column=0, columnspan=4, sticky="w")
        self.overlay_link_var = tk.StringVar(value="запускается...")
        overlay_entry = ttk.Entry(overlay_frame, textvariable=self.overlay_link_var, width=42, state="readonly")
        overlay_entry.grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 0))
        ttk.Button(overlay_frame, text="Скопировать ссылку", command=self._copy_overlay_link).grid(row=1, column=2, padx=(6, 0), pady=(2, 0))
        ttk.Button(overlay_frame, text="Открыть в браузере", command=self._open_overlay_link).grid(row=1, column=3, padx=(6, 0), pady=(2, 0))
        overlay_info_icon = tk.Label(overlay_frame, text="\u2753", fg="gray", cursor="question_arrow")
        overlay_info_icon.grid(row=1, column=4, sticky="w", padx=(6, 0))
        Tooltip(
            overlay_info_icon,
            "В OBS: Источники -> + -> Браузер -> вставь эту ссылку в поле URL.",
        )

        ttk.Label(overlay_frame, text="— Счётчик матов —", foreground="gray").grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))

        ttk.Label(overlay_frame, text="Текст счётчика:").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.overlay_counter_label_var = tk.StringVar(value=self.saved.get("obs_counter_label", "Матов:"))
        self.overlay_counter_label_var.trace_add("write", self._on_counter_label_changed)
        ttk.Entry(overlay_frame, textvariable=self.overlay_counter_label_var, width=24).grid(row=3, column=1, sticky="w", pady=(4, 0))

        ttk.Label(overlay_frame, text="Цвет текста:").grid(row=4, column=0, sticky="w", pady=(4, 0))
        self.overlay_counter_label_color_var = tk.StringVar(value=self.saved.get("obs_counter_label_color", "#cfcfcf"))
        self.overlay_counter_label_color_swatch = tk.Label(overlay_frame, text="  ", background=self.overlay_counter_label_color_var.get(), relief="sunken", width=4)
        self.overlay_counter_label_color_swatch.grid(row=4, column=1, sticky="w", pady=(4, 0))
        ttk.Button(overlay_frame, text="Выбрать цвет...", command=self._choose_counter_label_color).grid(row=4, column=2, sticky="w", pady=(4, 0))

        ttk.Label(overlay_frame, text="Цвет числа:").grid(row=5, column=0, sticky="w", pady=(4, 0))
        self.overlay_counter_value_color_var = tk.StringVar(value=self.saved.get("obs_counter_value_color", "#ff5b5b"))
        self.overlay_counter_value_color_swatch = tk.Label(overlay_frame, text="  ", background=self.overlay_counter_value_color_var.get(), relief="sunken", width=4)
        self.overlay_counter_value_color_swatch.grid(row=5, column=1, sticky="w", pady=(4, 0))
        ttk.Button(overlay_frame, text="Выбрать цвет...", command=self._choose_counter_value_color).grid(row=5, column=2, sticky="w", pady=(4, 0))

        ttk.Label(overlay_frame, text="Размер шрифта:").grid(row=6, column=0, sticky="w", pady=(4, 0))
        self.overlay_counter_font_size_var = tk.IntVar(value=self.saved.get("obs_counter_font_size", 22))
        counter_font_spin = ttk.Spinbox(overlay_frame, from_=8, to=200, width=6, textvariable=self.overlay_counter_font_size_var, command=self._on_counter_font_size_changed)
        counter_font_spin.grid(row=6, column=1, sticky="w", pady=(4, 0))
        counter_font_spin.bind("<Return>", lambda e: self._on_counter_font_size_changed())
        counter_font_spin.bind("<FocusOut>", lambda e: self._on_counter_font_size_changed())

        ttk.Label(overlay_frame, text="— Таймер «без мата» —", foreground="gray").grid(row=7, column=0, columnspan=4, sticky="w", pady=(10, 0))

        self.overlay_timer_enabled_var = tk.BooleanVar(value=self.saved.get("obs_timer_enabled", True))
        ttk.Checkbutton(overlay_frame, text="Показывать таймер", variable=self.overlay_timer_enabled_var, command=self._on_timer_enabled_toggle).grid(row=8, column=0, columnspan=2, sticky="w", pady=(4, 0))

        ttk.Label(overlay_frame, text="Формат таймера:").grid(row=9, column=0, sticky="w", pady=(4, 0))
        self.overlay_timer_format_var = tk.StringVar(value=self.saved.get("obs_timer_format", "Без мата: {time}"))
        self.overlay_timer_format_var.trace_add("write", self._on_timer_format_changed)
        ttk.Entry(overlay_frame, textvariable=self.overlay_timer_format_var, width=28).grid(row=9, column=1, sticky="w", pady=(4, 0))
        timer_format_icon = tk.Label(overlay_frame, text="\u2753", fg="gray", cursor="question_arrow")
        timer_format_icon.grid(row=9, column=2, sticky="w", padx=(4, 0))
        Tooltip(timer_format_icon, "{time} подставится как ЧЧ:ММ:СС (или 'Xд ЧЧ:ММ:СС' если больше суток).\nОтсчёт идёт от последнего запиканного мата (или от запуска, если мата ещё не было).")

        ttk.Label(overlay_frame, text="Цвет таймера:").grid(row=10, column=0, sticky="w", pady=(4, 0))
        self.overlay_timer_color_var = tk.StringVar(value=self.saved.get("obs_timer_color", "#cfcfcf"))
        self.overlay_timer_color_swatch = tk.Label(overlay_frame, text="  ", background=self.overlay_timer_color_var.get(), relief="sunken", width=4)
        self.overlay_timer_color_swatch.grid(row=10, column=1, sticky="w", pady=(4, 0))
        ttk.Button(overlay_frame, text="Выбрать цвет...", command=self._choose_timer_color).grid(row=10, column=2, sticky="w", pady=(4, 0))

        ttk.Label(overlay_frame, text="Размер шрифта:").grid(row=11, column=0, sticky="w", pady=(4, 0))
        self.overlay_timer_font_size_var = tk.IntVar(value=self.saved.get("obs_timer_font_size", 16))
        timer_font_spin = ttk.Spinbox(overlay_frame, from_=8, to=200, width=6, textvariable=self.overlay_timer_font_size_var, command=self._on_timer_font_size_changed)
        timer_font_spin.grid(row=11, column=1, sticky="w", pady=(4, 0))
        timer_font_spin.bind("<Return>", lambda e: self._on_timer_font_size_changed())
        timer_font_spin.bind("<FocusOut>", lambda e: self._on_timer_font_size_changed())

        ttk.Label(overlay_frame, text="— Текст события (слово + тайм-код при бипе) —", foreground="gray").grid(row=12, column=0, columnspan=4, sticky="w", pady=(10, 0))

        self.overlay_event_enabled_var = tk.BooleanVar(value=self.saved.get("obs_event_enabled", True))
        ttk.Checkbutton(overlay_frame, text="Показывать текст события", variable=self.overlay_event_enabled_var, command=self._on_event_enabled_toggle).grid(row=13, column=0, columnspan=2, sticky="w", pady=(4, 0))
        event_enabled_icon = tk.Label(overlay_frame, text="\u2753", fg="gray", cursor="question_arrow")
        event_enabled_icon.grid(row=13, column=2, sticky="w", padx=(4, 0))
        Tooltip(event_enabled_icon, "Появляется наверху экрана в момент, когда играет бип, в формате \"слово\" [начало-конец].\nПоказ синхронизирован с реальной задержкой звука, а не с моментом распознавания.")

        ttk.Label(overlay_frame, text="Цвет текста события:").grid(row=14, column=0, sticky="w", pady=(4, 0))
        self.overlay_event_color_var = tk.StringVar(value=self.saved.get("obs_event_color", "#ffffff"))
        self.overlay_event_color_swatch = tk.Label(overlay_frame, text="  ", background=self.overlay_event_color_var.get(), relief="sunken", width=4)
        self.overlay_event_color_swatch.grid(row=14, column=1, sticky="w", pady=(4, 0))
        ttk.Button(overlay_frame, text="Выбрать цвет...", command=self._choose_event_color).grid(row=14, column=2, sticky="w", pady=(4, 0))

        ttk.Label(overlay_frame, text="Размер шрифта:").grid(row=15, column=0, sticky="w", pady=(4, 0))
        self.overlay_event_font_size_var = tk.IntVar(value=self.saved.get("obs_event_font_size", 20))
        event_font_spin = ttk.Spinbox(overlay_frame, from_=8, to=200, width=6, textvariable=self.overlay_event_font_size_var, command=self._on_event_font_size_changed)
        event_font_spin.grid(row=15, column=1, sticky="w", pady=(4, 0))
        event_font_spin.bind("<Return>", lambda e: self._on_event_font_size_changed())
        event_font_spin.bind("<FocusOut>", lambda e: self._on_event_font_size_changed())

        ttk.Label(overlay_frame, text="— Баннер при мате —", foreground="gray").grid(row=16, column=0, columnspan=4, sticky="w", pady=(10, 0))

        ttk.Label(overlay_frame, text="Картинка баннера:").grid(row=17, column=0, sticky="w", pady=(4, 0))
        self.overlay_banner_image_var = tk.StringVar(value=self.saved.get("obs_banner_image_path") or "(не выбрана — баннер не показывается)")
        ttk.Entry(overlay_frame, textvariable=self.overlay_banner_image_var, width=42, state="readonly").grid(row=17, column=1, columnspan=2, sticky="we", pady=(4, 0))
        ttk.Button(overlay_frame, text="Выбрать картинку...", command=self._choose_banner_image).grid(row=18, column=1, sticky="w", pady=(2, 0))
        ttk.Button(overlay_frame, text="Сбросить", command=self._reset_banner_image).grid(row=18, column=2, sticky="w", pady=(2, 0))
        ttk.Button(overlay_frame, text="Показать тест баннера", command=self._test_banner).grid(row=18, column=3, sticky="w", pady=(2, 0))
        banner_info_icon = tk.Label(overlay_frame, text="\u2753", fg="gray", cursor="question_arrow")
        banner_info_icon.grid(row=18, column=4, sticky="w", padx=(6, 0))
        Tooltip(
            banner_info_icon,
            "Баннер при мате - это картинка или гифка, которую ты сам выбираешь.\n"
            "Пока картинка не выбрана - баннер вообще не показывается, только счётчик и таймер.",
        )


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
        self.log_text = tk.Text(log_frame, height=7, state="disabled")
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


    def _format_sound_mapping_row(self, entry):
        name = os.path.basename(entry.get("path", "?"))
        words = entry.get("words", [])
        label = ", ".join(words) if words else "(любой мат)"
        return f"{name} -> {label}"

    def _add_sound_mapping(self):
        paths = filedialog.askopenfilenames(title="Выбери .wav файл(ы)", filetypes=[("WAV files", "*.wav")])
        if not paths:
            return
        for p in paths:
            words_str = simpledialog.askstring(
                "Слова для звука",
                f"На какие слова триггерить звук '{os.path.basename(p)}'?\n"
                "Через запятую (например: бля, блять). Оставь пустым - звук для ЛЮБОГО мата.",
                parent=self.root,
            )
            words = [normalize_word(w) for w in (words_str or "").split(",") if w.strip()]
            entry = {"path": p, "words": words}
            self.custom_sound_mappings.append(entry)
            self.beep_sounds_listbox.insert("end", self._format_sound_mapping_row(entry))
        self._autosave()

    def _edit_sound_mapping_words(self):
        selection = self.beep_sounds_listbox.curselection()
        if not selection:
            messagebox.showinfo("Инфо", "Сначала выбери звук в списке.")
            return
        index = selection[0]
        entry = self.custom_sound_mappings[index]
        current_words = ", ".join(entry.get("words", []))
        words_str = simpledialog.askstring(
            "Слова для звука",
            f"Слова для '{os.path.basename(entry['path'])}' (через запятую, пусто = любой мат):",
            initialvalue=current_words,
            parent=self.root,
        )
        if words_str is None:
            return
        entry["words"] = [normalize_word(w) for w in words_str.split(",") if w.strip()]
        self.beep_sounds_listbox.delete(index)
        self.beep_sounds_listbox.insert(index, self._format_sound_mapping_row(entry))
        self._autosave()

    def _collect_profile_snapshot(self):
        return {
            "delay": self.delay_var.get(),
            "beep_volume": self.beep_volume_var.get(),
            "pad_before": self.pad_before_var.get(),
            "pad_after": self.pad_after_var.get(),
            "mic_gain": self.mic_gain_var.get(),
            "noise_suppression": self.noise_suppression_var.get(),
            "root_words": list(self.root_words),
            "whitelist_words": list(self.whitelist_words),
            "custom_sound_mappings": list(self.custom_sound_mappings),
            "censor_mode": self.censor_mode_var.get(),
            "hotkey": self.hotkey_var.get(),
        }

    def _apply_profile_snapshot(self, data):
        self.delay_var.set(data.get("delay", DEFAULT_DELAY))
        self.beep_volume_var.set(data.get("beep_volume", DEFAULT_BEEP_VOLUME))
        self.pad_before_var.set(data.get("pad_before", DEFAULT_PAD_BEFORE))
        self.pad_after_var.set(data.get("pad_after", DEFAULT_PAD_AFTER))
        self.mic_gain_var.set(data.get("mic_gain", DEFAULT_MIC_GAIN))
        self.censor_mode_var.set(data.get("censor_mode", "beep"))
        self.noise_suppression_var.set(data.get("noise_suppression", False))

        self.root_words = list(data.get("root_words", DEFAULT_ROOT_CORES))
        self.words_listbox.delete(0, "end")
        for w in self.root_words:
            self.words_listbox.insert("end", w)

        self.whitelist_words = list(data.get("whitelist_words", []))
        self.whitelist_listbox.delete(0, "end")
        for w in self.whitelist_words:
            self.whitelist_listbox.insert("end", w)

        self.custom_sound_mappings = list(data.get("custom_sound_mappings", []))
        self.beep_sounds_listbox.delete(0, "end")
        for entry in self.custom_sound_mappings:
            self.beep_sounds_listbox.insert("end", self._format_sound_mapping_row(entry))

        hotkey = data.get("hotkey", DEFAULT_HOTKEY)
        self.hotkey_var.set(hotkey)
        self._setup_hotkey(hotkey)

        self._autosave()

    def _refresh_profile_combo(self):
        self.profile_combo["values"] = list(self.profiles.keys())

    def _save_profile_as(self):
        name = simpledialog.askstring("Сохранить набор", "Имя набора:", parent=self.root)
        if not name:
            return
        self.profiles[name] = self._collect_profile_snapshot()
        save_profiles(self.profiles)
        self._refresh_profile_combo()
        self.profile_var.set(name)
        self._log(f"Набор '{name}' сохранён.")

    def _load_profile(self):
        name = self.profile_var.get()
        if not name or name not in self.profiles:
            messagebox.showinfo("Инфо", "Выбери существующий набор из списка.")
            return
        self._apply_profile_snapshot(self.profiles[name])
        self._log(f"Набор '{name}' загружен.")

    def _delete_profile(self):
        name = self.profile_var.get()
        if not name or name not in self.profiles:
            messagebox.showinfo("Инфо", "Выбери существующий набор из списка.")
            return
        if not messagebox.askyesno("Подтверждение", f"Удалить набор '{name}'?"):
            return
        del self.profiles[name]
        save_profiles(self.profiles)
        self._refresh_profile_combo()
        self.profile_var.set("")
        self._log(f"Набор '{name}' удалён.")

    def _reset_model_path(self):
        self.model_path_var.set(resource_path("model_ru"))
        self._autosave()

    def _browse_model(self):
        path = filedialog.askdirectory(title="Выбери папку модели Vosk")
        if path:
            self.model_path_var.set(path)

    def _remove_beep_sound(self):
        selection = self.beep_sounds_listbox.curselection()
        if not selection:
            return
        for index in sorted(selection, reverse=True):
            self.beep_sounds_listbox.delete(index)
            del self.custom_sound_mappings[index]
        self._autosave()

    def _reset_beep_sound(self):
        self.beep_sounds_listbox.delete(0, "end")
        self.custom_sound_mappings.clear()
        self._autosave()

    def _preview_beep_sound(self):
        try:
            out_val = self.test_output_device_var.get() or self.output_device_var.get()
            device_idx = self._parse_device_index(out_val) if out_val else None

            selection = self.beep_sounds_listbox.curselection()
            if selection and self.custom_sound_mappings:
                entry = self.custom_sound_mappings[selection[0]]
                path = entry["path"]
                data = load_wav_mono_float(path, PLAYBACK_RATE)
                self._log(f"Превью: {os.path.basename(path)}")
            elif self.custom_sound_mappings:
                entry = random.choice(self.custom_sound_mappings)
                path = entry["path"]
                data = load_wav_mono_float(path, PLAYBACK_RATE)
                self._log(f"Превью случайного звука: {os.path.basename(path)}")
            else:
                t = np.arange(int(PLAYBACK_RATE * 0.3)) / PLAYBACK_RATE
                data = (self.beep_volume_var.get() * np.sin(2 * np.pi * BEEP_FREQ * t)).astype(np.float32)
            sd.play(data, PLAYBACK_RATE, device=device_idx)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось воспроизвести звук: {e}")

    def _open_github_issues(self):
        webbrowser.open(GITHUB_ISSUES_URL)

    def _open_vbcable(self):
        webbrowser.open(VB_CABLE_URL)

    def _copy_overlay_link(self):
        url = self.overlay_link_var.get()
        if not url or url in ("запускается...", "не удалось запустить"):
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self._log("Ссылка на веб-виджет скопирована в буфер обмена.")

    def _open_overlay_link(self):
        url = self.overlay_link_var.get()
        if not url or url in ("запускается...", "не удалось запустить"):
            messagebox.showerror("Ошибка", "Веб-виджет ещё не запущен.")
            return
        webbrowser.open(url)

    def _on_counter_label_changed(self, *args):
        if getattr(self, "overlay_server", None):
            self.overlay_server.set_counter_label(self.overlay_counter_label_var.get())
        self._autosave()

    def _choose_counter_label_color(self):
        _, hex_color = colorchooser.askcolor(
            color=self.overlay_counter_label_color_var.get(), title="Цвет текста счётчика",
        )
        if not hex_color:
            return
        self.overlay_counter_label_color_var.set(hex_color)
        self.overlay_counter_label_color_swatch.config(background=hex_color)
        if getattr(self, "overlay_server", None):
            self.overlay_server.set_counter_colors(label_color=hex_color)
        self._autosave()

    def _choose_counter_value_color(self):
        _, hex_color = colorchooser.askcolor(
            color=self.overlay_counter_value_color_var.get(), title="Цвет числа счётчика",
        )
        if not hex_color:
            return
        self.overlay_counter_value_color_var.set(hex_color)
        self.overlay_counter_value_color_swatch.config(background=hex_color)
        if getattr(self, "overlay_server", None):
            self.overlay_server.set_counter_colors(value_color=hex_color)
        self._autosave()

    def _on_counter_font_size_changed(self):
        try:
            px = int(self.overlay_counter_font_size_var.get())
        except (tk.TclError, ValueError):
            return
        if getattr(self, "overlay_server", None):
            self.overlay_server.set_counter_font_size(px)
        self._autosave()

    def _on_timer_enabled_toggle(self):
        if getattr(self, "overlay_server", None):
            self.overlay_server.set_timer_enabled(self.overlay_timer_enabled_var.get())
        self._autosave()

    def _on_timer_format_changed(self, *args):
        if getattr(self, "overlay_server", None):
            self.overlay_server.set_timer_format(self.overlay_timer_format_var.get())
        self._autosave()

    def _choose_timer_color(self):
        _, hex_color = colorchooser.askcolor(
            color=self.overlay_timer_color_var.get(), title="Цвет таймера",
        )
        if not hex_color:
            return
        self.overlay_timer_color_var.set(hex_color)
        self.overlay_timer_color_swatch.config(background=hex_color)
        if getattr(self, "overlay_server", None):
            self.overlay_server.set_timer_color(hex_color)
        self._autosave()

    def _on_timer_font_size_changed(self):
        try:
            px = int(self.overlay_timer_font_size_var.get())
        except (tk.TclError, ValueError):
            return
        if getattr(self, "overlay_server", None):
            self.overlay_server.set_timer_font_size(px)
        self._autosave()

    def _on_event_enabled_toggle(self):
        if getattr(self, "overlay_server", None):
            self.overlay_server.set_event_enabled(self.overlay_event_enabled_var.get())
        self._autosave()

    def _choose_event_color(self):
        _, hex_color = colorchooser.askcolor(
            color=self.overlay_event_color_var.get(), title="Цвет текста события",
        )
        if not hex_color:
            return
        self.overlay_event_color_var.set(hex_color)
        self.overlay_event_color_swatch.config(background=hex_color)
        if getattr(self, "overlay_server", None):
            self.overlay_server.set_event_color(hex_color)
        self._autosave()

    def _on_event_font_size_changed(self):
        try:
            px = int(self.overlay_event_font_size_var.get())
        except (tk.TclError, ValueError):
            return
        if getattr(self, "overlay_server", None):
            self.overlay_server.set_event_font_size(px)
        self._autosave()

    def _choose_banner_image(self):
        path = filedialog.askopenfilename(
            title="Выбери картинку для баннера",
            filetypes=[("Изображения", "*.png *.jpg *.jpeg *.gif *.webp *.bmp"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        try:
            if getattr(self, "overlay_server", None):
                self.overlay_server.set_banner_image(path)
            self.overlay_banner_image_var.set(path)
            self._autosave()
            self._log(f"Картинка баннера установлена: {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось загрузить картинку: {e}")

    def _reset_banner_image(self):
        if getattr(self, "overlay_server", None):
            self.overlay_server.clear_banner_image()
        self.overlay_banner_image_var.set("(не выбрана — баннер не показывается)")
        self._autosave()

    def _test_banner(self):
        if not getattr(self, "overlay_server", None):
            return
        if not self.overlay_server.get_state().get("has_image"):
            messagebox.showinfo("Инфо", "Сначала выбери картинку баннера кнопкой 'Выбрать картинку...' — иначе показывать нечего.")
            return
        self.overlay_server.notify_censor_event("тест", 53.76, 54.18)
        self._log("Тестовый показ баннера отправлен на оверлей — глянь в OBS/браузере, где он появился.")

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

        self.mic_test_engine = SwearBeeperEngine(config, self._log, journal_callback=self._on_swear_journal, crash_callback=self._on_engine_crash, event_callback=self._on_censor_playback_event)
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

        if getattr(self, "obs_bridge", None) or getattr(self, "overlay_server", None):
            session_total, alltime_total = self._get_current_totals()
            if getattr(self, "obs_bridge", None):
                self.obs_bridge.broadcast({
                    "type": "censor_event",
                    "session_total": session_total,
                    "alltime_total": alltime_total,
                    "ts": timestamp,
                })
            if getattr(self, "overlay_server", None):
                self.overlay_server.update_snapshot(session_total, alltime_total)

    def _on_censor_playback_event(self, word, start_sec, end_sec):
        if getattr(self, "overlay_server", None):
            self.overlay_server.notify_censor_event(word, start_sec, end_sec)

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
                "delay_sec": self.delay_var.get(),
            })

        if getattr(self, "overlay_server", None):
            self.overlay_server.update_snapshot(session_total, alltime_total_display, self.delay_var.get())

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
            "custom_sound_mappings": list(self.custom_sound_mappings),
            "censor_mode": self.censor_mode_var.get() if hasattr(self, "censor_mode_var") else "beep",
            "noise_suppression": self.noise_suppression_var.get() if hasattr(self, "noise_suppression_var") else False,
            "root_words": list(self.root_words),
            "whitelist_words": list(self.whitelist_words),
            "hotkey": self.hotkey_var.get() if hasattr(self, "hotkey_var") else DEFAULT_HOTKEY,
            "alltime_total": self.alltime_stats["total"],
            "alltime_per_word": self.alltime_stats["per_word"],
            "input_device_name": self._parse_device_name(self.input_device_var.get()) if self.input_device_var.get() else None,
            "output_device_name": self._parse_device_name(self.output_device_var.get()) if self.output_device_var.get() else None,
            "test_output_device_name": self._parse_device_name(self.test_output_device_var.get()) if self.test_output_device_var.get() else None,
            "onboarding_dismissed": self.saved.get("onboarding_dismissed", False),
            "obs_counter_label": self.overlay_counter_label_var.get() if hasattr(self, "overlay_counter_label_var") else "Матов:",
            "obs_counter_label_color": self.overlay_counter_label_color_var.get() if hasattr(self, "overlay_counter_label_color_var") else "#cfcfcf",
            "obs_counter_value_color": self.overlay_counter_value_color_var.get() if hasattr(self, "overlay_counter_value_color_var") else "#ff5b5b",
            "obs_counter_font_size": self.overlay_counter_font_size_var.get() if hasattr(self, "overlay_counter_font_size_var") else 22,
            "obs_timer_enabled": self.overlay_timer_enabled_var.get() if hasattr(self, "overlay_timer_enabled_var") else True,
            "obs_timer_format": self.overlay_timer_format_var.get() if hasattr(self, "overlay_timer_format_var") else "Без мата: {time}",
            "obs_timer_color": self.overlay_timer_color_var.get() if hasattr(self, "overlay_timer_color_var") else "#cfcfcf",
            "obs_timer_font_size": self.overlay_timer_font_size_var.get() if hasattr(self, "overlay_timer_font_size_var") else 16,
            "obs_event_enabled": self.overlay_event_enabled_var.get() if hasattr(self, "overlay_event_enabled_var") else True,
            "obs_event_color": self.overlay_event_color_var.get() if hasattr(self, "overlay_event_color_var") else "#ffffff",
            "obs_event_font_size": self.overlay_event_font_size_var.get() if hasattr(self, "overlay_event_font_size_var") else 20,
            "obs_banner_image_path": (
                self.overlay_banner_image_var.get()
                if hasattr(self, "overlay_banner_image_var") and os.path.isfile(self.overlay_banner_image_var.get())
                else None
            ),
        }

    def _show_onboarding(self):
        self.wizard_step = 0
        self.wizard_win = tk.Toplevel(self.root)
        self.wizard_win.title("Быстрая настройка Swear Beeper")
        self.wizard_win.geometry("560x480")
        self.wizard_win.minsize(500, 420)
        self.wizard_win.maxsize(800, 700)
        self.wizard_win.transient(self.root)
        self.wizard_win.grab_set()
        self.wizard_win.protocol("WM_DELETE_WINDOW", self._finish_wizard)

        self.wizard_content = ttk.Frame(self.wizard_win)
        self.wizard_content.pack(fill="both", expand=True, padx=16, pady=16)

        self.wizard_nav = ttk.Frame(self.wizard_win)
        self.wizard_nav.pack(fill="x", padx=16, pady=(0, 16))

        self._render_wizard_step()

    def _clear_wizard_content(self):
        for widget in self.wizard_content.winfo_children():
            widget.destroy()
        for widget in self.wizard_nav.winfo_children():
            widget.destroy()

    def _render_wizard_step(self):
        self._clear_wizard_content()
        steps = [
            self._wizard_step_welcome,
            self._wizard_step_vbcable,
            self._wizard_step_devices,
            self._wizard_step_test,
        ]
        steps[self.wizard_step]()

    def _wizard_next(self):
        self.wizard_step += 1
        self._render_wizard_step()

    def _wizard_step_welcome(self):
        ttk.Label(
            self.wizard_content,
            text="Добро пожаловать в Swear Beeper! 👋",
            font=("", 13, "bold"),
        ).pack(anchor="w", pady=(0, 12))
        ttk.Label(
            self.wizard_content,
            text=(
                "Сейчас за 3 коротких шага настроим всё, что нужно:\n\n"
                "1. Проверим/поставим VB-CABLE (виртуальный микрофон для Discord/OBS)\n"
                "2. Автоматически подберём микрофон и выход\n"
                "3. Проверим, что всё реально работает\n\n"
                "Ничего вручную искать в настройках не придётся — только пара кликов."
            ),
            justify="left", wraplength=500,
        ).pack(anchor="w", fill="both", expand=True)

        ttk.Button(self.wizard_nav, text="Начать настройку →", command=self._wizard_next).pack(side="right")
        ttk.Button(self.wizard_nav, text="Пропустить, я сам разберусь", command=self._finish_wizard).pack(side="left")

    def _wizard_step_vbcable(self):
        has_cable = self._detect_vb_cable()

        ttk.Label(self.wizard_content, text="Шаг 1 из 3: VB-CABLE", font=("", 13, "bold")).pack(anchor="w", pady=(0, 12))

        if has_cable:
            ttk.Label(
                self.wizard_content,
                text="✅ VB-CABLE уже установлен и найден в системе — этот шаг можно пропустить.",
                foreground="green", justify="left", wraplength=500,
            ).pack(anchor="w", pady=(0, 12))
        else:
            ttk.Label(
                self.wizard_content,
                text=(
                    "VB-CABLE не найден. Это бесплатная программа, которая создаёт 'виртуальный микрофон' — "
                    "через неё Discord/OBS будут слышать уже очищенный от мата звук.\n\n"
                    "1. Нажми кнопку ниже — откроется официальный сайт\n"
                    "2. Скачай и установи (потребуется перезагрузка компьютера)\n"
                    "3. После перезагрузки вернись сюда и нажми 'Проверить снова'"
                ),
                justify="left", wraplength=500,
            ).pack(anchor="w", pady=(0, 12))

            btn_row = ttk.Frame(self.wizard_content)
            btn_row.pack(anchor="w", pady=(0, 8))
            ttk.Button(btn_row, text="Скачать VB-CABLE", command=self._open_vbcable).pack(side="left", padx=(0, 8))
            ttk.Button(btn_row, text="Проверить снова", command=self._render_wizard_step).pack(side="left")

        ttk.Button(self.wizard_nav, text="Далее →", command=self._wizard_next).pack(side="right")
        ttk.Button(self.wizard_nav, text="Пропустить этот шаг", command=self._wizard_next).pack(side="left")

    def _detect_vb_cable(self):
        try:
            devices = sd.query_devices()
        except Exception:
            return False
        return any("cable" in d.get("name", "").lower() for d in devices)

    def _wizard_step_devices(self):
        self._refresh_devices()

        ttk.Label(self.wizard_content, text="Шаг 2 из 3: устройства", font=("", 13, "bold")).pack(anchor="w", pady=(0, 12))
        ttk.Label(
            self.wizard_content,
            text="Подобрал автоматически — если что-то не так, поменяешь на главном экране в любой момент.",
            justify="left", wraplength=500,
        ).pack(anchor="w", pady=(0, 12))

        summary_frame = ttk.Frame(self.wizard_content)
        summary_frame.pack(anchor="w", fill="x", pady=(0, 8))

        def add_row(row, label, value, ok):
            ttk.Label(summary_frame, text=label, font=("", 10, "bold")).grid(row=row, column=0, sticky="w", pady=3)
            mark = "✅" if ok else "⚠️"
            ttk.Label(summary_frame, text=f"{mark} {value}").grid(row=row, column=1, sticky="w", padx=(8, 0))

        mic_value = self.input_device_var.get() or "не найден"
        output_value = self.output_device_var.get() or "не найден"
        test_value = self.test_output_device_var.get() or "не найден"

        add_row(0, "Микрофон:", mic_value, bool(self.input_device_var.get()))
        add_row(1, "Выход (в Discord/OBS):", output_value, "cable" in output_value.lower())
        add_row(2, "Выход для теста (твои уши):", test_value, bool(self.test_output_device_var.get()))

        if "cable" not in output_value.lower():
            ttk.Label(
                self.wizard_content,
                text="⚠️ CABLE Input не выбран автоматически — если только что установил VB-CABLE, "
                     "нажми 'Обновить' или вернись на предыдущий шаг после перезагрузки компьютера.",
                foreground="#a06000", justify="left", wraplength=500,
            ).pack(anchor="w", pady=(8, 0))
            ttk.Button(self.wizard_content, text="Обновить список устройств", command=self._wizard_refresh_devices_step).pack(anchor="w", pady=(6, 0))

        ttk.Button(self.wizard_nav, text="Далее →", command=self._wizard_next).pack(side="right")
        ttk.Button(self.wizard_nav, text="← Назад", command=self._wizard_back).pack(side="left")

    def _wizard_refresh_devices_step(self):
        self.input_device_var.set("")
        self.output_device_var.set("")
        self.test_output_device_var.set("")
        self._render_wizard_step()

    def _wizard_back(self):
        self.wizard_step -= 1
        self._render_wizard_step()

    def _wizard_step_test(self):
        ttk.Label(self.wizard_content, text="Шаг 3 из 3: проверка", font=("", 13, "bold")).pack(anchor="w", pady=(0, 12))
        ttk.Label(
            self.wizard_content,
            text=(
                "Нажми кнопку ниже и скажи что-нибудь с матом вслух — если всё настроено верно, "
                "услышишь через свои наушники/колонки, что слово запикано.\n\n"
                "(Это тот же тест, что доступен на главном экране в любой момент)"
            ),
            justify="left", wraplength=500,
        ).pack(anchor="w", pady=(0, 12))

        self.wizard_test_btn = ttk.Button(self.wizard_content, text="▶ Проверить микрофон", command=self._wizard_toggle_test)
        self.wizard_test_btn.pack(anchor="w", pady=(0, 12))

        ttk.Label(
            self.wizard_content,
            text="Готово? Жми 'Завершить' — эта настройка больше не появится при следующих запусках.",
            justify="left", wraplength=500, foreground="gray",
        ).pack(anchor="w", side="bottom")

        ttk.Button(self.wizard_nav, text="Завершить ✓", command=self._finish_wizard).pack(side="right")
        ttk.Button(self.wizard_nav, text="← Назад", command=self._wizard_back).pack(side="left")

    def _wizard_toggle_test(self):
        self._toggle_mic_test()
        if self.mic_test_engine and self.mic_test_engine.running:
            self.wizard_test_btn.config(text="■ Остановить проверку")
        else:
            self.wizard_test_btn.config(text="▶ Проверить микрофон")

    def _finish_wizard(self):
        if getattr(self, "mic_test_engine", None) and self.mic_test_engine.running:
            self.mic_test_engine.stop()
            self.mic_test_engine = None
        self.saved["onboarding_dismissed"] = True
        self._autosave()
        if getattr(self, "wizard_win", None):
            self.wizard_win.destroy()

    def _autosave(self, *args):
        if getattr(self, "_suppress_autosave", False):
            return
        save_settings(self._collect_settings())


    def _set_window_icon(self):
        icon_path = resource_path("icon.ico")
        if not os.path.isfile(icon_path):
            self._log(f"icon.ico не найден по пути: {icon_path} (иконка окна/трея не будет установлена)")
            return
        try:
            self.root.iconbitmap(default=icon_path)
            self.root.update_idletasks()
            self._log(f"Иконка окна установлена: {icon_path}")
        except Exception as e:
            self._log(f"Не удалось установить иконку окна: {e}")

    def _load_tray_icon_image(self):
        icon_path = resource_path("icon.ico")
        if os.path.isfile(icon_path):
            try:
                img = Image.open(icon_path)
                self._log(f"Иконка трея загружена: {icon_path}")
                return img
            except Exception as e:
                self._log(f"Не удалось открыть icon.ico для трея: {e}")
        else:
            self._log(f"icon.ico не найден по пути: {icon_path} - использую запасной значок трея")
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
        if getattr(self, "overlay_server", None):
            self.overlay_server.stop()
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
            "custom_sound_mappings": list(self.custom_sound_mappings),
            "censor_mode": self.censor_mode_var.get(),
            "noise_suppression": self.noise_suppression_var.get(),
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

        self.engine = SwearBeeperEngine(config, self._log, journal_callback=self._on_swear_journal, crash_callback=self._on_engine_crash, event_callback=self._on_censor_playback_event)

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
