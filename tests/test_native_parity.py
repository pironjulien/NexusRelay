"""Native MCP migration gate: stable schemas, grouped waits and cancelled deliveries.

The daemon uses an isolated mailbox and an explicit future wake cutoff. No AI receives anything.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

from jsonschema import Draft202012Validator
from test_native_csharp import NO_WINDOW, _native_exe, _stdio_rpc, _tool_call_data

from nexus_relay import server
from nexus_relay.box import Box


def test_all_native_input_and_output_contracts_match_python_oracle():
    native = json.loads(subprocess.check_output([str(_native_exe()), "--schemas"], creationflags=NO_WINDOW))
    oracle = {
        tool.name: tool.model_dump(by_alias=True, exclude_none=True) for tool in asyncio.run(server.mcp.list_tools())
    }
    assert {tool["name"] for tool in native} == set(oracle)
    for tool in native:
        for schema in ("inputSchema", "outputSchema"):
            assert tool.get(schema) == oracle[tool["name"]].get(schema), (tool["name"], schema)
            if schema in tool:
                Draft202012Validator.check_schema(tool[schema])
    by_name = {tool["name"]: tool for tool in native}
    for name, field, scalar, group in (
        ("send", "to", "codex", ["codex", "antigravity"]),
        ("wait_reply", "id", "id-a", ["id-a", "id-b"]),
    ):
        schema = by_name[name]["inputSchema"]["properties"][field]
        validator = Draft202012Validator(schema)
        assert validator.is_valid(scalar) and validator.is_valid(group)
        assert not validator.is_valid([1]) and not validator.is_valid(17)


def test_every_typed_native_tool_returns_data_valid_against_its_embedded_output_schema(tmp_path):
    relay = tmp_path / "typed-mailbox"
    box = Box(relay / "v1")
    ask = box.post("claude", "codex", "fixture café 😀", "typed-fixture")
    answer = box.post("codex", "claude", "fixture answer", "typed-fixture", reply_to=ask.id)
    blocked = box.post("claude", "codex", "fixture still unavailable", "typed-fixture")
    box.set_ring_state(
        blocked.id, {"agent": "codex", "incident": "fixture unavailable", "incident_at": blocked.created}
    )
    calls = {
        "inbox": {},
        "read": {"id": answer.id},
        "track": {"id": ask.id},
        "reservations": {},
        "search": {"query": "café"},
        "summary": {"topic": "typed-fixture"},
        "status": {},
    }
    requests = [
        {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        for i, (name, arguments) in enumerate(calls.items(), start=1)
    ]
    results = _stdio_rpc(_native_exe(), {**os.environ, "NEXUS_RELAY_HOME": str(relay)}, "claude", requests)
    schemas = {
        tool["name"]: tool
        for tool in json.loads(subprocess.check_output([str(_native_exe()), "--schemas"], creationflags=NO_WINDOW))
    }
    for name, response in zip(calls, results, strict=True):
        data = _tool_call_data(response)
        assert data["status"] == "success", (name, data)
        errors = list(Draft202012Validator(schemas[name]["outputSchema"]).iter_errors(data))
        assert not errors, (name, [e.message for e in errors])


@contextmanager
def native_daemon(tmp_path, fixture=True):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = "isolated-parity-token-" * 4
    relay = tmp_path / "mailbox"
    env = {
        **os.environ,
        "NEXUS_RELAY_HOME": str(relay),
        "NEXUS_RELAY_HOST": "claude",
        "NEXUS_MCP_TOKEN": token,
        "NEXUS_RELAY_WAKE_SINCE": "2099-01-01T00:00:00Z",
        "NEXUS_RELAY_CODEX_PORT": "1",
        "NEXUS_RELAY_AGY_PORT": "1",
    }
    command = [str(_native_exe()), "--http", str(port)]
    if fixture:
        command.append("--protocol-fixture")
    proc = subprocess.Popen(command, env=env, creationflags=NO_WINDOW)

    def rpc(request, accept="application/json"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp?agent=claude",
            data=json.dumps(request).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": accept,
                "Mcp-Session-Id": "parity-session",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            body = response.read().decode()
        if accept == "text/event-stream":
            return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        return json.loads(body) if body else None

    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                    break
            except (OSError, urllib.error.HTTPError):
                assert time.monotonic() < deadline
                time.sleep(0.05)
        yield Box(relay / "v1"), rpc, relay, proc
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=10)


def wait_call(ids, request_id=89, timeout=1):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": "wait_reply",
            "arguments": {"id": ids, "timeout_s": timeout},
            "_meta": {"progressToken": "parity-progress"},
        },
    }


def payload(response):
    return json.loads(response["result"]["content"][0]["text"])


def test_native_group_wait_has_one_deadline_and_real_sse_progress(tmp_path):
    with native_daemon(tmp_path) as (box, rpc, _, _):
        first = box.post("claude", "codex", "fixture", "parity-a")
        second = box.post("claude", "antigravity", "fixture", "parity-b")
        start = time.monotonic()
        events = rpc(wait_call([first.id, second.id]), "text/event-stream")
        assert isinstance(events, list)
        elapsed = time.monotonic() - start
        assert elapsed < 1.8, elapsed
        progress = [event for event in events if event.get("method") == "notifications/progress"]
        assert progress and progress[0]["params"]["progressToken"] == "parity-progress"
        result = payload(events[-1])
        assert result["status"] == "timeout" and result["total"] == 2
        assert [item["status"] for item in result["replies"]] == ["timeout", "timeout"]


def test_native_group_cancellation_does_not_consume_the_held_first_reply(tmp_path):
    with native_daemon(tmp_path) as (box, rpc, _, _):
        first = box.post("claude", "codex", "fixture", "parity-a")
        second = box.post("claude", "antigravity", "fixture", "parity-b")
        answer = box.post("codex", "claude", "fixture answer", "parity-a", reply_to=first.id)
        result = []
        waiting = threading.Thread(target=lambda: result.append(rpc(wait_call([first.id, second.id], timeout=10))))
        waiting.start()
        deadline = time.monotonic() + 5
        while set(box.state("waited") or {}) != {first.id, second.id}:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        time.sleep(0.2)
        assert not box.is_read(answer)
        rpc(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 89, "reason": "fixture interrupted"},
            }
        )
        waiting.join(5)
        assert not waiting.is_alive() and "error" in result[0]
        assert not box.is_read(answer) and not box.state("waited")


def test_native_second_wait_cannot_release_the_first_sessions_reservation(tmp_path):
    with native_daemon(tmp_path) as (box, rpc, _, _):
        ask = box.post("claude", "codex", "fixture", "parity-exclusive")
        first = []
        waiting = threading.Thread(target=lambda: first.append(rpc(wait_call([ask.id], request_id=100, timeout=10))))
        waiting.start()
        deadline = time.monotonic() + 5
        while ask.id not in (box.state("waited") or {}):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        rejected = payload(rpc(wait_call([ask.id], request_id=101, timeout=1)))
        assert rejected["status"] == "error" and "reserved" in rejected["error"]
        assert ask.id in (box.state("waited") or {}) and waiting.is_alive()
        rpc({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 100}})
        waiting.join(5)
        assert not waiting.is_alive() and not box.state("waited")


def test_native_restart_returns_held_results_and_preserves_unanswered_reservation(tmp_path):
    with native_daemon(tmp_path) as (box, rpc, relay, proc):
        first = box.post("claude", "codex", "fixture", "parity-a")
        second = box.post("claude", "antigravity", "fixture", "parity-b")
        answer = box.post("codex", "claude", "fixture answer", "parity-a", reply_to=first.id)
        result = []
        waiting = threading.Thread(target=lambda: result.append(rpc(wait_call([first.id, second.id], timeout=10))))
        waiting.start()
        deadline = time.monotonic() + 5
        while len(box.state("waited") or {}) != 2:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        (relay / "daemon-restart.request").write_text("force fixture", encoding="utf-8")
        waiting.join(6)
        assert not waiting.is_alive()
        group = payload(result[0])
        assert group["status"] == "partial"
        assert [item["status"] for item in group["replies"]] == ["success", "restarting"]
        assert box.is_read(answer) and second.id in (box.state("waited") or {})
        assert proc.wait(timeout=15) == 0


def test_production_worker_refreshes_card_watch_state_and_status_exposes_stored_map(tmp_path):
    # Empty mailbox, isolated app data and closed fixture ports: production dispatch runs, no app is operated.
    with native_daemon(tmp_path, fixture=False) as (box, rpc, _, _):
        deadline = time.monotonic() + 10
        while box.state("native-cards-check") is None:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        folder = box.root / "cartes"
        folder.mkdir(exist_ok=True)
        (folder / "codex-fixture.json").write_text(
            json.dumps(
                {
                    "app": "codex",
                    "version": "fixture",
                    "surveyed_at": "2026-10-29T22:00:00Z",
                    "elements": [{"role": "button"}],
                    "landmarks": {"NEW_CHAT": {"path": "vue de départ"}, "ARCHIVE": None},
                }
            ),
            encoding="utf-8",
        )
        response = rpc(
            {"jsonrpc": "2.0", "id": 200, "method": "tools/call", "params": {"name": "status", "arguments": {}}}
        )
        data = payload(response)
        assert data["agents"]["codex"]["carte"]["version"] == "fixture"
        assert data["agents"]["codex"]["carte"]["missing"] == ["ARCHIVE"]


def test_targeted_installer_restart_preserves_another_instance_on_another_port(tmp_path):
    from test_native_csharp import ROOT

    with native_daemon(tmp_path) as (_, _, relay, first):
        with native_daemon(tmp_path) as (_, _, _, second):
            # Both executables and mailboxes deliberately match; only the port/PID identity distinguishes them.
            assert isinstance(first.args, list) and isinstance(second.args, list)
            port_first = int(str(first.args[2]))
            port_second = int(str(second.args[2]))
            probe = [
                "pwsh",
                "-NoProfile",
                "-File",
                str(ROOT / "tests/native_restart/probe.ps1"),
                "-Mailbox",
                str(relay),
                "-Executable",
                str(_native_exe()),
            ]
            wrong = json.loads(
                subprocess.check_output([*probe, "-Port", str(port_first), "-Request"], creationflags=NO_WINDOW)
            )
            assert wrong["ok"] is False and "port" in wrong["reason"]
            assert first.poll() is None and second.poll() is None
            correct = json.loads(
                subprocess.check_output(
                    [*probe, "-Port", str(port_second), "-Request", "-ConnectionOnly"], creationflags=NO_WINDOW
                )
            )
            assert correct["ok"] is True and correct["pid"] == second.pid, correct
            assert second.wait(timeout=10) == 0
            time.sleep(2.1)  # The other watcher gets a full polling cycle and must remain alive.
            assert first.poll() is None
