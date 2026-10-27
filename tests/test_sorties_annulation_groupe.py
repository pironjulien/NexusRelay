"""1.8.0 (audit d'état de l'art du 27/10) : sorties typées, annulation d'une demande, envoi aux deux pairs."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

import anyio
import pytest
from conftest import as_agent, call, payload
from mcp.types import CallToolResult

from nexus_relay import server, wake
from nexus_relay.box import Box, Message

TYPED = {
    "inbox": "unread",
    "read": "message",
    "status": "agents",
    "track": "state",
    "search": "hits",
    "reservations": "active",
    "summary": "participants",
}


def _tools() -> dict:
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def _run(name: str, arguments: dict) -> CallToolResult:
    """Appel par le SDK, comme un client : le structuredContent y est validé contre le schéma annoncé."""
    result = asyncio.run(server.mcp.call_tool(name, arguments))
    assert isinstance(result, CallToolResult)
    return result


def test_schemas_reels_et_plus_de_faux_schema():
    tools = _tools()
    assert len(tools) == 20  # 19 + cancel
    for name, tool in tools.items():
        if name in TYPED:
            assert tool.output_schema and TYPED[name] in tool.output_schema["properties"], name
            assert tool.output_schema["properties"]["status"]["const"] == "success"
        else:
            assert tool.output_schema is None, name  # plus de {result: string}


def test_outils_types_valides_par_le_sdk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sent = call(server.send("codex", "Relis le diff de jetons", topic="t"))
    call(server.send("antigravity", "Pour info : livré", topic="t", info=True))
    call(server.reserve([str(tmp_path / "a.py")], "essai"))
    as_agent(monkeypatch, "codex")
    inbox = _run("inbox", {})
    assert inbox.structured_content and inbox.structured_content["unread"][0]["id"] == sent["id"]
    call(server.reply(sent["id"], "a.py:3 jeton en clair, gravité haute"))
    as_agent(monkeypatch, "claude")
    for name, arguments in (
        ("read", {"id": sent["id"]}),
        ("status", {}),
        ("track", {"id": sent["id"]}),
        ("search", {"query": "jetons"}),
        ("reservations", {}),
        ("summary", {"topic": "t"}),
    ):
        result = _run(name, arguments)
        data = json.loads(result.content[0].text)  # type: ignore[union-attr]
        assert not result.is_error and result.structured_content == data and data["status"] == "success", name


def test_erreur_rendue_is_error_sans_donnees():
    result = _run("track", {"id": "inconnu"})
    assert result.is_error and result.structured_content is None
    assert payload(result)["status"] == "error"


def test_annulation_avant_prise_en_charge(monkeypatch: pytest.MonkeyPatch):
    box = server._box()
    sent = call(server.send("codex", "q", topic="t"))
    assert server.bell(box).pending("codex") == [sent["id"]]
    done = call(server.cancel(sent["id"]))
    assert done["state"] == "annulé" and "note" not in done  # jamais sonné : rien n'a atteint Codex
    bell = server.bell(box)
    bell.clock = lambda: time.time() + wake.GROUP_S + 1
    assert bell.step("codex") is None and bell.pending("codex") == []  # plus de sonnette
    assert call(server.track(sent["id"]))["state"].startswith("annulé")
    assert json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=1)))["status"] == "cancelled"
    as_agent(monkeypatch, "codex")
    assert call(server.inbox())["unread"] == []
    assert call(server.read(sent["id"]))["message"]["cancelled"]
    assert "annulée" in json.loads(server.reply(sent["id"], "x"))["error"]
    assert "annulée" in json.loads(server.progress(sent["id"], "je commence"))["error"]


def test_annulation_apres_reveil_previent_le_pair():
    box = server._box()
    sent = call(server.send("codex", "q", topic="t"))
    bell = server.bell(box)
    bell.clock = lambda: time.time() + wake.GROUP_S + 1
    bell.step("codex")  # sonné (faux réveil des tests), pas encore lu
    assert "déjà réveillé" in call(server.cancel(sent["id"]))["note"]
    assert bell.watching("codex") and not bell.step("codex")


def test_annulation_refusee_trop_tard(monkeypatch: pytest.MonkeyPatch):
    read = call(server.send("codex", "lue", topic="t"))
    working = call(server.send("codex", "en cours", topic="t"))
    answered = call(server.send("codex", "répondue", topic="t"))
    info = call(server.send("codex", "pour info", topic="t", info=True))
    as_agent(monkeypatch, "codex")
    call(server.read(read["id"]))
    call(server.progress(working["id"], "je relis"))
    call(server.reply(answered["id"], "fait", state="refused"))
    assert "hôte" not in json.loads(server.cancel(read["id"]))["error"]  # pas l'expéditeur : refusé aussi
    as_agent(monkeypatch, "claude")
    for sent, why in ((read, "déjà lue"), (working, "déjà en cours"), (answered, "déjà répondue"), (info, "pour info")):
        assert why in json.loads(server.cancel(sent["id"]))["error"]
    fresh = call(server.send("codex", "à annuler", topic="t"))
    call(server.cancel(fresh["id"]))
    assert "annulée" in json.loads(server.cancel(fresh["id"]))["error"]


def test_annulation_par_l_expediteur_seulement(monkeypatch: pytest.MonkeyPatch):
    sent = call(server.send("codex", "q", topic="t"))
    as_agent(monkeypatch, "antigravity")
    assert "expéditeur" in json.loads(server.cancel(sent["id"]))["error"]


def test_annulee_sort_des_incidents():
    box = server._box()
    sent = call(server.send("codex", "q", topic="t"))
    box.set_ring_state(sent["id"], {"agent": "codex", "incident": "non lu après 3 réveils", "incident_at": "x"})
    assert call(asyncio.run(server.status()))["incidents"]
    box.set_ring_state(sent["id"], {"agent": "codex"})
    call(server.cancel(sent["id"]))
    box.set_ring_state(sent["id"], {"agent": "codex", "incident": "non lu après 3 réveils", "incident_at": "x"})
    assert call(asyncio.run(server.status()))["incidents"] == []


def test_envoi_aux_deux_pairs_et_attente_groupee(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "WAIT_POLL_S", 0.05)
    sent = call(server.send(["codex", "agy", "codex"], "Relecture croisée du diff", topic="t"))
    ids = sent["ids"]
    assert [m["to"] for m in sent["messages"]] == ["codex", "antigravity"] and len(set(ids)) == 2
    assert all(m.get("review") for m in sent["messages"])
    assert server.bell(server._box()).pending("codex") == [ids[0]]

    async def scenario() -> dict:
        task = asyncio.ensure_future(server.wait_reply(ids, timeout_s=5))
        await asyncio.sleep(0.2)
        for agent, message_id in zip(("codex", "antigravity"), ids, strict=True):
            as_agent(monkeypatch, agent)
            call(server.reply(message_id, f"rien d'important ({agent})"))
        as_agent(monkeypatch, "claude")
        return json.loads(await task)

    result = asyncio.run(scenario())
    assert result["status"] == "success" and result["answered"] == result["total"] == 2
    assert [r["id"] for r in result["replies"]] == ids
    assert "codex" in result["replies"][0]["reply"]["text"] and "antigravity" in result["replies"][1]["reply"]["text"]


def test_attente_groupee_partielle(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "WAIT_POLL_S", 0.05)
    ids = call(server.send(["codex", "antigravity"], "q", topic="t", review=False))["ids"]
    as_agent(monkeypatch, "codex")
    call(server.reply(ids[0], "vu"))
    as_agent(monkeypatch, "claude")
    result = json.loads(asyncio.run(server.wait_reply(ids, timeout_s=1)))
    assert result["status"] == "partial" and result["answered"] == 1
    assert [r["status"] for r in result["replies"]] == ["success", "timeout"]


def test_attente_groupee_annulee_ne_perd_aucune_reponse(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex du 27/10 : annulée par le client, l'attente groupée laissait tourner ses enfants, qui
    marquaient lues des réponses jamais rendues. Rien n'est consommé avant la livraison du groupe."""
    monkeypatch.setattr(server, "WAIT_POLL_S", 0.02)
    ids = call(server.send(["codex", "antigravity"], "q", topic="t", review=False))["ids"]

    async def scenario() -> None:
        task = asyncio.ensure_future(server.wait_reply(ids, timeout_s=5))
        await asyncio.sleep(0.1)
        as_agent(monkeypatch, "codex")
        call(server.reply(ids[0], "réponse A"))  # arrivée pendant l'attente, avant l'annulation
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        as_agent(monkeypatch, "antigravity")
        call(server.reply(ids[1], "réponse B"))  # arrivée après l'annulation
        as_agent(monkeypatch, "claude")

    asyncio.run(scenario())
    assert not any(wake.awaited(i) for i in ids)
    late = call(server.track(ids[0])).get("late_replies") or []
    assert sorted(m["reply_to"] for m in late) == sorted(ids)  # les deux rendues au prochain appel


