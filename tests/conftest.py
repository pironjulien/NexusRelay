"""Garde-fous communs : salon, dossiers et agents temporaires ; aucun réseau ; aucune vraie appli touchée."""

from __future__ import annotations

import json
import re
import socket
from pathlib import Path
from typing import Any

import pytest

_LOCAL = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    # réglages du poste ignorés : ni vrai coffre, ni vrai routeur de Codex
    for name in (
        "NEXUS_RELAY_VAULTS",
        "NEXUS_RELAY_CODEX_ROUTER",
        "NEXUS_RELAY_CODEX_ROUTER_START",
        "NEXUS_RELAY_HOST",
        "NEXUS_RELAY_CODEX_WORKSPACE",
        "NEXUS_RELAY_CODEX_PROJECT",
        "NEXUS_RELAY_CODEX_PROJECT_ID",
        "NEXUS_RELAY_WAKE_SINCE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_RELAY_HOME", str(tmp_path / "relay"))
    monkeypatch.setenv("NEXUS_RELAY_GEMINI_HOME", str(tmp_path / "gemini"))
    monkeypatch.setenv("NEXUS_JOURNAL_HOME", str(tmp_path / "journal"))  # jamais le vrai journal commun
    monkeypatch.setenv("NEXUS_RELAY_AGENT", "claude")
    monkeypatch.setenv("NEXUS_RELAY_LANG", "fr")  # textes des tests historiques ; l'anglais par défaut a ses tests
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))  # jamais la base ni les journaux du vrai Codex
    monkeypatch.setenv("NEXUS_RELAY_KEYPAD_QUOTA", str(tmp_path / "keypad-quota.json"))
    monkeypatch.setenv("NEXUS_RELAY_CODEX_PORT", "1")  # port fermé : jamais la vraie appli Codex
    monkeypatch.delenv("NEXUS_RELAY_AGENTAPI", raising=False)
    from nexus_relay import cdp, credits, foreground_lock, server, wake

    monkeypatch.setattr(wake, "ACTIVE", wake.Activity())  # une relance simulée ne ferme pas les tests suivants
    import threading

    monkeypatch.setattr(
        server, "RESTART_PENDING", threading.Event(), raising=False
    )  # idem pour les attentes (wait_reply)

    monkeypatch.setattr(cdp, "AGY_PORT", 1)  # port fermé : jamais le vrai Antigravity

    real_ps = cdp._ps

    def no_real_process(command: str, timeout: float = 30.0) -> str:
        # 23/10 : un test de relance a arrêté le VRAI Antigravity (Stop-Process par _ps). Aucun test n'arrête ni ne
        # lance de vrai programme : celui qui en a besoin simule _ps lui-même. Les lectures restent permises.
        if re.search(r"Stop-Process|Start-Process|taskkill", command, re.IGNORECASE):
            raise AssertionError(f"arrêt ou lancement réel refusé en test : {command[:80]}")
        return real_ps(command, timeout)

    monkeypatch.setattr(cdp, "_ps", no_real_process)
    monkeypatch.setattr(cdp, "targets", lambda port=0, timeout=2.0: None)  # aucun port ouvert, sans attente
    monkeypatch.setattr(credits, "availability", lambda box, agent, **_: None)  # pas de vraies applis ni journaux

    monkeypatch.setattr(foreground_lock, "LOCK_FILE", home / ".nexus" / "foreground.lock")
    woken: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        wake,
        "WAKERS",
        {
            "codex": lambda ids: woken.append(("codex", ids)) or {"status": "started", "via": "faux"},
        },
    )
    monkeypatch.setattr(wake, "tidy_codex", lambda *args, **kwargs: {})
    monkeypatch.setattr(wake.Bell, "start", lambda self: None)  # pas de fil : les tests appellent step()
    monkeypatch.setattr(server, "_BELL", None)
    wake.WAITED.clear()
    return tmp_path


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def connect(self: socket.socket, address: Any) -> None:
        if isinstance(address, tuple) and str(address[0]) in _LOCAL:
            return real_connect(self, address)
        raise OSError("réseau interdit dans les tests")

    monkeypatch.setattr(socket.socket, "connect", connect)


def payload(result: Any) -> dict[str, Any]:
    """JSON rendu par un outil, succès ou erreur : texte seul, ou CallToolResult des outils à schéma (schemas.py), dont
    le premier bloc texte et le structuredContent portent les mêmes données (erreur : isError, sans données)."""
    if isinstance(result, str):
        return json.loads(result)
    data = json.loads(result.content[0].text)
    if data["status"] == "success":
        assert not result.is_error and result.structured_content == data
    else:
        assert result.is_error and result.structured_content is None
    return data


def call(result: Any) -> dict[str, Any]:
    """Réponse JSON d'un outil, réussie."""
    data = payload(result)
    assert data["status"] == "success", data
    return data


def as_agent(monkeypatch: pytest.MonkeyPatch, agent: str) -> None:
    monkeypatch.setenv("NEXUS_RELAY_AGENT", agent)
