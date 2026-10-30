"""Native runtime regressions: live transports, honest health and delivery ownership.

All app endpoints and agentapi processes here are test fixtures; no installed AI is woken.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_native_csharp import NO_WINDOW, _native_exe

from nexus_relay.box import Box


def test_native_workers_recover_and_do_not_replay_uncertain_requests():
    completed = subprocess.run(
        [str(_native_exe()), "--runtime-selftest"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    assert json.loads(completed.stdout)["ok"] is True


def _read_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        chunk = stream.read(size - len(result))
        if not chunk:
            raise EOFError
        result.extend(chunk)
    return result


def test_cdp_reads_multiple_responses_and_real_controls():
    class Debugger(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args):
            pass

        def do_GET(self):
            if self.path == "/json/list":
                data = json.dumps(
                    [
                        {
                            "type": "page",
                            "url": "app://-/index.html",
                            "webSocketDebuggerUrl": f"ws://127.0.0.1:{server.server_port}/debugger",
                        }
                    ]
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            key = self.headers["Sec-WebSocket-Key"]
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept.decode())
            self.end_headers()
            while True:
                try:
                    header = _read_exact(self.rfile, 2)
                    if header[0] & 15 == 8:
                        return
                    size = header[1] & 127
                    if size == 126:
                        size = struct.unpack("!H", _read_exact(self.rfile, 2))[0]
                    elif size == 127:
                        size = struct.unpack("!Q", _read_exact(self.rfile, 8))[0]
                    mask = _read_exact(self.rfile, 4) if header[1] & 128 else bytes(4)
                    raw = _read_exact(self.rfile, size)
                    request = json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(raw)))
                    expression = request["params"]["expression"]
                    if expression.startswith("[...document.querySelectorAll('button,[role=button],[role=menuitem]"):
                        value = [
                            {"role": "button", "label": "Nouveau chat"},
                            {"role": "button", "label": "Démarrer un nouveau chat dans Nexus Relay"},
                        ]
                    elif "return e?e.innerText.trim():null" in expression:
                        value = "Codex"
                    else:
                        value = True
                    data = json.dumps({"id": request["id"], "result": {"result": {"value": value}}}).encode()
                    self.wfile.write(
                        bytes([0x81, len(data)]) + data
                        if len(data) < 126
                        else b"\x81\x7e" + struct.pack("!H", len(data)) + data
                    )
                    self.wfile.flush()
                except (EOFError, ConnectionError):
                    return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Debugger)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        response = subprocess.run(
            [str(_native_exe()), "--inspect", "codex"],
            env={**os.environ, "NEXUS_RELAY_CODEX_PORT": str(server.server_port)},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=True,
            creationflags=NO_WINDOW,
        )
        data = json.loads(response.stdout)
        assert data["status"] == "success", data
        assert data["relay_project_visible"] is True
        assert data["mode"] == "Codex"
        assert data["composer_visible"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_sidecar_delivers_by_official_adapter_and_reuses_topic(tmp_path):
    relay = tmp_path / "relay"
    box = Box(relay / "v1")
    info = box.post("claude", "antigravity", "Context only", "native-delivery", info=True)
    first = box.post("claude", "antigravity", "Technical fixture", "native-delivery")
    calls = tmp_path / "calls.jsonl"
    conversation = "12345678-1234-1234-1234-123456789abc"
    gemini = tmp_path / "gemini"
    folder = gemini / "antigravity" / "conversations"
    folder.mkdir(parents=True)
    (folder / f"{conversation}.db").touch()
    script = tmp_path / "fake_agentapi.py"
    script.write_text(
        "import json,os,sys\n"
        "with open(os.environ['TEST_CALLS'],'a',encoding='utf-8') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
        f"print(json.dumps({{'response':{{'newConversation':{{'conversationId':{conversation!r}}}}}}}))\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "NEXUS_RELAY_HOME": str(relay),
        "NEXUS_RELAY_GEMINI_HOME": str(gemini),
        "NEXUS_RELAY_AGENTAPI": json.dumps([sys.executable, str(script)]),
        "TEST_CALLS": str(calls),
    }
    proc = subprocess.Popen(
        [str(_native_exe()), "--sidecar"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 15
        while not box.has_event(first.id, "livré") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert box.has_event(first.id, "livré")
        second = box.post("claude", "antigravity", "Next fixture", "native-delivery")
        deadline = time.monotonic() + 10
        while not box.has_event(second.id, "livré") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert box.has_event(second.id, "livré")
        requests = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
        assert [r[0] for r in requests] == ["new-conversation", "send-message"]
        assert conversation in requests[1]
        assert first.id in requests[0][-1] and second.id in requests[1][-1]
        assert f'Pour info (sans réponse) : read(id="{info.id}")' in requests[0][-1]
        assert box.has_event(info.id, "livré")
        topic_state = box.topic_state("native-delivery", "agy")
        assert topic_state is not None and topic_state["conversation"] == conversation
    finally:
        proc.terminate()
        proc.communicate(timeout=10)


def test_http_health_reports_runtime_identity_and_graceful_restart(tmp_path):
    import urllib.error
    import urllib.request

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    relay = tmp_path / "relay"
    token = "native-test-token-" * 4
    env = {
        **os.environ,
        "NEXUS_RELAY_HOME": str(relay),
        "NEXUS_MCP_TOKEN": token,
        "NEXUS_RELAY_HOST": "claude",
        "CODEX_HOME": str(tmp_path / "codex"),
    }
    proc = subprocess.Popen(
        [str(_native_exe()), "--http", str(port), "--protocol-fixture"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                    data = json.load(response)
                    break
            except (OSError, urllib.error.HTTPError):
                assert time.monotonic() < deadline
                time.sleep(0.05)
        assert data["status"] == "ok"
        assert data["service"] == "nexus-relay" and data["runtime"] == "native"
        assert data["pid"] == proc.pid
        assert "activities" not in data
        (relay / "daemon-restart.request").write_text("force test", encoding="utf-8")
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.communicate(timeout=10)
