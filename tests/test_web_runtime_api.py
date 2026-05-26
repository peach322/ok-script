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
        runtime_api = ok_web.FrontendRuntimeAPI(
            start_task=self._start_task,
            stop_task=self._stop_task,
            get_status=self._get_status,
        )
        self.server, _ = ok_web.create_frontend_server(
            path=self.temp_dir.name,
            host=ok_web.LOCALHOST,
            port=0,
            runtime_api=runtime_api,
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        if getattr(self.server, "runtime_stream", None) is not None:
            self.server.runtime_stream.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    @staticmethod
    def _recv_exact(sock, n):
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("socket closed")
            data += chunk
        return data

    def _recv_ws_json(self, sock):
        header = self._recv_exact(sock, 2)
        payload_len = header[1] & 0x7F
        if payload_len == 126:
            payload_len = int.from_bytes(self._recv_exact(sock, 2), "big")
        elif payload_len == 127:
            payload_len = int.from_bytes(self._recv_exact(sock, 8), "big")
        payload = self._recv_exact(sock, payload_len)
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
            headers, _ = response.split(b"\r\n\r\n", 1)
            self.assertIn("101 Switching Protocols", headers.decode("ascii"))

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