def test_annulation_et_lecture_serialisees(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex du 27/10 : un read passé entre le contrôle « non lue » de cancel et son évènement laissait
    le pair traiter une demande que l'hôte croyait retirée. Le read attend la fin de cancel et voit « cancelled »."""
    sent = call(server.send("codex", "q", topic="t"))
    entered, gate = threading.Event(), threading.Event()
    real = server._outcome

    def slow(box, message):  # noqa: ANN001, ANN202 - barrière au milieu de cancel, verrou tenu
        entered.set()
        gate.wait(5)
        return real(box, message)

    monkeypatch.setattr(server, "_outcome", slow)
    out: dict[str, object] = {}

    def as_codex() -> None:
        server._AGENT.set("codex")
        out["read"] = server.read(sent["id"])

    canceller = threading.Thread(target=lambda: out.setdefault("cancel", server.cancel(sent["id"])))
    canceller.start()
    assert entered.wait(5)
    reader = threading.Thread(target=as_codex)
    reader.start()
    time.sleep(0.2)
    assert reader.is_alive()  # bloqué tant que cancel tient la demande
    gate.set()
    canceller.join(5)
    reader.join(5)
    assert call(out["cancel"])["state"] == "annulé"
    assert call(out["read"])["message"]["cancelled"]


def test_verrou_de_demande_commun_aux_processus():
    """Tour 2 de Codex : le verrou était propre au processus ; un serveur stdio et le démon servent le même salon."""
    box = server._box()
    sent = call(server.send("codex", "q", topic="t"))
    slot = zlib.crc32(sent["id"].encode()) % server.TAKING_SLOTS
    probe = (
        "import os, sys; from nexus_relay import server; "
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); print(server._try_lock(fd))"
    )
    lock = str(box.root / "locks" / f"demande-{slot:02d}.lock")
    with server._taking(box, sent["id"]):
        held = subprocess.run([sys.executable, "-c", probe, lock], capture_output=True, text=True, timeout=60)
    free = subprocess.run([sys.executable, "-c", probe, lock], capture_output=True, text=True, timeout=60)
    assert (held.stdout.strip(), free.stdout.strip()) == ("False", "True"), (held.stderr, free.stderr)


