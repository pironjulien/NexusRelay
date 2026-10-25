"""Tour de Codex arrêté sans réponse (filtre, erreur, appli figée) : incident signalé à l'hôte (24/10).

Vu le 24/10 : le filtre de contenu de Codex a fini son tour (task_complete avec « error ») après un progress ; le relais
a dit « en cours » pendant 40 min.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from conftest import as_agent, call

from nexus_relay import box as box_mod
from nexus_relay import server, wake

CHAT = "01a16b6e-5ddd-7a81-9f62-57cadbadfba0"
FILTER = "This content was flagged for possible cybersecurity risk."


def _iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(at)) + f".{int(at * 1000) % 1000:03d}Z"


def _journal(tmp_path: Path, *events: tuple[float, dict]) -> Path:
    folder = tmp_path / "codex-home" / "sessions" / "2026" / "10" / "24"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-10-24T03-02-37-{CHAT}.jsonl"
    lines = [{"timestamp": _iso(time.time() - 600), "type": "session_meta", "payload": {"id": CHAT, "cwd": "x"}}]
    lines += [{"timestamp": _iso(at), "type": "event_msg", "payload": payload} for at, payload in events]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _woken(monkeypatch: pytest.MonkeyPatch) -> tuple[box_mod.Box, str]:
    """Demande à Codex réveillée dans le chat CHAT, lue, avec un progress : prise en charge confirmée."""
    monkeypatch.setitem(wake.WAKERS, "codex", lambda ids: {"status": "started", "via": "faux", "conversation": CHAT})
    box = server._box()
    sent = call(server.send("codex", "Relecture du commit abc, sans rien modifier.", topic="relais"))
    bell = server.bell(box)
    bell.clock = lambda: time.time() + wake.GROUP_S + 1
    bell.step("codex")
    box.set_topic_state("relais", "codex", {"conversation": CHAT, "created": box_mod.now_iso()})
    as_agent(monkeypatch, "codex")
    call(server.read(sent["id"]))
    call(server.progress(sent["id"], "relecture lancée"))
    as_agent(monkeypatch, "claude")
    return box, sent["id"]


def test_tour_fini_sur_filtre_incident_signale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    box, message_id = _woken(monkeypatch)
    now = time.time()
    _journal(
        tmp_path,
        (now + 1, {"type": "task_started", "turn_id": "t1"}),
        (
            now + 30,
            {"type": "task_complete", "turn_id": "t1", "last_agent_message": None, "error": {"message": FILTER}},
        ),
    )
    wake.watch_codex(box, now=now + 60)
    state = call(server.track(message_id))["state"]
    assert "codex s'est arrêté" in state and "flagged" in state and "antigravity" in state
    incidents = call(asyncio.run(server.status()))["incidents"]
    assert [i["id"] for i in incidents] == [message_id] and "flagged" in incidents[0]["incident"]
    waited = json.loads(asyncio.run(server.wait_reply(message_id, timeout_s=30)))
    assert waited["status"] == "unavailable" and "flagged" in waited["reason"]
    # le chat du sujet est oublié : un renvoi part dans un chat neuf, l'ancien est rangé une fois fini
    assert not (box.topic_state("relais", "codex") or {}).get("conversation")


def test_tour_fini_sans_reponse_ni_erreur(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    box, message_id = _woken(monkeypatch)
    now = time.time()
    _journal(
        tmp_path,
        (now + 1, {"type": "task_started", "turn_id": "t1"}),
        (now + 30, {"type": "turn_aborted", "turn_id": "t1", "reason": "interrupted"}),
    )
    wake.watch_codex(box, now=now + 60)
    assert "interrupted" in call(server.track(message_id))["state"]


def test_tour_muet_trop_longtemps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    box, message_id = _woken(monkeypatch)
    now = time.time()
    _journal(tmp_path, (now + 1, {"type": "task_started", "turn_id": "t1"}), (now + 5, {"type": "token_count"}))
    wake.watch_codex(box, now=now + wake.CODEX_SILENT_S - 60)
    assert "arrêté" not in call(server.track(message_id))["state"]  # encore dans les temps
    wake.watch_codex(box, now=now + wake.CODEX_SILENT_S + 60)
    assert "muet" in call(server.track(message_id))["state"]


def test_tour_fini_avec_reponse_rien_signale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    box, message_id = _woken(monkeypatch)
    as_agent(monkeypatch, "codex")
    call(server.reply(message_id, "src/a.py:3 — gravité basse — rien de grave"))
    as_agent(monkeypatch, "claude")
    now = time.time()
    _journal(
        tmp_path,
        (now + 1, {"type": "task_started", "turn_id": "t1"}),
        (now + 30, {"type": "task_complete", "turn_id": "t1"}),
    )
    wake.watch_codex(box, now=now + 60)
    assert call(asyncio.run(server.status()))["incidents"] == []


def test_tour_fini_message_non_lu_laisse_aux_sonnettes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Non lu : la sonnette suivante (5 min) s'en charge, jamais un faux « arrêté »."""
    monkeypatch.setitem(wake.WAKERS, "codex", lambda ids: {"status": "started", "via": "faux", "conversation": CHAT})
    box = server._box()
    sent = call(server.send("codex", "q", topic="relais"))
    bell = server.bell(box)
    bell.clock = lambda: time.time() + wake.GROUP_S + 1
    bell.step("codex")
    now = time.time()
    _journal(
        tmp_path,
        (now + 1, {"type": "task_started", "turn_id": "t1"}),
        (now + 30, {"type": "task_complete", "turn_id": "t1"}),
    )
    wake.watch_codex(box, now=now + 60)
    assert "arrêté" not in call(server.track(sent["id"]))["state"]


