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
    "video_sources": [],
    "auto_sync_delay": True,
    "manual_delay_ms": 0,
}

VIDEO_DELAY_FILTER_NAME = "SwearBeeper Sync Delay"
_last_applied_delay_ms = {}


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
        "6. Запусти SwearBeeper и нажми Старт (или Тест микрофона) — счётчик и баннер должны заработать.\n\n"
        "СИНХРОНИЗАЦИЯ ВИДЕО С ЗАДЕРЖКОЙ ЗВУКА:\n"
        "Раз звук выходит с задержкой (~1-2 сек), картинка (веб-камера/захват игры) может "
        "визуально 'опережать' звук. Укажи в поле 'Видео-источник' имя источника (например твоей "
        "веб-камеры), включи 'Авто-синхронизация' — скрипт сам навесит на него фильтр "
        "'Video Delay (Async)' и будет держать задержку видео равной задержке звука в SwearBeeper. "
        "Либо выключи авто-синхронизацию и задай задержку вручную в миллисекундах."
    )


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_int(props, "port", "Порт подключения к SwearBeeper (см. лог приложения)", 1024, 65535, 1)
    obs.obs_properties_add_text(props, "counter_source", "Текстовый источник счётчика", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "counter_format", "Формат текста счётчика", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "banner_source", "Источник баннера (текст/картинка)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_float(props, "banner_duration", "Длительность показа баннера (сек)", 0.2, 10.0, 0.1)
    obs.obs_properties_add_text(props, "sound_source", "Media-источник звука (опционально)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "timer_source", "Текстовый источник таймера 'без мата'", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "timer_format", "Формат текста таймера", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "video_source", "Видео-источники для синхронизации (через запятую: камера, захват игры)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_bool(props, "auto_sync_delay", "Авто-синхронизация с задержкой SwearBeeper")
    obs.obs_properties_add_int(props, "manual_delay_ms", "Задержка видео вручную (мс, если авто выключена)", 0, 10000, 50)
    return props


def script_defaults(settings):
    obs.obs_data_set_default_int(settings, "port", PORT)
    obs.obs_data_set_default_string(settings, "counter_source", "")
    obs.obs_data_set_default_string(settings, "counter_format", "Маты: {session}")
    obs.obs_data_set_default_string(settings, "banner_source", "")
    obs.obs_data_set_default_double(settings, "banner_duration", 1.5)
    obs.obs_data_set_default_string(settings, "sound_source", "")
    obs.obs_data_set_default_string(settings, "timer_source", "")
    obs.obs_data_set_default_string(settings, "timer_format", "Без мата: {time}")
    obs.obs_data_set_default_string(settings, "video_source", "")
    obs.obs_data_set_default_bool(settings, "auto_sync_delay", True)
    obs.obs_data_set_default_int(settings, "manual_delay_ms", 0)


def script_update(settings):
    global PORT
    PORT = obs.obs_data_get_int(settings, "port") or PORT
    settings_cache["counter_source"] = obs.obs_data_get_string(settings, "counter_source")
    settings_cache["counter_format"] = obs.obs_data_get_string(settings, "counter_format") or "Маты: {session}"
    settings_cache["banner_source"] = obs.obs_data_get_string(settings, "banner_source")
    settings_cache["banner_duration"] = obs.obs_data_get_double(settings, "banner_duration")
    settings_cache["sound_source"] = obs.obs_data_get_string(settings, "sound_source")
    settings_cache["timer_source"] = obs.obs_data_get_string(settings, "timer_source")
    settings_cache["timer_format"] = obs.obs_data_get_string(settings, "timer_format") or "Без мата: {time}"
    raw_sources = obs.obs_data_get_string(settings, "video_source")
    settings_cache["video_sources"] = [s.strip() for s in raw_sources.split(",") if s.strip()]
    settings_cache["auto_sync_delay"] = obs.obs_data_get_bool(settings, "auto_sync_delay")
    settings_cache["manual_delay_ms"] = obs.obs_data_get_int(settings, "manual_delay_ms")

    if not settings_cache["auto_sync_delay"]:
        _apply_video_delay(settings_cache["manual_delay_ms"])


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


RENDER_DELAY_MAX_MS = 500
MAX_STACKED_FILTERS = 20


def _is_async_source(source):
    try:
        flags = obs.obs_source_get_output_flags(source)
        return bool(flags & obs.OBS_SOURCE_ASYNC)
    except Exception:
        return True  # по умолчанию считаем async (веб-камера) - безопаснее для типичного случая


def _create_or_update_filter(source, filt_name, filter_id, delay_ms):
    existing = obs.obs_source_get_filter_by_name(source, filt_name)
    filter_settings = obs.obs_data_create()
    obs.obs_data_set_int(filter_settings, "delay_ms", delay_ms)

    if existing:
        obs.obs_source_update(existing, filter_settings)
        obs.obs_source_release(existing)
    else:
        new_filter = obs.obs_source_create(filter_id, filt_name, filter_settings, None)
        if new_filter:
            obs.obs_source_filter_add(source, new_filter)
            obs.obs_source_release(new_filter)

    obs.obs_data_release(filter_settings)


def _remove_filter_if_exists(source, filt_name):
    existing = obs.obs_source_get_filter_by_name(source, filt_name)
    if existing:
        obs.obs_source_filter_remove(source, existing)
        obs.obs_source_release(existing)


def _cleanup_stacked_filters(source, keep_names):
    for i in range(1, MAX_STACKED_FILTERS + 1):
        name = f"{VIDEO_DELAY_FILTER_NAME} {i}"
        if name not in keep_names:
            _remove_filter_if_exists(source, name)


def _apply_delay_to_source(source_name, delay_ms):
    if _last_applied_delay_ms.get(source_name) == delay_ms:
        return

    source = obs.obs_get_source_by_name(source_name)
    if not source:
        return

    if _is_async_source(source):
        # Веб-камера / медиа-источник - обычный "Video Delay (Async)", без ограничения по мс
        _create_or_update_filter(source, VIDEO_DELAY_FILTER_NAME, "async_delay_filter", delay_ms)
        _cleanup_stacked_filters(source, keep_names=[])
    else:
        # Захват экрана/окна/игры - "Render Delay", максимум 500мс на инстанс,
        # поэтому при необходимости складываем несколько фильтров подряд.
        _remove_filter_if_exists(source, VIDEO_DELAY_FILTER_NAME)

        remaining = delay_ms
        idx = 1
        active_names = []
        while remaining > 0 and idx <= MAX_STACKED_FILTERS:
            chunk = min(remaining, RENDER_DELAY_MAX_MS)
            filt_name = f"{VIDEO_DELAY_FILTER_NAME} {idx}"
            _create_or_update_filter(source, filt_name, "gpu_delay", chunk)
            active_names.append(filt_name)
            remaining -= chunk
            idx += 1

        _cleanup_stacked_filters(source, keep_names=active_names)

    obs.obs_source_release(source)
    _last_applied_delay_ms[source_name] = delay_ms


def _apply_video_delay(delay_ms):
    delay_ms = max(0, int(delay_ms))
    for source_name in settings_cache["video_sources"]:
        _apply_delay_to_source(source_name, delay_ms)


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

            if settings_cache["auto_sync_delay"] and "delay_sec" in msg:
                _apply_video_delay(msg["delay_sec"] * 1000)

        if msg_type == "censor_event":
            _last_swear_time = time.time()
            _set_source_visible(settings_cache["banner_source"], True)
            _restart_media_source(settings_cache["sound_source"])
            duration_ms = max(100, int(settings_cache["banner_duration"] * 1000))
            obs.timer_add(_hide_banner_once, duration_ms)
