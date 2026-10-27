"""Les outils MCP du salon, lancés par n'importe lequel des trois."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from conftest import as_agent, call

from nexus_relay import __version__, server, wake


def test_envoi_lecture_reponse_entre_les_trois(monkeypatch: pytest.MonkeyPatch):
    from nexus_relay import cdp

    monkeypatch.setattr(cdp, "targets", lambda port=0, timeout=2.0: None)  # hors ligne : aucun port d'appli ouvert
    as_agent(monkeypatch, "claude")
    sent = call(server.send("codex", "Avis sur le diff ?", topic="nexuswindows"))
    assert "réveillé" in sent["wake"] and server.bell(server._box()).pending("codex") == [sent["id"]]
    as_agent(monkeypatch, "codex")
    unread = call(server.inbox())["unread"]
    assert [m["id"] for m in unread] == [sent["id"]] and "⟦relay:" in unread[0]["text"]
    assert "Avis sur le diff" in call(server.read(sent["id"]))["message"]["text"]
    answer = call(server.reply(sent["id"], "Une faille ligne 12"))
    assert answer["to"] == "claude"
    as_agent(monkeypatch, "claude")
    tracked = call(server.track(sent["id"]))
    assert tracked["state"] == "répondu" and tracked["replies"] == [answer["id"]]


def test_identite_obligatoire(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEXUS_RELAY_AGENT", "")
    assert json.loads(server.inbox())["status"] == "error"


def test_reponse_seulement_au_destinataire(monkeypatch: pytest.MonkeyPatch):
    sent = call(server.send("codex", "q", topic="t"))
    as_agent(monkeypatch, "antigravity")
    assert "pas adressé" in json.loads(server.reply(sent["id"], "x"))["error"]


def test_seul_claude_ouvre_un_echange(monkeypatch: pytest.MonkeyPatch):
    """L'utilisateur, 20/10 : Claude est l'hôte ; Antigravity et Codex répondent, sans ouvrir d'échange (plus de cascade)."""
    asked = call(server.send("antigravity", "Relis ?", topic="github", review=False))  # qualité : test_quality.py
    for agent in ("antigravity", "codex"):
        as_agent(monkeypatch, agent)
        assert "hôte" in json.loads(server.send("claude", "Une question", topic="github"))["error"]
        assert call(server.send("claude", "Pour info", topic="github", info=True))["to"] == "claude"
    as_agent(monkeypatch, "antigravity")
    assert call(server.reply(asked["id"], "Accord"))["to"] == "claude"


def test_info_ne_reveille_personne():
    sent = call(server.send("codex", "Pour info : livré", topic="t", info=True))
    assert "personne" in sent["wake"] and server.bell(server._box()).pending("codex") == []


def test_wait_reply_reserve_la_reponse(monkeypatch: pytest.MonkeyPatch):
    sent = call(server.send("codex", "q", topic="t"))

    async def scenario() -> dict:
        task = asyncio.ensure_future(server.wait_reply(sent["id"], timeout_s=5))
        await asyncio.sleep(0.2)
        as_agent(monkeypatch, "codex")
        answer = call(server.reply(sent["id"], "réponse"))
        assert "attend déjà" in answer["wake"]  # la session qui attend la reçoit : personne d'autre n'est réveillé
        as_agent(monkeypatch, "claude")
        hidden = call(server.inbox())
        assert hidden["unread"] == [] and "cachés" in hidden["hidden"]
        return json.loads(await task)

    result = asyncio.run(scenario())
    assert result["status"] == "success" and "réponse" in result["reply"]["text"]
    assert not wake.awaited(sent["id"])


def test_wait_reply_delai(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "WAIT_POLL_S", 0.05)
    sent = call(server.send("codex", "q", topic="t"))
    result = json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=1)))
    assert result["status"] == "timeout" and result["track"]["state"].startswith("déposé")


def test_status():
    call(server.send("codex", "q", topic="t"))
    data = call(asyncio.run(server.status()))  # lu hors de la boucle MCP (asyncio.to_thread)
    assert data["version"] == __version__
    assert data["agents"]["codex"]["unread"] == 1 and "port" in data["agents"]["codex"]
    assert data["bell"]["missing"] == sorted([*wake.WAKEABLE, wake.TIDY])  # fils jamais démarrés : dit, pas « sain »


def test_wait_reply_donne_signe_de_vie(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "WAIT_POLL_S", 0.05)
    monkeypatch.setattr(server, "PROGRESS_S", 0.1)
    sent = call(server.send("codex", "q", topic="t"))
    beats: list[float] = []

    class Ctx:
        async def report_progress(
            self, progress: float, total: float | None = None, message: str | None = None
        ) -> None:
            beats.append(progress)

    result = json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=1, ctx=Ctx())))  # type: ignore[arg-type]
    assert result["status"] == "timeout" and len(beats) >= 3


