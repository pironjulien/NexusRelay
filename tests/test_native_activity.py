"""Old timestamps never convert unfinished work or unreadable journals into idle apps."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from test_native_csharp import NO_WINDOW, _native_exe


def activity():
    return json.loads(subprocess.check_output([str(_native_exe()), "--activity-status"], creationflags=NO_WINDOW))


def test_codex_long_running_turn_remains_busy_until_durable_completion(tmp_path):
    root = Path(os.environ["CODEX_HOME"])
    root.mkdir()
    log = tmp_path / "rollout.jsonl"
    log.write_text(
        json.dumps({"timestamp": "2000-01-01T00:00:00Z", "payload": {"type": "task_started"}}) + "\n", encoding="utf-8"
    )
    os.utime(log, (time.time() - 7200, time.time() - 7200))
    with sqlite3.connect(root / "state_5.sqlite") as db:
        db.executescript(
            "CREATE TABLE threads(id TEXT,rollout_path TEXT,cwd TEXT,archived INTEGER); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);"
        )
        db.execute("INSERT INTO threads VALUES('long',?,'workspace',0)", (str(log),))
    assert activity()["codex"]["status"] == "working"
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"timestamp": "2000-01-01T02:00:00Z", "payload": {"type": "task_complete"}}) + "\n")
    assert activity()["codex"]["status"] == "idle"
    log.unlink()
    assert activity()["codex"]["status"] == "unknown"


def test_antigravity_old_incomplete_transcript_remains_busy_and_missing_is_unknown(tmp_path):
    root = Path(os.environ["NEXUS_RELAY_GEMINI_HOME"]) / "antigravity"
    conversations = root / "conversations"
    conversations.mkdir(parents=True)
    conversation = "12345678-1234-1234-1234-123456789abc"
    with sqlite3.connect(conversations / f"{conversation}.db") as db:
        db.execute("CREATE TABLE steps(idx INTEGER,step_type INTEGER,step_payload BLOB)")
        db.execute("INSERT INTO steps VALUES(1,1,?)", (b"active",))
    transcript = root / "brain" / conversation / ".system_generated/logs/transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({"created_at": "2000-01-01T00:00:00Z", "type": "GENERIC", "status": "RUNNING"}) + "\n",
        encoding="utf-8",
    )
    os.utime(transcript, (time.time() - 7200, time.time() - 7200))
    assert activity()["antigravity"]["status"] == "working"
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps({"created_at": "2000-01-01T02:00:00Z", "type": "PLANNER_RESPONSE", "status": "DONE"}) + "\n"
        )
    assert activity()["antigravity"]["status"] == "idle"
    transcript.unlink()
    assert activity()["antigravity"]["status"] == "unknown"
