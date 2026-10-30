"""Legacy Relay ownership requires a delivery event AND the exact user wake in that exact root chat."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

from test_native_csharp import NO_WINDOW, _native_exe

from nexus_relay.box import Box


def test_native_history_registration_never_adopts_a_human_child_or_current_chat(tmp_path, monkeypatch):
    documents = tmp_path / "Documents"
    monkeypatch.setenv("NEXUS_RELAY_CODEX_WORKSPACE", str(documents))
    monkeypatch.setenv("NEXUS_RELAY_CODEX_PROJECT", "Documents")
    monkeypatch.setenv("NEXUS_RELAY_CODEX_PROJECT_ID", "official-fixture-id")
    monkeypatch.setenv("CODEX_SESSION_ID", "parent")
    root = Path(os.environ["CODEX_HOME"])
    root.mkdir()
    box = Box()
    with sqlite3.connect(root / "state_5.sqlite") as db:
        db.executescript(
            "CREATE TABLE threads(id TEXT,rollout_path TEXT,cwd TEXT,archived INTEGER); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);"
        )
        for chat in ("relay", "human", "quoted", "child", "parent"):
            ask = box.post("claude", "codex", "fixture", "ownership")
            text = f'Nexus Relay : message d\'un pair pour toi, read(id="{ask.id}").'
            if chat == "quoted":
                text = "A user quote: " + text
            log = tmp_path / f"{chat}.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": text}],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            db.execute("INSERT INTO threads VALUES(?,?,?,1)", (chat, str(log), str(documents)))
            if chat != "human":
                box.event(ask.id, "réveil", status="started", conversation=chat)
        db.execute("INSERT INTO thread_spawn_edges VALUES('relay','child')")
    command = [str(_native_exe()), "--register-known-relay-chats"]
    result = json.loads(subprocess.check_output(command, creationflags=NO_WINDOW))
    assert result["registered"] == ["relay"]
    registry = box.state("codex-owned")
    assert registry and set(registry["threads"]) == {"relay"}
    assert registry["threads"]["relay"]["origin"] == "verified-relay-history"
    assert json.loads(subprocess.check_output(command, creationflags=NO_WINDOW))["count"] == 0