def test_vote_permis_hors_debat(monkeypatch: pytest.MonkeyPatch):
    sent = call(server.send("antigravity", "Relis le diff", topic="t"))
    as_agent(monkeypatch, "antigravity")
    assert call(server.reply(sent["id"], "RAS", vote="accord"))


def test_wait_reply_rend_une_reponse_tardive(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "WAIT_POLL_S", 0.05)
    sent = call(server.send("codex", "q", topic="t"))
    assert json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=1)))["status"] == "timeout"
    as_agent(monkeypatch, "codex")
    call(server.reply(sent["id"], "réponse tardive"))
    as_agent(monkeypatch, "claude")
    box = server._box()
    box.mark_read(box.unread("claude")[-1])  # lue par une autre session
    result = json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=1)))
    assert result["status"] == "success" and "réponse tardive" in result["reply"]["text"] and result["already_read"]


def test_track_libelles_honnetes(monkeypatch: pytest.MonkeyPatch):
    box = server._box()
    sent = call(server.send("codex", "q", topic="t"))
    bell = server.bell(box)
    bell.clock = lambda: time.time() + wake.GROUP_S + 1
    bell.step("codex")
    state = call(server.track(sent["id"]))["state"]
    assert state == f"réveillé, prise en charge non confirmée (sonnette 1/{wake.MAX_RINGS})"
    box.set_ring_state(sent["id"], {**(box.ring_state(sent["id"]) or {}), "incident": "non lu après 3 réveils"})
    assert call(server.track(sent["id"]))["state"].startswith("incident")
    assert call(asyncio.run(server.status()))["incidents"][0]["id"] == sent["id"]
    as_agent(monkeypatch, "codex")
    call(server.read(sent["id"]))
    assert "sans réponse (jamais resonné)" in call(server.track(sent["id"]))["state"]


def test_incident_sort_de_status_sujet_clos_ou_perime(monkeypatch: pytest.MonkeyPatch):
    box = server._box()
    first = call(server.send("codex", "q", topic="t"))
    box.set_ring_state(first["id"], {"agent": "codex", "incident": "non lu", "incident_at": server.now_iso()})
    other = call(server.send("codex", "autre question", topic="t"))
    as_agent(monkeypatch, "codex")
    call(server.reply(other["id"], "fait"))
    as_agent(monkeypatch, "claude")
    # relecture d'Antigravity : une autre question du sujet répondue ne masque pas celle restée sans réponse
    assert [i["id"] for i in call(asyncio.run(server.status()))["incidents"]] == [first["id"]]
    monkeypatch.setattr(server, "INCIDENT_KEEP_S", -1.0)  # périmé, compté depuis l'incident
    assert call(asyncio.run(server.status()))["incidents"] == []
    monkeypatch.setattr(server, "INCIDENT_KEEP_S", 3600.0)
    time.sleep(1.1)  # horodatage à la seconde : la clôture vient après le message
    call(server.close("t"))
    call(server.send("codex", "sujet rouvert", topic="t"))
    assert call(asyncio.run(server.status()))["incidents"] == []  # clos par l'hôte, même rouvert depuis
    assert call(server.track(first["id"]))["state"].startswith("incident")  # track le garde


def test_incident_heure_illisible_garde():
    box = server._box()
    sent = call(server.send("codex", "q", topic="t"))
    message = box.get(sent["id"])
    assert message and not server._incident_closed(box, message, "pas une heure")


def test_selftest_un_sens_de_bout_en_bout(monkeypatch: pytest.MonkeyPatch):
    from nexus_relay import cdp

    monkeypatch.setattr(cdp, "landmarks", lambda: {"codex": {"status": "ok"}, "antigravity": {"status": "non testé"}})
    monkeypatch.setattr(server, "_mechanism", lambda to, box: ("faux", None if to == "codex" else "fermé"))
    monkeypatch.setattr(server, "WAIT_POLL_S", 0.02)
    box = server._box()

    async def scenario() -> dict:
        async def pong() -> None:
            while not box.topic_messages("selftest"):
                await asyncio.sleep(0.02)
            ping = box.topic_messages("selftest")[0]
            box.mark_read(ping)
            box.post("codex", "claude", "pong", "selftest", reply_to=ping.id, info=True)

        test, _ = await asyncio.gather(server.selftest("codex", timeout_s=60), pong())
        return json.loads(test)

    result = asyncio.run(scenario())
    assert result["status"] == "success" and result["test"]["result"] == "ok" and result["test"]["correlated"]
    assert result["test"]["read_at"] and result["test"]["version"] and result["landmarks"]["codex"]["status"] == "ok"
    rows = {(r["source"], r["to"]): r["result"] for r in result["directions"]}
    assert rows[("claude", "codex")] == "ok" and rows[("claude", "antigravity")] == "non testé" and len(rows) == 2
    other = json.loads(asyncio.run(server.selftest("antigravity")))
    assert other["test"]["result"] == "non testé : fermé"
    assert not server.bell(box).pending("claude")  # la réponse du test ne réveille personne


