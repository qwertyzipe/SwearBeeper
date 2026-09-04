import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


OVERLAY_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Swear Beeper — Overlay</title>
<style>
  html, body {
    margin: 0;
    padding: 0;
    background: transparent;
    overflow: hidden;
    font-family: "Segoe UI", "Arial", sans-serif;
  }

  #counter {
    position: fixed;
    left: 24px;
    bottom: 54px;
    font-size: 22px;
    font-weight: 700;
    text-shadow: 0 0 6px rgba(0, 0, 0, 0.85), 0 1px 3px rgba(0, 0, 0, 0.9);
    transition: transform 0.15s ease-out;
    transform-origin: left bottom;
  }

  #counter.pulse {
    transform: scale(1.08);
  }

  #counter .label {
    font-weight: 500;
    margin-right: 6px;
  }

  #timer {
    position: fixed;
    left: 24px;
    bottom: 24px;
    font-size: 16px;
    font-weight: 600;
    text-shadow: 0 0 6px rgba(0, 0, 0, 0.85), 0 1px 3px rgba(0, 0, 0, 0.9);
  }

  #banner {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%) scale(0.7);
    opacity: 0;
    pointer-events: none;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  #bannerImg {
    display: none;
    max-width: 480px;
    max-height: 360px;
    border-radius: 14px;
    box-shadow: 0 0 40px rgba(0, 0, 0, 0.55);
  }

  #banner.show {
    animation: censorPop 1.3s ease-out forwards;
  }

  @keyframes censorPop {
    0%   { opacity: 0; transform: translate(-50%, -50%) scale(0.6); }
    12%  { opacity: 1; transform: translate(-50%, -50%) scale(1.08); }
    22%  { opacity: 1; transform: translate(-50%, -50%) scale(1.0); }
    80%  { opacity: 1; transform: translate(-50%, -50%) scale(1.0); }
    100% { opacity: 0; transform: translate(-50%, -50%) scale(0.92); }
  }

  #offline {
    position: fixed;
    right: 12px;
    top: 10px;
    font-size: 11px;
    color: rgba(255, 255, 255, 0.35);
  }
</style>
</head>
<body>
  <div id="counter">
    <span class="label" id="counterLabel">Матов:</span><span class="value" id="counterValue">0</span>
  </div>
  <div id="timer">
    <span id="timerText">Без мата: 00:00:00</span>
  </div>
  <div id="banner">
    <img id="bannerImg" alt="">
  </div>
  <div id="offline"></div>

<script>
  var lastEventId = null;
  var counterEl = document.getElementById("counter");
  var counterLabelEl = document.getElementById("counterLabel");
  var counterValueEl = document.getElementById("counterValue");
  var timerEl = document.getElementById("timer");
  var timerTextEl = document.getElementById("timerText");
  var bannerEl = document.getElementById("banner");
  var bannerImgEl = document.getElementById("bannerImg");
  var offlineEl = document.getElementById("offline");
  var missedPolls = 0;
  var currentImageVersion = -1;

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function formatDuration(totalSeconds) {
    totalSeconds = Math.max(0, Math.floor(totalSeconds));
    var days = Math.floor(totalSeconds / 86400); totalSeconds %= 86400;
    var hours = Math.floor(totalSeconds / 3600); totalSeconds %= 3600;
    var minutes = Math.floor(totalSeconds / 60);
    var seconds = totalSeconds % 60;
    if (days > 0) {
      return days + "д " + pad2(hours) + ":" + pad2(minutes) + ":" + pad2(seconds);
    }
    return pad2(hours) + ":" + pad2(minutes) + ":" + pad2(seconds);
  }

  function applyCounterStyle(state) {
    counterLabelEl.textContent = state.counter_label || "Матов:";
    counterLabelEl.style.color = state.counter_label_color || "#cfcfcf";
    counterValueEl.style.color = state.counter_value_color || "#ff5b5b";
  }

  function applyTimer(state) {
    if (!state.timer_enabled) {
      timerEl.style.display = "none";
      return;
    }
    timerEl.style.display = "block";
    var base = state.last_swear_epoch || state.start_epoch;
    var elapsed = (Date.now() / 1000) - base;
    var fmt = state.timer_format || "Без мата: {time}";
    timerTextEl.textContent = fmt.replace("{time}", formatDuration(elapsed));
    timerTextEl.style.color = state.timer_color || "#cfcfcf";
  }

  function applyBannerContent(state) {
    // Баннер - ТОЛЬКО картинка, которую выбрал пользователь. Нет картинки - нет баннера.
    if (state.has_image) {
      if (state.image_version !== currentImageVersion) {
        currentImageVersion = state.image_version;
        bannerImgEl.src = "/banner-image?v=" + state.image_version;
      }
      bannerImgEl.style.display = "block";
    } else {
      bannerImgEl.style.display = "none";
    }
  }

  function showBanner(state) {
    if (!state.has_image) return; // нечего показывать без картинки
    bannerEl.classList.remove("show");
    // форс-рефлоу, чтобы анимацию можно было перезапустить если сработает подряд
    void bannerEl.offsetWidth;
    bannerEl.classList.add("show");
  }

  function pulseCounter() {
    counterEl.classList.add("pulse");
    setTimeout(function () { counterEl.classList.remove("pulse"); }, 180);
  }

  function poll() {
    fetch("/state", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (state) {
        missedPolls = 0;
        offlineEl.textContent = "";
        counterValueEl.textContent = state.session_total;
        applyCounterStyle(state);
        applyTimer(state);
        applyBannerContent(state);

        if (lastEventId === null) {
          lastEventId = state.last_event_id;
        } else if (state.last_event_id !== lastEventId) {
          lastEventId = state.last_event_id;
          showBanner(state);
          pulseCounter();
        }
      })
      .catch(function () {
        missedPolls += 1;
        if (missedPolls > 5) {
          offlineEl.textContent = "нет связи с SwearBeeper...";
        }
      })
      .finally(function () {
        setTimeout(poll, 350);
      });
  }

  poll();
