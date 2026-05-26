from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import base64
import hashlib
import json
import logging
from pathlib import Path
import socket
import struct
import threading
import time
from urllib.parse import urlparse

LOCALHOST = "127.0.0.1"
ALL_INTERFACES = "0.0.0.0"


class FrontendRuntimeAPI:
    def __init__(self, start_task=None, stop_task=None, get_status=None):
        self._start_task = start_task
        self._stop_task = stop_task
        self._get_status = get_status

    def get_status(self):
        if not callable(self._get_status):
            raise RuntimeError("Runtime status API is not configured")
        return self._get_status()

    def start_task(self, task=None, exit_after=False):
        if not callable(self._start_task):
            raise RuntimeError("Runtime start API is not configured")
        return self._start_task(task=task, exit_after=exit_after)

    def stop_task(self, task=None):
        if not callable(self._stop_task):
            raise RuntimeError("Runtime stop API is not configured")
        return self._stop_task(task=task)


class RuntimeWebSocketClient:
    def __init__(self, handler):
        self.handler = handler
        self._write_lock = threading.Lock()
        self._closed = threading.Event()

    def send_json(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_frame(body)

    def send_frame(self, payload):
        if self._closed.is_set():
            return
        payload_len = len(payload)
        if payload_len <= 125:
            header = bytes([0x81, payload_len])
        elif payload_len <= 65535:
            header = bytes([0x81, 126]) + struct.pack("!H", payload_len)
        else:
            header = bytes([0x81, 127]) + struct.pack("!Q", payload_len)
        with self._write_lock:
            self.handler.wfile.write(header + payload)
            self.handler.wfile.flush()

    def close(self):
        self._closed.set()

    @property
    def closed(self):
        return self._closed.is_set()


class WebSocketLogHandler(logging.Handler):
    def __init__(self, stream):
        super().__init__()
        self.stream = stream

    def emit(self, record):
        try:
            self.stream.publish_event(
                "log",
                {
                    "level": record.levelname,
                    "message": self.format(record),
                    "logger": record.name,
                    "thread": record.threadName,
                    "created": record.created,
                },
            )
        except Exception:
            return


class RuntimeEventStream:
    STATUS_INTERVAL_SECONDS = 1.0

    def __init__(self, runtime_api):
        self.runtime_api = runtime_api or FrontendRuntimeAPI()
        self._clients = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._status_thread = None
        self._log_handler = None
        self._log_formatter = logging.Formatter('%(asctime)s %(levelname)s %(threadName)s %(message)s')
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        self._status_thread = threading.Thread(target=self._status_loop, name="RuntimeStatusStream", daemon=True)
        self._status_thread.start()
        self._log_handler = WebSocketLogHandler(self)
        self._log_handler.setFormatter(self._log_formatter)
        logging.getLogger("ok").addHandler(self._log_handler)

    def stop(self):
        self._stop_event.set()
        if self._status_thread and self._status_thread.is_alive():
            self._status_thread.join(timeout=2)
        if self._log_handler is not None:
            logger = logging.getLogger("ok")
            if self._log_handler in logger.handlers:
                logger.removeHandler(self._log_handler)
            self._log_handler = None
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            client.close()

    def subscribe(self, handler):
        client = RuntimeWebSocketClient(handler)
        with self._lock:
            self._clients.add(client)
        client.send_json({
            "event": "hello",
            "data": {
                "protocol": "runtime-stream.v1",
                "server_time": time.time(),
            },
        })
        self.publish_runtime_status(single_client=client)
        return client

    def unsubscribe(self, client):
        if client is None:
            return
        client.close()
        with self._lock:
            self._clients.discard(client)

    def publish_event(self, event, data):
        payload = {"event": event, "data": data, "server_time": time.time()}
        self._broadcast(payload)

    def publish_runtime_status(self, single_client=None):
        try:
            status = self.runtime_api.get_status()
        except Exception as e:
            self.publish_event("error", {"message": str(e), "source": "runtime_status"})
            return
        payload = {"event": "runtime_status", "data": status, "server_time": time.time()}
        if single_client is not None:
            try:
                single_client.send_json(payload)
            except Exception:
                self.unsubscribe(single_client)
            return
        self._broadcast(payload)

    def _status_loop(self):
        while not self._stop_event.wait(self.STATUS_INTERVAL_SECONDS):
            self.publish_runtime_status()

    def _broadcast(self, payload):
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.send_json(payload)
            except Exception:
                self.unsubscribe(client)


def _bool_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


class FrontendRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, runtime_api=None, runtime_stream=None, directory=None, **kwargs):
        self.runtime_api = runtime_api or FrontendRuntimeAPI()
        self.runtime_stream = runtime_stream
        super().__init__(*args, directory=directory, **kwargs)

    def _send_json(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON body: {e.msg}") from e
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object")
        return data

    def _ok(self, data=None, message="ok"):
        self._send_json(200, {"code": 0, "message": message, "data": data})

    def _error(self, status_code, message):
        self._send_json(status_code, {"code": status_code, "message": message, "data": None})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/ws/runtime":
            self._handle_runtime_websocket()
            return
        if path == "/api/runtime/status":
            try:
                self._ok(self.runtime_api.get_status())
            except Exception as e:
                self._error(500, str(e))
            return
        if path.startswith("/api/"):
            self._error(404, f"Unknown API path: {path}")
            return
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/runtime/start":
            try:
                payload = self._read_json_body()
                task = payload.get("task")
                exit_after = _bool_value(payload.get("exit_after", False))
                result = self.runtime_api.start_task(task=task, exit_after=exit_after)
                if self.runtime_stream:
                    self.runtime_stream.publish_event("task_event", {
                        "action": "start",
                        "task": task,
                        "exit_after": exit_after,
                    })
                self._ok(result, "runtime started")
            except ValueError as e:
                self._error(400, str(e))
            except Exception as e:
                self._error(500, str(e))
            return
        if path == "/api/runtime/stop":
            try:
                payload = self._read_json_body()
                task = payload.get("task")
                result = self.runtime_api.stop_task(task=task)
                if self.runtime_stream:
                    self.runtime_stream.publish_event("task_event", {"action": "stop", "task": task})
                self._ok(result, "runtime stopped")
            except ValueError as e:
                self._error(400, str(e))
            except Exception as e:
                self._error(500, str(e))
            return
        if path.startswith("/api/"):
            self._error(404, f"Unknown API path: {path}")
            return
        self._error(405, "POST is only supported on API endpoints")

    def _handle_runtime_websocket(self):
        if self.runtime_stream is None:
            self._error(500, "Runtime websocket stream is not available")
            return
        upgrade = (self.headers.get("Upgrade") or "").lower()
        connection = (self.headers.get("Connection") or "").lower()
        key = self.headers.get("Sec-WebSocket-Key")
        if upgrade != "websocket" or "upgrade" not in connection or not key:
            self._error(400, "Invalid websocket handshake")
            return
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        client = self.runtime_stream.subscribe(self)
        try:
            while not client.closed:
                try:
                    self.connection.settimeout(0.2)
                    if self.connection.recv(1, socket.MSG_PEEK) == b"":
                        break
                except TimeoutError:
                    continue
                except (ConnectionResetError, OSError):
                    break
        finally:
            self.runtime_stream.unsubscribe(client)


def _resolve_frontend_path(path):
    frontend_path = Path(path).expanduser().resolve()
    if not frontend_path.exists():
        raise ValueError(f"Frontend path does not exist: {frontend_path}")
    if not frontend_path.is_dir():
        raise ValueError(f"Frontend path must be a directory: {frontend_path}")
    return frontend_path


def get_frontend_url(host, port):
    url_host = LOCALHOST if host == ALL_INTERFACES else host
    return f"http://{url_host}:{port}"


class FrontendThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, request_handler_class, runtime_stream=None):
        self.runtime_stream = runtime_stream
        super().__init__(server_address, request_handler_class)

    def server_close(self):
        if self.runtime_stream is not None:
            self.runtime_stream.stop()
        super().server_close()


