import os
import sys
import json
import datetime

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

APP_VERSION = "1.3.0"
GITHUB_REPO = "qwertyzipe/SwearBeeper"


def resource_path(relative_path):
    if hasattr(sys, "_MEIPASS"):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def _base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def settings_path():
    return os.path.join(_base_dir(), SETTINGS_FILENAME)


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


def journal_path():
    return os.path.join(_base_dir(), JOURNAL_FILENAME)


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
