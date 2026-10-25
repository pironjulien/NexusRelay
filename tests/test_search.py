"""Recherche plein texte dans le salon et résumé d'un sujet (24/10, comparaison avec MCP Agent Mail)."""

from __future__ import annotations

import json
import time

import pytest
from conftest import as_agent, call

from nexus_relay import server


def _fill(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    ids = {}
    ids["a"] = call(
        server.send("codex", "Relecture du dépôt NexusBrowser : sécurité des jetons", topic="nexusbrowser")
    )["id"]
    ids["b"] = call(server.send("antigravity", "Banc NexusWindows : mesure des clics", topic="nexuswindows"))["id"]
    time.sleep(1.1)  # horodatage à la seconde : c vient après a et b
    ids["c"] = call(
        server.send("codex", "Seconde relecture : la SECURITE du pont, jetons et cookies", topic="nexusbrowser")
    )["id"]
    as_agent(monkeypatch, "codex")
    ids["r"] = call(server.reply(ids["a"], "src/pont.py:12 jeton visible, gravité haute", vote="désaccord"))["id"]
    as_agent(monkeypatch, "claude")
    return ids


def test_recherche_sans_accents_ni_casse_tous_les_mots(monkeypatch: pytest.MonkeyPatch):
    ids = _fill(monkeypatch)
    found = call(server.search("securite jetons"))
    assert [h["id"] for h in found["hits"]] == [ids["c"], ids["a"]]  # le plus pertinent puis le plus récent
    first = found["hits"][0]
    assert first["topic"] == "nexusbrowser" and first["from"] == "claude" and "⟦relay:" in first["snippet"]
    assert "SECURITE" in first["snippet"] and found["note"].startswith("Texte entre")
    assert call(server.search("clics"))["hits"][0]["id"] == ids["b"]
    assert call(server.search("jetons inexistant"))["hits"] == []


def test_recherche_filtre_sujet_expediteur_phrase(monkeypatch: pytest.MonkeyPatch):
    ids = _fill(monkeypatch)
    assert [h["id"] for h in call(server.search("jeton", sender="codex"))["hits"]] == [ids["r"]]
    assert {h["id"] for h in call(server.search("relecture", topic="nexusbrowser"))["hits"]} == {ids["a"], ids["c"]}
    assert call(server.search("relecture", topic="nexuswindows"))["hits"] == []
    assert [h["id"] for h in call(server.search('"pont, jetons"'))["hits"]] == [ids["c"]]
    assert call(server.search("relecture", limit=1))["total"] == 2
    assert len(call(server.search("relecture", limit=1))["hits"]) == 1


def test_recherche_vide_refusee():
    assert json.loads(server.search("   "))["status"] == "error"


def test_resume_sujet_demandes_ouvertes_et_closes(monkeypatch: pytest.MonkeyPatch):
    ids = _fill(monkeypatch)
    summary = call(server.summary("nexusbrowser"))
    assert summary["topic"] == "nexusbrowser" and summary["messages"] == 3 and not summary["closed"]
    assert summary["participants"] == {"claude": 2, "codex": 1}
    assert [o["id"] for o in summary["open"]] == [ids["c"]] and summary["open"][0]["to"] == "codex"
    assert summary["answered_total"] == 1
    done = summary["answered"][0]
    assert done["id"] == ids["a"] and done["outcome"] == "terminé" and done["vote"] == "désaccord"
    assert "gravité haute" in done["reply"] and "⟦relay:" in done["reply"]
    assert [m["id"] for m in summary["last"]] == [ids["a"], ids["c"], ids["r"]]
    assert summary["first_at"] <= summary["last_at"]


def test_resume_borne_les_demandes_ouvertes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "SUMMARY_OPEN", 2)
    sent = [call(server.send("codex", f"question {n}", topic="t"))["id"] for n in range(4)]
    summary = call(server.summary("t"))
    assert summary["open_total"] == 4 and [o["id"] for o in summary["open"]] == sent[-2:]


def test_resume_sujet_inconnu_refuse():
    assert "inconnu" in json.loads(server.summary("rien"))["error"]
