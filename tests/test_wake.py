"""La sonnette du démon : groupée, retentée, reprise après relance, jamais pour une réponse attendue."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from nexus_relay import wake
from nexus_relay.box import Box
from nexus_relay.wake import tidy_codex as real_tidy_codex  # avant que conftest ne le neutralise


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def bell_with(results: list[dict[str, Any]], clock: Clock) -> tuple[wake.Bell, list[list[str]]]:
    calls: list[list[str]] = []

    def waker(ids: list[str]) -> dict[str, Any]:
        calls.append(ids)
        return results.pop(0) if results else {"status": "started"}

    return wake.Bell(Box(), {"codex": waker, "claude": waker, "antigravity": waker}, clock), calls


def test_rafale_groupee_en_un_seul_reveil():
    clock = Clock()
    bell, calls = bell_with([], clock)
    box = bell.box
    a, b = box.post("claude", "codex", "1", "t"), box.post("antigravity", "codex", "2", "t")
    assert bell.ring(a) and bell.ring(b)
    assert bell.step("codex") is None  # rafale encore ouverte
    clock.t += wake.GROUP_S
    record = bell.step("codex")
    assert record and record["status"] == "started" and calls == [sorted([a.id, b.id])]
    assert bell.pending("codex") == [] and box.has_event(a.id, "réveil")


def test_reporte_puis_retente():
    clock = Clock()
    bell, calls = bell_with([{"status": "deferred", "reason": "L'utilisateur est dans Codex"}], clock)
    message = bell.box.post("claude", "codex", "x", "t")
    bell.ring(message)
    clock.t += wake.GROUP_S
    assert bell.step("codex")["status"] == "deferred"  # type: ignore[index]
    clock.t += 1
    assert bell.step("codex") is None  # trop tôt
    clock.t += wake.RETRY_S
    assert bell.step("codex")["status"] == "started"  # type: ignore[index]
    assert len(calls) == 2


def test_message_lu_entre_temps_ne_sonne_plus():
    clock = Clock()
    bell, calls = bell_with([], clock)
    message = bell.box.post("claude", "codex", "x", "t")
    bell.ring(message)
    bell.box.mark_read(message)
    clock.t += wake.GROUP_S
    assert bell.step("codex") is None and calls == []


def test_pas_de_reveil_pour_info_ou_reponse_attendue():
    bell, _ = bell_with([], Clock())
    box = bell.box
    question = box.post("claude", "codex", "q", "t")
    assert not bell.ring(box.post("claude", "codex", "x", "t", info=True))
    assert not bell.ring(box.post("claude", "antigravity", "x", "t", info=True))
    assert bell.ring(box.post("claude", "antigravity", "x", "t"))
    wake.wait_on(question.id, 10**12)
    try:
        assert not bell.ring(box.post("codex", "claude", "r", "t", reply_to=question.id))
    finally:
        wake.wait_done(question.id)
    assert not bell.ring(box.post("codex", "claude", "r2", "t", reply_to=question.id))  # Claude jamais sonné (20/10)


def test_abandon_apres_une_heure():
    clock = Clock()
    bell, calls = bell_with([{"status": "failed", "reason": "port muet"}], clock)
    message = bell.box.post("claude", "codex", "x", "t")
    bell.ring(message)
    clock.t += wake.GROUP_S
    bell.step("codex")
    clock.t += wake.GIVE_UP_S
    assert bell.step("codex")["status"] == "abandoned"  # type: ignore[index]
    assert len(calls) == 1 and bell.pending("codex") == []


def test_demon_relance_reprend_les_messages_non_sonnes():
    import time

    bell, calls = bell_with([], Clock())
    bell.clock = time.time
    message = bell.box.post("claude", "codex", "x", "t")
    assert bell.recover() == 1
    assert bell.step("codex")["status"] == "started" and calls == [[message.id]]  # type: ignore[index]
    again = wake.Bell(bell.box, bell.wakers, time.time)  # démon relancé
    assert again.recover() == 1 and again.pending("codex") == []  # déjà réveillé : surveillé, pas resonné
    assert message.id in again.watching("codex")


def test_un_echec_est_retente_au_plus_toutes_les_20_s():
    clock = Clock()
    bell, calls = bell_with([{"status": "failed", "reason": "champ"}, {"status": "failed", "reason": "champ"}], clock)
    bell.ring(bell.box.post("claude", "codex", "x", "t"))
    clock.t += wake.GROUP_S
    bell.step("codex")
    for _ in range(5):
        clock.t += 3
        bell.step("codex")
    assert len(calls) == 1
    clock.t += wake.RETRY_S
    bell.step("codex")
    assert len(calls) == 2


def test_reveil_non_lu_resonne_deux_fois_meme_id_puis_incident():
    clock = Clock()
    bell, calls = bell_with([], clock)
    message = bell.box.post("claude", "codex", "x", "t")
    bell.ring(message)
    clock.t += wake.GROUP_S
    bell.step("codex")
    for _ in range(2):
        clock.t += wake.CONFIRM_S
        bell.step("codex")  # relance échue : remis en attente puis sonné au même pas
    assert calls == [[message.id]] * 3
    assert (bell.box.ring_state(message.id) or {})["attempt"] == wake.MAX_RINGS
    clock.t += wake.CONFIRM_S
    assert bell.step("codex") is None and len(calls) == 3
    assert "non lu après 3 réveils" in (bell.box.ring_state(message.id) or {})["incident"]


def test_lu_sans_reponse_jamais_resonne():
    clock = Clock()
    bell, calls = bell_with([], clock)
    message = bell.box.post("claude", "codex", "x", "t")
    bell.ring(message)
    clock.t += wake.GROUP_S
    bell.step("codex")
    bell.box.mark_read(message)
    clock.t += wake.CONFIRM_S * 3
    bell.step("codex")
    assert len(calls) == 1 and bell.watching("codex") == {}


def test_compteur_et_echeance_survivent_au_redemarrage():
    clock = Clock()
    bell, calls = bell_with([], clock)
    message = bell.box.post("claude", "codex", "x", "t")
    clock.t = wake._epoch(message.created)
    bell.ring(message)
    clock.t += wake.GROUP_S
    bell.step("codex")
    ring = bell.box.ring_state(message.id) or {}
    assert ring["attempt"] == 1 and ring["recheck_at"] == clock.t + wake.CONFIRM_S
    clock.t += 10  # nouveau démon, même salon
    again = wake.Bell(bell.box, bell.wakers, clock)
    assert again.recover() == 1 and again.watching("codex") == {message.id: ring["recheck_at"]}
    again.step("codex")
    assert len(calls) == 1  # échéance pas encore venue
    clock.t = ring["recheck_at"]
    again.step("codex")
    assert len(calls) == 2 and (again.box.ring_state(message.id) or {})["attempt"] == 2


def test_reports_de_plus_en_plus_espaces_et_dedoublonnes():
    clock = Clock()
    deferred = {"status": "deferred", "reason": "appli Codex au premier plan (l'utilisateur y travaille)"}
    bell, calls = bell_with([dict(deferred) for _ in range(20)], clock)
    message = bell.box.post("claude", "codex", "x", "t")
    bell.ring(message)
    clock.t += wake.GROUP_S
    for _ in range(90):  # 30 min, un pas toutes les 20 s (vu le 20/10 : 87 essais)
        bell.step("codex")
        clock.t += 20
    assert 8 <= len(calls) <= 12
    reveils = [e for e in bell.box.events(message.id) if e.get("kind") == "réveil"]
    assert len(reveils) == 1  # même report : une seule ligne, le compte est dans wake-codex
    assert (bell.box.state("wake-codex") or {})["streak"] == len(calls)


def test_retry_delay():
    assert wake.retry_delay({}) == wake.RETRY_S
    assert round(wake.retry_delay({"streak": 2}), 2) == round(wake.RETRY_S * wake.PHI, 2)
    assert wake.retry_delay({"streak": 30}) == wake.RETRY_MAX_S
    assert wake.retry_delay({"streak": 1, "retry_s": 120}) == wake.RETRY_S  # plus de plancher « l'utilisateur a agi »


def test_un_fil_par_destinataire_et_un_fil_de_rangement_figes_visibles():
    """Relectures du 20/10 : un seul fil pour tout, figé par un réveil de Codex bloqué, arrêtait aussi Antigravity et
    les rangements, sans rien dire. Un fil par destinataire, un pour le rangement ; un fil figé ou jamais démarré se
    voit (status)."""
    bell, _ = bell_with([], Clock())
    assert set(bell._events) == {*wake.WAKEABLE, wake.TIDY}
    every = sorted([*wake.WAKEABLE, wake.TIDY])
    assert bell.health(now=0.0) == {"ages_s": {}, "stuck": [], "missing": every}  # jamais démarrés : pas « sains »
    assert wake.STUCK_S == 420.0  # une relance de Codex peut durer ~3 min : pas « figé » pour autant
    bell.beats.update({"codex": 1000.0, "antigravity": 1000.0 - wake.STUCK_S - 1})
    health = bell.health(now=1000.0)
    assert health["ages_s"] == {"codex": 0.0, "antigravity": wake.STUCK_S + 1}
    assert health["stuck"] == ["antigravity"] and health["missing"] == [wake.TIDY]
    bell.beats[wake.TIDY] = 1000.0 - 180.0  # 3 min : encore sain
    assert bell.health(now=1000.0)["stuck"] == ["antigravity"] and bell.health(now=1000.0)["missing"] == []


# --- rangement des chats du relais dans Codex
def _codex_tidy(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[dict[str, Any]], chats: list[Any] | None = None
) -> list[Any]:
    from nexus_relay import cdp, codex_app

    calls: list[Any] = []
    found = [codex_app.Chat("c1", Path("rollout.jsonl"), True, 0.0)] if chats is None else chats
    monkeypatch.setattr(codex_app, "relay_chats", lambda root=None, now=None: found)
    # journaux simulés par l'objet Chat (le vrai `ready` relit le fichier : voir test_codex_app)
    monkeypatch.setattr(
        codex_app, "ready", lambda chat, now, grace_s: chat.finished and (chat.idle_s(now) or 0) >= grace_s
    )
    monkeypatch.setattr(cdp, "user_in_codex", lambda: False)

    def archive(chats: list[Any], root: Any = None, grace_s: float = 10.0, eligible: Any = None) -> dict[str, Any]:
        calls.append(chats)
        return outcomes.pop(0)

    monkeypatch.setattr(codex_app, "archive_chats", archive)
    return calls


def test_rangement_codex_rien_sans_chat_pret(monkeypatch: pytest.MonkeyPatch):
    from nexus_relay import codex_app

    box = Box()
    calls = _codex_tidy(monkeypatch, [], chats=[])
    assert real_tidy_codex(box, now=1000.0) == {} and calls == []
    busy = codex_app.Chat("c1", Path("rollout.jsonl"), False, None)  # tour en cours
    calls = _codex_tidy(monkeypatch, [], chats=[busy])
    assert real_tidy_codex(box, now=1000.0) == {} and calls == []
    recent = codex_app.Chat("c1", Path("rollout.jsonl"), True, 1000.0 - wake.TIDY_S + 1)  # fini il y a 9 s
    calls = _codex_tidy(monkeypatch, [], chats=[recent])
    assert real_tidy_codex(box, now=1000.0) == {} and calls == [] and box.state("codex-tidy") is None


def test_rangement_codex_un_chat_pret_suffit(monkeypatch: pytest.MonkeyPatch):
    """Tour 2 : un chat figé ne bloque plus les autres ; un seul chat prêt déclenche le rangement."""
    from nexus_relay import codex_app

    stuck = codex_app.Chat("fige", Path("a.jsonl"), False, None)
    done = codex_app.Chat("fini", Path("b.jsonl"), True, 0.0)
    calls = _codex_tidy(monkeypatch, [{"status": "archived"}], chats=[stuck, done])
    assert real_tidy_codex(Box(), now=1000.0)["status"] == "archived" and calls == [[stuck, done]]


def test_rangement_codex_reporte_quand_utilisateur_est_dans_codex(monkeypatch: pytest.MonkeyPatch):
    from nexus_relay import cdp

    box = Box()
    calls = _codex_tidy(monkeypatch, [])
    monkeypatch.setattr(cdp, "user_in_codex", lambda: True)
    assert real_tidy_codex(box, now=1000.0)["status"] == "deferred" and calls == []
    assert box.state("codex-tidy") is None  # un report n'est pas un échec


def test_rangement_codex_pause_croissante_apres_echec_puis_remise_a_zero(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex du 20/10 : un chat impossible à ranger était retenté toutes les 2 à 10 s pendant 7 jours."""
    box = Box()
    failed = {"status": "failed", "clicked": 1, "archived": []}
    calls = _codex_tidy(monkeypatch, [dict(failed), dict(failed), {"status": "archived"}])
    assert real_tidy_codex(box, now=1000.0)["status"] == "failed"
    state = box.state("codex-tidy") or {}
    assert state["failures"] == 1 and state["next_at"] == 1000.0 + wake.RETRY_S
    assert real_tidy_codex(box, now=1000.0 + wake.RETRY_S - 1) == {} and len(calls) == 1  # pause pas finie
    second = 1000.0 + wake.RETRY_S
    real_tidy_codex(box, now=second)
    state = box.state("codex-tidy") or {}
    assert state["failures"] == 2 and round(state["next_at"] - second, 3) == round(wake.RETRY_S * wake.PHI, 3)
    third = state["next_at"]
    assert real_tidy_codex(box, now=third)["status"] == "archived" and len(calls) == 3
    state = box.state("codex-tidy") or {}
    assert state["failures"] == 0 and state["next_at"] == third  # remis à zéro : plus de pause


