"""Contrôle qualité d'une réponse de relecture (24/10) : sans fichier:ligne ni gravité, renvoyée une fois au pair."""

from __future__ import annotations

import json

import pytest
from conftest import as_agent, call

from nexus_relay import server

GOOD = "src/nexus_relay/server.py:612 : incident masqué si le sujet continue ; scénario A puis B ; gravité moyenne"


def _review(monkeypatch: pytest.MonkeyPatch, text: str = "Relecture du commit 1afe2d7, sans rien modifier.", **kw):
    sent = call(server.send("codex", text, topic="relais", **kw))
    as_agent(monkeypatch, "codex")
    return sent


def test_relecture_vague_renvoyee_une_fois_puis_acceptee_marquee(monkeypatch: pytest.MonkeyPatch):
    sent = _review(monkeypatch)
    first = json.loads(server.reply(sent["id"], "Ça me paraît bien, quelques détails à revoir.", vote="accord"))
    assert first["status"] == "error" and "fichier:ligne" in first["error"] and "gravité" in first["error"]
    assert server._box().replies(sent["id"]) == []  # rien de classé « terminé »
    assert server._box().has_event(sent["id"], "relecture renvoyée")
    second = call(server.reply(sent["id"], "Ça me paraît bien, quelques détails à revoir.", vote="accord"))
    assert second["quality"] == "incomplète"
    as_agent(monkeypatch, "claude")
    shown = call(server.read(second["id"]))["message"]
    assert shown["quality"] == "incomplète" and shown["state"] == "terminé"


@pytest.mark.parametrize(
    "text",
    [
        GOOD,
        "Rien d'important : le correctif tient (tests relancés, 249 verts).",
        "tests/test_box.py:40 — P2 : comparaison à la seconde.",
        "RAS",
    ],
)
def test_relecture_conforme_acceptee_du_premier_coup(monkeypatch: pytest.MonkeyPatch, text: str):
    sent = _review(monkeypatch)
    answer = call(server.reply(sent["id"], text))
    assert "quality" not in answer


@pytest.mark.parametrize(
    ("text", "complete"),
    [  # relecture de Codex du 24/10
        ("src/api.py:12 — Antigravity confirme le crash.", False),
        ("Pas RAS : il reste un risque sérieux, sans emplacement précis.", False),
        ("Dockerfile:12 — P2 : COPY échoue.", True),
        ("https://example.org:443 — gravité faible", False),
        ("Réunion à 12:30, gravité haute", False),
        ("C:\\Dev\\x\\main.rs:40-42 — gravité moyenne", True),
        ("\\\\serveur\\partage\\a.py:3 — gravité basse", True),  # chemin UNC
        ("C:\\\\Dev\\\\x\\\\main.rs:40 — gravité basse", True),  # chemin échappé (JSON)
        # relecture d'Antigravity du 24/10
        ("Relecture du commit abc :\nLes tests passent.\nRien d'important.", True),
        ("Bilan : aucun défaut", True),
        ("Il reste un point, ce n'est pas rien d'important.", False),
        ("src/api.py:10 — régression bloquante", True),
        ("src/api.py:10 — bogue sévère", True),
        ("src/api.py:10 — défaut grave", True),
    ],
)
def test_controle_qualite_faux_positifs_et_negatifs(text: str, complete: bool):
    assert server._review_complete(text) is complete


@pytest.mark.parametrize("unit", ["-\\", "a\\", "a-", "1-"])
def test_controle_qualite_en_temps_lineaire(unit: str):
    """Alerte CodeQL py/redos : « -\\ » répété sans « :ligne » faisait un retour arrière exponentiel."""
    import time

    start = time.perf_counter()
    assert not server._review_complete(" " + unit * 20_000 + "!")
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize(
    ("text", "review"),
    [
        ("Relecture croisée, sans rien modifier. Dépôt X.", True),
        ("Tour 2 de ta relecture, sans rien modifier.", True),
        ("Relis le diff de 2832bc7 et dis ce qui casse.", True),
        ("Merci. J'ai fait la revue du code, tu peux reprendre les mesures du banc.", False),
        ("Mesure le banc. " + "Contexte. " * 30 + "La relecture viendra plus tard.", False),
    ],
)
def test_relecture_deduite_du_debut_du_message(text: str, review: bool):
    """Relecture d'Antigravity du 24/10 : « revue » n'importe où dans un message n'en fait pas une relecture."""
    assert bool(call(server.send("codex", text, topic="t")).get("review")) is review


def test_refus_ou_echec_jamais_renvoyes(monkeypatch: pytest.MonkeyPatch):
    sent = _review(monkeypatch)
    assert call(server.reply(sent["id"], "Dépôt introuvable chez moi.", state="échoué"))


def test_demande_ordinaire_sans_controle(monkeypatch: pytest.MonkeyPatch):
    sent = _review(monkeypatch, "Mesure le banc NexusWindows et rends les chiffres.")
    assert call(server.reply(sent["id"], "Fait : 132 sur 140."))


def test_relecture_explicite_ou_ecartee(monkeypatch: pytest.MonkeyPatch):
    sent = _review(monkeypatch, "Regarde ce diff et dis ce qui casse.", review=True)
    assert json.loads(server.reply(sent["id"], "Bien."))["status"] == "error"
    as_agent(monkeypatch, "claude")
    other = _review(monkeypatch, "Relecture finie de ton côté ? Rends juste le SHA.", review=False)
    assert call(server.reply(other["id"], "abc1234"))
