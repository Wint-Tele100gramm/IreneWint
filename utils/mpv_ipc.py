# Минимальный клиент mpv через JSON IPC (unix-сокет), без сторонних зависимостей.
# mpv запускается один раз в режиме --idle и живёт, пока работает Ирина.

import json
import os
import socket
import subprocess
import threading
import time
import logging

logger = logging.getLogger(__name__)


class MpvPlayer:
    def __init__(self, socket_path="/tmp/irene-mpv.sock", extra_args=None, on_event=None):
        self.socket_path = socket_path
        self.extra_args = extra_args or []
        self.on_event = on_event
        self._proc = None
        self._sock = None
        self._lock = threading.Lock()
        self._req_id = 0
        self._pending = {}
        self._reader = None

    # ---------- процесс ----------
    def start(self):
        if self._proc and self._proc.poll() is None and self._sock:
            return
        if os.path.exists(self.socket_path):
            try:
                os.remove(self.socket_path)
            except OSError:
                pass
        args = ["mpv", "--idle=yes", "--no-video", "--no-terminal", "--audio-display=no",
                "--input-ipc-server=" + self.socket_path, "--cache=yes",
                "--demuxer-max-bytes=8MiB"] + self.extra_args
        logger.info("Запуск mpv: " + " ".join(args))
        self._proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        for _ in range(50):
            if os.path.exists(self.socket_path):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(self.socket_path)
                    self._sock = s
                    break
                except OSError:
                    pass
            time.sleep(0.1)
        if not self._sock:
            raise RuntimeError("mpv не запустился (установлен ли пакет mpv?)")
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def stop_process(self):
        if self._proc:
            self._proc.terminate()
        self._proc = None
        self._sock = None

    def _read_loop(self):
        buf = b""
        sock = self._sock
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "request_id" in msg and msg["request_id"] in self._pending:
                    holder = self._pending[msg["request_id"]]
                    holder["resp"] = msg
                    holder["ev"].set()
                elif "event" in msg and self.on_event:
                    try:
                        self.on_event(msg)
                    except Exception as e:
                        logger.exception(e)
        self._sock = None

    # ---------- команды ----------
    def command(self, *args, timeout=3.0):
        self.start()
        with self._lock:
            self._req_id += 1
            rid = self._req_id
            holder = {"ev": threading.Event(), "resp": None}
            self._pending[rid] = holder
            data = json.dumps({"command": list(args), "request_id": rid}).encode("utf-8") + b"\n"
            self._sock.sendall(data)
        holder["ev"].wait(timeout)
        self._pending.pop(rid, None)
        resp = holder["resp"] or {}
        if resp.get("error") not in (None, "success"):
            logger.debug(f"mpv {args}: {resp.get('error')}")
        return resp.get("data")

    def get(self, prop, default=None):
        try:
            v = self.command("get_property", prop)
            return default if v is None else v
        except Exception:
            return default

    def set(self, prop, value):
        return self.command("set_property", prop, value)

    def load(self, url):
        self.command("loadfile", url, "replace")
        self.set("pause", False)

    def stop(self):
        self.command("stop")
