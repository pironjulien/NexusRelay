"""A completed/no-op edit must not block the next agent for an hour; live or dirty work stays protected."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nexus_relay import edit_guard, reservations
from nexus_relay.box import Box


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout.decode()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    folder = tmp_path / "repo"
    folder.mkdir()
    git(folder, "init", "-q")
    git(folder, "config", "user.email", "test@example.org")
    git(folder, "config", "user.name", "Test")
    for name in ("a.py", "b.py"):
        (folder / name).write_text("original\n", encoding="utf-8")
    git(folder, "add", ".")
    git(folder, "-c", "core.hooksPath=", "commit", "-qm", "initial")
    return folder


def begin(path: Path, *, agent: str = "claude", session: str = "s1", tool: str | None = "call1") -> dict:
    assert edit_guard.decide(agent, [str(path)], conversation=session, tool=tool) is None
    return next(r for r in reservations.active(Box()) if r.get("automatic") and r.get("session") == session)


def test_tentative_sans_ecriture_libere_des_la_fin_outil(repo: Path):
    target = repo / "a.py"
    held = begin(target)
    assert edit_guard.decide("codex", [str(target)], conversation="other")
    assert reservations.finish_edits("claude", "s1", tool="call1") == [held["id"]]
    assert edit_guard.decide("codex", [str(target)], conversation="other") is None


@pytest.mark.parametrize("dirty", ["worktree", "index", "untracked", "ignored", "deleted-before", "assumed"])
def test_fin_outil_et_session_gardent_tout_changement_reel(repo: Path, dirty: str):
    target = repo / "a.py"
    if dirty == "untracked":
        target = repo / "new.py"
    elif dirty == "ignored":
        target = repo / "ignored.py"
        (repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    elif dirty == "deleted-before":
        target.unlink()
    elif dirty == "assumed":
        git(repo, "update-index", "--assume-unchanged", "a.py")
    held = begin(target)
    if dirty != "deleted-before":
        target.write_text("real change, even if the tool then reports failure\n", encoding="utf-8")
    if dirty == "index":
        git(repo, "add", "a.py")
        target.write_text("original\n", encoding="utf-8")  # staged change + clean-looking working file
    assert reservations.finish_edits("claude", "s1", tool="call1") == []
    assert reservations.finish_edits("claude", "s1", session_ended=True) == []
    assert [r["id"] for r in reservations.active(Box())] == [held["id"]]


def test_creation_jamais_ecrite_liberee_mais_pas_un_fichier_hors_git(tmp_path: Path, repo: Path):
    missing = repo / "missing" / "never.py"
    held = begin(missing)
    assert reservations.finish_edits("claude", "s1", tool="call1") == [held["id"]]
    outside = tmp_path / "outside.py"
    outside.write_text("local work\n", encoding="utf-8")
    held = begin(outside, tool="call2")
    assert reservations.finish_edits("claude", "s1", tool="call2") == []
    assert reservations.active(Box())[0]["id"] == held["id"]


def test_fin_session_ne_devine_pas_la_fin_d_un_outil_sans_id(repo: Path):
    held = begin(repo / "a.py", tool=None)
    assert reservations.finish_edits("claude", "s1", tool="guessed") == []
    assert reservations.finish_edits("claude", None, session_ended=True) == []
    assert reservations.finish_edits("claude", "s2", session_ended=True) == []
    assert reservations.finish_edits("codex", "s1", session_ended=True) == []
    assert reservations.finish_edits("claude", "s1", session_ended=True) == []
    assert reservations.active(Box())[0]["id"] == held["id"]


def test_fin_outil_ne_libere_pas_un_autre_appel_en_vol(repo: Path):
    target = repo / "a.py"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(
            pool.map(lambda tool: edit_guard.decide("claude", [str(target)], conversation="s1", tool=tool), ["A", "B"])
        ) == [None, None]
    held = reservations.active(Box())
    assert len(held) == 1 and held[0]["pending_tools"] == ["A", "B"]
    assert reservations.finish_edits("claude", "s1", tool="A") == []
    assert reservations.finish_edits("claude", "s1", session_ended=True) == []  # root Stop while child B is in flight
    assert reservations.active(Box())[0]["pending_tools"] == ["B"]
    assert edit_guard.decide("codex", [str(target)], conversation="other")
    assert reservations.finish_edits("claude", "s1", tool="B") == [held[0]["id"]]
    new = begin(target, tool="C")
    assert reservations.finish_edits("claude", "s1", tool="A") == []  # delayed duplicate completion
    assert reservations.active(Box())[0]["id"] == new["id"]


@pytest.mark.parametrize("reason", ["manual refactor", reservations.AUTO_REASON])
def test_finalisation_ne_convertit_ni_ne_libere_une_reservation_manuelle(repo: Path, reason: str):
    path = str(repo / "a.py")
    manual = reservations.reserve(Box(), "claude", [path], reason, session="s1")
    auto = begin(Path(path))
    assert auto["id"] != manual["id"]
    assert reservations.finish_edits("claude", "s1", tool="call1") == [auto["id"]]
    assert reservations.active(Box()) == [manual]


def test_ancienne_reservation_sans_preuve_de_cycle_reste_reservee(repo: Path):
    legacy = reservations.reserve(Box(), "claude", [str(repo / "a.py")], reservations.AUTO_REASON, session="s1")
    assert reservations.finish_edits("claude", "s1", session_ended=True) == []
    assert reservations.active(Box()) == [legacy]


def test_fin_session_recontrole_un_outil_deja_termine_apres_nettoyage(repo: Path):
    target = repo / "a.py"
    held = begin(target)
    target.write_text("modified\n", encoding="utf-8")
    assert reservations.finish_edits("claude", "s1", tool="call1") == []
    target.write_text("original\n", encoding="utf-8")
    assert reservations.finish_edits("claude", "s1", session_ended=True) == [held["id"]]


def test_preuve_contenu_ne_se_fie_pas_au_cache_taille_et_mtime_de_git(repo: Path):
    target = repo / "a"
    target.write_text("original\n", encoding="utf-8")
    git(repo, "add", "a")
    git(repo, "-c", "core.hooksPath=", "commit", "-qm", "stat-cache baseline")
    time.sleep(1.1)  # let Git's index timestamp move past its racy-stat window
    git(repo, "update-index", "--refresh")
    stat = target.stat()
    held = begin(target)
    target.write_text("modified\n", encoding="utf-8")  # same size and platform newline as the original
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert git(repo, "status", "--porcelain=v1", "--", "a") == ""  # the old proof was fooled on Windows
    assert reservations.finish_edits("claude", "s1", tool="call1") == []
    assert reservations.active(Box())[0]["id"] == held["id"]


def test_preuve_contenu_respecte_les_conversions_crlf_de_git(repo: Path):
    (repo / ".gitattributes").write_text("a.py text eol=crlf\n", encoding="utf-8")
    target = repo / "a.py"
    target.write_bytes(b"original\r\n")
    assert reservations._unchanged(str(target), False)


def test_post_commit_garde_un_reliquat_non_commite_et_un_appel_en_vol(repo: Path):
    target = repo / "a.py"
    held = begin(target)
    target.write_text("staged change\n", encoding="utf-8")
    git(repo, "add", "a.py")
    target.write_text("unstaged remainder\n", encoding="utf-8")
    assert reservations.finish_edits("claude", "s1", tool="call1") == []
    git(repo, "-c", "core.hooksPath=", "commit", "-qm", "partial")
    assert reservations.release_committed("claude", [str(target)], session="s1") == []
    assert reservations.active(Box())[0]["id"] == held["id"]
    git(repo, "add", "a.py")
    git(repo, "-c", "core.hooksPath=", "commit", "-qm", "remainder")
    assert edit_guard.decide("claude", [str(target)], conversation="s1", tool="child-in-flight") is None
    assert reservations.release_committed("claude", [str(target)], session="s1") == []
    assert reservations.finish_edits("claude", "s1", tool="child-in-flight") == [held["id"]]


@pytest.mark.parametrize("agent", ["claude", "codex", "antigravity"])
def test_entree_reelle_du_hook_et_formats_des_trois_hotes(repo: Path, agent: str):
    target = str(repo / "a.py")
    if agent == "antigravity":
        event = {"conversationId": "s1", "stepIdx": 0, "toolCall": {"name": "edit_file", "args": {"Path": target}}}
    else:
        event = {"session_id": "s1", "tool_use_id": "call1", "tool_name": "Edit", "tool_input": {"file_path": target}}
        if agent == "codex":
            event.update(tool_name="apply_patch", tool_input={"command": f"*** Update File: {target}"})

    def hook(*flags: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", "-B", "-m", "nexus_relay.edit_guard", agent, *flags],
            input=json.dumps(event).encode(),
            capture_output=True,
            check=True,
        )

    assert hook().stdout == b""
    assert len(reservations.active(Box())) == 1
    event["error"] = "failed before writing"
    result = hook("--finish-tool")
    assert result.stdout == b"{}" and result.stderr == b""
    assert reservations.active(Box()) == []
    assert hook().stdout == b""
    assert hook("--finish-session").stdout == b"{}"
    assert len(reservations.active(Box())) == 1  # Stop alone cannot clear an in-flight child call
    assert hook("--finish-tool").stdout == b"{}"
    assert reservations.active(Box()) == []
