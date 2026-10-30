"""Hook and Git guard must agree on ownership across a Codex parent and its subagents."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_native_csharp import NO_WINDOW, _native_exe

from nexus_relay import edit_guard, reservations
from nexus_relay.box import Box


@pytest.mark.parametrize(
    ("parent", "child", "override", "expected"),
    [
        ("root-a", "child-1", None, "root-a"),
        ("root-a", "child-2", None, "root-a"),
        ("root-b", "child-3", None, "root-b"),
        (None, "legacy-thread", None, "legacy-thread"),
        ("  ", "legacy-thread", None, "legacy-thread"),
        ("root-a", "child-1", "explicit-session", "explicit-session"),
        (None, None, None, None),
    ],
)
def test_canonical_session_matches_codex_hook(monkeypatch, parent, child, override, expected):
    for name, value in (("CODEX_SESSION_ID", parent), ("CODEX_THREAD_ID", child), ("NEXUS_RELAY_SESSION", override)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert reservations.session_from_env("codex") == expected
    if parent and parent.strip() and override is None:
        assert edit_guard.session({"session_id": parent}) == reservations.session_from_env("codex")


def test_python_and_native_guards_allow_siblings_and_reject_other_chats(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True, creationflags=NO_WINDOW)
    target = repository / "a.py"
    target.write_text("change = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "a.py"], check=True, creationflags=NO_WINDOW)
    relay = tmp_path / "relay"
    box = Box(relay / "v1")
    held = reservations.reserve(box, "codex", [str(target)], "parent's work", session="root-a")
    commands = [
        [sys.executable, "-I", "-B", "-m", "nexus_relay.reservations", "--staged"],
        [str(_native_exe()), "--staged"],
    ]
    base = {
        **os.environ,
        "NEXUS_RELAY_HOME": str(relay),
        "NEXUS_RELAY_AGENT": "codex",
        "CODEX_SESSION_ID": "root-a",
    }
    base.pop("NEXUS_RELAY_SESSION", None)
    for command in commands:
        for child in ("child-1", "child-2"):
            same = subprocess.run(
                command,
                cwd=repository,
                env={**base, "CODEX_THREAD_ID": child},
                capture_output=True,
                timeout=15,
                creationflags=NO_WINDOW,
            )
            assert same.returncode == 0, same.stderr.decode(errors="replace")
        other = subprocess.run(
            command,
            cwd=repository,
            env={**base, "CODEX_SESSION_ID": "root-b", "CODEX_THREAD_ID": "child-3"},
            capture_output=True,
            timeout=15,
            creationflags=NO_WINDOW,
        )
        assert other.returncode == 1 and b"a.py" in other.stderr
        assert reservations.active(box)[0]["id"] == held["id"]  # Checking never releases or changes its owner.
        legacy = {key: value for key, value in base.items() if key != "CODEX_SESSION_ID"}
        old_client = subprocess.run(
            command,
            cwd=repository,
            env={**legacy, "CODEX_THREAD_ID": "root-a"},
            capture_output=True,
            timeout=15,
            creationflags=NO_WINDOW,
        )
        assert old_client.returncode == 0
