"""Journal commun des outils Nexus (même format que Nexus Windows), produit « relay » (24/10)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from conftest import as_agent, call, payload

from nexus_relay import journal, server, wake

SECRET = "TEXTE-SECRET-4217 ne doit jamais sortir"


def _lines(tmp_path: Path, agent: str) -> list[dict]:
    journal.flush()
    files = sorted((tmp_path / "journal" / "relay" / agent).glob("*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]


def _all_text(tmp_path: Path) -> str:
    journal.flush()
    return "\n".join(f.read_text(encoding="utf-8") for f in (tmp_path / "journal").rglob("*.jsonl"))


def test_send_read_reply_wait_journalises_sans_texte(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sent = call(server.send("codex", SECRET, topic="relais"))
    as_agent(monkeypatch, "codex")
    call(server.read(sent["id"]))
    call(server.reply(sent["id"], SECRET + " (réponse)"))
    as_agent(monkeypatch, "claude")
    assert json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=5)))["status"] == "success"
    mine = _lines(tmp_path, "claude")
    assert [(line["action"], line["app"], line["issue"]) for line in mine] == [
        ("send", "codex", "✓"),
        ("wait_reply", "codex", "✓"),
    ]
    theirs = _lines(tmp_path, "codex")
    assert [(line["action"], line["app"], line["issue"]) for line in theirs] == [
        ("read", "claude", "✓"),
        ("reply", "claude", "✓"),
    ]
    for line in mine + theirs:
        assert line["v"] == 1 and line["outil"] and line["cadre"] is None and line["premier_plan"] is False
        assert isinstance(line["ms"], int) and line["appel"] == sent["id"] and line["ts"].endswith("Z")
    assert "TEXTE-SECRET" not in _all_text(tmp_path)  # jamais le texte des messages


def test_echec_d_envoi_journalise_en_echec():
    assert json.loads(server.send("codex", "   ", topic="relais"))["status"] == "error"
    line = journal_lines()[-1]
    assert line["action"] == "send" and line["issue"] == "✗" and line["erreur"] == "message vide"


def journal_lines() -> list[dict]:
    journal.flush()
    files = journal._root().glob("*/*.jsonl")
    lines = [json.loads(x) for f in files for x in f.read_text(encoding="utf-8").splitlines()]
    return sorted(lines, key=lambda x: x["ts"])


def test_reveil_rate_et_reussi(monkeypatch: pytest.MonkeyPatch):
    results = iter(
        [
            {"status": "failed", "reason": "Codex : 95 % utilisés (5 h), remise à zéro à 1792819398", "via": "cdp"},
            {"status": "started", "via": "cdp", "conversation": "c1"},
        ]
    )
    monkeypatch.setitem(wake.WAKERS, "codex", lambda ids: next(results))
    box = server._box()
    call(server.send("codex", SECRET, topic="relais"))
    bell = server.bell(box)
    bell.clock = lambda: time.time() + wake.GROUP_S + 1
    bell.step("codex")
    bell.clock = lambda: time.time() + wake.GROUP_S + 60  # après la pause de 20 s, avant l'abandon d'une heure
    bell.step("codex")
    wakes = [x for x in journal_lines() if x["action"] == "wake"]
    assert [(x["agent"], x["app"], x["issue"], x["technique"]) for x in wakes] == [
        ("claude", "codex", "✗", "cdp"),
        ("claude", "codex", "✓", "cdp"),
    ]
    assert wakes[0]["erreur"] == "quota" and isinstance(wakes[0]["ms"], int)


@pytest.mark.parametrize(
    ("reason", "cause"),
    [
        ("This content was flagged for possible cybersecurity risk.", "filtre"),
        ("Codex : 95 % utilisés (5 h)", "quota"),
        ("tour muet depuis 21 min (appli figée ?)", "appli figée"),
        ("appli Codex fermée", "appli fermée"),
        ("L'utilisateur est dans Codex", "L'utilisateur actif"),
        ("tour fini sans réponse", "tour fini sans réponse"),
        ("ConnectionRefusedError: [WinError 10061] « nom secret »", "ConnectionRefusedError"),
        # raisons réelles du 24/10
        (
            "l'interface de l'appli est déjà pilotée (réveil, rangement, crédits ou cartes) : reporté",
            "interface occupée",
        ),
        ("Antigravity ouvert mais sidecar muet : il redémarre seul, réessai", "sidecar muet"),
        ("premier plan tenu par un autre agent : relance d'Antigravity reportée", "jeton pris"),
    ],
)
def test_cause_courte_sans_texte(reason: str, cause: str):
    assert journal.cause(reason) == cause


def test_tour_arrete_journalise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from test_codex_halt import FILTER, _journal, _woken

    box, _ = _woken(monkeypatch)
    now = time.time()
    _journal(
        tmp_path,
        (now + 1, {"type": "task_started", "turn_id": "t1"}),
        (now + 30, {"type": "task_complete", "turn_id": "t1", "error": {"message": FILTER}}),
    )
    wake.watch_codex(box, now=now + 60)
    turn = [x for x in journal_lines() if x["action"] == "tour"]
    assert [(x["agent"], x["app"], x["issue"], x["erreur"]) for x in turn] == [("claude", "codex", "✗", "filtre")]


def test_identifiant_refuse_jamais_dans_appel():
    """Relecture de Codex : read(id="extrait confidentiel") gardait l'argument brut dans `appel`."""
    result = server.read("extrait confidentiel du message")
    assert payload(result)["status"] == "error"
    line = journal_lines()[-1]
    assert line["action"] == "read" and line["appel"] is None and "confidentiel" not in json.dumps(line)