def test_relance_seulement_si_le_code_a_change(monkeypatch: pytest.MonkeyPatch):
    loaded = server.code_fingerprint()
    assert server.restart_due(loaded, None, 0.0) == (False, None)  # aucune demande
    request = server.restart_request()
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text("x", encoding="utf-8")
    assert server.restart_due(loaded, None, 0.0) == (False, None)  # code inchangé : demande consommée, aucune coupure
    assert not request.exists()
    request.write_text("x", encoding="utf-8")
    assert wake.ACTIVE.enter()  # un wait_reply (ou un réveil) en cours : on l'attend
    assert server.restart_due("ancien", None, 10.0) == (False, 10.0)
    assert server.restart_due("ancien", 10.0, 20.0) == (False, 10.0)
    wake.ACTIVE.leave()
    assert server.restart_due("ancien", 10.0, 21.0) == (True, 10.0)
    assert not wake.ACTIVE.enter()  # fermé : plus aucun réveil ni rangement ne commence


def test_relance_sans_repit_puis_forcee(monkeypatch: pytest.MonkeyPatch):
    loaded = server.code_fingerprint()
    request = server.restart_request()
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text("force 2026-10-20", encoding="utf-8")  # réglages du démon changés, code identique
    assert wake.ACTIVE.enter()
    assert server.restart_due(loaded, None, 10.0) == (False, 10.0)
    assert server.restart_due(loaded, 10.0, 11.0 + server.RESTART_GRACE_S) == (True, 10.0)  # sans répit : relancé


def test_un_reveil_en_cours_retient_la_relance():
    """Tour 5 : un réveil de Codex en pleine séquence n'est plus coupé par la relance du démon."""
    seen: list[int] = []
    wake._counted(lambda: seen.append(wake.ACTIVE.count))
    assert seen == [1] and wake.ACTIVE.count == 0
    assert wake.ACTIVE.close_if_idle()
    wake._counted(lambda: pytest.fail("aucun réveil après la fermeture"))


def test_un_appel_mcp_est_compte(monkeypatch: pytest.MonkeyPatch):
    """Tour 5 : le compteur des appels en cours est vraiment tenu par le middleware pendant un tools/call."""
    from starlette.testclient import TestClient

    entered: list[int] = []
    real = wake.ACTIVE.enter

    labels: list[str] = []

    def enter(label: str = "?") -> int | None:
        ok = real(label)
        entered.append(wake.ACTIVE.count)
        labels.append(label)
        return ok

    monkeypatch.setattr(wake.ACTIVE, "enter", enter)
    token = "t" * 40
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "inbox", "arguments": {}}}
    with TestClient(server.http_app(token), base_url="http://127.0.0.1:16181") as client:
        assert client.post("/mcp", json=body, headers=headers).status_code == 200
    assert entered == [1] and wake.ACTIVE.count == 0


def test_http_sans_session_survit_a_une_relance():
    """Un client qui garde l'identifiant de session d'un démon disparu est servi quand même (avant : « unknown or
    expired session ID », puis « server not connected » côté Claude)."""
    from starlette.testclient import TestClient

    token = "t" * 40
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "mcp-session-id": "077b18aa0cef44e383e5bb614ccf1cd2",
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "inbox", "arguments": {}}}
    for _ in range(2):  # deux démons successifs, même client
        with TestClient(server.http_app(token), base_url="http://127.0.0.1:16181") as client:
            response = client.post("/mcp?agent=claude", json=body, headers=headers)
            assert response.status_code == 200, response.text
            assert '\\"status\\": \\"success\\"' in response.text and "unread" in response.text
            assert client.post("/mcp", json=body, headers={**headers, "Authorization": "Bearer x"}).status_code == 401


def test_demande_gardee_si_l_empreinte_echoue(monkeypatch: pytest.MonkeyPatch):
    """Tour 5 : une erreur au calcul de l'empreinte consommait la demande, perdue pour toujours."""
    from nexus_relay import paths

    request = server.restart_request()
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text("code", encoding="utf-8")

    def broken() -> str:
        raise PermissionError("verrou passager")

    monkeypatch.setattr(paths, "code_fingerprint", broken)
    with pytest.raises(PermissionError):
        server.restart_due("ancien", None, 0.0)
    assert request.exists()


