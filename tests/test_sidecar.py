"""Le sidecar d'Antigravity : livraison par agentapi dans la conversation du sujet, et rien d'autre."""

from __future__ import annotations

import time
from typing import Any, cast

from nexus_relay.agentapi import AgentApiError
from nexus_relay.box import Box
from nexus_relay.sidecar import Sidecar, prompt_for

CONV_A = "11111111-1111-1111-1111-111111111111"
CONV_B = "22222222-2222-2222-2222-222222222222"


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.refuse: set[str] = set()
        self.fail = False
        self.uncertain = False
        self._next = [CONV_A, CONV_B]
        self.dead: set[str] = set()
        self.titles: list[str | None] = []

    def new_conversation(self, prompt: str, title: str) -> str:
        if self.fail:
            raise AgentApiError("agentapi muet")
        if self.uncertain:
            raise AgentApiError("agentapi new-conversation : délai dépassé", uncertain=True)
        conversation = self._next.pop(0)
        self.calls.append(("new", conversation, prompt))
        return conversation

    def send_message(self, conversation: str, prompt: str, title: str | None = None) -> None:
        if conversation in self.refuse:
            raise AgentApiError("conversation inconnue")
        self.calls.append(("send", conversation, prompt))
        self.titles.append(title)


def sidecar() -> tuple[Sidecar, Box, FakeApi]:
    box, api = Box(), FakeApi()
    return Sidecar(box, api, log=lambda _: None, alive=lambda c: c not in api.dead, paused=lambda: None), box, api  # type: ignore[arg-type]


def test_une_conversation_par_sujet_gardee():
    car, box, api = sidecar()
    first = box.post("claude", "antigravity", "q1", "nexuswindows")
    car.step()
    second = box.post("claude", "antigravity", "q2", "nexuswindows")
    car.step()
    assert [c[:2] for c in api.calls] == [("new", CONV_A), ("send", CONV_A)]
    assert f'read(id="{first.id}")' in api.calls[0][2] and f'read(id="{second.id}")' in api.calls[1][2]
    car.step()
    assert len(api.calls) == 2  # déjà livrés


def test_message_suivant_titre_lisible_dans_antigravity():
    """L'utilisateur, 24/10 : dans Antigravity, un message suivant du sujet n'apparaissait que comme « Message from System »
    (la question invisible) : on croyait une conversation par question. Son titre dit qui et quoi."""
    car, box, api = sidecar()
    box.post("claude", "antigravity", "q1", "relais")
    car.step()
    box.post("claude", "antigravity", "Relecture croisée du commit 2832bc7, sans rien modifier.\nDétail…", "relais")
    car.step()
    title = api.titles[-1] or ""
    assert title.startswith("Claude :") and "Relecture croisée du commit 2832bc7" in title and "\n" not in title
    assert api.calls[0][2].startswith("Claude : q1\n\n")  # premier message : la question en tête de la bulle


def test_caractere_nul_jamais_dans_la_ligne_de_commande():
    """Tour 2 de Codex : un NUL dans la première ligne faisait lever Popen, et la livraison passait « incertaine »."""
    car, box, api = sidecar()
    box.post("claude", "antigravity", "Question\x00suite", "relais")
    assert car.step()[0]["status"] == "delivered"
    assert "\x00" not in api.calls[0][2]


def test_agentapi_valeur_refusee_par_popen_echec_connu(monkeypatch: Any):
    from nexus_relay import agentapi

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("embedded null character")

    monkeypatch.setattr(agentapi.subprocess, "Popen", refuse)
    try:
        agentapi.AgentApi(["agentapi"])._run(["send-message", CONV_A, "x"])
    except AgentApiError as error:
        assert not error.uncertain  # rien n'est parti : renvoyable
    else:
        raise AssertionError("ValueError non convertie")


def test_titre_borne_meme_au_premier_message():
    """Relecture de Codex : une première ligne de 40 000 caractères dépassait la ligne de commande de Windows."""
    car, box, api = sidecar()
    box.post("claude", "antigravity", "x" * 40_000, "relais")
    car.step()
    head = api.calls[0][2].split("\n\n", 1)[0]
    assert len(head) <= 144 and head.endswith("…")


