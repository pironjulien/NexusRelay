"""Réglages du poste : fichier de ce PC, puis celui du dépôt ; variables développées ; environnement prioritaire."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nexus_relay import box, cdp, settings


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_premier_fichier_cle_par_cle_et_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NR_BASE", str(tmp_path))
    monkeypatch.delenv("NR_ABSENTE", raising=False)
    (tmp_path / "token.txt").write_text("x" * 40, encoding="utf-8")
    pc = _write(tmp_path / "pc.json", {"codex_router": "http://127.0.0.1:1/health"})
    depot = _write(
        tmp_path / "depot.json",
        {
            "codex_router": "http://ignore",
            "token_file": ["%NR_ABSENTE%\\token.txt", "%NR_BASE%\\absent.txt", "%NR_BASE%\\token.txt"],
            "vaults": ["%NR_BASE%\\coffre", "%NR_ABSENTE%\\coffre", "%NR_BASE%\\coffre"],
            "codex_router_start": ['"%NR_BASE%\\lb.exe" --background'],
        },
    )
    found = settings.load([pc, tmp_path / "manquant.json", depot])
    assert found["codex_router"] == "http://127.0.0.1:1/health"  # le fichier de ce PC l'emporte
    assert found["token_file"] == str(tmp_path / "token.txt")  # premier fichier qui existe
    assert found["vaults"] == [f"{tmp_path}\\coffre"]  # variable manquante ignorée, doublon retiré
    assert found["codex_router_start"] == [f'"{tmp_path}\\lb.exe" --background']


def test_apply_respecte_l_environnement_et_sert_au_coffre_et_au_routeur(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault = tmp_path / "coffre"
    vault.mkdir()
    (vault / "cle.txt").write_text("sk-factice", encoding="utf-8")
    depot = _write(
        tmp_path / "depot.json",
        {"vaults": [str(vault)], "codex_router": "http://127.0.0.1:2455/health/ready", "token_file": "absent.txt"},
    )
    environ = {"NEXUS_RELAY_CODEX_ROUTER": "http://deja"}
    applied = settings.apply(environ, [depot])
    assert applied == {"NEXUS_RELAY_VAULTS": str(vault)} and environ["NEXUS_RELAY_CODEX_ROUTER"] == "http://deja"
    assert "NEXUS_MCP_TOKEN_FILE" not in environ  # jeton absent : rien posé
    monkeypatch.setattr(os, "environ", {**os.environ, **environ})
    assert box._vaults() == [box._plain(os.path.realpath(vault))]
    assert cdp.router_url() == "http://deja"


def test_fichiers_lus(tmp_path: Path):
    pc, depot = settings.files()
    assert pc == Path.home() / ".nexus" / "nexus-relay" / "settings.json"
    assert depot.name == "settings.local.json" and (depot.parent / "pyproject.toml").exists()


def test_reglages_du_depot_prive_lisibles():
    """settings.local.json (postes de Julien, privé) : JSON valide, clés connues, aucune valeur en dur sans variable."""
    path = Path(__file__).resolve().parents[1] / "settings.local.json"
    if not path.exists():
        pytest.skip("copie publique : pas de réglages de poste")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) <= set(settings.KEYS)
    for key in ("token_file", "vaults", "codex_router_start"):
        for value in data.get(key, []):
            assert "%" in value and ":\\" not in value, value