def test_tour_long_au_dela_de_la_fin_lue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex : plus de 256 Kio entre task_started et la fin, l'arrêt passait inaperçu."""
    box, message_id = _woken(monkeypatch)
    now = time.time()
    noise = [(now + 2, {"type": "agent_reasoning", "text": "x" * 4096}) for _ in range(100)]
    _journal(
        tmp_path,
        (now + 1, {"type": "task_started", "turn_id": "t1"}),
        *noise,
        (now + 30, {"type": "task_complete", "turn_id": "t1", "error": {"message": FILTER}}),
    )
    wake.watch_codex(box, now=now + 60)
    assert "flagged" in call(server.track(message_id))["state"]


def test_tour_commence_pendant_un_reveil_lent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex : le tour commence pendant le réveil (avant son évènement) ; daté par tried_at."""
    monkeypatch.setitem(
        wake.WAKERS,
        "codex",
        lambda ids: (time.sleep(0.01), {"status": "started", "via": "faux", "conversation": CHAT})[1],
    )
    box, message_id = _woken(monkeypatch)
    woken = next(e for e in box.events(message_id) if e.get("kind") == "réveil")
    start = float(woken["tried_at"])
    _journal(
        tmp_path,
        (start + 0.005, {"type": "task_started", "turn_id": "t1"}),
        (start + 30, {"type": "task_complete", "turn_id": "t1", "error": {"message": FILTER}}),
    )
    wake.watch_codex(box, now=start + 60)
    assert "flagged" in call(server.track(message_id))["state"]


def test_incident_leve_quand_codex_reprend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    box, message_id = _woken(monkeypatch)
    now = time.time()
    _journal(
        tmp_path,
        (now + 1, {"type": "task_started", "turn_id": "t1"}),
        (now + 30, {"type": "task_complete", "turn_id": "t1", "error": {"message": FILTER}}),
    )
    wake.watch_codex(box, now=now + 60)
    assert call(asyncio.run(server.status()))["incidents"]
    as_agent(monkeypatch, "codex")
    call(server.progress(message_id, "je reprends"))
    as_agent(monkeypatch, "claude")
    # levé tout de suite partout, avant même le passage suivant de la surveillance
    assert call(asyncio.run(server.status()))["incidents"] == []
    assert server._unavailable(box, box.get(message_id)) is None  # type: ignore[arg-type]
    wake.watch_codex(box, now=now + 70)
    assert call(asyncio.run(server.status()))["incidents"] == []
    assert not (box.ring_state(message_id) or {}).get("incident")  # réarmé
    assert "reprends" in call(server.track(message_id))["state"]


def test_course_progress_pendant_le_passage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex : un progress arrivé entre le cliché et l'écriture faisait déclarer arrêté un chat vivant."""
    from nexus_relay import codex_app

    box, message_id = _woken(monkeypatch)
    now = time.time()
    _journal(
        tmp_path,
        (now + 1, {"type": "task_started", "turn_id": "t1"}),
        (now + 30, {"type": "task_complete", "turn_id": "t1", "error": {"message": FILTER}}),
    )
    real = codex_app.turn_after
    calls = []

    def racing(path: Path, since: float):
        calls.append(since)
        if len(calls) == 1:  # premier regard fait : Codex reprend à ce moment-là
            box.event(message_id, "en cours", by="codex", note="je reprends")
        return real(path, since)

    monkeypatch.setattr(codex_app, "turn_after", racing)
    assert wake.watch_codex(box, now=now + 60) == []
    assert (box.topic_state("relais", "codex") or {}).get("conversation") == CHAT


