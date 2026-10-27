"""Superviseur du seul démon nexus-relay (poste sans superviseur des démons Nexus)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nexus_relay import daemon


def _spec(tmp_path: Path) -> Path:
    path = tmp_path / "nexus-relay.json"
    spec = {"port": 16181, "python": "py.exe", "args": ["-B", "-m", "nexus_relay.server"], "env": {"NEXUS_X": "1"}}
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


class _Clock:
    def __init__(self, ups: list[float]) -> None:
        self.ups, self.now, self.calls = ups, 0.0, 0

    def __call__(self) -> float:
        self.calls += 1
        if self.calls % 2 == 0:  # fin de vie du démon : sa durée de vie s'ajoute
            self.now += self.ups.pop(0)
        return self.now


def test_relance_aussitot_apres_une_relance_par_demande_pause_croissante_apres_plantage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    codes = [0, 1, 1, 0]
    launched: list[tuple[list[str], dict[str, str]]] = []
    pauses: list[float] = []

    class Proc:
        def __init__(self, argv: list[str], env: dict[str, str], **kwargs: Any) -> None:
            launched.append((argv, env))
            assert kwargs["creationflags"] == daemon.NO_WINDOW

        def wait(self) -> int:
            return codes.pop(0)

    clock = _Clock([60.0, 1.0, 1.0, 60.0])
    monkeypatch.setattr(daemon.subprocess, "Popen", Proc)
    daemon.run(_spec(tmp_path), sleep=pauses.append, clock=clock, rounds=4)
    argv, env = launched[0]
    assert argv == ["py.exe", "-B", "-m", "nexus_relay.server", "--http", "16181"] and env["NEXUS_X"] == "1"
    assert pauses == [0.5, 2.0, 4.0, 0.5]  # relance voulue, plantage, plantage, relance voulue
    assert (Path.home() / ".nexus" / "nexus-relay" / "daemon.log").exists()


def test_demande_de_relance_anterieure_satisfaite_par_le_lancement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CI du 27/10 (1.8.0) : install.ps1 dépose la demande puis lance la tâche ; le démon neuf la suivait et se
    relançait aussitôt, port fermé pour l'étape suivante. Effacée au lancement, jamais pendant que le démon vit."""
    request = tmp_path / "relay" / "daemon-restart.request"  # NEXUS_RELAY_HOME des tests (conftest)
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text("force 2026-10-27", encoding="utf-8")
    seen: list[bool] = []

    class Proc:
        def __init__(self, argv: list[str], env: dict[str, str], **kwargs: Any) -> None:
            seen.append(request.exists())
            request.write_text("code 2026-10-27", encoding="utf-8")  # déposée pendant la vie du démon : gardée

        def wait(self) -> int:
            seen.append(request.exists())
            return 0

    monkeypatch.setattr(daemon.subprocess, "Popen", Proc)
    daemon.run(_spec(tmp_path), sleep=lambda s: None, clock=_Clock([60.0]), rounds=1)
    assert seen == [False, True]


def test_demande_posee_apres_la_prise_gardee(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex : une demande écrite entre la lecture de la spec et le lancement (install suivant) était
    effacée ; prise avant la lecture, seule la demande antérieure est satisfaite."""
    request = tmp_path / "relay" / "daemon-restart.request"
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text("force A", encoding="utf-8")
    real = daemon.command

    def install_b(spec: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        request.write_text("force B", encoding="utf-8")  # spec B et sa demande, après la prise
        return real(spec)

    monkeypatch.setattr(daemon, "command", install_b)
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: type("P", (), {"wait": lambda self: 0})())
    daemon.run(_spec(tmp_path), sleep=lambda s: None, clock=_Clock([60.0]), rounds=1)
    assert request.read_text(encoding="utf-8") == "force B"
    assert not request.with_name(request.name + ".lancement").exists()


def test_demande_rendue_si_rien_n_est_lance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    request = tmp_path / "relay" / "daemon-restart.request"
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text("force A", encoding="utf-8")
    daemon.run(tmp_path / "absente.json", sleep=lambda s: None, rounds=1)
    assert request.read_text(encoding="utf-8") == "force A"


def test_spec_illisible_attend_sans_rien_lancer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pauses: list[float] = []
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: pytest.fail("rien lancé"))
    daemon.run(tmp_path / "absente.json", sleep=pauses.append, rounds=2)
    assert pauses == [2.0, 4.0]


def test_une_seule_instance(tmp_path: Path):
    held = daemon.lock(tmp_path / "daemon.lock")
    assert held is not None
    try:
        assert daemon.lock(tmp_path / "daemon.lock") is None
    finally:
        held.close()
    again = daemon.lock(tmp_path / "daemon.lock")
    try:
        assert again is not None
    finally:
        if again is not None:
            again.close()


def test_spec_declare_la_demande_suivie_par_le_demon():
    """install.ps1 écrit dans la spec la demande que suit le démon (`restart_request`) : un superviseur général (NexusInfra)
    la prend avant chaque lancement sans connaître le relais. Même fichier que celui que lit le serveur."""
    from nexus_relay import server

    install = (Path(__file__).parents[1] / "install.ps1").read_text(encoding="utf-8-sig")
    assert f"$daemonRequest = Join-Path $mailbox '{server.restart_request().name}'" in install
    assert "restart_request = $daemonRequest }" in install
    assert install.index("$daemonRequest = ") < install.index("$spec = [ordered]@{")
    # spec autonome (relecture de Codex) : démon et sidecar épinglés sur la boîte où install.ps1 dépose les demandes
    assert "$mailbox = Join-Path $HOME '.nexus\\relay'" in install
    assert "$daemonEnv.NEXUS_RELAY_HOME = $mailbox" in install
    assert "PYTHONPYCACHEPREFIX = $pycache; NEXUS_RELAY_HOME = $mailbox }" in install


def test_demande_declaree_par_la_spec_prise_au_lancement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex : la boîte du démon est celle de sa spec (épinglée par install.ps1), pas celle de
    l'environnement de ce superviseur ; la demande déclarée est prise, celle de relay_home() n'est pas touchée."""
    declared = tmp_path / "epinglee" / "daemon-restart.request"
    declared.parent.mkdir()
    declared.write_text("force A", encoding="utf-8")
    other = tmp_path / "relay" / "daemon-restart.request"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("force autre", encoding="utf-8")
    spec = _spec(tmp_path)
    spec.write_text(
        json.dumps({**json.loads(spec.read_text(encoding="utf-8")), "restart_request": str(declared)}), encoding="utf-8"
    )
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: type("P", (), {"wait": lambda self: 0})())
    daemon.run(spec, sleep=lambda s: None, clock=_Clock([60.0]), rounds=1)
    assert not declared.exists()
    assert other.read_text(encoding="utf-8") == "force autre"