@pytest.mark.parametrize("status", ["deferred", "skipped", "nothing", "partial"])
def test_rangement_codex_un_report_ne_compte_pas(monkeypatch: pytest.MonkeyPatch, status: str):
    """Tour 2 : un report (port occupé, l'utilisateur revenu, rien à ranger) comptait comme un échec et allongeait la pause."""
    box = Box()
    box.set_state("codex-tidy", {"failures": 2, "next_at": 0})
    calls = _codex_tidy(monkeypatch, [{"status": status}, {"status": status}])
    real_tidy_codex(box, now=1000.0)
    state = box.state("codex-tidy") or {}
    pause = 0.0 if status == "partial" else 60.0  # tour 3 : pas compté, mais pas toutes les 10 s
    assert state["failures"] == 2 and state["next_at"] == 1000.0 + pause
    real_tidy_codex(box, now=1000.0 + pause)
    assert len(calls) == 2  # retenté au pas suivant


def test_rangement_codex_pause_bornee_a_une_heure(monkeypatch: pytest.MonkeyPatch):
    box = Box()
    box.set_state("codex-tidy", {"failures": 29, "next_at": 0})
    _codex_tidy(monkeypatch, [{"status": "failed", "reason": "port fermé"}])
    real_tidy_codex(box, now=1000.0)
    state = box.state("codex-tidy") or {}
    assert state["failures"] == 30 and state["next_at"] == 1000.0 + 3600.0


