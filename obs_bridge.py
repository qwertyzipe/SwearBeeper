import json
import socket
import threading


class ObsBridgeServer:
    """Локальный TCP-сервер: рассылает JSON-события подключённому OBS-скрипту
    (счётчик матов, момент цензуры) построчно (JSON Lines)."""

    def __init__(self, port):
        self.port = port
        self.server_socket = None
        self.clients = []
        self.lock = threading.Lock()
        self.running = False

    def start(self, max_attempts=10):
        for attempt in range(max_attempts):
            candidate_port = self.port + attempt
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", candidate_port))
                s.listen(5)
            except OSError:
                s.close()
                continue
            self.server_socket = s
            self.port = candidate_port
            self.running = True
            threading.Thread(target=self._accept_loop, daemon=True).start()
            return True
        return False

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