def test_agentapi_passe_le_titre(monkeypatch: Any):
    from nexus_relay import agentapi

    seen: list[list[str]] = []
    api = agentapi.AgentApi(["agentapi"])
    monkeypatch.setattr(api, "_run", lambda args: seen.append(args) or {})
    api.send_message(CONV_A, "texte", title="Claude : Relecture   croisée")
    assert seen[0] == ["send-message", "--title=Claude : Relecture croisée", CONV_A, "texte"]
    api.send_message(CONV_A, "texte")
    assert seen[1] == ["send-message", CONV_A, "texte"]


def test_pour_info_attend_le_prochain_message_du_sujet():
    car, box, api = sidecar()
    info = box.post("claude", "antigravity", "pour info", "t", info=True)
    car.step()
    assert api.calls == []
    ask = box.post("claude", "antigravity", "question", "t")
    car.step()
    assert len(api.calls) == 1 and info.id in api.calls[0][2] and ask.id in api.calls[0][2]


def test_conversation_refusee_on_en_ouvre_une_neuve():
    car, box, api = sidecar()
    box.set_topic_state("t", "agy", {"conversation": CONV_B})
    api.refuse.add(CONV_B)
    box.post("claude", "antigravity", "q", "t")
    car.step()
    assert api.calls[0][:2] == ("new", CONV_A)
    assert (box.topic_state("t", "agy") or {})["conversation"] == CONV_A


def test_conversation_archivee_on_en_ouvre_une_neuve():
    """Vu le 20/10 : messages envoyés à la conversation archivée du sujet, acceptés puis jamais livrés."""
    car, box, api = sidecar()
    box.post("claude", "antigravity", "q1", "t")
    car.step()
    api.dead.add(CONV_A)  # le relais a rangé la conversation du sujet
    box.post("claude", "antigravity", "q2", "t")
    car.step()
    assert [c[:2] for c in api.calls] == [("new", CONV_A), ("new", CONV_B)]
    assert (box.topic_state("t", "agy") or {})["conversation"] == CONV_B


def test_conversation_vivante_selon_antigravity(tmp_path, monkeypatch):
    from nexus_relay.paths import conversation_alive

    root = tmp_path / "gemini" / "antigravity"
    monkeypatch.setenv("NEXUS_RELAY_GEMINI_HOME", str(tmp_path / "gemini"))
    (root / "conversations").mkdir(parents=True)
    (root / "annotations").mkdir()
    assert not conversation_alive(CONV_A)  # disparue
    (root / "conversations" / f"{CONV_A}.db").write_bytes(b"")
    assert conversation_alive(CONV_A)
    (root / "annotations" / f"{CONV_A}.pbtxt").write_text('title:"Nexus Relay · t" archived:true', encoding="utf-8")
    assert not conversation_alive(CONV_A)  # archivée


def test_echec_retente_plus_tard():
    car, box, api = sidecar()
    api.fail = True
    message = box.post("claude", "antigravity", "q", "t")
    assert car.step()[0]["status"] == "failed" and box.has_event(message.id, "livraison ratée")
    api.fail = False
    assert car.step() == []  # attente de RETRY_S
    car._failed.clear()
    assert car.step()[0]["status"] == "delivered"


def test_battement_et_message_court():
    car, box, _ = sidecar()
    car.beat()
    assert (box.state("sidecar") or {}).get("pid")
    car.step()
    assert (box.state("sidecar-loop") or {}).get("at")  # boucle vivante (tour 3)
    message = box.post("claude", "antigravity", "x" * 5000, "t")
    assert len(prompt_for([message])) < 400 and "x" * 50 not in prompt_for([message])  # jamais le contenu