def test_ecriture_lente_ne_bloque_pas(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex : écriture synchrone ; un disque lent bloquait le démon. File bornée, fil à part."""
    real = journal._write_to
    monkeypatch.setattr(journal, "_write_to", lambda path, line: (time.sleep(0.5), real(path, line)))
    started = time.perf_counter()
    call(server.send("codex", "q", topic="relais"))
    assert time.perf_counter() - started < 0.4
    journal.flush()
    assert journal_lines()[-1]["action"] == "send"


def test_reveil_avec_activation_premier_plan(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex : premier_plan vient de l'activation réelle (ActivateApplication), comptée à la source, même
    quand le réveil échoue ensuite ; un lancement réduit sans activation (« launched » seul) ne compte pas."""
    from nexus_relay import cdp

    def activated_then_failed(ids: list[str]) -> dict:
        cdp.count_activation()  # ce que fait cdp._activate_codex
        return {"status": "failed", "reason": "port 9223 muet 40 s après le lancement de Codex", "via": "cdp"}

    results = [activated_then_failed, lambda ids: {"status": "started", "via": "sidecar", "launched": True}]
    monkeypatch.setitem(wake.WAKERS, "codex", lambda ids: results.pop(0)(ids))
    box = server._box()
    call(server.send("codex", "q", topic="relais"))
    bell = server.bell(box)
    bell.clock = lambda: time.time() + wake.GROUP_S + 1
    bell.step("codex")
    bell.clock = lambda: time.time() + wake.GROUP_S + 60
    bell.step("codex")
    assert [x["premier_plan"] for x in journal_lines() if x["action"] == "wake"] == [True, False]


def test_activation_d_un_autre_fil_pas_attribuee(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex (tour 3) : Codex activé dans son fil pendant le réveil d'Antigravity ; seul le réveil qui a
    activé compte le premier plan."""
    import threading

    from nexus_relay import cdp

    counted: list[int] = []

    def codex_thread() -> None:
        cdp.count_activation()
        counted.append(cdp.activations())

    def agy_waker(ids: list[str]) -> dict:
        other = threading.Thread(target=codex_thread)  # le fil de Codex active pendant ce réveil
        other.start()
        other.join()
        assert counted == [1]  # l'activation a bien eu lieu, dans l'autre fil
        return {"status": "deferred", "reason": "Antigravity ouvert mais sidecar muet", "via": None}

    monkeypatch.setitem(wake.WAKERS, "antigravity", agy_waker)
    box = server._box()
    call(server.send("antigravity", "q", topic="relais"))
    bell = server.bell(box)
    bell.clock = lambda: time.time() + wake.GROUP_S + 1
    bell.step("antigravity")
    assert [x["premier_plan"] for x in journal_lines() if x["action"] == "wake"] == [False]


def test_destination_figee_a_l_enfilage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex : la destination était lue au drainage ; une ligne en attente pouvait partir ailleurs."""
    real = journal._write_to
    monkeypatch.setattr(journal, "_write_to", lambda path, line: (time.sleep(0.3), real(path, line)))
    call(server.send("codex", "q", topic="relais"))
    monkeypatch.setenv("NEXUS_JOURNAL_HOME", str(tmp_path / "ailleurs"))
    journal.flush()
    assert list((tmp_path / "journal" / "relay" / "claude").glob("*.jsonl"))
    assert not (tmp_path / "ailleurs").exists()


def test_livraison_antigravity_journalisee():
    """Relecture de Codex : le ✓ du réveil d'Antigravity n'est que la prise en charge du sidecar ; la livraison a sa
    propre ligne (✓ livrée, ✗ refusée)."""
    from test_sidecar import sidecar

    car, box, api = sidecar()
    box.post("claude", "antigravity", "q", "relais")
    car.step()
    api.fail = True
    box.post("claude", "antigravity", "q2", "autre")
    car.step()
    deliveries = [(x["agent"], x["app"], x["issue"]) for x in journal_lines() if x["action"] == "livraison"]
    assert deliveries == [("claude", "antigravity", "✓"), ("claude", "antigravity", "✗")]


def test_journal_jamais_bloquant(monkeypatch: pytest.MonkeyPatch):
    def broken(*args, **kwargs):
        raise OSError("disque plein")

    monkeypatch.setattr(journal, "_write_to", broken)
    assert call(server.send("codex", "q", topic="relais"))["id"]
