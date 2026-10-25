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
    assert again is not None
    again.close()