def test_marqueur_de_relance_par_demande():
    from nexus_relay import paths

    paths.mark_restartable("daemon")
    assert (paths.relay_home() / "daemon-restart.pid").read_text(encoding="utf-8") == str(__import__("os").getpid())


def test_demande_deposee_pendant_l_examen_reste_entiere(monkeypatch: pytest.MonkeyPatch):
    """Tour 6 (Codex) : une demande « force » déposée pendant le calcul de l'empreinte était effacée sans être lue."""
    from nexus_relay import paths

    loaded = paths.code_fingerprint()
    request = server.restart_request()
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text("code A", encoding="utf-8")

    def during() -> str:
        request.write_text("force B", encoding="utf-8")  # une autre installation, réglages changés
        return loaded

    monkeypatch.setattr(paths, "code_fingerprint", during)
    assert server.restart_due(loaded, None, 0.0) == (False, None)  # A : rien à faire
    assert request.read_text(encoding="utf-8") == "force B"  # B intacte, lue au tour suivant
    monkeypatch.setattr(paths, "code_fingerprint", lambda: loaded)
    assert server.restart_due(loaded, None, 1.0) == (True, 1.0)  # B forcée : relance


def test_appel_refuse_avant_tout_effet_une_fois_la_relance_decidee():
    """Tour 6 (Codex) : après la décision, un appel n'est plus exécuté puis coupé : il est refusé net."""
    from starlette.testclient import TestClient

    token = "t" * 40
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "inbox", "arguments": {}}}
    assert wake.ACTIVE.close_if_idle()
    with TestClient(server.http_app(token), base_url="http://127.0.0.1:16181") as client:
        response = client.post("/mcp", json=body, headers=headers)
    text = response.text
    assert "relance" in text and '"id":7' in text.replace(" ", "") and wake.ACTIVE.count == 0


def test_issue_de_la_demande_en_cours_refuse_en_retard(monkeypatch: pytest.MonkeyPatch):
    """Chantier A du 21/10 (accord des trois, états de tâche à la A2A) : progress dit « en cours » sans clore ; une
    réponse clôt avec son issue ; une échéance dépassée sans réponse dit « en retard », jamais « échoué »."""
    sent = call(server.send("codex", "Relis le diff", topic="t", due_min=5))
    assert "due" in call(server.read(sent["id"]))["message"]
    as_agent(monkeypatch, "antigravity")
    assert "pas adressé" in json.loads(server.progress(sent["id"], "x"))["error"]
    as_agent(monkeypatch, "codex")
    assert call(server.progress(sent["id"], "relecture de cdp.py"))["state"] == "en cours"
    assert "issue" not in call(server.progress(sent["id"], "toujours"))  # non terminal, répétable
    as_agent(monkeypatch, "claude")
    state = call(server.track(sent["id"]))["state"]
    assert state.startswith("en cours") and "toujours" in state
    as_agent(monkeypatch, "codex")
    assert "state" in json.loads(server.reply(sent["id"], "x", state="peut-être"))["error"]
    answer = call(server.reply(sent["id"], "Hors de mon périmètre", state="refusé"))
    as_agent(monkeypatch, "claude")
    assert call(server.track(sent["id"]))["state"] == "répondu : refusé"
    assert call(server.read(answer["id"]))["message"]["state"] == "refusé"
    box = server._box()
    late = box.post("claude", "codex", "Vieux", "t", extra={"due": "2000-01-01T00:00:00Z"})
    assert call(server.track(late.id))["state"].startswith("en retard (échéance 2000-01-01T00:00:00Z)")
    assert "1 à 1440" in json.loads(server.send("codex", "q", topic="t", due_min=0))["error"]


