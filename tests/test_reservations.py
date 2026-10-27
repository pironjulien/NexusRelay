"""Réservation de fichiers avant de les modifier (24/10, comparaison avec MCP Agent Mail et les équipes de Claude Code)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import as_agent, call

from nexus_relay import reservations, server


def test_reserver_puis_conflit_pour_un_autre(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "depot"
    held = call(server.reserve([str(repo / "src" / "a.py")], "correctif du réveil", ttl_min=30))
    assert held["paths"] == [str(repo / "src" / "a.py")] and held["expires"]
    as_agent(monkeypatch, "codex")
    refused = json.loads(server.reserve([str(repo / "src")], "refonte"))  # le dossier couvre le fichier réservé
    assert refused["status"] == "error" and "claude" in refused["error"] and "a.py" in refused["error"]
    assert call(server.reserve([str(repo / "tests" / "b.py")], "tests"))  # ailleurs : libre


def test_motifs_et_dossiers_se_recouvrent(tmp_path: Path):
    base = str(tmp_path)
    assert reservations.overlap(f"{base}\\src", f"{base}\\src\\x\\y.py")
    assert reservations.overlap(f"{base}\\src\\*.py", f"{base}\\src\\a.py")
    assert reservations.overlap(f"{base}\\SRC\\A.PY", f"{base}\\src\\a.py")  # casse de Windows
    assert not reservations.overlap(f"{base}\\src\\a.py", f"{base}\\src\\ab.py")
    assert not reservations.overlap(f"{base}\\src2", f"{base}\\src\\a.py")


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [  # relecture de Codex du 24/10 : intersection des ensembles, pas comparaison des chaînes
        ("C:\\repo\\a*.py", "C:\\repo\\*b.py", True),  # ab.py dans les deux
        ("C:\\repo\\a", "C:\\repo\\*\\b.py", True),  # C:\repo\a\b.py dans les deux
        ("C:\\repo\\a*.py", "C:\\repo\\b*.md", False),
        ("C:\\repo\\*.py", "C:\\repo\\sub\\x.py", False),  # * ne traverse pas un dossier
        ("C:\\repo\\**", "C:\\repo\\sub\\x.py", True),
        ("C:\\", "D:\\x.py", False),
        ("C:\\", "C:\\x\\y.py", True),
        ("\\\\?\\UNC\\serveur\\partage\\a.txt", "\\\\serveur\\partage\\a.txt", True),
        # tour 2 de Codex : alias Win32 (points et espaces finaux retirés à l'ouverture), marqueur UNC sans casse
        ("C:\\repo\\a.py", "C:\\repo\\a.py.", True),
        ("C:\\repo\\src\\a.py", "C:\\repo\\src. \\a.py", True),
        ("\\\\?\\unc\\serveur\\partage\\a.txt", "\\\\serveur\\partage\\a.txt", True),
    ],
)
def test_recouvrements(a: str, b: str, expected: bool):
    assert reservations.overlap(a, b) is expected and reservations.overlap(b, a) is expected


@pytest.mark.parametrize("final", [".", " "])
def test_vrai_nom_a_point_final_survit_au_stockage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, final: str):
    """Tours 3 et 4 de Codex : \\\\?\\C:\\repo\\a. ou « a » + espace (vrais noms) perdaient leur dernier caractère au
    stockage ; le motif \\\\?\\C:\\repo\\a? passait alors pour libre."""
    real = "\\\\?\\" + str(tmp_path) + "\\a" + final
    stored = reservations.clean(real)
    assert stored.endswith("\\a" + final)  # le caractère est gardé, pas seulement un résultat stable
    assert reservations.clean(stored) == stored and reservations.norm(stored) == reservations.norm(real)
    call(server.reserve([real], "vrai nom à point final"))
    as_agent(monkeypatch, "codex")
    assert json.loads(server.reserve(["\\\\?\\" + str(tmp_path / "a?")], "motif"))["status"] == "error"
    assert json.loads(server.reserve([str(tmp_path)], "dossier parent"))["status"] == "error"
    as_agent(monkeypatch, "claude")
    ordinary = call(server.reserve([str(tmp_path / "a")], "autre fichier : a, pas a."))
    assert len({r["id"] for r in call(server.reservations())["active"]}) == 2 and ordinary["id"]


def test_lecteur_entier_et_unc_reservables():
    assert reservations.clean("C:\\") == "C:\\" and reservations.clean(reservations.clean("C:\\")) == "C:\\"
    assert reservations.clean("\\\\?\\UNC\\serveur\\partage\\a.txt") == "\\\\serveur\\partage\\a.txt"
    assert call(server.reserve(["C:\\"], "tout le lecteur", ttl_min=1))


def test_meme_agent_prolonge_et_libere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = str(tmp_path / "a.py")
    first = call(server.reserve([path], "x", ttl_min=5))
    again = call(server.reserve([path], "x encore", ttl_min=60))  # soi-même : pas un conflit, prolongé
    assert again["id"] == first["id"] and again["expires"] > first["expires"]
    assert [r["id"] for r in call(server.reservations())["active"]] == [first["id"]]
    assert call(server.release())["released"] == [first["id"]]
    assert call(server.reservations())["active"] == []
    as_agent(monkeypatch, "codex")
    assert call(server.reserve([path], "à moi maintenant"))


def test_on_ne_libere_pas_la_reservation_d_un_autre(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    held = call(server.reserve([str(tmp_path / "a.py")], "x"))
    as_agent(monkeypatch, "antigravity")
    assert "pas à toi" in json.loads(server.release(held["id"]))["error"]


def test_expiree_ne_bloque_plus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = str(tmp_path / "a.py")
    call(server.reserve([path], "x", ttl_min=1))
    monkeypatch.setattr(reservations, "now", lambda: reservations.time.time() + 120)
    as_agent(monkeypatch, "codex")
    assert call(server.reserve([path], "après expiration"))


def test_partagee_ne_bloque_que_l_exclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = str(tmp_path / "a.py")
    call(server.reserve([path], "lecture", exclusive=False))
    as_agent(monkeypatch, "codex")
    assert call(server.reserve([path], "lecture aussi", exclusive=False))
    assert json.loads(server.reserve([path], "écriture"))["status"] == "error"


def test_chemin_relatif_refuse():
    assert "absolu" in json.loads(server.reserve(["src/a.py"], "x"))["error"]


def test_status_montre_les_reservations(tmp_path: Path):
    call(server.reserve([str(tmp_path / "a.py")], "correctif"))
    shown = call(asyncio.run(server.status()))["reservations"]
    assert shown[0]["agent"] == "claude" and shown[0]["reason"] == "correctif"


def test_deux_reserve_simultanes_un_seul_gagne(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Le démon sert plusieurs appels à la fois : contrôle et écriture d'un seul tenant."""
    import threading
    import time

    from nexus_relay.box import Box

    real = reservations._conflicts

    def slow(*args, **kwargs):
        found = real(*args, **kwargs)
        time.sleep(0.2)  # fenêtre de course élargie entre le contrôle et l'écriture
        return found

    monkeypatch.setattr(reservations, "_conflicts", slow)
    path, outcomes = str(tmp_path / "a.py"), []

    def take(agent: str) -> None:
        try:
            reservations.reserve(Box(), agent, [path], "course")
            outcomes.append(agent)
        except reservations.RelayError:
            outcomes.append("refus")

    threads = [threading.Thread(target=take, args=(a,)) for a in ("claude", "codex")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes).count("refus") == 1 and len(reservations.active(Box())) == 1


