"""The stable dispatcher exercises the real native hook CLI with isolated product state."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_native_csharp import NO_WINDOW, _native_exe

from nexus_relay import reservations
from nexus_relay.box import Box

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("physical", [False, True])
def test_dispatcher_native_begin_finish_and_notice_utf8(tmp_path, physical):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, creationflags=NO_WINDOW)
    profile = tmp_path / "profile"
    state = profile / ".nexus" / "nexus-relay"
    state.mkdir(parents=True)
    executable = _native_exe().resolve() if physical else _native_exe()
    settings = {"runtime": "native"}
    if physical:
        settings["native_root"] = str(next(parent for parent in executable.parents if parent.name == "NexusRelay"))
    (state / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    specs = profile / ".nexus" / "daemons" / "specs"
    specs.mkdir(parents=True)
    (specs / "nexus-relay.json").write_text(json.dumps({"python": str(executable)}), encoding="utf-8")
    relay = tmp_path / "relay"
    env = {**os.environ, "USERPROFILE": str(profile), "NEXUS_RELAY_HOME": str(relay), "NEXUS_RELAY_LANG": "fr"}
    target = tmp_path / "épreuve.py"
    event = {
        "session_id": "root-a",
        "tool_use_id": "appel-1",
        "tool_name": "apply_patch",
        "tool_input": {"command": f"*** Add File: {target}"},
    }

    def hook(phase):
        return subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(ROOT / "scripts" / "invoke-hook.ps1"),
                "-Agent",
                "codex",
                "-Phase",
                phase,
            ],
            input=json.dumps(event, ensure_ascii=False),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=True,
            creationflags=NO_WINDOW,
        )

    assert hook("begin").stdout == ""
    box = Box(relay / "v1")
    held = reservations.active(box)
    assert len(held) == 1 and held[0]["session"] == "root-a"
    assert held[0]["paths"] == [str(target)]
    assert json.loads(hook("finish-tool").stdout) == {}
    assert reservations.active(box) == []
    incoming = box.post("codex", "claude", "Avis technique", "native-hooks", info=True)
    event = {"session_id": "root-a", "hook_event_name": "UserPromptSubmit"}
    notice = json.loads(hook("notice").stdout)
    assert notice["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "native-hooks" in notice["hookSpecificOutput"]["additionalContext"]
    assert not box.is_read(incoming)
    assert hook("notice").stdout == ""  # One notice per host conversation, with no destructive read.