def test_attente_groupee_refusee_ne_libere_rien(monkeypatch: pytest.MonkeyPatch):
    """Tour 2 de Codex : une demande d'autrui dans la liste libérait la réservation de la session qui l'attend."""
    sent = call(server.send("antigravity", "q", topic="t"))
    wake.wait_on(sent["id"], time.time() + 60)
    as_agent(monkeypatch, "codex")
    result = json.loads(asyncio.run(server.wait_reply([sent["id"]], timeout_s=1)))
    assert result["status"] == "error" and wake.awaited(sent["id"])


def test_attente_groupee_annulee_par_anyio(monkeypatch: pytest.MonkeyPatch):
    """Tour 2 de Codex : le SDK annule par CancelScope ; le nettoyage (abandon suivi, réservations) doit aller au bout."""
    monkeypatch.setattr(server, "WAIT_POLL_S", 0.02)
    ids = call(server.send(["codex", "antigravity"], "q", topic="t", review=False))["ids"]

    async def scenario() -> None:
        with anyio.move_on_after(0.2):
            await server.wait_reply(ids, timeout_s=5)

    anyio.run(scenario)
    assert not any(wake.awaited(i) for i in ids)
    assert set(server._box().state("abandoned-waits") or {}) == set(ids)


def test_remise_groupee_rend_tout_malgre_une_ecriture_ratee(monkeypatch: pytest.MonkeyPatch):
    """Tour 2 de Codex : une panne d'écriture à la remise laissait une réponse marquée lue sans être rendue."""
    ids = call(server.send(["codex", "antigravity"], "q", topic="t", review=False))["ids"]
    for agent, message_id in zip(("codex", "antigravity"), ids, strict=True):
        as_agent(monkeypatch, agent)
        call(server.reply(message_id, f"vu par {agent}"))
    as_agent(monkeypatch, "claude")
    real = Box.mark_read

    def flaky(self: Box, message: Message) -> None:
        if message.sender == "antigravity":
            raise OSError("disque plein")
        real(self, message)

    monkeypatch.setattr(Box, "mark_read", flaky)
    monkeypatch.setattr(wake, "wait_done", lambda message_id: (_ for _ in ()).throw(OSError("disque plein")))
    result = json.loads(asyncio.run(server.wait_reply(ids, timeout_s=1)))
    assert result["status"] == "success" and [r["reply"]["from"] for r in result["replies"]] == ["codex", "antigravity"]