# --- tour d'Antigravity lu dans son journal, et rangement de sa conversation
CONV = "33333333-3333-3333-3333-333333333333"
CONV2 = "44444444-4444-4444-4444-444444444444"


def _stamp(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def _transcript(conversation: str, *steps: dict[str, Any] | str, end: str = "\n") -> Path:
    from nexus_relay.paths import gemini_home

    path = gemini_home() / "antigravity" / "brain" / conversation / ".system_generated" / "logs" / "transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(s if isinstance(s, str) else json.dumps(s) for s in steps) + end, encoding="utf-8")
    return path


def _done(at: float, **extra: Any) -> dict[str, Any]:
    return {"type": "PLANNER_RESPONSE", "status": "DONE", "created_at": _stamp(at), **extra}


def test_tour_d_antigravity_lu_a_la_fin_de_son_journal():
    """Relecture de Codex du 20/10 : rangée dès « reply », parfois en plein travail ; seule une réponse terminée sans
    appel d'outil finit le tour, et seule la DERNIÈRE ligne compte (tour 2)."""
    from nexus_relay.paths import agy_turn

    assert agy_turn(CONV) == (False, None)  # pas de journal
    at = 1_760_000_000.0
    user = {"type": "USER_INPUT", "status": "DONE", "created_at": _stamp(at - 5)}
    _transcript(CONV, user, _done(at))
    assert agy_turn(CONV) == (True, at)
    _transcript(CONV, user, _done(at, tool_calls=[{"name": "run_command"}]))
    assert agy_turn(CONV) == (False, at)  # réponse qui appelle un outil : il travaille
    _transcript(CONV, user, {**_done(at), "status": "RUNNING"})
    assert agy_turn(CONV)[0] is False
    _transcript(CONV, _done(at), user)
    assert agy_turn(CONV)[0] is False  # nouvelle demande après la réponse
    _transcript(CONV, user, _done(at), '{"type": "PLANNER_RESP')
    assert agy_turn(CONV) == (False, None)  # dernière ligne illisible : jamais l'ancienne fin


def test_etape_en_cours_d_ecriture_ne_ressuscite_pas_l_ancienne_fin():
    """Tour 2 : fin de fichier sans retour à la ligne = étape en cours d'écriture ; l'ancien « DONE » d'avant ne vaut
    plus rien (rangée en plein travail)."""
    from nexus_relay.paths import agy_turn

    at = 1_760_000_000.0
    _transcript(CONV, _done(at), '{"type": "PLANNER_RESPONSE", "status": "RUN', end="")
    assert agy_turn(CONV) == (False, None)
    _transcript(CONV, _done(at), end="")  # même une ligne complète, tant que le retour à la ligne manque
    assert agy_turn(CONV) == (False, None)
    _transcript(CONV, _done(at))
    assert agy_turn(CONV) == (True, at)


def test_derniere_ligne_plus_longue_que_la_fenetre_lue():
    """Tour 2 : une réponse de 200 Kio dépassait la fenêtre de 64 Kio ; la fenêtre grandit (×4, 8 Mio au plus)."""
    from nexus_relay.paths import agy_turn

    at = 1_760_000_000.0
    big = _done(at, content="x" * 300_000)
    _transcript(CONV, {"type": "USER_INPUT", "status": "DONE", "created_at": _stamp(at - 5)}, big)
    assert agy_turn(CONV) == (True, at)
    _transcript(CONV, _done(at), {**big, "tool_calls": [{"name": "write_file"}]})
    assert agy_turn(CONV) == (False, at)


def _agy_world(monkeypatch: pytest.MonkeyPatch, topic: str = "t", conversation: str = CONV) -> tuple[Box, list[str]]:
    """Sujet fini pour Antigravity, conversation vivante, tour fini depuis 1 min ; « Archive » simulé par son port."""
    from nexus_relay import cdp
    from nexus_relay.paths import gemini_home

    box = Box()
    question = box.post("claude", "antigravity", "Question", topic)
    box.mark_read(question)
    box.post("antigravity", "claude", "Réponse", topic, reply_to=question.id)
    _alive(conversation)
    box.set_topic_state(topic, "agy", {"conversation": conversation})
    box.set_topic_state(topic, "closed", {"closed": True})  # sujet clos par l'hôte : à ranger (règle du 21/10)
    _transcript(conversation, _done(time.time() - 60))
    monkeypatch.setattr(cdp, "user_in_agy", lambda: False)  # jamais la vraie fenêtre
    clicked: list[str] = []

    def archive(conv: str, guard: Any, port: int = 0) -> tuple[str, str | None]:
        if reason := guard():  # garde relue juste avant le clic
            return "skipped", reason
        clicked.append(conv)
        note = gemini_home() / "antigravity" / "annotations" / f"{conv}.pbtxt"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("archived:true", encoding="utf-8")
        return "clicked", None

    monkeypatch.setattr(cdp, "archive_antigravity_conversation", archive)
    return box, clicked


def _alive(conversation: str) -> None:
    from nexus_relay.paths import gemini_home

    db = gemini_home() / "antigravity" / "conversations" / f"{conversation}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    (gemini_home() / "antigravity" / "annotations" / f"{conversation}.pbtxt").unlink(missing_ok=True)


def test_rangement_d_antigravity_verifie_puis_conversation_suivante(monkeypatch: pytest.MonkeyPatch):
    """Vu le 20/10 : un sujet rangé une fois (« selftest ») ne l'était plus jamais, même avec une conversation neuve."""
    box, clicked = _agy_world(monkeypatch)
    assert wake.tidy_antigravity(box, verify_s=0.0) == {"t": {"status": "archived", "reason": None}}
    assert (box.topic_state("t", "agy_archived") or {})["conversation"] == CONV and clicked == [CONV]
    assert wake.tidy_antigravity(box) == {} and clicked == [CONV]  # rangée : plus rien
    question = box.post("claude", "antigravity", "Nouvelle question", "t")  # nouvelle conversation du sujet
    box.mark_read(question)
    box.post("antigravity", "claude", "Réponse", "t", reply_to=question.id)
    _alive(CONV2)
    _transcript(CONV2, _done(time.time() - 60))
    box.set_topic_state("t", "agy", {"conversation": CONV2})
    assert wake.tidy_antigravity(box, verify_s=0.0)["t"]["status"] == "archived" and clicked == [CONV, CONV2]


def test_rangement_d_antigravity_attend_la_fin_de_l_echange_et_du_tour(monkeypatch: pytest.MonkeyPatch):
    box, clicked = _agy_world(monkeypatch)
    pending = box.post("claude", "antigravity", "Encore une question", "t")
    assert wake.tidy_antigravity(box) == {} and clicked == []  # échange pas fini
    box.mark_read(pending)
    box.post("antigravity", "claude", "Réponse", "t", reply_to=pending.id)
    _transcript(CONV, _done(time.time() - 60, tool_calls=[{"name": "view_file"}]))
    assert wake.tidy_antigravity(box) == {} and clicked == []  # il travaille encore
    _transcript(CONV, _done(time.time() - 2))
    assert wake.tidy_antigravity(box) == {} and clicked == []  # tour fini il y a moins de TIDY_S
    assert box.topic_state("t", "agy_archive_tries") is None and box.topic_state("t", "agy_archived") is None


def test_rangement_d_antigravity_garde_relue_avant_le_clic(monkeypatch: pytest.MonkeyPatch):
    """Une demande arrivée entre le premier contrôle et le clic arrête tout, sans compter d'échec."""
    from nexus_relay import cdp

    box, clicked = _agy_world(monkeypatch)
    archive = cdp.archive_antigravity_conversation

    def late(conv: str, guard: Any, port: int = 0) -> tuple[str, str | None]:
        box.post("claude", "antigravity", "Arrivée juste avant le clic", "t")
        return archive(conv, guard)

    monkeypatch.setattr(cdp, "archive_antigravity_conversation", late)
    assert wake.tidy_antigravity(box) == {"t": {"status": "skipped", "reason": "échange pas fini"}}
    assert clicked == [] and box.topic_state("t", "agy_archive_tries") is None


def test_rangement_d_antigravity_reports_jamais_comptes(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex du 20/10 : trois reports (l'utilisateur dans l'appli, port fermé) abandonnaient pour toujours."""
    from nexus_relay import cdp

    box, _ = _agy_world(monkeypatch)
    monkeypatch.setattr(cdp, "archive_antigravity_conversation", lambda conv, guard: ("skipped", "port 9227 fermé"))
    for _ in range(wake.AGY_ARCHIVE_TRIES + 2):
        assert wake.tidy_antigravity(box)["t"]["status"] == "skipped"
    assert box.topic_state("t", "agy_archived") is None and box.topic_state("t", "agy_archive_tries") is None


def test_rangement_d_antigravity_vrais_echecs_comptes_puis_abandon(monkeypatch: pytest.MonkeyPatch):
    from nexus_relay import cdp

    box, _ = _agy_world(monkeypatch)
    monkeypatch.setattr(cdp, "archive_antigravity_conversation", lambda conv, guard: ("clicked", None))  # sans effet
    for tries in range(1, wake.AGY_ARCHIVE_TRIES):
        result = wake.tidy_antigravity(box, verify_s=0.0)["t"]
        assert result["status"] == "failed" and "toujours active" in result["reason"]
        assert (box.topic_state("t", "agy_archive_tries") or {})["count"] == tries
        assert box.topic_state("t", "agy_archived") is None
    assert wake.tidy_antigravity(box, verify_s=0.0)["t"]["status"] == "failed"
    done = box.topic_state("t", "agy_archived") or {}
    assert done["conversation"] == CONV and done["tries"] == wake.AGY_ARCHIVE_TRIES and done["status"] == "failed"
    assert wake.tidy_antigravity(box, verify_s=0.0) == {}  # abandonné : plus retenté


def test_rangement_d_antigravity_conversation_deja_archivee(monkeypatch: pytest.MonkeyPatch):
    from nexus_relay.paths import gemini_home

    box, clicked = _agy_world(monkeypatch)
    (gemini_home() / "antigravity" / "conversations" / f"{CONV}.db").unlink()  # disparue ou rangée par l'utilisateur
    assert wake.tidy_antigravity(box) == {} and clicked == []
    assert (box.topic_state("t", "agy_archived") or {})["by"] == "déjà"


def test_rangement_d_antigravity_jamais_devant_utilisateur(monkeypatch: pytest.MonkeyPatch):
    """Tour 2 : la garde relue avant le clic regarde aussi si l'utilisateur est dans Antigravity ; report, jamais compté."""
    from nexus_relay import cdp

    box, clicked = _agy_world(monkeypatch)
    monkeypatch.setattr(cdp, "user_in_agy", lambda: True)
    assert wake.tidy_antigravity(box) == {} and clicked == []  # dès le premier contrôle
    archive = cdp.archive_antigravity_conversation
    here = iter([False, True])  # l'utilisateur arrive entre le premier contrôle et le clic
    monkeypatch.setattr(cdp, "user_in_agy", lambda: next(here, True))
    monkeypatch.setattr(cdp, "archive_antigravity_conversation", lambda conv, guard: archive(conv, guard))
    assert wake.tidy_antigravity(box) == {"t": {"status": "skipped", "reason": "L'utilisateur est dans Antigravity"}}
    assert clicked == [] and box.topic_state("t", "agy_archive_tries") is None


def test_rangement_d_antigravity_ligne_absente_reportee_24_h_puis_comptee(monkeypatch: pytest.MonkeyPatch):
    """Tour 2 : ligne pas affichée (autre projet ouvert, barre repliée) : trois passages en 30 s l'abandonnaient pour
    toujours ; c'est un report pendant 24 h (date du premier essai gardée), un échec ensuite."""
    from nexus_relay import cdp

    box, _ = _agy_world(monkeypatch)
    monkeypatch.setattr(cdp, "archive_antigravity_conversation", lambda conv, guard: ("absent", None))
    start = time.time()
    for _ in range(wake.AGY_ARCHIVE_TRIES + 2):
        assert wake.tidy_antigravity(box, now=start)["t"]["status"] == "absent"
    assert box.topic_state("t", "agy_archive_tries") is None and box.topic_state("t", "agy_archived") is None
    first = box.topic_state("t", "agy_archive_first") or {}
    assert first == {"conversation": CONV, "at": start}
    later = start + wake.AGY_GIVE_UP_S + 1
    wake.tidy_antigravity(box, now=later)
    assert (box.topic_state("t", "agy_archive_tries") or {})["count"] == 1  # désormais compté
    for _ in range(wake.AGY_ARCHIVE_TRIES - 1):
        wake.tidy_antigravity(box, now=later)
    done = box.topic_state("t", "agy_archived") or {}
    assert done["status"] == "absent" and done["tries"] == wake.AGY_ARCHIVE_TRIES


def test_tour_d_antigravity_fini_sur_erreur():
    """Tour 3 : un tour qui s'arrête sur une erreur est fini (sinon jamais rangé)."""
    from nexus_relay.paths import agy_turn

    at = 1_760_000_000.0
    _transcript(CONV, {"type": "ERROR_MESSAGE", "status": "DONE", "created_at": _stamp(at)})
    assert agy_turn(CONV) == (True, at)


def test_sidecar_dont_la_boucle_est_figee_est_tenu_pour_mort():
    """Tour 3 : le battement a son propre fil ; seul, il ne prouve pas que la boucle de livraison tourne."""
    import os

    from nexus_relay.box import now_iso

    box = Box()
    box.set_state("sidecar", {"at": now_iso(), "pid": os.getpid()})
    box.set_state("sidecar-loop", {"at": now_iso()})
    assert wake.sidecar_alive(box)
    assert not wake.sidecar_alive(box, now=time.time() + wake.SIDECAR_LOOP_S + 1)


def test_rangement_codex_par_sujet(monkeypatch: pytest.MonkeyPatch):
    """21/10 (l'utilisateur) : une conversation par sujet. Le chat d'un sujet vivant n'est jamais rangé après une réponse ; il
    l'est une fois le sujet clos ou après 12 h sans message ; un chat sans sujet connu reste rangé une fois fini."""
    from nexus_relay import codex_app

    box = Box()
    box.post("claude", "codex", "Relis", "vivant")
    box.set_topic_state("vivant", "codex", {"conversation": "chat-vivant"})
    live = codex_app.Chat("chat-vivant", Path("a.jsonl"), True, 0.0)
    orphan = codex_app.Chat("chat-ancien", Path("b.jsonl"), True, 0.0)
    now = time.time()
    calls = _codex_tidy(monkeypatch, [{"status": "archived"}], chats=[live, orphan])
    assert real_tidy_codex(box, now=now)["status"] == "archived" and calls == [[orphan]]  # le vivant est gardé
    calls = _codex_tidy(monkeypatch, [], chats=[live])
    assert real_tidy_codex(box, now=now) == {} and calls == []
    calls = _codex_tidy(monkeypatch, [{"status": "archived"}], chats=[live])
    assert real_tidy_codex(box, now=now + wake.IDLE_S + 60)["status"] == "archived" and calls == [[live]]  # 12 h
    box.set_state("codex-tidy", {})
    box.set_topic_state("vivant", "closed", {"closed": True})
    calls = _codex_tidy(monkeypatch, [{"status": "archived"}], chats=[live])
    assert real_tidy_codex(box, now=now)["status"] == "archived" and calls == [[live]]  # clos par l'hôte


def test_rangement_d_antigravity_garde_un_sujet_actif(monkeypatch: pytest.MonkeyPatch):
    """Sujet fini pour Antigravity mais ni clos ni inactif 12 h : sa conversation est gardée (contexte, cache)."""
    box, clicked = _agy_world(monkeypatch, topic="actif")
    box.set_topic_state("actif", "closed", {"closed": False})
    assert wake.tidy_antigravity(box) == {} and clicked == []
    assert wake.tidy_antigravity(box, now=time.time() + wake.IDLE_S + 60)["actif"]["status"] == "archived"


def test_un_reveil_par_sujet(monkeypatch: pytest.MonkeyPatch):
    """Une conversation par sujet chez le destinataire : un réveil ne mélange jamais deux sujets ; l'autre suit."""
    box = Box()
    woken: list[list[str]] = []
    bell = wake.Bell(box, wakers={"codex": lambda ids: woken.append(list(ids)) or {"status": "started"}})
    first = box.post("claude", "codex", "a", "sujet-a")
    second = box.post("claude", "codex", "b", "sujet-b")
    third = box.post("claude", "codex", "c", "sujet-a")
    for message in (first, second, third):
        bell.ring(message)
    bell.clock = lambda: time.time() + wake.GROUP_S + 1
    bell.step("codex")
    bell.step("codex")
    assert woken == [sorted([first.id, third.id]), [second.id]]


def test_rangement_codex_reel_ne_range_que_le_sujet_clos(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex (21/10) : archive_chats repassait TOUS les chats du relais à archive_project ; un sujet clos
    faisait ranger aussi un sujet actif. Vraies fonctions tidy_codex → archive_chats → archive_project, interface
    simulée."""
    from nexus_relay import cdp, codex_app

    box = Box()
    for topic, chat in (("clos", "chat-clos"), ("actif", "chat-actif")):
        box.post("claude", "codex", "q", topic)
        box.set_topic_state(topic, "codex", {"conversation": chat})
    box.set_topic_state("clos", "closed", {"closed": True})
    chats = [
        codex_app.Chat("chat-clos", Path("a.jsonl"), True, 0.0),
        codex_app.Chat("chat-actif", Path("b.jsonl"), True, 0.0),
    ]
    monkeypatch.setattr(codex_app, "relay_chats", lambda root=None, now=None: list(chats))
    monkeypatch.setattr(codex_app, "ready", lambda chat, now, grace_s: True)
    clicked: list[str] = []
    monkeypatch.setattr(codex_app, "is_archived", lambda root, chat_id: chat_id in clicked)
    monkeypatch.setattr(cdp, "page_url", lambda port=0, url="": "ws://codex")
    monkeypatch.setattr(cdp, "Page", lambda url: type("P", (), {"close": lambda self: None})())
    monkeypatch.setattr(cdp, "user_in_codex", lambda: False)
    monkeypatch.setattr(cdp, "codex_mode", lambda page: "Codex")
    monkeypatch.setattr(cdp, "ensure_codex_mode", lambda page, sleep=None: True)
    monkeypatch.setattr(cdp, "selection_size", lambda page: 0)
    monkeypatch.setattr(
        cdp, "row_conversations", lambda page: [("r-clos", "chat-clos", False), ("r-actif", "chat-actif", False)]
    )

    def archive_thread(page: Any, row: str, guard: Any) -> tuple[str, str | None]:
        if reason := guard():
            return "skipped", reason
        clicked.append({"r-clos": "chat-clos", "r-actif": "chat-actif"}[row])
        return "clicked", None

    monkeypatch.setattr(cdp, "archive_thread", archive_thread)
    result = real_tidy_codex(box, now=time.time())
    assert result["status"] == "archived" and clicked == ["chat-clos"]


def test_rotation_des_sujets_et_pause_par_sujet():
    """Relecture de Codex (21/10) : un report propre au chat de A (« en plein tour ») bloquait B, jamais tenté."""
    box = Box()
    tried: list[str] = []

    def waker(ids: list[str]) -> dict[str, Any]:
        topic = box.get(ids[0]).topic  # type: ignore[union-attr]
        tried.append(topic)
        return (
            {"status": "deferred", "reason": "chat du sujet en plein tour"} if topic == "a" else {"status": "started"}
        )

    bell = wake.Bell(box, wakers={"codex": waker})
    for topic in ("a", "b"):
        bell.ring(box.post("claude", "codex", "q", topic))
    bell.clock = lambda: time.time() + wake.GROUP_S + 1
    bell.step("codex")
    bell.step("codex")
    bell.step("codex")
    assert tried == ["a", "b"]  # A en pause (la sienne), B tenté aussitôt ; A pas resonné avant sa pause


def test_antigravity_fige_relance(monkeypatch: pytest.MonkeyPatch):
    """Figé 2 min : relancé (l'utilisateur hors de l'appli), avant même de regarder le sidecar (qui peut battre encore)."""
    from nexus_relay import cdp

    calls: list[str] = []
    monkeypatch.setattr(cdp, "app_hung", lambda process: True)
    monkeypatch.setattr(cdp, "hung_for", lambda name, hung, now=None: cdp.HUNG_RESTART_S + 1)
    monkeypatch.setattr(
        cdp, "restart_frozen_antigravity", lambda port=0: calls.append("restart") or {"status": "failed"}
    )
    assert wake._antigravity(["x"])["status"] == "failed" and calls == ["restart"]


def test_reservation_survit_a_la_relance_du_demon():
    """Relecture de Codex (21/10) : WAITED n'était qu'en mémoire ; après « restarting », le démon relancé ne savait plus
    que la réponse était réservée (une autre session la lisait dans inbox)."""
    wake.wait_on("q-1", time.time() + 600)
    wake.WAITED.clear()  # nouveau processus : mémoire vide
    assert wake.awaited("q-1")
    wake.wait_done("q-1")
    assert not wake.awaited("q-1")
    wake.wait_on("q-2", time.time() - 1)  # échéance passée : plus réservée
    wake.WAITED.clear()
    assert not wake.awaited("q-2")


def test_activites_en_cours_nommees():
    """21/10 : une relance restait retenue (« 1 en cours ») sans qu'on sache par quoi."""
    activity = wake.Activity()
    token = activity.enter("wait_reply (claude)")
    assert [a["quoi"] for a in activity.running()] == ["wait_reply (claude)"]
    activity.leave(token)
    assert activity.running() == [] and activity.count == 0


def test_conversation_remplacee_rangee_sans_attendre_la_fin_du_sujet(monkeypatch: pytest.MonkeyPatch):
    """21/10 : deux « Nexus Relay · relais » chez Antigravity ; la cassée, remplacée, restait à côté de la neuve."""
    from nexus_relay import cdp, paths

    box = Box()
    box.set_topic_state("relais", "agy_retired", {"conversations": ["vieille", "disparue"]})
    clicked: list[str] = []
    monkeypatch.setattr(paths, "conversation_alive", lambda c: c == "vieille")
    monkeypatch.setattr(paths, "agy_turn", lambda c: (True, 0.0))
    monkeypatch.setattr(cdp, "user_in_agy", lambda: True)
    wake._tidy_retired(box, "relais", 1000.0)
    assert clicked == [] and box.topic_state("relais", "agy_retired") == {
        "conversations": ["vieille"]
    }  # l'utilisateur là
    monkeypatch.setattr(cdp, "user_in_agy", lambda: False)
    monkeypatch.setattr(
        cdp, "archive_antigravity_conversation", lambda c, guard: clicked.append(c) or ("clicked", None)
    )
    wake._tidy_retired(box, "relais", 1000.0)
    assert clicked == ["vieille"] and box.topic_state("relais", "agy_retired") == {"conversations": []}


def test_verdict_des_pairs_memes_signaux_que_le_reveil(monkeypatch: pytest.MonkeyPatch):
    """22/10 : la barre d'état de l'utilisateur lit le verdict du relais (state/peers.json), jamais une règle à part."""
    from nexus_relay import cdp, credits

    box = Box()
    monkeypatch.setattr(credits, "availability", lambda box, agent, **_: None)
    monkeypatch.setattr(wake, "sidecar_alive", lambda box: True)
    monkeypatch.setattr(cdp, "page_url", lambda *a, **k: "ws://127.0.0.1:9223/devtools/page/x")
    monkeypatch.setattr(cdp, "router_ready", lambda timeout: True)
    verdict = wake.peers_verdict(box)
    assert verdict["codex"] == {"ok": True} and verdict["antigravity"] == {"ok": True}
    monkeypatch.setattr(cdp, "page_url", lambda *a, **k: None)
    assert wake.peers_verdict(box)["codex"] == {"ok": False, "why": "appli Codex fermée"}
    monkeypatch.setattr(cdp, "page_url", lambda *a, **k: "ws://x")
    monkeypatch.setattr(cdp, "router_ready", lambda timeout: False)
    assert wake.peers_verdict(box)["codex"] == {"ok": False, "why": "routeur arrêté"}
    monkeypatch.setattr(wake, "sidecar_alive", lambda box: False)
    monkeypatch.setattr(
        credits, "availability", lambda box, agent, **_: "quota épuisé" if agent == "antigravity" else None
    )
    assert wake.peers_verdict(box)["antigravity"] == {"ok": False, "why": "quota épuisé"}
