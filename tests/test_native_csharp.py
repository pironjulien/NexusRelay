"""Tests boîte noire du moteur natif C# (.NET 10) de NexusRelay, sans interaction avec les IA installées."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from nexus_relay import __version__

ROOT = Path(__file__).resolve().parents[1]
NATIVE_DIR = ROOT / "native"
CSPROJ = NATIVE_DIR / "NexusRelay.csproj"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

EXPECTED_TOOLS = {
    "send",
    "close",
    "inbox",
    "read",
    "reply",
    "progress",
    "cancel",
    "wait_reply",
    "track",
    "reserve",
    "release",
    "reservations",
    "file_reservation_paths",
    "release_file_reservations",
    "search",
    "summary",
    "status",
    "selftest",
    "where",
    "survey",
}


def _native_exe() -> Path:
    if override := os.environ.get("NEXUS_RELAY_TEST_EXE"):
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    digest = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(NATIVE_DIR.glob("*")) if p.is_file())).hexdigest()[
        :16
    ]
    exe = Path(local_app_data) / "NexusRelay" / "test-native" / digest / "NexusRelay.exe"
    if not exe.is_file():
        build_root = Path(local_app_data) / "NexusRelay" / "test-native-build" / digest
        subprocess.run(
            [
                "dotnet",
                "publish",
                str(CSPROJ),
                "-c",
                "Release",
                "-o",
                str(exe.parent),
                f"-p:BaseIntermediateOutputPath={build_root}\\obj\\",
                f"-p:BaseOutputPath={build_root}\\bin\\",
                "--nologo",
            ],
            check=True,
            creationflags=NO_WINDOW,
        )
    return exe


def _stdio_rpc(exe: Path, env: dict[str, str], agent: str, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n"
    proc = subprocess.run(
        [str(exe), "--mcp", "--agent", agent, "--protocol-fixture"],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
        check=True,
        creationflags=NO_WINDOW,
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _tool_call_data(resp: dict[str, Any]) -> dict[str, Any]:
    result = resp["result"]
    text = result["content"][0]["text"]
    return json.loads(text)


def test_aucun_binaire_ni_cache_dans_onedrive_et_selftest_natif():
    exe = _native_exe()
    assert exe.is_file(), f"Exécutable natif introuvable : {exe}"
    assert not (NATIVE_DIR / "bin").exists(), "Dossier bin interdit dans OneDrive"
    assert not (NATIVE_DIR / "obj").exists(), "Dossier obj interdit dans OneDrive"

    proc = subprocess.run(
        [str(exe), "--selftest"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
        creationflags=NO_WINDOW,
    )
    report = json.loads(proc.stdout.strip())
    assert report["ok"] is True
    assert report["version"] == __version__
    for key in ("workflow", "concurrency", "cancel", "crash_recovery", "mcp_server"):
        assert report["tests"][key] == "ok", key


def test_stdio_discover_tools_et_cycle_complet_salon(tmp_path: Path):
    exe = _native_exe()
    relay_home = tmp_path / "relay-stdio"
    env = {
        **os.environ,
        "NEXUS_RELAY_HOME": str(relay_home),
        "NEXUS_RELAY_HOST": "claude",
        "NEXUS_RELAY_LANG": "fr",
    }

    # 1. server/discover et tools/list
    resps = _stdio_rpc(
        exe,
        env,
        "claude",
        [
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ],
    )
    discover = resps[0]["result"]
    assert discover["protocolVersion"] == "2026-07-28"
    assert discover["serverInfo"]["name"] == "nexus-relay"
    assert discover["serverInfo"]["version"] == __version__
    assert {t["name"] for t in discover["tools"]} == EXPECTED_TOOLS

    listed = resps[1]["result"]["tools"]
    assert len(listed) == 20
    assert {t["name"] for t in listed} == EXPECTED_TOOLS

    # 2. Envoi par l'hôte (claude -> codex) et refus d'un non-hôte
    send_resps = _stdio_rpc(
        exe,
        env,
        "claude",
        [
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "send",
                    "arguments": {
                        "to": "codex",
                        "text": "Vérifie la migration C# avec caractères accentués éèê",
                        "topic": "nexusrelay",
                    },
                },
            }
        ],
    )
    sent = _tool_call_data(send_resps[0])
    assert sent["status"] == "success"
    msg_id = sent["id"]

    non_host = _stdio_rpc(
        exe,
        env,
        "codex",
        [
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "send",
                    "arguments": {"to": "claude", "text": "Interdit", "topic": "nexusrelay"},
                },
            }
        ],
    )
    assert _tool_call_data(non_host[0])["status"] == "error"

    # 3. Inbox, progress, read et reply par codex
    codex_resps = _stdio_rpc(
        exe,
        env,
        "codex",
        [
            {"jsonrpc": "2.0", "id": 20, "method": "tools/call", "params": {"name": "inbox", "arguments": {}}},
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {"name": "progress", "arguments": {"id": msg_id, "note": "Vérification en cours"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 22,
                "method": "tools/call",
                "params": {"name": "read", "arguments": {"id": msg_id}},
            },
            {
                "jsonrpc": "2.0",
                "id": 23,
                "method": "tools/call",
                "params": {
                    "name": "reply",
                    "arguments": {
                        "id": msg_id,
                        "text": "Parité confirmée dans Program.cs:10 (gravité basse).",
                        "vote": "agree",
                        "state": "done",
                    },
                },
            },
        ],
    )
    inbox_data = _tool_call_data(codex_resps[0])
    assert len(inbox_data["unread"]) == 1
    assert "⟦relay:" in inbox_data["unread"][0]["text"]
    assert _tool_call_data(codex_resps[1])["state"] == "en cours"
    assert _tool_call_data(codex_resps[2])["message"]["id"] == msg_id
    assert _tool_call_data(codex_resps[3])["status"] == "success"

    # 4. Track, search, summary et close par claude
    claude_follow = _stdio_rpc(
        exe,
        env,
        "claude",
        [
            {
                "jsonrpc": "2.0",
                "id": 30,
                "method": "tools/call",
                "params": {"name": "track", "arguments": {"id": msg_id}},
            },
            {
                "jsonrpc": "2.0",
                "id": 31,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "accentues eee", "topic": "nexusrelay"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 32,
                "method": "tools/call",
                "params": {"name": "summary", "arguments": {"topic": "nexusrelay"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 33,
                "method": "tools/call",
                "params": {"name": "close", "arguments": {"topic": "nexusrelay"}},
            },
        ],
    )
    assert _tool_call_data(claude_follow[0])["state"] == "répondu"
    assert _tool_call_data(claude_follow[1])["total"] == 1
    summary_data = _tool_call_data(claude_follow[2])
    assert summary_data["answered_total"] == 1
    assert summary_data["open_total"] == 0
    assert _tool_call_data(claude_follow[3])["closed"] is True
    assert (relay_home / "v1" / "salon" / "nexusrelay.md").is_file()


def test_annulation_reservations_et_edit_guard_natif(tmp_path: Path):
    exe = _native_exe()
    relay_home = tmp_path / "relay-guard"
    env = {
        **os.environ,
        "NEXUS_RELAY_HOME": str(relay_home),
        "NEXUS_RELAY_HOST": "claude",
        "NEXUS_RELAY_LANG": "fr",
    }

    # Annulation : non lu -> annulé ; lu -> refusé
    resps = _stdio_rpc(
        exe,
        env,
        "claude",
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "send", "arguments": {"to": "codex", "text": "A annuler", "topic": "canceltest"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "send", "arguments": {"to": "codex", "text": "Deja lu", "topic": "canceltest"}},
            },
        ],
    )
    id_unread = _tool_call_data(resps[0])["id"]
    id_read = _tool_call_data(resps[1])["id"]

    # Codex lit le second message
    _stdio_rpc(
        exe,
        env,
        "codex",
        [{"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read", "arguments": {"id": id_read}}}],
    )

    cancel_resps = _stdio_rpc(
        exe,
        env,
        "claude",
        [
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "cancel", "arguments": {"id": id_unread}},
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "cancel", "arguments": {"id": id_read}},
            },
        ],
    )
    assert _tool_call_data(cancel_resps[0])["state"] == "annulé"
    assert _tool_call_data(cancel_resps[1])["status"] == "error"

    # Garde d'édition (--edit-guard) : fichier réservé par claude -> refusé à codex et antigravity
    protected = str(tmp_path / "protected.py")
    _stdio_rpc(
        exe,
        env,
        "claude",
        [
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "reserve",
                    "arguments": {"paths": [protected], "reason": "refactoring critique"},
                },
            }
        ],
    )

    codex_hook = subprocess.run(
        [str(exe), "--edit-guard", "codex"],
        input=json.dumps(
            {
                "session_id": "sess-codex-1",
                "tool_name": "apply_patch",
                "tool_input": {"command": f"*** Update File: {protected}\n@@\n"},
            }
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=15,
        check=True,
        creationflags=NO_WINDOW,
    )
    codex_decision = json.loads(codex_hook.stdout)
    assert codex_decision["hookSpecificOutput"]["permissionDecision"] == "deny"

    agy_hook = subprocess.run(
        [str(exe), "--edit-guard", "antigravity"],
        input=json.dumps(
            {
                "conversationId": "conv-agy-1",
                "stepIdx": 3,
                "toolCall": {"args": {"Path": protected}},
            }
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=15,
        check=True,
        creationflags=NO_WINDOW,
    )
    agy_decision = json.loads(agy_hook.stdout)
    assert agy_decision["decision"] == "deny"


def test_serveur_http_loopback_natif(tmp_path: Path):
    exe = _native_exe()
    relay_home = tmp_path / "relay-http"
    token = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"  # gitleaks:allow (fake test token)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    env = {
        **os.environ,
        "NEXUS_RELAY_HOME": str(relay_home),
        "NEXUS_RELAY_HOST": "claude",
        "NEXUS_RELAY_LANG": "fr",
        "NEXUS_MCP_TOKEN": token,
    }

    proc = subprocess.Popen(
        [str(exe), "--http", str(port)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=NO_WINDOW,
    )
    try:
        base_url = f"http://127.0.0.1:{port}"
        ready = False
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"{base_url}/health", timeout=1.0) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.05)
        assert ready, "Le serveur HTTP natif n'a pas démarré sur /health"

        # Sans jeton -> 401
        req_unauth = urllib.request.Request(
            f"{base_url}/mcp?agent=claude",
            data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req_unauth, timeout=2.0)
            raise AssertionError("Attendait HTTP 401 sans jeton")
        except urllib.error.HTTPError as err:
            assert err.code == 401

        def http_rpc(agent: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode("utf-8")
            req = urllib.request.Request(
                f"{base_url}/mcp?agent={agent}",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5.0) as r:
                return json.loads(r.read().decode("utf-8"))

        disc = http_rpc("claude", "server/discover")["result"]
        assert disc["protocolVersion"] == "2026-07-28"
        assert len(disc["tools"]) == 20

        st = http_rpc("claude", "tools/call", {"name": "status", "arguments": {}})["result"]
        assert st["structuredContent"]["me"] == "claude"
        assert st["structuredContent"]["host"] == "claude"

        send_r = http_rpc(
            "claude",
            "tools/call",
            {"name": "send", "arguments": {"to": "antigravity", "text": "Ping HTTP", "topic": "httptest"}},
        )
        sent_id = _tool_call_data(send_r)["id"]

        inbox_r = http_rpc("antigravity", "tools/call", {"name": "inbox", "arguments": {}})
        assert len(_tool_call_data(inbox_r)["unread"]) == 1

        read_r = http_rpc("antigravity", "tools/call", {"name": "read", "arguments": {"id": sent_id}})
        assert _tool_call_data(read_r)["message"]["id"] == sent_id

        reply_r = http_rpc(
            "antigravity",
            "tools/call",
            {"name": "reply", "arguments": {"id": sent_id, "text": "Pong HTTP", "info": True}},
        )
        assert _tool_call_data(reply_r)["status"] == "success"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