def test_issue_canonique_et_note_du_pair_dans_l_attente(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex (21/10) : la première réponse fixe l'issue (un second reply sans state effaçait un refus) ;
    progress après clôture refusé ; la note « en cours » remonte dans le signe de vie de wait_reply."""
    monkeypatch.setattr(server, "WAIT_POLL_S", 0.05)
    monkeypatch.setattr(server, "PROGRESS_S", 0.1)
    sent = call(server.send("codex", "q", topic="t"))
    as_agent(monkeypatch, "codex")
    call(server.progress(sent["id"], "étape 2 sur 3"))
    as_agent(monkeypatch, "claude")
    notes: list[str | None] = []

    class Ctx:
        async def report_progress(
            self, progress: float, total: float | None = None, message: str | None = None
        ) -> None:
            notes.append(message)

    asyncio.run(server.wait_reply(sent["id"], timeout_s=1, ctx=Ctx()))  # type: ignore[arg-type]
    assert any(n and "étape 2 sur 3" in n for n in notes)
    as_agent(monkeypatch, "codex")
    call(server.reply(sent["id"], "Non", state="refusé"))
    assert "déjà fixée" in json.loads(server.reply(sent["id"], "Si", state="échoué"))["error"]
    extra = call(server.reply(sent["id"], "Complément"))  # sans state : complète, n'efface pas le refus
    assert "state" not in call(server.read(extra["id"]))["message"]
    assert "déjà close" in json.loads(server.progress(sent["id"], "encore"))["error"]
    as_agent(monkeypatch, "claude")
    assert call(server.track(sent["id"]))["state"] == "répondu : refusé"


def test_close_clot_et_send_rouvre(monkeypatch: pytest.MonkeyPatch):
    """close(topic) : hôte seulement, sujet connu ; ses conversations sont alors rangées ; un nouveau send le rouvre."""
    assert "inconnu" in json.loads(server.close("jamais-vu"))["error"]
    call(server.send("codex", "q", topic="fini"))
    as_agent(monkeypatch, "codex")
    assert "hôte" in json.loads(server.close("fini"))["error"]
    as_agent(monkeypatch, "claude")
    assert call(server.close("fini"))["closed"] is True
    box = server._box()
    assert box.topic_closed("fini") and box.topic_idle("fini", time.time(), 3600)
    call(server.send("codex", "On reprend", topic="fini"))
    assert not box.topic_closed("fini") and not box.topic_idle("fini", time.time(), 3600)


def test_wait_reply_ne_retient_pas_la_relance(monkeypatch: pytest.MonkeyPatch):
    """Une attente de 600 s retenait la relance sur le nouveau code jusqu'à 20 min : elle rend « restarting » tout de
    suite, et la réponse reste réservée (à rendre au prochain wait_reply)."""
    import threading

    monkeypatch.setattr(server, "WAIT_POLL_S", 0.05)
    monkeypatch.setattr(server, "RESTART_PENDING", threading.Event())
    sent = call(server.send("codex", "q", topic="t"))
    server.RESTART_PENDING.set()
    started = time.time()
    result = json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=5)))
    assert result["status"] == "restarting" and time.time() - started < 2
    assert wake.awaited(sent["id"])  # toujours réservée
    as_agent(monkeypatch, "codex")
    call(server.reply(sent["id"], "Réponse"))
    as_agent(monkeypatch, "claude")
    server.RESTART_PENDING.clear()
    assert json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=1)))["status"] == "success"


def test_abonnement_aux_notifications_ne_retient_pas_la_relance():
    """21/10 (journal du démon) : « subscriptions/listen » de Claude, ouvert en permanence, retenait chaque relance
    jusqu'au délai de grâce."""
    seen: list[int] = []

    class Ctx:
        request = None
        message = None
        method = "subscriptions/listen"

    async def call_next(ctx: object) -> str:
        seen.append(wake.ACTIVE.count)
        return "ok"

    before = wake.ACTIVE.count
    assert asyncio.run(server._bind_agent(Ctx(), call_next)) == "ok" and seen == [before]
    Ctx.method = "tools/call"
    asyncio.run(server._bind_agent(Ctx(), call_next))
    assert seen[-1] == before + 1 and wake.ACTIVE.count == before


CONV_AGY = "215cdc5d-fca9-4cc3-8a7e-4d59633e016f"


def _agy_transcript(*steps: dict) -> None:
    from nexus_relay.paths import gemini_home

    path = gemini_home() / "antigravity" / "brain" / CONV_AGY / ".system_generated" / "logs" / "transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(s) + "\n" for s in steps), encoding="utf-8")


def _step(at: float, kind: str, status: str = "DONE", **extra: object) -> dict:
    return {"type": kind, "status": status, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)), **extra}


DENIED = "tool call denied by pre-tool hook: "