</script>
</body>
</html>
"""


class _OverlayRequestHandler(BaseHTTPRequestHandler):
    server_version = "SwearBeeperOverlay/1.0"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/overlay", "/overlay/"):
            self._send_html(OVERLAY_HTML)
        elif path in ("/state", "/state.json"):
            self._send_json(self.server.overlay.get_state())
        elif path == "/banner-image":
            image = self.server.overlay.get_banner_image()
            if image is None:
                self.send_error(404, "No custom banner image set")
                return
            data, content_type = image
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404, "Not Found")

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def _looks_like_hex_color(value):
    if not isinstance(value, str) or len(value) != 7 or value[0] != "#":
        return False
    try:
        int(value[1:], 16)
        return True
    except ValueError:
        return False


class OverlayServer:
    """Локальный HTTP-сервер веб-виджета для OBS.

    Отдаёт по /overlay готовую HTML-страничку:
    - счётчик матов (просто текст, без фона/рамки/иконок - настраиваются текст и цвета)
    - таймер "без мата" (тоже просто текст, формат настраивается через плейсхолдер {time})
    - баннер при цензуре - ТОЛЬКО картинка, которую выбирает пользователь
      (нет картинки - нет баннера)

    Страничка сама опрашивает /state. Стримеру достаточно добавить в OBS
    источник «Браузер» и вставить адрес — никаких путей к Python и скриптов."""

    def __init__(self, port):
        self.port = port
        self.httpd = None
        self.thread = None
        self.state_lock = threading.Lock()
        self.state = {
            "session_total": 0,
            "alltime_total": 0,
            "delay_sec": 0,
            "last_event_id": 0,
            "counter_label": "Матов:",
            "counter_label_color": "#cfcfcf",
            "counter_value_color": "#ff5b5b",
            "timer_enabled": True,
            "timer_format": "Без мата: {time}",
            "timer_color": "#cfcfcf",
            "start_epoch": time.time(),
            "last_swear_epoch": None,
            "has_image": False,
            "image_version": 0,
        }
        self.banner_image_bytes = None
        self.banner_image_content_type = "application/octet-stream"

    def start(self, max_attempts=10):
        for attempt in range(max_attempts):
            candidate_port = self.port + attempt
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", candidate_port), _OverlayRequestHandler)
            except OSError:
                continue
            httpd.overlay = self
            httpd.daemon_threads = True
            self.httpd = httpd
            self.port = candidate_port
            self.thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            self.thread.start()
            return True
        return False

    def update_snapshot(self, session_total, alltime_total, delay_sec=None):
        with self.state_lock:
            self.state["session_total"] = session_total
            self.state["alltime_total"] = alltime_total
            if delay_sec is not None:
                self.state["delay_sec"] = delay_sec

    def notify_censor_event(self, word=None, ts=None):
        with self.state_lock:
            self.state["last_swear_epoch"] = time.time()
            self.state["last_event_id"] = self.state.get("last_event_id", 0) + 1

    def set_counter_label(self, text):
        text = (text or "").strip() or "Матов:"
        with self.state_lock:
            self.state["counter_label"] = text

    def set_counter_colors(self, label_color=None, value_color=None):
        with self.state_lock:
            if label_color and _looks_like_hex_color(label_color):
                self.state["counter_label_color"] = label_color
            if value_color and _looks_like_hex_color(value_color):
                self.state["counter_value_color"] = value_color

    def set_timer_enabled(self, enabled):
        with self.state_lock:
            self.state["timer_enabled"] = bool(enabled)

    def set_timer_format(self, text):
        text = (text or "").strip() or "Без мата: {time}"
        if "{time}" not in text:
            text = text + " {time}"  
        with self.state_lock:
            self.state["timer_format"] = text

    def set_timer_color(self, color_hex):
        with self.state_lock:
            if color_hex and _looks_like_hex_color(color_hex):
                self.state["timer_color"] = color_hex

    def set_banner_image(self, path):
        """Загружает картинку с диска и отдаёт её дальше как /banner-image.
        Хостинга не нужно - файл читается один раз и раздаётся локальным сервером.
        Баннер - только картинка; текстовой альтернативы не предусмотрено."""
        content_type, _ = mimetypes.guess_type(path)
        if not content_type or not content_type.startswith("image/"):
            raise ValueError("Файл не похож на изображение (ожидается .png/.jpg/.gif/.webp)")
        with open(path, "rb") as f:
            data = f.read()
        with self.state_lock:
            self.banner_image_bytes = data
            self.banner_image_content_type = content_type
            self.state["has_image"] = True
            self.state["image_version"] = self.state.get("image_version", 0) + 1

    def clear_banner_image(self):
        with self.state_lock:
            self.banner_image_bytes = None
            self.state["has_image"] = False

    def get_banner_image(self):
        with self.state_lock:
            if self.banner_image_bytes is None:
                return None
            return self.banner_image_bytes, self.banner_image_content_type

    def get_state(self):
        with self.state_lock:
            return dict(self.state)

    def url(self):
        return f"http://localhost:{self.port}/overlay"

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
