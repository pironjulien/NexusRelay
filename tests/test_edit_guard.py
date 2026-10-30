"""Hook d'édition commun aux trois agents : refus d'un fichier réservé par un autre, réservation automatique sinon,
libération au commit."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import as_agent, call

from nexus_relay import edit_guard, reservations, server
from nexus_relay.box import Box

PATCH = """*** Begin Patch
*** Update File: src/a.py
@@
-x = 1
+x = 2
*** Add File: docs/nouveau.md
+bonjour
*** Update File: ancien.py
*** Move to: neuf.py
*** Delete File: vieux.py
*** End Patch"""


def commit_files(repo: Path, names: list[str], *, initial: bool = False) -> None:
    if initial:
        repo.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        for name in names:
            (repo / name).write_text("original\n", encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.hooksPath="]
    subprocess.run([*git, "add", *names], check=True)
    subprocess.run([*git, "commit", "-qm", "test"], check=True)


def test_cibles_des_trois_formats(tmp_path: Path):
    base = str(tmp_path)
    claude = {"tool_name": "Edit", "cwd": base, "tool_input": {"file_path": str(tmp_path / "a.py")}}
    assert edit_guard.targets("claude", claude) == [str(tmp_path / "a.py")]
    notebook = {"tool_name": "NotebookEdit", "cwd": base, "tool_input": {"notebook_path": "n.ipynb"}}
    assert edit_guard.targets("claude", notebook) == [str(tmp_path / "n.ipynb")]
    codex = {"tool_name": "apply_patch", "cwd": base, "tool_input": {"command": PATCH}}
    names = ["src/a.py", "docs/nouveau.md", "ancien.py", "neuf.py", "vieux.py"]
    assert edit_guard.targets("codex", codex) == [str(tmp_path / n) for n in names]
    agy = {"toolCall": {"name": "replace_file_content", "args": {"TargetFile": "b.py"}}, "workspacePaths": [base]}
    assert edit_guard.targets("antigravity", agy) == [str(tmp_path / "b.py")]
    args = {"Path": str(tmp_path / "c.py"), "Target": "a", "Replacement": "b"}
    real = {"toolCall": {"name": "edit_file", "args": args}}
    assert edit_guard.targets("antigravity", real) == [str(tmp_path / "c.py")]
    assert edit_guard.targets("claude", {"tool_name": "Bash", "tool_input": {"command": "ls"}}) == []


def test_refus_si_un_autre_tient_le_fichier_sinon_reserve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    held, free = str(tmp_path / "a.py"), str(tmp_path / "b.py")
    as_agent(monkeypatch, "codex")
    call(server.reserve([held], "refonte"))
    reason = edit_guard.decide("claude", [held])
    assert reason and "codex" in reason and "refonte" in reason
    assert edit_guard.decide("claude", [free]) is None
    mine = [r for r in reservations.active(Box()) if r["agent"] == "claude"]
    assert mine[0]["paths"] == [free] and mine[0]["reason"] == reservations.AUTO_REASON
    assert edit_guard.decide("codex", [free])  # désormais tenu par claude
    assert edit_guard.decide("codex", [held]) is None  # son propre fichier


def test_hook_rend_le_refus_au_format_de_l_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = str(tmp_path / "a.py")
    as_agent(monkeypatch, "codex")
    call(server.reserve([target], "refonte"))

    def hook(agent: str, event: dict[str, object]) -> str:
        return subprocess.run(
            [sys.executable, "-m", "nexus_relay.edit_guard", agent],
            input=json.dumps(event).encode(),
            capture_output=True,
            check=True,
        ).stdout.decode()

    claude = json.loads(hook("claude", {"tool_name": "Write", "tool_input": {"file_path": target}}))
    assert claude["hookSpecificOutput"]["permissionDecision"] == "deny"
    agy = json.loads(hook("antigravity", {"toolCall": {"name": "write_to_file", "args": {"TargetFile": target}}}))
    assert agy["decision"] == "deny" and "codex" in agy["reason"]
    assert hook("codex", {"tool_name": "apply_patch", "tool_input": {"command": f"*** Update File: {target}"}}) == ""
    assert hook("claude", {"tool_name": "Bash", "tool_input": {"command": "ls"}}) == ""
    assert hook("claude", {}) == "" and hook("claude", {"tool_name": "Edit"}) == ""


def test_evenement_illisible_ne_bloque_pas():
    done = subprocess.run(
        [sys.executable, "-m", "nexus_relay.edit_guard", "claude"],
        input=b"pas du json",
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0 and done.stdout == b""


def test_commit_libere_les_seules_reservations_automatiques_commitees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    commit_files(tmp_path, ["a.py", "b.py", "c.py"], initial=True)
    a, b, c = (str(tmp_path / n) for n in ("a.py", "b.py", "c.py"))
    assert edit_guard.decide("claude", [a], conversation="s1", tool="a") is None
    assert edit_guard.decide("claude", [b], conversation="s1", tool="b") is None
    for path in (a, b):
        Path(path).write_text("modified\n", encoding="utf-8")
    assert reservations.finish_edits("claude", "s1", tool="a") == []
    assert reservations.finish_edits("claude", "s1", tool="b") == []
    as_agent(monkeypatch, "claude")
    call(server.reserve([c], "à la main"))
    commit_files(tmp_path, ["a.py"])
    assert len(reservations.release_committed("claude", [a, c], session="s1")) == 1
    left = sorted(p for r in reservations.active(Box()) for p in r["paths"])
    assert left == [b, c]
    assert reservations.release_committed("codex", [b]) == []  # jamais celles d'un autre


def test_hook_post_commit_libere_apres_le_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "depot"
    commit_files(repo, ["a.py"], initial=True)
    assert edit_guard.decide("claude", [str(repo / "a.py")], conversation="s1", tool="a") is None
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert reservations.finish_edits("claude", "s1", tool="a") == []
    commit_files(repo, ["a.py"])
    monkeypatch.chdir(repo)
    monkeypatch.setenv("NEXUS_RELAY_SESSION", "s1")
    assert reservations.main(["--agent", "claude", "--release-committed"]) == 0
    assert reservations.active(Box()) == []


def test_deux_conversations_du_meme_agent_se_bloquent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Réservation automatique liée à la conversation : une autre conversation du même agent est refusée, la même
    prolonge ; une réservation faite à la main (sans conversation) vaut pour tout l'agent."""
    a, b = str(tmp_path / "a.py"), str(tmp_path / "b.py")
    assert edit_guard.decide("claude", [a], conversation="s1") is None
    reason = edit_guard.decide("claude", [a], conversation="s2")
    assert reason and "autre conversation" in reason and "commit" in reason
    assert edit_guard.decide("claude", [a], conversation="s1") is None
    assert len(reservations.active(Box())) == 1  # prolongée, pas doublée
    assert edit_guard.decide("claude", [a]) is None  # conversation inconnue : l'agent entier, comme avant
    as_agent(monkeypatch, "claude")
    call(server.reserve([b], "à la main"))
    assert edit_guard.decide("claude", [b], conversation="s2") is None
    call(server.release())  # « toutes les miennes » : jamais celles des conversations
    assert [r.get("session") for r in reservations.active(Box()) if r["paths"] == [a]] == ["s1"]
    hook = {"session_id": "s3", "tool_name": "Edit", "tool_input": {"file_path": a}}
    assert edit_guard.session(hook) == "s3"
    assert edit_guard.session({"conversationId": "c1"}) == "c1"