def test_chat_repris_par_un_autre_reveil_garde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Tour fini en erreur, mais le chat a déjà un nouveau tour (autre demande du sujet) : seul le DERNIER tour compte,
    rien n'est déclaré arrêté et le chat n'est pas oublié."""
    box, message_id = _woken(monkeypatch)
    now = time.time()
    _journal(
        tmp_path,
        (now + 1, {"type": "task_started", "turn_id": "t1"}),
        (now + 30, {"type": "task_complete", "turn_id": "t1", "error": {"message": FILTER}}),
        (now + 40, {"type": "task_started", "turn_id": "t2"}),
    )
    assert wake.watch_codex(box, now=now + 60) == []
    assert (box.topic_state("relais", "codex") or {}).get("conversation") == CHAT


def test_chat_reveille_depuis_pour_une_autre_demande_garde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Vu en vrai le 24/10 : demande ancienne sans réponse signalée, et le chat du sujet, réutilisé depuis par une autre
    session pour une nouvelle demande, était oublié (sa demande suivante partait dans un chat neuf, sans contexte)."""
    box, old = _woken(monkeypatch)
    time.sleep(1.1)
    bell = server.bell(box)
    bell.clock = time.time  # dépôt à l'heure réelle, réveil après le délai de groupement
    newer = call(server.send("codex", "Nouvelle demande, même sujet.", topic="relais"))["id"]
    bell.clock = lambda: time.time() + wake.GROUP_S + 1
    bell.step("codex")
    assert any(e.get("kind") == "réveil" for e in box.events(newer))
    as_agent(monkeypatch, "codex")
    call(server.reply(newer, "Fait."))
    as_agent(monkeypatch, "claude")
    now = time.time()
    _journal(
        tmp_path,
        (now + 1, {"type": "task_started", "turn_id": "t2"}),
        (now + 30, {"type": "task_complete", "turn_id": "t2"}),
    )
    assert wake.watch_codex(box, now=now + 60) == [old]  # vrai : l'ancienne n'a jamais eu de réponse
    assert (box.topic_state("relais", "codex") or {}).get("conversation") == CHAT  # mais le chat vivant est gardé


def test_quota_presque_plein_pas_unavailable_si_au_travail(monkeypatch: pytest.MonkeyPatch):
    """Vu le 24/10 : Codex à 95 % rendu « unavailable » en pleine relecture."""
    from nexus_relay import credits

    monkeypatch.setattr(credits, "availability", lambda box, agent, **_: "Codex : 95 % utilisés (5 h)")
    _, message_id = _woken(monkeypatch)
    assert server._unavailable(server._box(), server._box().get(message_id)) is None  # type: ignore[arg-type]


def test_tour_d_avant_le_reveil_ignore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Un tour fini AVANT le réveil (tour précédent du même chat) ne dit rien de cette demande."""
    box, message_id = _woken(monkeypatch)
    now = time.time()
    _journal(
        tmp_path,
        (now - 300, {"type": "task_started", "turn_id": "t0"}),
        (now - 200, {"type": "task_complete", "turn_id": "t0", "error": {"message": FILTER}}),
    )
    wake.watch_codex(box, now=now + 60)
    assert "arrêté" not in call(server.track(message_id))["state"]
