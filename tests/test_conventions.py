"""Règles du poste : aucune API interne d'Antigravity, aucun shell, aucune fenêtre, plus rien de l'ancien réveil."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted(ROOT.glob("src/**/*.py"))


def test_aucune_api_interne_ni_shell():
    for path in [*SOURCES, ROOT / "install.ps1"]:
        text = path.read_text(encoding="utf-8")
        assert "LanguageServerService" not in text and "x-codeium-csrf-token" not in text, path
        assert "ANTIGRAVITY_CSRF_TOKEN" not in text, path
        assert "shell=True" not in text, path


def test_sous_processus_sans_fenetre():
    """Chaque programme lancé l'est sans fenêtre de console (NO_WINDOW), `run` comme `Popen` (routeur de Codex, 20/10) ; seule
    exception : une appli graphique lancée réduite sans activation (startupinfo), qui n'a pas de console."""
    import ast

    spawners = sorted(p.name for p in SOURCES if "subprocess." in p.read_text(encoding="utf-8"))
    assert spawners == ["agentapi.py", "cdp.py", "daemon.py"], spawners
    for name in spawners:
        tree = ast.parse((ROOT / "src" / "nexus_relay" / name).read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr in ("run", "Popen", "call", "check_call", "check_output")
        ]
        assert calls, name
        for call in calls:
            kwargs = {k.arg: ast.unparse(k.value) for k in call.keywords if k.arg}
            flags = kwargs.get("creationflags", "")
            windowless = "NO_WINDOW" in flags
            gui_minimized = call.func.attr == "Popen" and "startupinfo" in kwargs  # type: ignore[union-attr]
            assert windowless or gui_minimized, f"{name}:{call.lineno} sans creationflags=NO_WINDOW"


def test_plus_de_tache_planifiee_codex_ni_eclair():
    names = {p.name for p in SOURCES}
    assert not names & {"mailbox.py", "sidebar.py", "watchdog.py", "codex_hook.py"}
    assert not (ROOT / "scripts" / "codex_wake.py").exists()
    for path in SOURCES:
        assert "Set-Clipboard" not in path.read_text(encoding="utf-8"), path


def test_entete_des_skills_lisible_en_yaml():
    # Antigravity rejette (toutes les 10 s) un en-tête dont une valeur nue contient « : » ou « # ».
    for path in ROOT.glob("**/SKILL.md"):
        if ".venv" in path.parts:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "---", path
        for line in lines[1 : lines.index("---", 1)]:
            key, _, value = line.partition(": ")
            if value and value[0] not in "\"'":
                assert ": " not in value and " #" not in value, f"{path} : mettre {key} entre guillemets"


def test_port_local_seulement():
    cdp = (ROOT / "src" / "nexus_relay" / "cdp.py").read_text(encoding="utf-8")
    assert "127.0.0.1" in cdp and "0.0.0.0" not in cdp


def test_version_unique():
    from nexus_relay import __version__

    assert f'version = "{__version__}"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_documentation_a_jour():
    for path in [ROOT / "README.md", *SOURCES]:
        text = path.read_text(encoding="utf-8")
        assert "à prouver" not in text and "en attendant la preuve" not in text, path
        assert "9225" not in text and "9226" not in text, path


def test_arret_sur_erreur_seulement_sur_une_etape_d_erreur(tmp_path, monkeypatch):
    """21/10 : Antigravity relisait paths.py ; sa sortie d'outil citait la phrase d'erreur et passait pour un arrêt."""
    import sqlite3

    from nexus_relay import paths

    conversation = "63de3bdf-e7ca-4c38-b02f-99aa650e246a"
    folder = tmp_path / "antigravity" / "conversations"
    folder.mkdir(parents=True)
    with sqlite3.connect(folder / f"{conversation}.db") as db:
        db.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, step_payload BLOB)")
        db.execute("INSERT INTO steps VALUES (1, 132, ?)", (b"... Agent execution terminated due to error. ...",))
    monkeypatch.setattr(paths, "gemini_home", lambda: tmp_path)
    assert paths.agy_error(conversation) is None
    with sqlite3.connect(folder / f"{conversation}.db") as db:
        db.execute(
            "INSERT INTO steps VALUES (2, 17, ?)",
            (b"*\nAgent execution terminated due to error.\x12\x0dmodel missing!",),
        )
    assert paths.agy_error(conversation) == ("model missing", None)  # longueur annoncée 13 : « ! » hors du champ
