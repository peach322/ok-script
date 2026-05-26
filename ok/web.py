from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
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


def _bool_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


class FrontendRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, runtime_api=None, directory=None, **kwargs):
        self.runtime_api = runtime_api or FrontendRuntimeAPI()
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


def create_frontend_server(path=".", host=LOCALHOST, port=10086, runtime_api=None):
    frontend_path = _resolve_frontend_path(path)
    handler = partial(FrontendRequestHandler, directory=str(frontend_path), runtime_api=runtime_api)
    try:
        server = ThreadingHTTPServer((host, port), handler)
    except OSError as e:
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