def test_outils_en_erreur_lus_a_la_fin_du_journal():
    """Cas du 23/10 : erreurs d'outil d'affilée depuis la livraison ; un outil réussi depuis remet à zéro, les erreurs
    d'avant la livraison ne comptent pas."""
    from nexus_relay.paths import agy_tool_errors, gemini_home

    at = time.time() - 600
    assert agy_tool_errors(CONV_AGY, at) is None  # pas de journal
    _agy_transcript(
        _step(at - 60, "GENERIC", "ERROR", error="file does not exist"),  # avant la livraison
        _step(at + 1, "SYSTEM_MESSAGE"),
        _step(at + 2, "PLANNER_RESPONSE", tool_calls=[{"name": "call_mcp_tool"}]),
        _step(at + 3, "GENERIC", "ERROR", error=DENIED),
        _step(at + 4, "PLANNER_RESPONSE", tool_calls=[{"name": "run_command"}]),
        _step(at + 5, "GENERIC", "ERROR", error=DENIED),
        _step(at + 6, "PLANNER_RESPONSE", content="Blocage : crochet PreToolUse"),
    )
    found = agy_tool_errors(CONV_AGY, at)
    assert found is not None and found["count"] == 2 and found["ended"] and found["at"] == int(at + 5)
    assert found["reason"] == "tool call denied by pre-tool hook" and "PreToolUse" in found["hint"]
    _agy_transcript(
        _step(at + 3, "GENERIC", "ERROR", error="target content not found"),
        _step(at + 4, "PLANNER_RESPONSE", tool_calls=[{"name": "read_file"}]),
        _step(at + 5, "GENERIC", "DONE"),  # rattrapée
        _step(at + 6, "PLANNER_RESPONSE", content="fini"),
    )
    assert agy_tool_errors(CONV_AGY, at) is None
    # relecture de Codex, tour 1 : une reprise en cours d'écriture n'est jamais une fin, et une entrée différée (heure
    # plus ancienne que la livraison, plus loin dans le fichier) n'arrête pas la lecture
    errors = [_step(at + 3, "GENERIC", "ERROR", error=DENIED), _step(at + 4, "PLANNER_RESPONSE", content="bloqué")]
    _agy_transcript(*errors)
    path = next((gemini_home() / "antigravity" / "brain").rglob("transcript.jsonl"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "PLANNER_RESPONSE", "status": "RUNN')
    found = agy_tool_errors(CONV_AGY, at)
    assert found is not None and found["count"] == 1 and not found["ended"]
    _agy_transcript(errors[0], _step(at - 300, "USER_INPUT"), errors[1])
    found = agy_tool_errors(CONV_AGY, at)
    assert found is not None and found["count"] == 1 and found["ended"]


def test_outils_en_erreur_lecture_ratee_jamais_gardee(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex, tour 2 : un journal verrouillé un instant n'est pas « aucune erreur » pour toujours."""
    from nexus_relay import paths

    at = time.time() - 600
    _agy_transcript(_step(at + 3, "GENERIC", "ERROR", error=DENIED), _step(at + 4, "PLANNER_RESPONSE", content="x"))
    real = paths._tool_errors

    def locked(path: object, since: float) -> None:
        raise PermissionError("verrou de partage")

    monkeypatch.setattr(paths, "_tool_errors", locked)
    assert paths.agy_tool_errors(CONV_AGY, at) is None
    monkeypatch.setattr(paths, "_tool_errors", real)
    found = paths.agy_tool_errors(CONV_AGY, at)
    assert found is not None and found["count"] == 1 and found["ended"]


def test_track_status_wait_reply_disent_les_outils_refuses(monkeypatch: pytest.MonkeyPatch):
    """23/10 : « livré, lecture non confirmée (sonnette 3/3) » alors qu'un hook refusait tous les outils d'Antigravity."""
    monkeypatch.setattr(server, "WAIT_POLL_S", 0.02)
    monkeypatch.setattr(server, "UNAVAILABLE_CHECK_S", 0.0)
    box = server._box()
    sent = call(server.send("antigravity", "Relis le diff", topic="nexuskeypad"))
    assert box.claim(sent["id"], 1, "sidecar")
    box.set_claim(sent["id"], 1, "sidecar", "delivered", conversation=CONV_AGY)
    at = time.time() - 2
    _agy_transcript(
        _step(at, "SYSTEM_MESSAGE"),
        _step(at, "PLANNER_RESPONSE", tool_calls=[{"name": "call_mcp_tool"}]),
        _step(at, "GENERIC", "ERROR", error=DENIED),
    )
    assert "tool_errors" not in call(server.track(sent["id"]))  # une seule erreur en plein tour : il peut s'en sortir
    _agy_transcript(
        _step(at, "PLANNER_RESPONSE", tool_calls=[{"name": "call_mcp_tool"}]),
        _step(at, "GENERIC", "ERROR", error=DENIED),
        _step(at, "PLANNER_RESPONSE", content="En attente de déblocage"),
    )
    tracked = call(server.track(sent["id"]))
    assert (
        "outils échouent" in tracked["state"]
        and "pre-tool hook" in tracked["state"]
        and "PreToolUse" in tracked["state"]
    )
    assert tracked["tool_errors"]["ended"] and tracked["tool_errors"]["conversation"] == CONV_AGY
    incidents = call(asyncio.run(server.status()))["incidents"]
    assert incidents[0]["id"] == sent["id"] and "pre-tool hook" in incidents[0]["incident"]
    result = json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=5)))
    assert result["status"] == "unavailable" and "pre-tool hook" in result["reason"]
    _agy_transcript(_step(at, "GENERIC", "ERROR", error=DENIED), _step(at + 1, "GENERIC", "DONE"))
    assert "tool_errors" not in call(server.track(sent["id"]))  # hook corrigé : un outil passe, plus rien à signaler