def create_frontend_server(path=".", host=LOCALHOST, port=10086, runtime_api=None):
    frontend_path = _resolve_frontend_path(path)
    runtime_stream = RuntimeEventStream(runtime_api=runtime_api)
    runtime_stream.start()
    handler = partial(
        FrontendRequestHandler,
        directory=str(frontend_path),
        runtime_api=runtime_api,
        runtime_stream=runtime_stream,
    )
    try:
        server = FrontendThreadingHTTPServer((host, port), handler, runtime_stream=runtime_stream)
    except OSError as e:
        runtime_stream.stop()
        raise RuntimeError(f"Failed to start web server on {host}:{port}. The port may already be in use: {e}") from e
    return server, frontend_path


def start_frontend_server(path=".", host=LOCALHOST, port=10086, runtime_api=None):
    server, frontend_path = create_frontend_server(path=path, host=host, port=port, runtime_api=runtime_api)
    thread = threading.Thread(target=server.serve_forever, name="FrontendWebServer", daemon=True)
    thread.start()
    return server, thread, frontend_path, get_frontend_url(host, port)


def serve_frontend(path=".", host=LOCALHOST, port=10086, runtime_api=None):
    server, frontend_path = create_frontend_server(path=path, host=host, port=port, runtime_api=runtime_api)
    print(f"Serving frontend: {frontend_path}")
    print(f"URL: {get_frontend_url(host, port)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