def test_commit_d_une_autre_conversation_refuse_et_ne_libere_pas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    commit_files(tmp_path, ["a.py"], initial=True)
    a = str(tmp_path / "a.py")
    assert edit_guard.decide("claude", [a], conversation="s1", tool="a") is None
    Path(a).write_text("modified\n", encoding="utf-8")
    assert reservations.finish_edits("claude", "s1", tool="a") == []
    commit_files(tmp_path, ["a.py"])
    assert reservations.blocking("claude", [a], session="s2")[0]["agent"] == "claude (autre conversation)"
    assert reservations.blocking("claude", [a], session="s1") == []
    assert reservations.release_committed("claude", [a], session="s2") == []
    assert reservations.release_committed("claude", [a]) == []  # unknown caller cannot release another session
    assert len(reservations.release_committed("claude", [a], session="s1")) == 1
    for name in ("NEXUS_RELAY_SESSION", "CODEX_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s9")
    assert reservations.session_from_env("claude") == "s9" and reservations.session_from_env("codex") is None
    monkeypatch.setenv("CODEX_THREAD_ID", "t1")
    assert reservations.session_from_env("codex") == "t1"


def test_reserve_sans_conversation_ne_touche_pas_celle_d_une_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Vu le 27/10 (ancien code du démon) : reserve() de Codex prolongeait et renommait la réservation automatique
    d'une autre conversation Codex. Désormais une réservation à part, la sienne restant intacte."""
    a = str(tmp_path / "a.py")
    assert edit_guard.decide("codex", [a], conversation="t1") is None
    as_agent(monkeypatch, "codex")
    call(server.reserve([a], "à la main"))
    held = sorted((r.get("session") or "", r["reason"]) for r in reservations.active(Box()))
    assert held == [("", "à la main"), ("t1", reservations.AUTO_REASON)]


def test_reservations_finies_depuis_un_jour_effacees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    box = Box()
    old = reservations.reserve(box, "claude", [str(tmp_path / "a.py")], "vieux", ttl_min=1)
    monkeypatch.setattr(reservations, "now", lambda: old["expires_at"] + reservations.KEEP_ENDED_S + 1)
    reservations.reserve(box, "claude", [str(tmp_path / "b.py")], "neuf")
    kept = [reservations.read_json(p) or {} for p in (box.root / "reservations").glob("*.json")]
    assert [r.get("reason") for r in kept] == ["neuf"]