def test_outils_en_erreur_bornes_a_l_injection():
    """Relecture de Codex, tour 1 : réservation à T0, injection 2 min plus tard ; un ancien tour fini sur une erreur
    entre les deux n'est pas un refus de la nouvelle demande."""
    box = server._box()
    sent = call(server.send("antigravity", "Relis le diff", topic="nexuskeypad"))
    assert box.claim(sent["id"], 1, "sidecar")
    injected = time.time() - 30
    box.set_claim(
        sent["id"], 1, "sidecar", "submitting", submitted=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(injected))
    )
    box.set_claim(sent["id"], 1, "sidecar", "delivered", conversation=CONV_AGY)
    old = injected - 100
    _agy_transcript(
        _step(old, "GENERIC", "ERROR", error=DENIED),
        _step(old + 1, "PLANNER_RESPONSE", content="fini"),
        _step(injected + 1, "SYSTEM_MESSAGE"),
        _step(injected + 2, "PLANNER_RESPONSE", content="je regarde"),
    )
    assert "tool_errors" not in call(server.track(sent["id"]))
    _agy_transcript(
        _step(injected + 2, "PLANNER_RESPONSE", tool_calls=[{"name": "call_mcp_tool"}]),
        _step(injected + 3, "GENERIC", "ERROR", error=DENIED),  # pendant l'injection, avant l'acquittement : compté
        _step(injected + 4, "PLANNER_RESPONSE", content="bloqué"),
    )
    assert call(server.track(sent["id"]))["tool_errors"]["count"] == 1


def test_wait_reply_n_abandonne_pas_sous_un_quota_plein(monkeypatch: pytest.MonkeyPatch):
    """24/10 : Codex à 97-98 % rendu « unavailable » en 10 s, sa réponse arrivée 4 min plus tard restée non lue."""
    from nexus_relay import credits

    monkeypatch.setattr(server, "WAIT_POLL_S", 0.02)
    monkeypatch.setattr(server, "UNAVAILABLE_CHECK_S", 0.0)
    used = {"pct": 98.0}

    def usage() -> dict:
        return {"windows": {"primary": {"used_pct": used["pct"], "current": True, "resets_at": time.time() + 600}}}

    monkeypatch.setattr(credits, "codex_usage", usage)
    monkeypatch.setattr(credits, "availability", _vraie_dispo)
    sent = call(server.send("codex", "q", topic="t"))
    assert json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=1)))["status"] == "timeout"
    assert "98 %" in (credits.availability(server._box(), "codex") or "")  # le choix automatique l'écarte toujours
    used["pct"] = 100.0
    result = json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=5)))
    assert result["status"] == "unavailable" and "100 %" in result["reason"]


def _vraie_dispo(box, agent: str, used_pct: float = 95.0):  # type: ignore[no-untyped-def]
    from nexus_relay import credits

    return credits.exhausted(agent, credits.codex_usage(), used_pct=used_pct) if agent == "codex" else None


def test_reponse_tardive_jointe_au_prochain_appel_puis_lue(monkeypatch: pytest.MonkeyPatch):
    """24/10 : réponses arrivées après la fin du wait_reply, jamais rendues ni lues (✉7 dans la barre)."""
    monkeypatch.setattr(server, "WAIT_POLL_S", 0.05)
    sent = call(server.send("codex", "q", topic="t"))
    assert json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=1)))["status"] == "timeout"
    as_agent(monkeypatch, "codex")
    call(server.reply(sent["id"], "j'y suis", info=True))  # avis d'étape : la demande reste suivie
    as_agent(monkeypatch, "claude")
    first = call(server.track(sent["id"]))
    assert [m["text"] for m in first["late_replies"]] and "j'y suis" in first["late_replies"][0]["text"]
    assert "late_replies" not in call(server.track(sent["id"]))  # rendue une seule fois
    as_agent(monkeypatch, "codex")
    call(server.reply(sent["id"], "verdict final"))
    as_agent(monkeypatch, "antigravity")
    assert "late_replies" not in call(server.inbox())  # jamais à un autre agent
    as_agent(monkeypatch, "claude")
    status = call(asyncio.run(server.status()))
    assert "verdict final" in status["late_replies"][0]["text"] and status["agents"]["claude"]["unread"] == 0
    assert server._box().state("abandoned-waits") == {}  # réponse définitive rendue : plus suivie


