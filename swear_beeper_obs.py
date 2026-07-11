import obspython as obs
import socket
import threading
import json
import queue
import time

HOST = "127.0.0.1"
PORT = 47823

_running = False
_client_socket = None
_msg_queue = queue.Queue()
_last_swear_time = None

settings_cache = {
    "counter_source": "",
    "banner_source": "",
    "sound_source": "",
    "banner_duration": 1.5,
    "counter_format": "Маты: {session}",
    "timer_source": "",
    "timer_format": "Без мата: {time}",
}


def script_description():
    return (
        "SwearBeeper OBS Bridge\n\n"
        "Подключается к запущенному приложению SwearBeeper (127.0.0.1:47823) "
        "и в реальном времени обновляет текстовый счётчик матов, показывает баннер "
        "цензуры и проигрывает звук в момент запика.\n\n"
        "Как настроить:\n"
        "1. Создай в OBS текстовый источник для счётчика (например 'SwearCounter').\n"
        "2. Создай источник-баннер (текст или картинка 'ЦЕНЗУРА'), назови его, например 'CensorBanner'.\n"
        "   По умолчанию он должен быть СКРЫТ на сцене — скрипт сам будет включать его на пару секунд.\n"
        "3. (Опционально) добавь Media Source со звуковым файлом, назови, например 'CensorSound'.\n"
        "4. (Опционально) создай текстовый источник для таймера 'без мата', например 'NoSwearTimer'.\n"
        "5. Впиши точные названия этих источников в настройках скрипта ниже.\n"
        "6. Запусти SwearBeeper и нажми Старт (или Тест микрофона) — счётчик и баннер должны заработать."
    )


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_text(props, "counter_source", "Текстовый источник счётчика", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "counter_format", "Формат текста счётчика", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "banner_source", "Источник баннера (текст/картинка)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_float(props, "banner_duration", "Длительность показа баннера (сек)", 0.2, 10.0, 0.1)
    obs.obs_properties_add_text(props, "sound_source", "Media-источник звука (опционально)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "timer_source", "Текстовый источник таймера 'без мата'", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "timer_format", "Формат текста таймера", obs.OBS_TEXT_DEFAULT)
    return props


def script_defaults(settings):
    obs.obs_data_set_default_string(settings, "counter_source", "")
    obs.obs_data_set_default_string(settings, "counter_format", "Маты: {session}")
    obs.obs_data_set_default_string(settings, "banner_source", "")
    obs.obs_data_set_default_double(settings, "banner_duration", 1.5)
    obs.obs_data_set_default_string(settings, "sound_source", "")
    obs.obs_data_set_default_string(settings, "timer_source", "")
    obs.obs_data_set_default_string(settings, "timer_format", "Без мата: {time}")


def script_update(settings):
    settings_cache["counter_source"] = obs.obs_data_get_string(settings, "counter_source")
    settings_cache["counter_format"] = obs.obs_data_get_string(settings, "counter_format") or "Маты: {session}"
    settings_cache["banner_source"] = obs.obs_data_get_string(settings, "banner_source")
    settings_cache["banner_duration"] = obs.obs_data_get_double(settings, "banner_duration")
    settings_cache["sound_source"] = obs.obs_data_get_string(settings, "sound_source")
    settings_cache["timer_source"] = obs.obs_data_get_string(settings, "timer_source")
    settings_cache["timer_format"] = obs.obs_data_get_string(settings, "timer_format") or "Без мата: {time}"


def script_load(settings):
    global _running, _last_swear_time
    _running = True
    _last_swear_time = time.time()
    threading.Thread(target=_connect_loop, daemon=True).start()
    obs.timer_add(_process_queue, 100)
    obs.timer_add(_update_timer_text, 1000)


def script_unload():
    global _running, _client_socket
    _running = False
    if _client_socket:
        try:
            _client_socket.close()
        except Exception:
            pass
    obs.timer_remove(_process_queue)
    obs.timer_remove(_update_timer_text)


def _connect_loop():
    global _client_socket
    while _running:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((HOST, PORT))
            s.settimeout(None)
            _client_socket = s
            buf = b""
            while _running:
                data = s.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        try:
                            msg = json.loads(line.decode("utf-8"))
                            _msg_queue.put(msg)
                        except Exception:
                            pass
        except Exception:
            time.sleep(2)
        finally:
            _client_socket = None


def _set_text_source(source_name, text):
    if not source_name:
        return
    source = obs.obs_get_source_by_name(source_name)
    if source:
        data = obs.obs_data_create()
        obs.obs_data_set_string(data, "text", text)
        obs.obs_source_update(source, data)
        obs.obs_data_release(data)
        obs.obs_source_release(source)


def _set_source_visible(source_name, visible):
    if not source_name:
        return
    scenes = obs.obs_frontend_get_scenes()
    if scenes is None:
        return
    for scene_source in scenes:
        scene = obs.obs_scene_from_source(scene_source)
        item = obs.obs_scene_find_source(scene, source_name)
        if item:
            obs.obs_sceneitem_set_visible(item, visible)
    obs.source_list_release(scenes)


def _restart_media_source(source_name):
    if not source_name:
        return
    source = obs.obs_get_source_by_name(source_name)
    if source:
        obs.obs_source_media_restart(source)
        obs.obs_source_release(source)


def _hide_banner_once():
    _set_source_visible(settings_cache["banner_source"], False)
    obs.timer_remove(_hide_banner_once)


def _format_duration(seconds):
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}д {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _update_timer_text():
    if not settings_cache["timer_source"] or _last_swear_time is None:
        return
    elapsed = time.time() - _last_swear_time
    text = settings_cache["timer_format"].format(time=_format_duration(elapsed))
    _set_text_source(settings_cache["timer_source"], text)


def _process_queue():
    global _last_swear_time
    while not _msg_queue.empty():
        msg = _msg_queue.get_nowait()
        msg_type = msg.get("type")

        if msg_type in ("snapshot", "censor_event"):
            session_total = msg.get("session_total", 0)
            alltime_total = msg.get("alltime_total", 0)
            text = settings_cache["counter_format"].format(session=session_total, alltime=alltime_total)
            _set_text_source(settings_cache["counter_source"], text)

        if msg_type == "censor_event":
            _last_swear_time = time.time()
            _set_source_visible(settings_cache["banner_source"], True)
            _restart_media_source(settings_cache["sound_source"])
            duration_ms = max(100, int(settings_cache["banner_duration"] * 1000))
            obs.timer_add(_hide_banner_once, duration_ms)