def test_attente_groupee_en_panne_libere_et_suit_sa_reservation(monkeypatch: pytest.MonkeyPatch):
    """Tour 3 de Codex : une panne APRÈS la prise de réservation la laissait en place, réponse masquée et jamais
    rendue en late_replies. Libérée et suivie comme abandonnée."""
    ids = call(server.send(["codex", "antigravity"], "q", topic="t", review=False))["ids"]
    as_agent(monkeypatch, "antigravity")
    call(server.reply(ids[1], "vu"))
    as_agent(monkeypatch, "claude")
    real = Box.replies

    def broken(self: Box, message_id: str) -> list[Message]:
        if message_id == ids[0]:
            raise OSError("lecture impossible")
        return real(self, message_id)

    monkeypatch.setattr(Box, "replies", broken)
    result = json.loads(asyncio.run(server.wait_reply(ids, timeout_s=5)))
    assert result["status"] == "partial" and [r["status"] for r in result["replies"]] == ["error", "success"]
    monkeypatch.setattr(Box, "replies", real)
    assert not wake.awaited(ids[0])
    as_agent(monkeypatch, "codex")
    call(server.reply(ids[0], "réponse après la panne"))
    as_agent(monkeypatch, "claude")
    late = call(server.inbox()).get("late_replies") or []
    assert [m["reply_to"] for m in late] == [ids[0]]


def test_reservation_ratee_ne_laisse_rien(monkeypatch: pytest.MonkeyPatch):
    """Tour 4 de Codex : wait_on posait la réservation en mémoire avant de l'écrire ; une écriture ratée la laissait
    active sans qu'aucune attente ne la libère (réponse masquée jusqu'à 605 s)."""
    ids = call(server.send(["codex", "antigravity"], "q", topic="t", review=False))["ids"]
    as_agent(monkeypatch, "antigravity")
    call(server.reply(ids[1], "vu"))
    as_agent(monkeypatch, "claude")
    real = Box.set_state
    failures = [1]

    def flaky(self: Box, name: str, data: dict) -> None:
        if name == "waited" and failures:
            failures.pop()
            raise OSError("disque plein")
        real(self, name, data)

    monkeypatch.setattr(Box, "set_state", flaky)
    result = json.loads(asyncio.run(server.wait_reply(ids, timeout_s=5)))
    assert [r["status"] for r in result["replies"]] == ["error", "success"]
    assert not wake.awaited(ids[0])
    as_agent(monkeypatch, "codex")
    call(server.reply(ids[0], "réponse après la panne"))
    as_agent(monkeypatch, "claude")
    assert [m["reply_to"] for m in call(server.inbox()).get("late_replies") or []] == [ids[0]]


def test_summary_sans_les_annulees():
    kept = call(server.send("codex", "vraie question", topic="t"))
    dropped = call(server.send("codex", "retirée", topic="t"))
    call(server.cancel(dropped["id"]))
    data = call(server.summary("t"))
    assert data["open_total"] == 1 and [m["id"] for m in data["open"]] == [kept["id"]]


def test_envoi_groupe_reserve_a_l_hote(monkeypatch: pytest.MonkeyPatch):
    as_agent(monkeypatch, "codex")
    assert "hôte" in json.loads(server.send(["antigravity", "claude"], "q", topic="t"))["error"]
    as_agent(monkeypatch, "claude")
    assert "agent" in json.loads(server.send([], "q", topic="t"))["error"]
