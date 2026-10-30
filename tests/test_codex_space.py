"""Shared Documents execution space never grants ownership of Julien's other chats."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from nexus_relay import codex_app, codex_space, wake
from nexus_relay.box import Box
from nexus_relay.wake import tidy_codex as real_tidy_codex


def test_shared_workspace_requires_verified_ids_and_excludes_children_and_parent(tmp_path, monkeypatch):
    documents = tmp_path / "Documents"
    documents.mkdir()
    monkeypatch.setenv("NEXUS_RELAY_CODEX_WORKSPACE", str(documents))
    monkeypatch.setenv("NEXUS_RELAY_CODEX_PROJECT", "Documents")
    monkeypatch.setenv("NEXUS_RELAY_CODEX_PROJECT_ID", "official-project-id")
    root = codex_app.home()
    root.mkdir()
    db = sqlite3.connect(root / "state_5.sqlite")
    db.executescript(
        "CREATE TABLE threads(id TEXT,rollout_path TEXT,cwd TEXT,archived INTEGER); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);"
    )
    for thread in ("relay", "julien", "child", "current-parent"):
        db.execute("INSERT INTO threads VALUES(?,?,?,0)", (thread, "", str(documents)))
    db.execute("INSERT INTO thread_spawn_edges VALUES('relay','child')")
    db.commit()
    db.close()
    box = Box()
    assert not codex_space.owns("julien", box)
    assert not codex_space.registered("relay", box)
    assert codex_space.remember("review", "relay", box)
    assert codex_space.owns("relay", box)
    assert not codex_space.owns("julien", box)  # Same cwd and same project confer no ownership.
    assert not codex_space.remember("review", "child", box)
    monkeypatch.setenv("CODEX_SESSION_ID", "current-parent")
    assert not codex_space.remember("review", "current-parent", box)
    db = sqlite3.connect(root / "state_5.sqlite")
    db.execute("UPDATE threads SET cwd=? WHERE id='relay'", (str(tmp_path / "other"),))
    db.commit()
    db.close()
    assert not codex_space.owns("relay", box)
    assert codex_space.configured()["project_id"] == "official-project-id"
    assert codex_space.labels() == ("Démarrer un nouveau chat dans Documents", "Start new chat in Documents")


def test_old_backlog_and_non_host_senders_stay_unread_without_a_wake(monkeypatch):
    box = Box()
    old = box.post("claude", "codex", "old", "review")
    peer = box.post("antigravity", "codex", "peer", "review")
    monkeypatch.setenv("NEXUS_RELAY_WAKE_SINCE", "2099-01-01T00:00:00Z")
    assert not wake.needs_wake(old) and not wake.needs_wake(peer)
    assert not box.is_read(old) and not box.is_read(peer)
    monkeypatch.delenv("NEXUS_RELAY_WAKE_SINCE")
    assert wake.needs_wake(old) and not wake.needs_wake(peer)


def test_unregistered_finished_documents_chat_never_reaches_archive(monkeypatch, tmp_path):
    chat = codex_app.Chat("julien", Path("not-used"), True, 1)
    monkeypatch.setattr(codex_app, "relay_chats", lambda **_: [chat])
    monkeypatch.setattr(
        codex_app,
        "archive_chats",
        lambda *_, **__: (_ for _ in ()).throw(AssertionError("unregistered chat reached UI")),
    )
    assert real_tidy_codex(Box(), now=1000) == {}