def test_prise_concurrente_un_seul_proprietaire():
    import threading

    box = Box()
    message = box.post("claude", "antigravity", "q", "t")
    start = threading.Barrier(16)
    won: list[str] = []

    def take(owner: str) -> None:
        start.wait()
        if box.claim(message.id, 1, owner):
            won.append(owner)

    threads = [threading.Thread(target=take, args=(f"voie{i}",)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(won) == 1 and box.claim_of(message.id, 1)["owner"] == won[0]  # type: ignore[index]


def test_issue_inconnue_incertaine_jamais_rejouee():
    from nexus_relay import server

    car, box, api = sidecar()
    api.uncertain = True
    message = box.post("claude", "antigravity", "q", "t")
    assert car.step()[0]["status"] == "uncertain"
    api.uncertain = False
    car._failed.clear()
    assert car.step() == [] and api.calls == []
    assert "incertaine" in server._state_of(box, message)


def test_arret_entre_soumission_et_acquittement():
    """Sidecar tué après l'envoi, avant d'écrire « livré » : le suivant la marque incertaine et ne la rejoue pas."""
    from nexus_relay import server

    box = Box()
    message = box.post("claude", "antigravity", "q", "t")
    assert box.claim(message.id, 1, "sidecar")
    box.set_claim(message.id, 1, "sidecar", "submitting")
    claim = box.claim_of(message.id, 1) or {}
    from nexus_relay.box import write_state

    write_state(box._claim_path(message.id, 1), {**claim, "pid": 4_000_000})  # processus mort
    assert "incertaine" in server._state_of(box, message)
    car = Sidecar(box, FakeApi(), log=lambda _: None)  # type: ignore[arg-type]
    car.step()
    assert car.api.calls == []  # type: ignore[attr-defined]
    assert box.claim_of(message.id, 1)["phase"] == "uncertain"  # type: ignore[index]
    assert box.has_event(message.id, "livraison incertaine")


def test_nouvelle_sonnette_relivree_par_le_sidecar():
    car, box, api = sidecar()
    message = box.post("claude", "antigravity", "q", "t")
    car.step()
    box.set_ring_state(message.id, {"agent": "antigravity", "attempt": 2})  # le démon a resonné
    car.step()
    assert [c[0] for c in api.calls] == ["new", "send"] and box.claim_of(message.id, 2)["owner"] == "sidecar"  # type: ignore[index]
    car.step()
    assert len(api.calls) == 2


def test_refus_repete_un_seul_evenement_et_non_livre():
    from nexus_relay import server

    car, box, api = sidecar()
    api.fail = True
    message = box.post("claude", "antigravity", "q", "t")
    for _ in range(5):
        car._failed.clear()
        car.step()
    assert len([e for e in box.events(message.id) if e.get("kind") == "livraison ratée"]) == 1
    box.set_ring_state(message.id, {"agent": "antigravity", "attempt": 1})
    assert server._state_of(box, message).startswith("non livré")


def test_incertaine_au_premier_essai_jamais_relivree_a_la_sonnette_suivante():
    """Relecture du 20/10 : une livraison incertaine (attempt 1) repartait à la nouvelle sonnette (attempt 2) ;
    Antigravity pouvait recevoir le même message deux fois."""
    car, box, api = sidecar()
    api.uncertain = True
    message = box.post("claude", "antigravity", "q", "t")
    assert car.step()[0]["status"] == "uncertain"
    api.uncertain = False
    car._failed.clear()
    box.set_ring_state(message.id, {"agent": "antigravity", "attempt": 2})  # le démon a resonné
    assert car.step() == [] and api.calls == []
    assert box.claim_of(message.id, 2) is None  # la nouvelle sonnette n'est même pas prise


def test_erreur_imprevue_apres_la_prise_est_incertaine():
    """Toute exception imprévue après la prise (ici AttributeError) : incertaine, jamais bloquée en « submitting »."""

    class BrokenApi(FakeApi):
        def new_conversation(self, prompt: str, title: str) -> str:
            raise AttributeError("'NoneType' object has no attribute 'get'")

    box, api = Box(), BrokenApi()
    car = Sidecar(box, api, log=lambda _: None, alive=lambda c: True)  # type: ignore[arg-type]
    message = box.post("claude", "antigravity", "q", "t")
    result = car.step()[0]
    assert result["status"] == "uncertain" and "AttributeError" in result["reason"]
    claim = box.claim_of(message.id, 1) or {}
    assert claim["phase"] == "uncertain" and box.has_event(message.id, "livraison incertaine")
    car._failed.clear()
    assert car.step() == []  # jamais rejouée


def test_reponse_illisible_d_agentapi_est_incertaine(monkeypatch):
    """Sortie coupée : la demande a pu être acceptée (relecture du 20/10), donc incertaine, jamais rejouée ; une erreur
    JSON explicite reste un refus connu."""

    import pytest

    from nexus_relay import agentapi

    outputs = iter(['{"response": {"newConversation": {"conversationId": "1111', '{"error": "conversation inconnue"}'])

    class Popen:
        def __init__(self, args, **kwargs):
            self.args, self.returncode, self.pid = args, 0, 1

        def communicate(self, timeout=None):
            return next(outputs), ""

    monkeypatch.setattr(agentapi.subprocess, "Popen", Popen)
    api = agentapi.AgentApi(["agentapi"])
    with pytest.raises(AgentApiError) as cut:
        api.new_conversation("Nexus Relay : message", "Nexus Relay · t")
    assert cut.value.uncertain and "illisible" in str(cut.value)
    with pytest.raises(AgentApiError) as refused:
        api.send_message(CONV_A, "Nexus Relay : message")
    assert not refused.value.uncertain


# --- tour 2
def test_envoi_en_cours_au_premier_essai_bloque_la_sonnette_suivante():
    """Une livraison « submitting » (sidecar vivant, agentapi encore en route) ne se double jamais d'une autre."""
    car, box, api = sidecar()
    message = box.post("claude", "antigravity", "q", "t")
    assert box.claim(message.id, 1, "sidecar")
    box.set_claim(message.id, 1, "sidecar", "submitting")  # notre pid : vivant
    box.set_ring_state(message.id, {"agent": "antigravity", "attempt": 2})
    assert car.step() == [] and api.calls == [] and box.claim_of(message.id, 2) is None


def test_acquittement_rate_apres_l_envoi_jamais_rejoue():
    """Envoi accepté par agentapi, puis écriture de « delivered » ratée : la sonnette suivante ne renvoie rien."""
    import pytest

    car, box, api = sidecar()
    message = box.post("claude", "antigravity", "q", "t")
    real = box.set_claim

    def flaky(message_id: str, attempt: int, owner: str, phase: str, **data):  # type: ignore[no-untyped-def]
        if phase == "delivered":
            raise OSError("disque plein")
        return real(message_id, attempt, owner, phase, **data)

    box.set_claim = flaky  # type: ignore[method-assign]
    with pytest.raises(OSError):
        car.step()  # run() l'attrape : « pas raté »
    box.set_claim = real  # type: ignore[method-assign]
    assert len(api.calls) == 1
    box.set_ring_state(message.id, {"agent": "antigravity", "attempt": 2})
    car._failed.clear()
    assert car.step() == [] and len(api.calls) == 1


def test_livre_avant_les_prises_saute_au_premier_essai_seulement():
    """Salon d'avant les prises (20/10) : « livré » sans prise au 1er essai n'est pas relivré ; une nouvelle sonnette
    du démon (essai 2) l'est."""
    car, box, api = sidecar()
    message = box.post("claude", "antigravity", "q", "t")
    box.event(message.id, "livré", conversation=CONV_A)
    assert car.step() == [] and api.calls == []
    box.set_ring_state(message.id, {"agent": "antigravity", "attempt": 2})
    assert car.step()[0]["status"] == "delivered" and len(api.calls) == 1


def test_revalidation_apres_la_prise():
    """Une autre tentative devenue incertaine entre la lecture et la prise : prise rendue, rien envoyé."""
    car, box, api = sidecar()
    message = box.post("claude", "antigravity", "q", "t")
    box.set_ring_state(message.id, {"agent": "antigravity", "attempt": 2})
    assert box.claim(message.id, 1, "port")
    box.set_claim(message.id, 1, "port", "uncertain")  # posée juste après pending()
    result = car.deliver("t", [message])
    assert result["status"] == "skipped" and api.calls == [] and box.claim_of(message.id, 2) is None


def test_erreur_avant_l_envoi_est_un_echec_connu():
    """Tout l'envoi est dans le try : une erreur n'y laisse pas « submitting » ; avant tout appel agentapi, rien n'est
    parti : échec connu, retenté plus tard (tour 3 : « incertain » bloquait le message pour toujours)."""
    box, api = Box(), FakeApi()

    def alive(conversation: str) -> bool:
        raise PermissionError("base verrouillée")

    car = Sidecar(box, api, log=lambda _: None, alive=alive)  # type: ignore[arg-type]
    box.set_topic_state("t", "agy", {"conversation": CONV_A})
    message = box.post("claude", "antigravity", "q", "t")
    assert car.step()[0]["status"] == "failed" and api.calls == []
    assert (box.claim_of(message.id, 1) or {}).get("phase") not in ("uncertain", "submitting")
    car.alive = lambda conversation: True
    car._failed.clear()
    assert car.step()[0]["status"] == "delivered"  # retenté, livré


def test_battement_dans_son_propre_fil(monkeypatch):
    """Tour 2 : un appel agentapi de 2 min arrêtait le battement ; le sidecar était cru mort, Antigravity relancé."""
    import pytest

    from nexus_relay import sidecar as module

    car, box, _ = sidecar()
    started: list[dict] = []

    class Thread:
        def __init__(self, target, name, daemon):  # type: ignore[no-untyped-def]
            started.append({"target": target, "name": name, "daemon": daemon})

        def start(self) -> None:
            pass

    def stop(*args) -> None:  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    monkeypatch.setattr(module.threading, "Thread", Thread)
    monkeypatch.setattr(car, "step", stop)
    with pytest.raises(KeyboardInterrupt):
        car.run()
    (beat,) = started
    assert beat["daemon"] and "battement" in beat["name"]
    monkeypatch.setattr(module.time, "sleep", stop)
    with pytest.raises(KeyboardInterrupt):
        beat["target"]()
    assert (box.state("sidecar") or {}).get("pid")


def test_delai_agentapi_tue_toute_la_descendance(tmp_path):
    """Tour 4 (essai de Codex) : agentapi sort en laissant un enfant qui garde les tubes ; taskkill /T ne le voyait
    plus. L'objet tâche le tue quand même."""
    import os
    import sys
    import time

    import pytest

    from nexus_relay import agentapi
    from nexus_relay.box import pid_alive

    if os.name != "nt":
        pytest.skip("objet tâche : Windows")
    pid_file = tmp_path / "enfant.pid"
    parent = (
        "import subprocess, sys; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout=sys.stdout, stderr=sys.stderr); "
        f"open(r'{pid_file}', 'w').write(str(p.pid))"
    )
    api = agentapi.AgentApi([sys.executable, "-c", parent], timeout=2.0)
    with pytest.raises(AgentApiError) as cut:
        api._run(["x"])
    assert cut.value.uncertain
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while pid_alive(child) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not pid_alive(child)


def test_sidecar_relance_seulement_si_son_code_a_change(tmp_path):
    """Tour 5 : install.ps1 ne tue plus le sidecar ; il sort entre deux livraisons, seulement si son code a changé."""
    from nexus_relay import paths

    sidecar = Sidecar(Box(), None, lambda line: None)  # type: ignore[arg-type]
    request = paths.relay_home() / "sidecar-restart.request"
    request.parent.mkdir(parents=True, exist_ok=True)
    loaded = paths.code_fingerprint()
    assert not sidecar.restart_due(loaded)  # aucune demande
    request.write_text("code", encoding="utf-8")
    assert not sidecar.restart_due(loaded) and not request.exists()  # code inchangé : demande consommée
    request.write_text("code", encoding="utf-8")
    assert sidecar.restart_due("ancien")


def test_lancement_rate_d_agentapi_n_est_pas_incertain(monkeypatch):
    """Tour 6 : un échec à la mise en route (objet tâche, reprise) tue le processus suspendu ; rien n'a tourné."""
    import sys

    import pytest

    from nexus_relay import agentapi

    monkeypatch.setattr(agentapi, "_resume", lambda process: False)
    api = agentapi.AgentApi([sys.executable, "-B", "-c", "print('{}')"], timeout=10)
    with pytest.raises(AgentApiError) as caught:
        api._run(["x"])
    assert not caught.value.uncertain and "lancement" in str(caught.value)


def test_sidecar_sort_quand_son_antigravity_est_ferme(monkeypatch):
    """20/10 : Antigravity fermé, son sidecar orphelin prenait les livraisons du sidecar suivant (OSError 22)."""
    from nexus_relay import sidecar as module

    car = Sidecar(Box(), None, lambda line: None, host_gone=lambda: True)  # type: ignore[arg-type]
    monkeypatch.setattr(car, "step", lambda: None)
    monkeypatch.setattr(module.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(module.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("pas de sortie")))
    car.run()  # rend la main au lieu de boucler


def test_hote_du_sidecar_au_dessus_du_lanceur_du_venv():
    from nexus_relay.sidecar import host_pid

    table = {1: (0, "explorer.exe"), 10: (1, "language_server.exe"), 20: (10, "pythonw.exe"), 30: (20, "pythonw.exe")}
    assert host_pid(table, 30) == 10
    assert host_pid({**table, 10: (1, "pwsh.exe")}, 30) is None  # lancé à la main : rien n'est conclu
    assert host_pid({k: v for k, v in table.items() if k != 10}, 30) is None  # hôte déjà parti au démarrage


def test_fin_d_un_processus_vue_par_sa_poignee():
    import os
    import subprocess
    import sys

    import pytest

    from nexus_relay.sidecar import gone_watch

    if os.name != "nt":
        pytest.skip("poignée de processus : Windows")
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    gone = gone_watch(child.pid)
    assert gone is not None and not gone()
    child.communicate(b"")
    assert gone()


def test_conversation_figee_on_en_ouvre_une_neuve():
    """21/10 (diagnostic d'Antigravity) : language_server relancé en plein tour, conversation figée ; trois messages
    acceptés par agentapi sans erreur, jamais dépilés. Un tour non fini, muet depuis plus de ZOMBIE_S : remplacée."""
    box, api = Box(), FakeApi()
    now = 10_000.0
    car = Sidecar(
        box, cast(Any, api), log=lambda _: None, clock=lambda: now, alive=lambda c: True,
        turn=lambda c: (False, now - 7200) if c == CONV_B else (True, now),
    )  # fmt: skip
    box.set_topic_state("t", "agy", {"conversation": CONV_B})
    box.post("claude", "antigravity", "q", "t")
    car.step()
    assert api.calls[0][:2] == ("new", CONV_A)


def test_conversation_au_travail_gardee():
    """Un tour en cours mais actif (dernière étape récente) n'est pas une conversation figée."""
    box, api = Box(), FakeApi()
    now = 10_000.0
    car = Sidecar(
        box,
        cast(Any, api),
        log=lambda _: None,
        clock=lambda: now,
        alive=lambda c: True,
        turn=lambda c: (False, now - 30),
    )
    box.set_topic_state("t", "agy", {"conversation": CONV_B})
    box.post("claude", "antigravity", "q", "t")
    car.step()
    assert api.calls[0][:2] == ("send", CONV_B)


def test_quota_epuise_rien_n_est_livre():
    """21/10 (souhait d'Antigravity) : quota Gemini épuisé, les messages attendent au lieu d'échouer en boucle."""
    box, api = Box(), FakeApi()
    car = Sidecar(box, cast(Any, api), log=lambda _: None, paused=lambda: "Antigravity : 3 % restants")
    box.post("claude", "antigravity", "q", "t")
    assert car.step() == [] and api.calls == []
    assert "3 %" in (box.state("sidecar-paused") or {}).get("reason", "")


def test_conversation_arretee_sur_une_erreur():
    """21/10 : la conversation du sujet s'arrêtait sur « unknown model key … model not found », écrit seulement dans
    sa base : le relais disait « lu, sans réponse » sans fin et lui confiait encore les messages suivants."""
    box, api = Box(), FakeApi()
    halted: dict[str, tuple[str, float | None]] = {}
    car = Sidecar(box, api, log=lambda _: None, alive=lambda c: True, paused=lambda: None, stopped=halted.get)  # type: ignore[arg-type]
    first = box.post("claude", "antigravity", "Relis", "relais")
    car.step()
    assert api.calls[0][:2] == ("new", CONV_A)
    halted[CONV_A] = ("unknown model key MODEL_X: model not found", time.time() + 1)
    car.step()
    assert box.has_event(first.id, "arrêt sur erreur")
    from nexus_relay import server

    state = server._state_of(box, first)
    assert "arrêté sur une erreur" in state and "model not found" in state
    assert server._unavailable(box, first) is not None
    box.event(first.id, "en cours", by="antigravity", note="repris")
    assert server._unavailable(box, first) is None  # signe de vie après l'arrêt : il a repris
    second = box.post("claude", "antigravity", "Renvoi", "relais")
    car.step()
    assert api.calls[-1][0] == "new" and f'read(id="{second.id}")' in api.calls[-1][2]  # conversation neuve
    assert (box.topic_state("relais", "agy_retired") or {}).get("conversations") == [CONV_A]  # l'ancienne à ranger
