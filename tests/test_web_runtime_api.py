import base64
import http.client
import importlib.util
import json
import logging
import socket
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path


WEB_MODULE_PATH = Path(__file__).resolve().parents[1] / "ok" / "web.py"
_spec = importlib.util.spec_from_file_location("ok_web_module_for_test", WEB_MODULE_PATH)
ok_web = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ok_web)


class TestWebRuntimeApi(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        Path(self.temp_dir.name, "index.html").write_text("ok", encoding="utf-8")
        self.calls = []
        self.config_data = {
            "runtime": {"debug": False, "use_gui": False},
            "browser": {"url": "http://127.0.0.1:10086", "nick": "Browser"},
            "device": {"preferred": "browser", "capture": "browser", "interaction": ""},
        }
        runtime_api = ok_web.FrontendRuntimeAPI(
            start_task=self._start_task,
            stop_task=self._stop_task,
            get_status=self._get_status,
            get_config=self._get_config,
            update_config=self._update_config,
        )
        self.server, _ = ok_web.create_frontend_server(
            path=self.temp_dir.name,
            host=ok_web.LOCALHOST,
            port=0,
            runtime_api=runtime_api,
        )
        self.port = self.server.server_address[1]
        self._ws_buffer = b""
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        if getattr(self.server, "runtime_stream", None) is not None:
            self.server.runtime_stream.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def _recv_ws_json(self, sock):
        while len(self._ws_buffer) < 2:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("socket closed")
            self._ws_buffer += chunk
        header = self._ws_buffer[:2]
        self._ws_buffer = self._ws_buffer[2:]
        payload_len = header[1] & 0x7F
        if payload_len == 126:
            while len(self._ws_buffer) < 2:
                self._ws_buffer += sock.recv(4096)
            payload_len = int.from_bytes(self._ws_buffer[:2], "big")
            self._ws_buffer = self._ws_buffer[2:]
        elif payload_len == 127:
            while len(self._ws_buffer) < 8:
                self._ws_buffer += sock.recv(4096)
            payload_len = int.from_bytes(self._ws_buffer[:8], "big")
            self._ws_buffer = self._ws_buffer[8:]
        while len(self._ws_buffer) < payload_len:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("socket closed")
            self._ws_buffer += chunk
        payload = self._ws_buffer[:payload_len]
        self._ws_buffer = self._ws_buffer[payload_len:]
        return json.loads(payload.decode("utf-8"))

    def _wait_for_event(self, sock, event_name, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            payload = self._recv_ws_json(sock)
            if payload.get("event") == event_name:
                return payload
        raise TimeoutError(f"Timed out waiting for websocket event: {event_name}")

    def _request_json(self, method, path, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw_body = response.read().decode("utf-8")
        connection.close()
        return response.status, json.loads(raw_body)

    def _start_task(self, task=None, exit_after=False):
        self.calls.append(("start", task, exit_after))
        return {"started": True, "task": task, "exit_after": exit_after}

    def _stop_task(self, task=None):
        self.calls.append(("stop", task))
        return {"stopped_tasks": [task] if task else []}

    def _get_status(self):
        self.calls.append(("status",))
        return {"initialized": True, "current_task": "TaskA"}

    def _get_config(self):
        self.calls.append(("config_get",))
        return self.config_data

    def _update_config(self, patch):
        self.calls.append(("config_update", patch))
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(self.config_data.get(key), dict):
                self.config_data[key].update(value)
            else:
                self.config_data[key] = value
        return {"applied": patch, "config": self.config_data}

    def test_runtime_status_start_stop_api(self):
        status_code, payload = self._request_json("GET", "/api/runtime/status")
        self.assertEqual(200, status_code)
        self.assertEqual(0, payload["code"])
        self.assertEqual("TaskA", payload["data"]["current_task"])

        status_code, payload = self._request_json(
            "POST",
            "/api/runtime/start",
            {"task": "TaskB", "exit_after": True},
        )
        self.assertEqual(200, status_code)
        self.assertEqual("runtime started", payload["message"])

        status_code, payload = self._request_json("POST", "/api/runtime/stop", {"task": "TaskB"})
        self.assertEqual(200, status_code)
        self.assertEqual("runtime stopped", payload["message"])

        self.assertEqual(
            [("status",), ("start", "TaskB", True), ("stop", "TaskB")],
            self.calls,
        )

    def test_runtime_api_invalid_json_returns_400(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            "/api/runtime/start",
            body=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()

        self.assertEqual(400, response.status)
        self.assertEqual(400, payload["code"])

    def test_config_get_and_update_api(self):
        status_code, payload = self._request_json("GET", "/api/config/get")
        self.assertEqual(200, status_code)
        self.assertEqual("Browser", payload["data"]["browser"]["nick"])

        status_code, payload = self._request_json(
            "POST",
            "/api/config/update",
            {"browser": {"nick": "Browser2"}, "runtime": {"debug": True}},
        )
        self.assertEqual(200, status_code)
        self.assertEqual("config updated", payload["message"])
        self.assertEqual("Browser2", payload["data"]["config"]["browser"]["nick"])
        self.assertTrue(payload["data"]["config"]["runtime"]["debug"])

    def test_config_update_rejects_unsupported_section(self):
        status_code, payload = self._request_json("POST", "/api/config/update", {"invalid": {"k": 1}})
        self.assertEqual(400, status_code)
        self.assertEqual(400, payload["code"])
        self.assertIn("Unsupported config section", payload["message"])
        self.assertFalse(any(call[0] == "config_update" for call in self.calls))

    def test_config_update_rejects_unsupported_field(self):
        status_code, payload = self._request_json("POST", "/api/config/update", {"runtime": {"bad": True}})
        self.assertEqual(400, status_code)
        self.assertEqual(400, payload["code"])
        self.assertIn("Unsupported runtime field", payload["message"])
        self.assertFalse(any(call[0] == "config_update" for call in self.calls))

    def test_config_update_rejects_invalid_runtime_type(self):
        status_code, payload = self._request_json("POST", "/api/config/update", {"runtime": {"debug": "true"}})
        self.assertEqual(400, status_code)
        self.assertEqual(400, payload["code"])
        self.assertIn("runtime.debug must be a boolean", payload["message"])
        self.assertFalse(any(call[0] == "config_update" for call in self.calls))

    def test_runtime_websocket_stream_pushes_task_and_log_events(self):
        ws_key = base64.b64encode(uuid.uuid4().bytes).decode("ascii")
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            request = (
                "GET /ws/runtime HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {ws_key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            )
            sock.sendall(request.encode("ascii"))
            response = b""
            while b"\r\n\r\n" not in response:
                response += sock.recv(1024)
            headers, remainder = response.split(b"\r\n\r\n", 1)
            self.assertIn("101 Switching Protocols", headers.decode("ascii"))
            self._ws_buffer = remainder
            hello_event = self._wait_for_event(sock, "hello")
            self.assertEqual("runtime-stream.v1", hello_event["data"]["protocol"])

            self.server.runtime_stream.publish_event("task_event", {"action": "start", "task": "TaskWS"})
            task_event = self._wait_for_event(sock, "task_event")
            self.assertEqual("start", task_event["data"]["action"])
            self.assertEqual("TaskWS", task_event["data"]["task"])

            marker = f"ws-log-{uuid.uuid4().hex}"
            logging.getLogger("ok").warning(marker)
            log_event = self._wait_for_event(sock, "log")
            self.assertIn(marker, log_event["data"]["message"])
            self.assertEqual("WARNING", log_event["data"]["level"])
        finally:
            sock.close()


if __name__ == "__main__":
    unittest.main()
