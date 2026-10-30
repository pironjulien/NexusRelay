"""Hôte unique configurable (option A validée par l'utilisateur le 26/10) : Claude par défaut, sans rien changer ;
NEXUS_RELAY_HOST pour un autre hôte, qui seul ouvre un échange et n'est jamais sonné."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import as_agent, call

from nexus_relay import server, settings, wake
from nexus_relay.box import Box, Message
from nexus_relay.paths import relay_host


def _peers(monkeypatch: pytest.MonkeyPatch) -> dict:
    from nexus_relay import cdp, credits

    monkeypatch.setattr(credits, "availability", lambda box, agent, **_: None)
    monkeypatch.setattr(wake, "sidecar_alive", lambda box: True)
    monkeypatch.setattr(cdp, "page_url", lambda *a, **k: "ws://x")
    monkeypatch.setattr(cdp, "router_ready", lambda timeout: True)
    return wake.peers_verdict(Box())


def test_par_defaut_tout_reste_identique(monkeypatch: pytest.MonkeyPatch):
    """Workflow de l'utilisateur et pad : Claude hôte, Codex et Antigravity sonnés, peers.json sans clé nouvelle."""
    monkeypatch.delenv("NEXUS_RELAY_HOST", raising=False)
    assert relay_host() == "claude" and wake.wakeable() == ("codex", "antigravity")
    assert set(_peers(monkeypatch)) == {"at", "codex", "antigravity"}  # ce que lit le pad, inchangé
    table = server._selftest_table(Box())
    assert [(row["source"], row["to"]) for row in table] == [("claude", "codex"), ("claude", "antigravity")]
    as_agent(monkeypatch, "codex")
    assert "claude est l'hôte" in json.loads(server.send("antigravity", "q", topic="t"))["error"]
    as_agent(monkeypatch, "claude")
    sent = call(server.send("codex", "q", topic="t"))
    assert wake.needs_wake(Box().get(sent["id"]))  # type: ignore[arg-type]


def test_valeur_inconnue_retombe_sur_claude(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEXUS_RELAY_HOST", "copilot")
    assert relay_host() == "claude"
    monkeypatch.setenv("NEXUS_RELAY_HOST", "ChatGPT")  # alias
    assert relay_host() == "codex"


def test_codex_hote_seul_a_ouvrir_jamais_sonne(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEXUS_RELAY_HOST", "codex")
    assert wake.wakeable() == ("antigravity",)
    as_agent(monkeypatch, "claude")
    assert "codex est l'hôte" in json.loads(server.send("antigravity", "q", topic="t"))["error"]
    as_agent(monkeypatch, "codex")
    sent = call(server.send("antigravity", "Avis ?", topic="t"))
    assert "réveillé" in sent["wake"] or "sidecar" in sent["wake"] or "Antigravity" in sent["wake"]
    assert wake.needs_wake(Box().get(sent["id"]))  # type: ignore[arg-type]
    as_agent(monkeypatch, "antigravity")
    answer = call(server.reply(sent["id"], "Rien d'important"))
    reply = Box().get(answer["id"])
    assert isinstance(reply, Message) and reply.to == "codex"
    assert not wake.needs_wake(reply)  # l'hôte n'est jamais sonné : il attend par wait_reply
    assert answer["wake"].startswith("pas de réveil")  # même texte qu'une réponse à Claude aujourd'hui
    as_agent(monkeypatch, "codex")
    status = call(asyncio.run(server.status()))
    assert status["host"] == "codex"
    assert [(r["source"], r["to"]) for r in status["selftest"]] == [("codex", "antigravity")]
    assert _peers(monkeypatch)["host"] == "codex"  # le pad sait qui est au centre


def test_antigravity_hote(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEXUS_RELAY_HOST", "antigravity")
    assert wake.wakeable() == ("codex",)
    as_agent(monkeypatch, "antigravity")
    sent = call(server.send("codex", "q", topic="t"))
    as_agent(monkeypatch, "codex")
    assert not wake.needs_wake(Box().get(call(server.reply(sent["id"], "ok"))["id"]))  # type: ignore[arg-type]


def test_reglage_host_lu_dans_les_fichiers(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"host": "codex"}), encoding="utf-8")
    environ: dict[str, str] = {}
    assert settings.apply(environ, [path]) == {"NEXUS_RELAY_HOST": "codex"}


def test_trinite_symetrique_tous_hotes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEXUS_RELAY_HOST", "all")
    assert relay_host() == "all"
    assert set(wake.wakeable()) == {"codex", "antigravity"}
    as_agent(monkeypatch, "antigravity")
    sent = call(server.send("codex", "q", topic="t"))
    assert sent["id"]
    as_agent(monkeypatch, "codex")
    sent2 = call(server.send("claude", "q2", topic="t2"))
    assert sent2["id"]