def test_garde_de_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Garde pour un hook pre-commit : refuse un fichier réservé par un AUTRE agent."""
    target = tmp_path / "a.py"
    as_agent(monkeypatch, "codex")
    call(server.reserve([str(target)], "refonte"))
    assert reservations.blocking("claude", [str(target)])[0]["agent"] == "codex"
    assert reservations.blocking("codex", [str(target)]) == []
    done = subprocess.run(
        [sys.executable, "-m", "nexus_relay.reservations", "--agent", "claude", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert done.returncode == 1 and "codex" in done.stderr


def test_agent_reconnu_par_ses_ancetres(monkeypatch: pytest.MonkeyPatch):
    """Sans --agent ni NEXUS_RELAY_AGENT : le plus proche ancêtre reconnu ; Julien à la main → « user »."""
    monkeypatch.delenv("NEXUS_RELAY_AGENT", raising=False)
    table = {
        10: (9, "python.exe"),
        9: (8, "sh.exe"),
        8: (7, "codex.exe"),
        7: (6, "claude.exe"),
        20: (21, "pwsh.exe"),
        21: (20, "explorer.exe"),  # cycle d'un pid réutilisé
    }
    assert reservations.whoami(table, 10) == "codex"
    assert reservations.whoami({10: (5, "python.exe"), 5: (1, "antigravity.exe")}, 10) == "antigravity"
    assert reservations.whoami(table, 20) == "user"
    monkeypatch.setenv("NEXUS_RELAY_AGENT", "claude")
    assert reservations.whoami(table, 10) == "claude"


def test_hook_refuse_le_commit_d_un_fichier_reserve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """--staged lit l'index du dépôt : fichier réservé par codex indexé par claude → refus ; libéré → accepté."""
    repo = tmp_path / "depot"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.py"], check=True)
    as_agent(monkeypatch, "codex")
    call(server.reserve([str(repo / "a.py")], "refonte"))

    def guard() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "nexus_relay.reservations", "--agent", "claude", "--staged"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    refused = guard()
    assert refused.returncode == 1 and "codex" in refused.stderr and "a.py" in refused.stderr
    call(server.release())
    assert guard().returncode == 0