def test_messages_de_l_hote_signales_par_le_hook_puis_remis_par_inbox(monkeypatch: pytest.MonkeyPatch):
    """27/10 : ✉26 dans la barre, des compléments après la réponse rendue et des « pour info » que l'hôte ne voyait pas.
    Le hook les signale une fois par conversation ; inbox les remet (l'hôte ne reçoit que réponses et « pour info »)."""
    from nexus_relay import hook

    sent = call(server.send("codex", "q", topic="t"))
    wake.wait_on(sent["id"], time.time() + 60)  # une session attend la réponse : elle lui reste
    as_agent(monkeypatch, "codex")
    call(server.reply(sent["id"], "verdict"))
    call(server.reply(sent["id"], "complément"))
    call(server.send("claude", "avancement", topic="t", info=True))
    as_agent(monkeypatch, "claude")
    box = server._box()
    text = hook.notice(box, "conv-1") or ""
    assert "1 message(s)" in text and "t (codex, 1)" in text
    assert hook.notice(box, "conv-1") is None and hook.notice(box, "conv-2")  # une fois par conversation
    assert hook.notice(box, "conv-1", agent="codex") is None  # un pair est réveillé
    assert "late_replies" not in call(server.track(sent["id"]))  # jamais remis à une session qui fait autre chose
    first = call(server.inbox())
    assert [m["text"].splitlines()[1] for m in first["unread"]] == ["avancement"]
    wake.wait_done(sent["id"])
    assert hook.notice(box, "conv-1")  # la réponse n'est plus réservée : nouvelle pour la conversation
    second = call(server.inbox())
    assert {m["text"].splitlines()[1] for m in second["unread"]} == {"verdict", "complément"}
    assert not box.unread("claude") and hook.notice(box, "conv-3") is None


def test_reponse_tardive_rendue_meme_si_le_suivi_ne_s_ecrit_pas(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex, 24/10 : une erreur d'écriture du suivi consommait la réponse (lue) sans la rendre."""
    monkeypatch.setattr(server, "WAIT_POLL_S", 0.05)
    sent = call(server.send("codex", "q", topic="t"))
    assert json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=1)))["status"] == "timeout"
    as_agent(monkeypatch, "codex")
    call(server.reply(sent["id"], "verdict"))
    as_agent(monkeypatch, "claude")
    real = server.Box.set_state

    def broken(self, name: str, data: dict) -> None:  # type: ignore[no-untyped-def]
        if name == "abandoned-waits":
            raise OSError("disque plein")
        real(self, name, data)

    monkeypatch.setattr(server.Box, "set_state", broken)
    assert "verdict" in call(server.inbox())["late_replies"][0]["text"]


def test_collecte_tardive_ne_prend_pas_une_reponse_reservee(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex, 24/10 : une session reprend l'attente, la collecte d'une autre ne lui vole pas la réponse."""
    monkeypatch.setattr(server, "WAIT_POLL_S", 0.05)
    sent = call(server.send("codex", "q", topic="t"))
    assert json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=1)))["status"] == "timeout"
    wake.wait_on(sent["id"], time.time() + 60)  # une session a relancé wait_reply
    as_agent(monkeypatch, "codex")
    call(server.reply(sent["id"], "verdict"))
    as_agent(monkeypatch, "claude")
    assert "late_replies" not in call(server.inbox())
    wake.wait_done(sent["id"])
    assert "verdict" in call(server.inbox())["late_replies"][0]["text"]


def test_reponse_obtenue_gardee_si_la_reservation_ne_se_libere_pas(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex, 24/10 : une erreur de wait_done dans le finally remplaçait la réponse lue par une erreur."""
    sent = call(server.send("codex", "q", topic="t"))
    as_agent(monkeypatch, "codex")
    call(server.reply(sent["id"], "verdict"))
    as_agent(monkeypatch, "claude")

    def broken(question_id: str) -> None:
        raise OSError("disque plein")

    monkeypatch.setattr(wake, "wait_done", broken)
    result = json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=1)))
    assert result["status"] == "success" and "verdict" in result["reply"]["text"]


def test_issues_et_votes_en_anglais(monkeypatch: pytest.MonkeyPatch):
    """Descriptions publiques en anglais : done/refused/failed et agree/disagree rangés sous les issues d'origine."""
    first = call(server.send("codex", "q1", topic="t"))
    second = call(server.send("codex", "q2", topic="t"))
    third = call(server.send("codex", "q3", topic="t"))
    as_agent(monkeypatch, "codex")
    call(server.reply(first["id"], "No", state="Refused"))
    call(server.reply(second["id"], "Tried", state="failed"))
    agreed = call(server.reply(third["id"], "Fine", vote="agree"))
    as_agent(monkeypatch, "claude")
    assert call(server.track(first["id"]))["state"] == "répondu : refusé"
    assert call(server.track(second["id"]))["state"] == "répondu : échoué"
    assert call(server.track(third["id"]))["state"] == "répondu"  # done par défaut
    assert call(server.read(agreed["id"]))["message"]["vote"] == "accord"
