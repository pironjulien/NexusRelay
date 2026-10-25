"""Réveil de Codex par le port de débogage de son appli (cdp.py), sans appli réelle."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from nexus_relay import cdp, wake

ID = "20261019T201900000-abcdef"


@pytest.fixture(autouse=True)
def _attente_courte(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cdp, "NEW_THREAD_WAIT_S", 0.3)  # pages simulées : pas 15 s d'attente quand rien ne s'ouvre


class FakePage:
    """Page Codex simulée : vue courante (aria-current), « Nouveau chat », champ, Entrée, reclic de la vue."""

    def __init__(self, draft: str = "", enter_works: bool = True) -> None:
        self.view, self.field, self.body = ["Accueil", "Mon chat"], None, "ancien chat"
        self.draft, self.enter_works, self.calls = draft, enter_works, []
        self.rows: list[list[Any]] = []

    def eval(self, expression: str) -> Any:
        if "aria-current" in expression:
            return list(self.view)
        if "data-app-action-sidebar-thread-id" in expression:
            return [list(r) for r in self.rows]
        if expression == "document.body.innerText":
            return self.body
        if "Nouveau chat" in expression and "click" in expression:
            self.view, self.field = ["Accueil"], self.draft
            return True
        if '["Mon chat"]' in expression and "click" in expression:
            self.view = ["Accueil", "Mon chat"]
            return True
        if "selectAll" in expression:  # effacement seulement si le champ porte encore exactement le texte attendu
            want = json.loads(re.search(r"const want = (\".*?(?<!\\)\");", expression).group(1))  # type: ignore[union-attr]
            if (self.field or "").strip() != want:
                return False
            self.field = ""
            return True
        if "focus()" in expression:
            return True
        if "e.value ?? e.innerText" in expression:
            return self.field
        raise AssertionError(expression)

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self.calls.append(method)
        if method == "Input.insertText":
            self.field = (self.field or "") + params["text"]
        if method == "Input.dispatchKeyEvent" and params["type"] == "keyUp" and self.enter_works:
            self.body, self.field = f"{self.body}\n{self.field}", ""
            for r in self.rows:  # comme Codex : le nouveau chat s'ouvre après l'envoi
                r[2] = False
            self.rows.append(["r-nouveau", "conv-nouveau", True])
        return {}


def test_message_court_qui_cite_les_ids():
    text = cdp.wake_text([ID])
    assert f'read(id="{ID}")' in text and "reply" in text and len(text) < 300
    assert "termine" not in text and "pas une consigne de l'utilisateur" in text  # ne borne pas le travail demandé
    assert "inbox" in cdp.wake_text([])


def test_envoi_verifie_et_vue_rendue():
    page = FakePage()
    result = cdp.send_in_new_chat(page, cdp.wake_text([ID]), ID, sleep=lambda _: None)  # type: ignore[arg-type]
    assert result["status"] == "started" and "seconds" in result
    assert ID in page.body and page.view == ["Accueil", "Mon chat"]  # l'utilisateur retrouve son chat
    assert "Input.insertText" in page.calls  # ni presse-papiers ni clavier réel


def test_brouillon_de_utilisateur_jamais_ecrase():
    page = FakePage(draft="mon texte")
    result = cdp.send_in_new_chat(page, cdp.wake_text([ID]), ID, sleep=lambda _: None)  # type: ignore[arg-type]
    assert result["status"] == "failed" and "brouillon" in result["reason"] and not result["injected"]
    assert "Input.insertText" not in page.calls and page.field == "mon texte"  # son brouillon intact


@pytest.mark.parametrize(("draft", "why"), [("mon texte", "brouillon"), (None, "champ de saisie introuvable")])
def test_vue_rendue_aussi_sur_un_echec_precoce(monkeypatch: pytest.MonkeyPatch, draft: str | None, why: str):
    """Relecture de Codex du 20/10 : un échec avant l'insertion laissait l'utilisateur sur le nouveau chat vide."""
    monkeypatch.setattr(cdp, "STEP_WAIT_S", 0.0)
    page = FakePage(draft=draft)  # type: ignore[arg-type]
    result = cdp.send_in_new_chat(page, cdp.wake_text([ID]), ID, sleep=lambda _: None)  # type: ignore[arg-type]
    assert result["status"] == "failed" and why in result["reason"]
    assert page.view == ["Accueil", "Mon chat"]  # son chat recliqué


def test_entree_sans_effet_est_incertaine_sans_reste(monkeypatch: pytest.MonkeyPatch):
    """Entrée peut-être partie : « uncertain » (jamais rejoué en double), texte retiré, vue rendue."""
    monkeypatch.setattr(cdp, "STEP_WAIT_S", 0.0)
    page = FakePage(enter_works=False)
    result = cdp.send_in_new_chat(page, cdp.wake_text([ID]), ID, sleep=lambda _: None)  # type: ignore[arg-type]
    assert result["status"] == "uncertain" and "Entrée sans effet vérifié" in result["reason"]
    assert "injected" not in result
    assert page.field == ""  # notre texte retiré du champ
    assert page.view == ["Accueil", "Mon chat"]


def test_reste_d_un_reveil_interrompu_efface_puis_envoi():
    """Vu le 19-20/10 : le texte d'un réveil interrompu, pris pour un brouillon de l'utilisateur, bloquait tous les suivants."""
    other = "20261019T190000000-ffffff"
    page = FakePage(draft=cdp.wake_text([other]))
    assert cdp.is_wake_text(page.draft) and not cdp.is_wake_text(page.draft + " et mon ajout")
    result = cdp.send_in_new_chat(page, cdp.wake_text([ID]), ID, sleep=lambda _: None)  # type: ignore[arg-type]
    assert result["status"] == "started"
    assert ID in page.body and other not in page.body  # le reste n'est jamais envoyé
    assert page.calls.count("Input.insertText") == 1


def test_le_champ_vise_est_le_champ_visible():
    assert "getClientRects" in cdp._COMPOSER and ".pop()" in cdp._COMPOSER


def _ensure(monkeypatch: pytest.MonkeyPatch, port_up: list[bool], **kw: Any) -> tuple[dict[str, Any], list[str]]:
    actions: list[str] = []
    monkeypatch.setattr(cdp, "page_url", lambda port=0, url="": "ws://x" if port_up[0] else None)

    def start(path: Path, port: int) -> None:
        actions.append(f"start {port}")
        port_up[0] = True

    defaults: dict[str, Any] = {
        "running": lambda: True,
        "busy": lambda: False,
        "present": lambda: False,
        "exe": lambda: Path("ChatGPT.exe"),
        "foreground": lambda: FakeForeground([]),  # jamais le vrai jeton du premier plan
        "lb": lambda: True,  # routeur prêt (jamais le vrai lanceur dans les tests)
        "hung": lambda process: 0.0,  # fenêtre qui répond (jamais le vrai Get-Process)
        "responsive": lambda url: True,  # page qui exécute (jamais la vraie page)
        "page_silent": lambda name, silent: 0.0,  # jamais l'état du salon
    }
    result = cdp.ensure_codex(
        9223, start=start, stop=lambda: actions.append("stop"), wait_s=1, sleep=lambda _: None, **{**defaults, **kw}
    )
    return result, actions


def test_port_deja_ouvert_rien_n_est_relance(monkeypatch: pytest.MonkeyPatch):
    result, actions = _ensure(monkeypatch, [True])
    assert result == {"status": "ready", "launched": False} and actions == []


def test_codex_sans_port_relance_s_il_est_libre(monkeypatch: pytest.MonkeyPatch):
    result, actions = _ensure(monkeypatch, [False])
    assert result["status"] == "ready" and result["restarted"] and actions == ["stop", "start 9223"]


def test_codex_ferme_lance_avec_le_port(monkeypatch: pytest.MonkeyPatch):
    result, actions = _ensure(monkeypatch, [False], running=lambda: False)
    assert result["status"] == "ready" and not result["restarted"] and actions == ["start 9223"]


def test_codex_lance_par_windows_avec_son_identite_store(monkeypatch: pytest.MonkeyPatch):
    # 20/10 : lancé directement depuis WindowsApps, Codex perdait l'identité de son paquet (mises à jour bloquées)
    from nexus_relay import uia

    commands: list[str] = []
    monkeypatch.setattr(cdp, "_ps", lambda command, timeout=30.0: commands.append(command) or "4242")
    monkeypatch.setattr(uia, "user32", lambda: type("U", (), {"GetForegroundWindow": staticmethod(lambda: 0)})())
    monkeypatch.setattr(uia, "windows", lambda process, hidden=False: [7])
    minimized: list[int] = []
    monkeypatch.setattr(uia, "minimize", minimized.append)
    cdp._start_codex(Path("ChatGPT.exe"), 9223)
    assert "ActivateApplication" in commands[0] and "Get-AppxPackage OpenAI.Codex" in commands[0]
    assert "'--remote-debugging-port=9223'" in commands[0] and minimized == [7]
    monkeypatch.setattr(cdp, "_ps", lambda command, timeout=30.0: "")
    with pytest.raises(cdp.CdpError):
        cdp._start_codex(Path("ChatGPT.exe"), 9223)


@pytest.mark.parametrize(
    ("kw", "why"), [({"busy": lambda: True}, "travaille"), ({"present": lambda: True}, "L'utilisateur")]
)
def test_jamais_de_relance_pendant_un_tour_ou_devant_utilisateur(monkeypatch: pytest.MonkeyPatch, kw: Any, why: str):
    result, actions = _ensure(monkeypatch, [False], **kw)
    assert result["status"] == "deferred" and why in result["reason"] and actions == []


def test_tour_en_cours_lu_dans_les_journaux(tmp_path: Path):
    day = tmp_path / "sessions" / "2026" / "10" / "19"
    day.mkdir(parents=True)
    log = day / "rollout-2026-10-19T20-00-00-abc.jsonl"
    log.write_text('{"type":"event_msg","payload":{"type":"task_started"}}\n', encoding="utf-8")
    assert cdp.codex_busy(tmp_path)
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"event_msg","payload":{"type":"task_complete","completed_at":1}}\n')
    assert not cdp.codex_busy(tmp_path)


def test_claude_n_est_jamais_sonne():
    """L'utilisateur, 20/10 : Claude est l'hôte ; il attend les réponses (wait_reply), aucun réveil par tâche planifiée."""
    assert "claude" not in wake.WAKEABLE and "claude" not in wake.WAKERS
    assert wake.WAKEABLE == ("codex", "antigravity")


def test_mode_codex_deja_actif_ou_absent_aucun_clic():
    """Mode déjà « Codex », ou ancienne appli sans sélecteur : aucun geste."""

    class ModePage:
        def __init__(self, mode: str | None) -> None:
            self.mode, self.calls = mode, 0

        def eval(self, expr: str) -> Any:
            return self.mode

        def call(self, method: str, **params: Any) -> dict[str, Any]:
            self.calls += 1
            return {}

    for mode in ("Codex", None):
        page = ModePage(mode)
        assert cdp.ensure_codex_mode(page, sleep=lambda s: None) and page.calls == 0


def test_page_d_attente_d_antigravity_ecartee(monkeypatch: pytest.MonkeyPatch):
    """Vu le 20/10 : à l'ouverture de sa fenêtre, Antigravity liste une page « data:text/html » avant la vraie."""
    pages = [
        {"type": "page", "url": "data:text/html;charset=utf-8,%3C!DOCTYPE", "webSocketDebuggerUrl": "ws://attente"},
        {"type": "page", "url": "https://127.0.0.1:52688/", "webSocketDebuggerUrl": "ws://vraie"},
    ]
    monkeypatch.setattr(cdp, "targets", lambda port=0, timeout=2.0: pages)
    assert cdp.agy_page_url() == "ws://vraie"
    monkeypatch.setattr(cdp, "targets", lambda port=0, timeout=2.0: pages[:1])
    assert cdp.agy_page_url() is None


def test_projet_choisi_lu_dans_l_appli():
    class P:
        def __init__(self, label: str) -> None:
            self.label = label

        def eval(self, expression: str) -> str:
            return self.label

    assert cdp.current_project(P("Changer de projet : Documents")) == "Documents"
    assert cdp.current_project(P("")) is None


class FakeSocket:
    """WebSocket simulé : d'autres évènements de la page en flot continu, la réponse attendue jamais ou après n ;
    `late_s` : la réponse n'arrive qu'après ce délai (recv attend alors au plus son timeout, puis lève comme
    websocket-client)."""

    def __init__(self, answer_after: int | None = None, late_s: float | None = None) -> None:
        self.answer_after, self.late_s, self.received, self.sent = answer_after, late_s, 0, []
        self.timeouts: list[float] = []

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def recv(self) -> str:
        import time

        from websocket import WebSocketTimeoutException

        self.received += 1
        if self.late_s is not None:
            if self.late_s > self.timeouts[-1]:
                time.sleep(self.timeouts[-1])
                raise WebSocketTimeoutException("Connection timed out")
            return json.dumps({"id": self.sent[-1]["id"], "result": {"ok": True}})
        if self.answer_after is not None and self.received > self.answer_after:
            return json.dumps({"id": self.sent[-1]["id"], "result": {"ok": True}})
        time.sleep(0.001)
        return json.dumps({"method": "Runtime.consoleAPICalled", "params": {}})


def _page_on(socket: FakeSocket, timeout: float) -> cdp.Page:
    page = cdp.Page.__new__(cdp.Page)  # sans connexion réelle
    page._ws, page._id, page._timeout = socket, 0, timeout  # type: ignore[attr-defined]
    return page


def test_appel_du_port_borne_meme_sous_un_flot_d_evenements():
    """Relecture du 20/10 : chaque évènement étranger relançait l'attente ; un flot continu figeait le fil."""
    import time

    socket = FakeSocket()
    page = _page_on(socket, timeout=0.2)
    start = time.monotonic()
    with pytest.raises(cdp.CdpError, match="pas de réponse"):
        page.call("Runtime.evaluate", expression="1")
    assert time.monotonic() - start < 2.0
    assert socket.timeouts and all(0 < t <= 0.2 for t in socket.timeouts)
    assert socket.timeouts == sorted(socket.timeouts, reverse=True)  # chaque attente bornée par le reste du délai
    assert _page_on(FakeSocket(answer_after=5), timeout=5.0).call("Page.enable") == {"ok": True}


def test_reponse_arrivee_apres_le_budget_est_une_cdp_error():
    """Réponse plus lente que le budget : l'attente de recv est bornée par le reste du délai, puis CdpError."""
    import time

    socket = FakeSocket(late_s=5.0)
    start = time.monotonic()
    with pytest.raises(cdp.CdpError):
        _page_on(socket, timeout=0.2).call("Runtime.evaluate", expression="1")
    assert time.monotonic() - start < 1.0 and socket.timeouts[0] <= 0.2


def test_reponse_dans_le_budget_rendue():
    socket = FakeSocket(late_s=0.05)
    assert _page_on(socket, timeout=1.0).call("Page.enable") == {"ok": True} and socket.timeouts[0] <= 1.0


def test_routeur_lance_detache_sans_attendre_sa_fin(monkeypatch: pytest.MonkeyPatch, isolated: Path):
    """Vu le 20/10 : subprocess.run avec capture attendait les tubes que le routeur garde ouverts ; sonnette figée."""
    import io
    import subprocess

    script = isolated / "routeur" / "start-router.ps1"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    monkeypatch.setenv("NEXUS_RELAY_CODEX_ROUTER", "http://127.0.0.1:2455/health/ready")
    # premier lanceur absent : le script suivant sert
    monkeypatch.setenv("NEXUS_RELAY_CODEX_ROUTER_START", f'"{isolated / "absent.exe"}" --background; "{script}"')
    launched: list[dict[str, Any]] = []
    health: Any = iter([OSError("pas prêt"), io.BytesIO(b'{"status": "ok"}')])

    def urlopen(url: str, timeout: float = 0) -> Any:
        assert url == "http://127.0.0.1:2455/health/ready"
        answer = next(health)
        if isinstance(answer, Exception):
            raise answer
        return answer

    class Process:
        def wait(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("jamais attendu")

        communicate = wait

    def popen(args: list[str], **kwargs: Any) -> Process:
        launched.append({"args": args, **kwargs})
        return Process()

    def run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("subprocess.run interdit ici : il attend la fin")

    monkeypatch.setattr(cdp.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(cdp.subprocess, "Popen", popen)
    monkeypatch.setattr(cdp.subprocess, "run", run)
    assert cdp.ensure_router(timeout_s=1.0)
    (spawn,) = launched
    assert spawn["args"][-1] == str(script) and "-NonInteractive" in spawn["args"]
    assert spawn["stdin"] == spawn["stdout"] == spawn["stderr"] == subprocess.DEVNULL
    group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    assert spawn["creationflags"] & cdp.NO_WINDOW == cdp.NO_WINDOW and spawn["creationflags"] & group == group
    assert not spawn["creationflags"] & getattr(subprocess, "DETACHED_PROCESS", 0)  # pwsh détaché : rien exécuté


def _wake_setup(
    monkeypatch: pytest.MonkeyPatch,
    send: Any,
    project: str | None = "Documents",
    relay_button: bool = True,
    late: int = 0,
) -> tuple[dict[str, Any], list[Any]]:
    """wake_codex sur une appli simulée : mode ChatGPT de l'utilisateur, son projet, bloc du relais présent ou non (`late` :
    absent des `late` premières lectures, comme juste après le passage en mode Codex)."""
    state: dict[str, Any] = {"mode": "ChatGPT", "closed": False, "project": project, "sent": 0, "reads": 0}
    clicks: list[Any] = []

    class CodexPage:
        def eval(self, expression: str) -> Any:
            if "data-clear-project-button" in expression:  # bouton « aucun projet » du nouveau chat
                if "click()" in expression:
                    state["project"] = None
                return True
            state["reads"] += 1
            # bouton « Démarrer un nouveau chat dans Nexus Relay » ; « home » : Accueil ouvert depuis les Paramètres
            return (relay_button or state.get("home", False)) and state["reads"] > late

        def close(self) -> None:
            state["closed"] = True

    def set_mode(page: Any, wanted: str, sleep: Any = None) -> bool:
        state["mode"] = wanted
        return True

    def click(page: Any, labels: tuple[str, ...]) -> bool:
        clicks.append(labels)
        prefix = "Démarrer un nouveau chat dans "
        if labels != cdp.NEW_CHAT:  # un nouveau chat garde le projet affiché
            state["project"] = None if labels == cdp.NO_PROJECT else labels[0].removeprefix(prefix)
        return True

    def counted(*args: Any, **kwargs: Any) -> dict[str, Any]:
        state["sent"] += 1
        return send(*args, **kwargs)

    monkeypatch.setattr(cdp, "user_in_codex", lambda: False)
    monkeypatch.setattr(cdp, "ensure_codex", lambda port=0: {"status": "ready", "launched": False})
    monkeypatch.setattr(cdp, "page_url", lambda port=0, url="": "ws://codex")
    monkeypatch.setattr(cdp, "Page", lambda url: CodexPage())
    monkeypatch.setattr(cdp, "codex_mode", lambda page: state["mode"])
    monkeypatch.setattr(cdp, "ensure_codex_mode", lambda page, sleep=None: set_mode(page, "Codex"))
    monkeypatch.setattr(cdp, "set_codex_mode", set_mode)
    monkeypatch.setattr(cdp, "current_project", lambda page: state["project"])
    monkeypatch.setattr(cdp, "_click", click)
    monkeypatch.setattr(cdp, "send_in_new_chat", counted)
    monkeypatch.setattr(cdp, "PROJECT_WAIT_S", 0.3)
    return state, clicks


@pytest.mark.parametrize("project", ["Documents", None])
def test_reveil_rend_le_mode_et_le_projet_de_utilisateur(monkeypatch: pytest.MonkeyPatch, project: str | None):
    """Vu le 20/10 : l'appli restait en mode Codex et sur le projet du relais après un réveil ; tour 2 : sans projet,
    l'appli est remise sur « aucun projet » (sinon le chat suivant de l'utilisateur naissait dans le dossier du relais)."""
    seen: dict[str, Any] = {}

    def send(page: Any, text: str, marker: str, **kwargs: Any) -> dict[str, Any]:
        seen.update(mode=state["mode"], new_chat=kwargs["new_chat"], marker=marker, present=kwargs["present"])
        state["project"] = cdp.RELAY_PROJECT  # le chat de réveil naît dans le projet du relais
        kwargs["restore_first"]()  # send_in_new_chat le rappelle dans son finally
        return {"status": "started", "seconds": 1.0}

    state, clicks = _wake_setup(monkeypatch, send, project)
    result = cdp.wake_codex([ID])
    assert result["status"] == "started" and result["via"] == "cdp"
    assert seen["mode"] == "Codex" and seen["new_chat"] == cdp.RELAY_NEW_CHAT and seen["marker"] == ID
    assert seen["present"] is cdp.user_in_codex  # relu juste avant de taper
    expected = (
        cdp.NEW_CHAT  # puis le bouton d'effacement du projet (tour 3)
        if project is None
        else (f"Démarrer un nouveau chat dans {project}", f"Start new chat in {project}")
    )
    assert clicks == [expected] and state["project"] == project
    assert state["mode"] == "ChatGPT" and state["closed"]  # mode de l'utilisateur rendu, page fermée


def test_reveil_sans_projet_du_relais_echoue_clairement(monkeypatch: pytest.MonkeyPatch):
    """Tour 2 : plus de repli sur « Nouveau chat » ; le chat naîtrait dans le projet de l'utilisateur, jamais rangé."""
    state, clicks = _wake_setup(monkeypatch, lambda *a, **k: pytest.fail("rien ne doit être tapé"), relay_button=False)
    result = cdp.wake_codex([ID])
    assert result["status"] == "failed" and result["injected"] is False and cdp.RELAY_PROJECT in result["reason"]
    # 21/10 : barre latérale d'abord rouverte (le projet n'y était pas), puis refermée : vue de l'utilisateur rendue.
    assert state["sent"] == 0 and clicks == [cdp.SHOW_SIDEBAR, cdp.HIDE_SIDEBAR]
    assert state["mode"] == "ChatGPT" and state["closed"] and "codex-projet" in result["reason"]


def test_reveil_attend_la_liste_des_projets_apres_le_changement_de_mode(monkeypatch: pytest.MonkeyPatch):
    """21/10 : la liste des projets se dessine après le passage en mode Codex ; lue aussitôt, le projet était dit absent
    à chaque essai (9 min de réveils ratés). Le bouton du relais paraît à la 3e lecture : réveil envoyé, sans détour."""
    state, clicks = _wake_setup(monkeypatch, lambda *a, **k: {"status": "started", "seconds": 1.0}, late=2)
    result = cdp.wake_codex([ID])
    assert result["status"] == "started" and state["sent"] == 1
    assert cdp.SHOW_SIDEBAR not in clicks  # barre latérale de l'utilisateur pas touchée


def test_reveil_interrompu_avant_l_envoi_est_un_echec_et_rend_le_mode(monkeypatch: pytest.MonkeyPatch):
    """Tour 2 : une erreur avant l'envoi (mode, page) était comptée « uncertain », jamais retentée ; rien n'est parti."""

    def send(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise cdp.CdpError("Runtime.evaluate : pas de réponse en 10 s")

    state, _ = _wake_setup(monkeypatch, send)
    result = cdp.wake_codex([ID])
    assert result["status"] == "failed" and result["injected"] is False and "pas de réponse" in result["reason"]
    assert state["mode"] == "ChatGPT" and state["closed"]


def test_archivage_d_une_ligne_par_le_menu_actions_du_chat():
    """Codex d'octobre 2026 : plus de bouton « Archiver » sur la ligne, l'entrée est dans le menu « Actions du chat » ;
    la garde est relue juste avant le clic."""

    class MenuPage:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def eval(self, expr: str) -> Any:
            self.calls.append(expr)
            if "const rows" in expr:
                return "menu"  # aucun bouton direct sur la ligne
            return True

    page = MenuPage()
    assert cdp.archive_thread(page, "t-1", lambda: None) == ("clicked", None)
    assert '=== "t-1"' in page.calls[0] and "__nexusArchive.click()" in page.calls[-1]
    refused = MenuPage()
    assert cdp.archive_thread(refused, "t-1", lambda: "L'utilisateur est revenu") == (
        "skipped",
        "L'utilisateur est revenu",
    )
    assert "Escape" in refused.calls[-1] and not any("__nexusArchive.click()" in c for c in refused.calls)


def test_rangement_d_antigravity_par_identifiant_et_garde(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex du 20/10 : rangée par son identifiant, jamais par le titre (« relais » trouvait
    « relais-v2 ») ; garde relue avant le clic."""
    conversation = "11111111-1111-1111-1111-111111111111"
    pages: list[Any] = []

    class AgyPage:
        mouse: list[tuple[Any, Any]] = []  # survol émulé de la ligne (le bouton « Archive » n'existe qu'au survol)

        def __init__(self, url: str) -> None:
            self.calls: list[str] = []
            pages.append(self)

        def eval(self, expr: str) -> Any:
            self.calls.append(expr)
            return [10, 20] if "getBoundingClientRect" in expr else True

        def call(self, method: str, **params: Any) -> dict[str, Any]:
            self.mouse.append((params.get("x"), params.get("y")))
            return {}

        def close(self) -> None:
            pass

    monkeypatch.setattr(cdp, "user_in_agy", lambda: False)
    monkeypatch.setattr(cdp, "agy_page_url", lambda port=0: "ws://agy")
    monkeypatch.setattr(cdp, "Page", AgyPage)
    busy = cdp.archive_antigravity_conversation(conversation, lambda: "Antigravity travaille encore")
    assert busy == ("skipped", "Antigravity travaille encore")
    assert f'=== "{conversation}"' in pages[0].calls[0] and not any(
        "__nexusArchive.click()" in c for c in pages[0].calls
    )
    assert cdp.archive_antigravity_conversation(conversation, lambda: None) == ("clicked", None)
    assert "__nexusArchive.click()" in pages[1].calls[-1] and (10, 20) in AgyPage.mouse  # survolée avant le clic
    monkeypatch.setattr(cdp, "user_in_agy", lambda: True)
    assert cdp.archive_antigravity_conversation(conversation)[0] == "skipped"
    assert len(pages) == 2  # l'utilisateur dans Antigravity : pas même une connexion au port


# --- tour 2 : l'utilisateur revenu, phase de l'erreur, projet puis conversation rendus
def test_utilisateur_revenu_juste_avant_de_taper_rien_n_est_tape():
    page = FakePage()
    result = cdp.send_in_new_chat(page, cdp.wake_text([ID]), ID, sleep=lambda _: None, present=lambda: True)  # type: ignore[arg-type]
    assert result == {"status": "deferred", "reason": "L'utilisateur est revenu dans Codex", "injected": False}
    assert "Input.insertText" not in page.calls and page.view == ["Accueil", "Mon chat"]


class FailingPage(FakePage):
    """Le port lâche à une étape donnée (insertion ou Entrée)."""

    def __init__(self, fail_on: str) -> None:
        super().__init__()
        self.fail_on = fail_on

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        if method == self.fail_on or params.get("type") == self.fail_on:
            raise cdp.CdpError(f"{method} : pas de réponse en 10 s")
        return super().call(method, **params)


def test_erreur_avant_l_entree_echec_sans_rien_envoye_apres_incertaine():
    """Tour 2 : une erreur avant l'Entrée était comptée « uncertain », donc jamais retentée alors que rien n'était parti."""
    before = FailingPage("Input.insertText")
    result = cdp.send_in_new_chat(before, cdp.wake_text([ID]), ID, sleep=lambda _: None)  # type: ignore[arg-type]
    assert result["status"] == "failed" and result["injected"] is False and "CdpError" in result["reason"]
    assert before.field == "" and before.view == ["Accueil", "Mon chat"]
    after = FailingPage("keyUp")  # Entrée partie (rawKeyDown, char), acquittement perdu
    result = cdp.send_in_new_chat(after, cdp.wake_text([ID]), ID, sleep=lambda _: None)  # type: ignore[arg-type]
    assert result["status"] == "uncertain" and "injected" not in result
    assert after.field == "" and after.view == ["Accueil", "Mon chat"]


class RowsPage(FakePage):
    """Barre latérale avec l'identité de chaque ligne (conversationId) : le chat de l'utilisateur est sélectionné."""

    def __init__(self) -> None:
        super().__init__()
        self.rows = [["r-user", "conv-user", True], ["r-autre", "conv-autre", False]]
        self.log: list[str] = []

    def eval(self, expression: str) -> Any:
        if "data-app-action-sidebar-thread-row" in expression:
            return [list(r) for r in self.rows]
        if "r.click()" in expression and "data-app-action-sidebar-thread-id" in expression:
            row = next(r for r in self.rows if json.dumps(r[0]) in expression)
            for r in self.rows:
                r[2] = r is row
            self.log.append(f"open:{row[0]}")
            return True
        if "Nouveau chat" in expression and "click" in expression:
            for r in self.rows:
                r[2] = False
        return super().eval(expression)


def test_projet_rendu_d_abord_puis_conversation_rouverte_par_son_identite():
    """Tour 2 : rouvrir son chat AVANT de rendre le projet était défait par le changement de projet ; et le chat est
    rouvert par son identifiant (deux chats peuvent porter le même titre)."""
    page = RowsPage()
    result = cdp.send_in_new_chat(
        page,  # type: ignore[arg-type]
        cdp.wake_text([ID]),
        ID,
        sleep=lambda _: None,
        restore_first=lambda: page.log.append("projet"),
    )
    assert result["status"] == "started"
    assert page.log == ["projet", "open:r-user"] and cdp.selected_conversation(page) == "conv-user"


def test_lignes_de_la_barre_avec_leur_conversation():
    class P:
        def eval(self, expression: str) -> Any:
            assert "conversationId" in expression and "__reactFiber" in expression
            return [["r1", "conv-1", False], ["r2", "conv-2", True]]

    assert cdp.row_conversations(P()) == [("r1", "conv-1", False), ("r2", "conv-2", True)]
    assert cdp.selected_conversation(P()) == "conv-2"
    assert not hasattr(cdp, "project_rows")


def test_projet_rendu_ou_aucun_projet(monkeypatch: pytest.MonkeyPatch):
    state: dict[str, Any] = {"project": "Nexus Relay"}
    clicks: list[tuple[str, ...]] = []

    def click(page: Any, labels: tuple[str, ...]) -> bool:
        clicks.append(labels)
        if labels != cdp.NEW_CHAT:
            state["project"] = None if labels == cdp.NO_PROJECT else labels[0].rsplit(" dans ", 1)[1]
        return True

    class P:  # pas de bouton d'effacement visible : repli sur le menu « aucun projet »
        def eval(self, expression: str) -> Any:
            assert "getClientRects" in expression  # visibles seulement
            return False

    monkeypatch.setattr(cdp, "_until", lambda probe, timeout, sleep=None: probe())
    monkeypatch.setattr(cdp, "current_project", lambda page: state["project"])
    monkeypatch.setattr(cdp, "_click", click)
    assert cdp.restore_project(P(), "Documents") and state["project"] == "Documents"
    assert cdp.restore_project(P(), None) and state["project"] is None and clicks[-2:] == [cdp.NEW_CHAT, cdp.NO_PROJECT]
    assert cdp.restore_project(P(), None) and len(clicks) == 3  # déjà bon : aucun clic
    monkeypatch.setattr(cdp, "_click", lambda page, labels: False)  # bouton absent : non rendu, dit
    assert not cdp.restore_project(P(), "Documents")


class FakeForeground:
    def __init__(self, actions: list[str], granted: bool = True) -> None:
        self.actions, self.granted = actions, granted

    def acquire(self) -> bool:
        self.actions.append("jeton" if self.granted else "jeton refusé")
        return self.granted

    def release(self) -> None:
        self.actions.append("jeton rendu")


def test_jeton_du_premier_plan_pris_avant_d_arreter_codex(monkeypatch: pytest.MonkeyPatch):
    """Tour 2 : Codex était tué, puis le jeton refusé au lancement : Codex restait fermé."""
    actions: list[str] = []
    result, _ = _ensure(monkeypatch, [False], foreground=lambda: FakeForeground(actions))
    assert result["status"] == "ready" and result["restarted"]
    refused: list[str] = []
    result, done = _ensure(monkeypatch, [False], foreground=lambda: FakeForeground(refused, granted=False))
    assert result["status"] == "deferred" and "premier plan" in result["reason"]
    assert done == [] and refused == ["jeton refusé"]  # Codex jamais arrêté sans le jeton


def test_ordre_jeton_arret_lancement(monkeypatch: pytest.MonkeyPatch):
    actions: list[str] = []
    up = [False]
    monkeypatch.setattr(cdp, "page_url", lambda port=0, url="": "ws://x" if up[0] else None)

    def start(path: Path, port: int) -> None:
        actions.append("lancement")
        up[0] = True

    result = cdp.ensure_codex(
        9223,
        running=lambda: True,
        busy=lambda: False,
        present=lambda: False,
        exe=lambda: Path("ChatGPT.exe"),
        start=start,
        stop=lambda: actions.append("arrêt"),
        foreground=lambda: FakeForeground(actions),
        wait_s=1,
        sleep=lambda _: None,
        lb=lambda: True,  # jamais le vrai lanceur du routeur (relecture de Codex)
    )
    assert result["status"] == "ready" and actions == ["jeton", "arrêt", "lancement", "jeton rendu"]


def test_lancement_de_codex_ne_prend_plus_le_jeton_lui_meme(monkeypatch: pytest.MonkeyPatch):
    from nexus_relay import foreground_lock

    seen: list[int] = []
    monkeypatch.setattr(cdp, "_activate_codex", lambda uia, port: seen.append(port))
    monkeypatch.setattr(
        foreground_lock.ForegroundLock, "acquire", lambda self, *a, **k: pytest.fail("jeton déjà pris par ensure_codex")
    )
    cdp._start_codex(Path("ChatGPT.exe"), 9223)
    assert seen == [9223]


def test_conversation_vue_sous_deux_lignes_ecartee():
    """Tour 3 : un même id sous deux lignes = identité douteuse ; « ouverte » se lit dans l'attribut active."""

    class P:
        def eval(self, expression: str) -> Any:
            return [["r1", "c1", False, True], ["r2", "c1", True, True], ["r3", "c2", True, True]]

    assert cdp.row_conversations(P()) == [("r3", "c2", True)]
    assert cdp.selected_conversation(P()) == "c2" and cdp.selection_size(P()) == 2


def test_scripts_de_reouverture_bien_formes():
    """Vu à l'essai réel du 20/10 : « }})() » hors f-string, erreur JavaScript, la conversation n'était jamais rouverte
    (les pages simulées ne lisent pas le script)."""
    seen: list[str] = []

    class P:
        def eval(self, expression: str) -> Any:
            if "data-app-action-sidebar-thread-row" in expression:
                return [["r1", "c1", False, False]]
            seen.append(expression)
            return True

    assert cdp.open_conversation(P(), "c1")
    script = seen[-1]
    assert script.count("{") == script.count("}") and script.count("(") == script.count(")") and "}}" not in script


class LatePage(RowsPage):
    """Comme Codex le 20/10 : le nouveau chat ne s'ouvre qu'après quelques lectures (~8 s en vrai)."""

    def __init__(self, delay: int = 4) -> None:
        super().__init__()
        self.delay, self.opened = delay, False

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        result = FakePage.call(self, method, **params)
        if self.rows and self.rows[-1][0] == "r-nouveau" and not self.opened:
            self.rows.pop()  # pas encore ouvert
            for r in self.rows:
                r[2] = False
            self.pending = self.delay
        return result

    def eval(self, expression: str) -> Any:
        if "data-app-action-sidebar-thread-row" in expression and getattr(self, "pending", 0):
            self.pending -= 1
            if not self.pending:
                self.opened = True
                self.rows.append(["r-nouveau", "conv-nouveau", True])
                self.log.append("nouveau chat ouvert")
        return super().eval(expression)


def test_vue_rendue_apres_l_ouverture_tardive_du_nouveau_chat():
    """Essai réel du 20/10 : Codex ouvrait le nouveau chat après le retour de vue et le défaisait."""
    page = LatePage()
    result = cdp.send_in_new_chat(page, cdp.wake_text([ID]), ID, sleep=lambda _: None)  # type: ignore[arg-type]
    assert result["status"] == "started"
    assert page.log == ["nouveau chat ouvert", "open:r-user"] and cdp.selected_conversation(page) == "conv-user"


def test_utilisateur_revenu_pendant_l_attente_on_ne_touche_plus_a_rien():
    """Tour 4 : après l'Entrée, l'utilisateur revient pendant l'attente du nouveau chat : ni projet ni vue repris de force."""
    page = LatePage(delay=50)
    seen: list[str] = []
    calls = {"n": 0}

    def present() -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # absent aux deux relectures d'avant l'Entrée, puis revenu

    report: dict[str, Any] = {}
    result = cdp.send_in_new_chat(
        page,  # type: ignore[arg-type]
        cdp.wake_text([ID]),
        ID,
        sleep=lambda _: None,
        present=present,
        restore_first=lambda: seen.append("projet"),
        report=report,
    )
    assert result["status"] == "started" and seen == [] and "open:r-user" not in page.log
    assert report["restore_skipped"] and report["view_restored"] is False


def test_brouillon_de_utilisateur_jamais_efface():
    """Tour 4 : l'utilisateur écrit pendant la dernière relecture de présence ; son texte reste, rien n'est envoyé."""
    page = FakePage()
    calls = {"n": 0}

    def present() -> bool:
        calls["n"] += 1
        if calls["n"] == 2:  # juste avant l'Entrée
            page.field = "Brouillon personnel de l'utilisateur"
            return True
        return False

    result = cdp.send_in_new_chat(page, cdp.wake_text([ID]), ID, sleep=lambda _: None, present=present)  # type: ignore[arg-type]
    assert result["status"] == "deferred" and page.field == "Brouillon personnel de l'utilisateur"
    assert "keyUp" not in str(page.calls)


def test_reste_du_relais_reconnu_au_texte_exact():
    assert cdp.is_wake_text(cdp.wake_text([ID])) and cdp.is_wake_text(cdp.wake_text([]))
    assert not cdp.is_wake_text(cdp.wake_text([ID]) + " et ce que l'utilisateur a ajouté")


def test_utilisateur_revenu_apres_l_entree_son_mode_n_est_pas_repris(monkeypatch: pytest.MonkeyPatch):
    """Tour 5 : projet et vue n'étaient plus repris devant l'utilisateur revenu, mais le mode l'était encore."""

    def send(page: Any, text: str, marker: str, **kwargs: Any) -> dict[str, Any]:
        kwargs["report"].update(project_restored=False, view_restored=False, restore_skipped="L'utilisateur est revenu")
        return {"status": "started", "seconds": 1.0}

    state, _ = _wake_setup(monkeypatch, send)
    result = cdp.wake_codex([ID])
    assert result["status"] == "started" and result["restore_skipped"]
    assert state["mode"] == "Codex" and state["closed"]  # laissé tel quel devant l'utilisateur


def test_utilisateur_revenu_avant_l_entree_dans_un_autre_chat_y_reste():
    """Tour 5 : l'utilisateur revenu avant l'Entrée et déjà dans un autre chat : on ne l'arrache pas pour le ramener."""
    page = RowsPage()
    seen: list[str] = []

    def present() -> bool:
        for r in page.rows:  # il ouvre « conv-autre » au moment où on allait taper
            r[2] = r[1] == "conv-autre"
        return True

    report: dict[str, Any] = {}
    result = cdp.send_in_new_chat(
        page,  # type: ignore[arg-type]
        cdp.wake_text([ID]),
        ID,
        sleep=lambda _: None,
        present=present,
        restore_first=lambda: seen.append("projet"),
        report=report,
    )
    assert result["status"] == "deferred" and result["injected"] is False
    assert seen == [] and "open:r-user" not in page.log and report["restore_skipped"]
    assert cdp.selected_conversation(page) == "conv-autre"


def test_effacement_d_un_seul_geste_jamais_le_texte_de_utilisateur():
    """Tour 5 : l'utilisateur écrit entre notre lecture et l'effacement ; la comparaison est faite dans le même script."""
    page = FakePage()
    page.field = "Brouillon personnel de l'utilisateur"
    assert not cdp._clear(page, cdp.wake_text([ID]))  # type: ignore[arg-type]
    assert page.field == "Brouillon personnel de l'utilisateur"
    page.field = cdp.wake_text([ID]) + "  "
    assert cdp._clear(page, cdp.wake_text([ID])) and page.field == ""  # type: ignore[arg-type]


def test_retour_de_utilisateur_vu_pendant_l_attente_n_est_pas_oublie():
    """Tour 5 : présence vue une fois pendant l'attente du nouveau chat, puis plus à la relecture : rien n'est repris."""
    page = LatePage(delay=50)
    seen: list[str] = []
    answers = iter([False, False, True])

    report: dict[str, Any] = {}
    result = cdp.send_in_new_chat(
        page,  # type: ignore[arg-type]
        cdp.wake_text([ID]),
        ID,
        sleep=lambda _: None,
        present=lambda: next(answers, False),
        restore_first=lambda: seen.append("projet"),
        report=report,
    )
    assert result["status"] == "started" and seen == [] and report["restore_skipped"]


def test_retour_de_utilisateur_avant_l_entree_memorise():
    """Tour 6 (Codex) : l'utilisateur ouvre un autre chat (présent), puis la relecture suivante le dit absent : il y reste."""
    page = RowsPage()
    answers = iter([True])

    def present() -> bool:
        if next(answers, False):
            for r in page.rows:
                r[2] = r[1] == "conv-autre"
            return True
        return False

    seen: list[str] = []
    report: dict[str, Any] = {}
    result = cdp.send_in_new_chat(
        page,  # type: ignore[arg-type]
        cdp.wake_text([ID]),
        ID,
        sleep=lambda _: None,
        present=present,
        restore_first=lambda: seen.append("projet"),
        report=report,
    )
    assert result["status"] == "deferred" and seen == [] and report["restore_skipped"]
    assert cdp.selected_conversation(page) == "conv-autre"


def test_sans_routeur_codex_n_est_pas_reveille(monkeypatch: pytest.MonkeyPatch):
    """21/10 : routeur arrêté, Codex recevait le message sans modèle pour répondre ; désormais rien n'est lancé."""
    result, actions = _ensure(monkeypatch, [False], lb=lambda: False)
    assert result["status"] == "deferred" and "routeur" in result["reason"] and actions == []


def test_routeur_lance_avec_ses_arguments(monkeypatch: pytest.MonkeyPatch, isolated: Path):
    """Un lanceur .exe garde ses arguments (ex. --background : le routeur et son icône, sans ouvrir Codex)."""
    import io
    import subprocess

    launcher = isolated / "Router Launcher.exe"
    launcher.write_text("", encoding="utf-8")
    monkeypatch.setenv("NEXUS_RELAY_CODEX_ROUTER", "http://127.0.0.1:2455/health/ready")
    monkeypatch.setenv("NEXUS_RELAY_CODEX_ROUTER_START", f'"{launcher}" --background')
    health: Any = iter([OSError("pas prêt"), io.BytesIO(b'{"status": "ok"}')])

    def urlopen(url: str, timeout: float = 0) -> Any:
        answer = next(health)
        if isinstance(answer, Exception):
            raise answer
        return answer

    launched: list[list[str]] = []
    monkeypatch.setattr(cdp.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: launched.append(args))
    monkeypatch.setattr(cdp.time, "sleep", lambda _s: None)
    assert cdp.ensure_router(timeout_s=5) is True
    assert launched == [[str(launcher), "--background"]]


def test_sans_routeur_regle_codex_pret_sans_rien_lancer(monkeypatch: pytest.MonkeyPatch):
    """Sans NEXUS_RELAY_CODEX_ROUTER : Codex parle à son fournisseur, aucune vérification ni lancement."""
    import subprocess

    monkeypatch.setattr(cdp.urllib.request, "urlopen", lambda *a, **k: pytest.fail("aucune requête"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("aucun lancement"))
    assert cdp.router_url() is None and cdp.router_ready(1.0) and cdp.ensure_router(timeout_s=1.0)


def test_routeur_sans_lanceur_pas_pret(monkeypatch: pytest.MonkeyPatch):
    """Routeur réglé mais arrêté, aucun lanceur existant : pas prêt, rien n'est lancé."""
    import subprocess

    monkeypatch.setenv("NEXUS_RELAY_CODEX_ROUTER", "http://127.0.0.1:2455/health/ready")
    monkeypatch.setenv("NEXUS_RELAY_CODEX_ROUTER_START", r"relatif.exe; C:\absent\lanceur.exe")
    monkeypatch.setattr(cdp.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("fermé")))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("aucun lancement"))
    assert cdp.ensure_router(timeout_s=1.0) is False


def test_nouveau_chat_memorise_comme_chat_du_sujet():
    """Le chat né à l'envoi est rapporté (report["conversation"]) : wake_codex le garde comme chat du sujet."""
    page = FakePage()
    report: dict[str, Any] = {}
    result = cdp.send_in_new_chat(page, cdp.wake_text([ID]), ID, sleep=lambda _: None, report=report)  # type: ignore[arg-type]
    assert result["status"] == "started" and report["conversation"] == "conv-nouveau"


def test_chat_du_sujet_repris_sans_nouveau_chat(monkeypatch: pytest.MonkeyPatch):
    """21/10 (l'utilisateur) : une conversation par sujet ; le chat du sujet est rouvert par son identité, pas de nouveau
    chat, même envoi vérifié, vue de l'utilisateur rendue."""
    page = FakePage()
    page.field = ""
    opened: list[str] = []

    def reopen(p: Any, conv: str) -> bool:
        opened.append(conv)
        p.rows = [["r-sujet", conv, True]]
        p.view = ["Accueil"]
        return True

    monkeypatch.setattr(cdp, "open_conversation", reopen)
    monkeypatch.setattr(cdp, "selected_conversation", lambda p: next((c for _, c, a in p.rows if a), None))
    report: dict[str, Any] = {}
    result = cdp.send_in_new_chat(
        page,  # type: ignore[arg-type]
        cdp.wake_text([ID]),
        ID,
        sleep=lambda _: None,
        report=report,
        existing="conv-sujet",  # type: ignore[arg-type]
    )
    assert result["status"] == "started" and opened[0] == "conv-sujet"
    assert report["conversation"] == "conv-sujet" and ID in page.body


def test_reveil_reprend_le_chat_du_sujet_ou_en_cree_un(monkeypatch: pytest.MonkeyPatch):
    """wake_codex : chat du sujet visible → repris ; absent (rangé) → nouveau chat, mémorisé ; tour en cours → reporté."""
    from nexus_relay.box import Box

    box = Box()
    message = box.post("claude", "codex", "Relis", "sujet")
    seen: dict[str, Any] = {}

    def send(page: Any, text: str, marker: str, **kwargs: Any) -> dict[str, Any]:
        seen["existing"] = kwargs["existing"]
        kwargs["report"]["conversation"] = kwargs["existing"] or "conv-neuf"
        return {"status": "started"}

    _wake_setup(monkeypatch, send)
    rows: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(cdp, "row_conversations", lambda page: rows)
    monkeypatch.setattr(cdp, "_chat_finished", lambda conv: True)
    assert cdp.wake_codex([message.id])["status"] == "started" and seen["existing"] is None
    assert box.topic_state("sujet", "codex")["conversation"] == "conv-neuf"  # type: ignore[index]
    rows.append(("r1", "conv-neuf", False))
    result = cdp.wake_codex([message.id])
    assert result["status"] == "started" and result["reused"] and seen["existing"] == "conv-neuf"
    monkeypatch.setattr(cdp, "_chat_finished", lambda conv: False)
    assert cdp.wake_codex([message.id])["status"] == "deferred"


def test_message_parti_jamais_failed_si_le_sujet_ne_s_enregistre_pas(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex (21/10) : une erreur en mémorisant le chat du sujet changeait un envoi réussi en « failed »,
    injected False : le message aurait été réinjecté."""
    from nexus_relay.box import Box

    message = Box().post("claude", "codex", "Relis", "sujet")

    def send(page: Any, text: str, marker: str, **kwargs: Any) -> dict[str, Any]:
        kwargs["report"]["conversation"] = "conv-neuf"
        return {"status": "started"}

    _wake_setup(monkeypatch, send)
    monkeypatch.setattr(cdp, "row_conversations", lambda page: [])

    def broken(topic: str, conversation: str) -> None:
        raise OSError("disque plein")

    monkeypatch.setattr(cdp, "_remember_chat", broken)
    result = cdp.wake_codex([message.id])
    assert result["status"] == "started" and "disque plein" in result["remember_error"]


def test_utilisateur_revenu_avant_l_insertion_retrouve_sa_vue(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex (21/10) : le chat du sujet ouvert par le relais passait pour une navigation de l'utilisateur ; sa
    vue restait sur le chat du relais."""

    def reopen(p: Any, conv: str) -> bool:
        p.rows = [["r-sujet", conv, True]]
        p.view = ["Accueil"]
        return True

    monkeypatch.setattr(cdp, "open_conversation", reopen)
    monkeypatch.setattr(cdp, "selected_conversation", lambda p: next((c for _, c, a in p.rows if a), None))
    page = FakePage()
    page.field = ""
    report: dict[str, Any] = {}
    result = cdp.send_in_new_chat(
        page,  # type: ignore[arg-type]
        cdp.wake_text([ID]),
        ID,
        sleep=lambda _: None,
        present=lambda: True,
        report=report,
        existing="conv-sujet",
    )
    assert result["status"] == "deferred" and "restore_skipped" not in report
    assert page.view == ["Accueil", "Mon chat"]  # sa vue rendue


def test_codex_fige_relance_meme_en_plein_tour(monkeypatch: pytest.MonkeyPatch):
    """21/10, l'utilisateur : relancer une IA qui ne répond plus. Page qui n'exécute plus rien, ou fenêtre « ne répond pas »
    depuis 2 min : relancée, même si son journal dit un tour en cours (il n'avancera pas) ; jamais l'utilisateur présent."""
    monkeypatch.setattr(cdp, "reset_hung", lambda name: None)
    result, actions = _ensure(monkeypatch, [True], responsive=lambda url: False)
    assert result == {"status": "ready", "launched": False} and actions == []  # muette 5 s : pas encore (Antigravity)
    result, actions = _ensure(
        monkeypatch, [True], responsive=lambda url: False, page_silent=lambda n, s: cdp.HUNG_RESTART_S + 1
    )
    assert result["status"] == "ready" and actions == ["stop", "start 9223"]
    result, actions = _ensure(monkeypatch, [True], hung=lambda p: cdp.HUNG_RESTART_S + 1, busy=lambda: True)
    assert result["status"] == "ready" and result.get("frozen") and actions == ["stop", "start 9223"]
    result, actions = _ensure(monkeypatch, [True], hung=lambda p: 30.0, busy=lambda: True)
    assert result == {"status": "ready", "launched": False} and actions == []  # 30 s : pas encore
    result, actions = _ensure(monkeypatch, [True], hung=lambda p: cdp.HUNG_RESTART_S + 1, present=lambda: True)
    assert result["status"] == "deferred" and actions == []


def test_duree_de_figement_continue(tmp_path, monkeypatch: pytest.MonkeyPatch):
    assert cdp.hung_for("essai", True, now=1000.0) == 0.0
    assert cdp.hung_for("essai", True, now=1100.0) == 100.0
    assert cdp.hung_for("essai", False, now=1150.0) == 0.0  # elle a répondu : on repart de zéro
    assert cdp.hung_for("essai", True, now=1200.0) == 0.0


def test_antigravity_jamais_tue_pendant_un_travail(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Relecture d'Antigravity (21/10) : l'utilisateur le laisse travailler en fond ; fenêtre muette mais agent qui avance :
    pas de relance. Relance actée : le compte de figement repart de zéro (sinon relance en boucle au démarrage)."""
    from nexus_relay import paths

    log = tmp_path / "conv" / ".system_generated" / "logs" / "transcript.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "brain_dir", lambda: tmp_path)
    assert cdp.agy_working()
    assert not cdp.agy_working(now=log.stat().st_mtime + cdp.AGY_WORK_S + 1)
    monkeypatch.setattr(cdp, "user_in_agy", lambda: False)
    assert cdp.restart_frozen_antigravity()["status"] == "deferred"
    cdp.hung_for("antigravity", True, now=1000.0)
    cdp.reset_hung("antigravity")
    assert cdp.hung_for("antigravity", True, now=5000.0) == 0.0


def test_figement_vu_par_ishungappwindow(monkeypatch: pytest.MonkeyPatch):
    """Electron : MainWindowHandle vaut 0, Process.Responding ne voyait jamais rien (relecture d'Antigravity)."""
    from nexus_relay import uia

    monkeypatch.setattr(uia, "windows", lambda process, hidden=False: [11, 12])
    monkeypatch.setattr(uia, "user32", lambda: type("U", (), {"IsHungAppWindow": staticmethod(lambda h: h == 12)})())
    assert cdp.app_hung("Antigravity.exe")
    monkeypatch.setattr(uia, "windows", lambda process, hidden=False: [11])
    assert not cdp.app_hung("Antigravity.exe")


def test_chat_du_sujet_envoi_prouve_par_le_tour_demarre(monkeypatch: pytest.MonkeyPatch):
    """22/10 : chat du sujet long, message pas encore affiché dans le délai : le tour démarré dans CE chat (journal de
    Codex) prouve l'envoi ; sans l'un ni l'autre, « uncertain » comme avant."""
    for turn_running, expected in ((True, "started"), (False, "uncertain")):
        page = FakePage()
        page.field = ""

        def reopen(p: Any, conv: str) -> bool:
            p.rows = [["r-sujet", conv, True]]
            p.view = ["Accueil"]
            return True

        monkeypatch.setattr(cdp, "open_conversation", reopen)
        monkeypatch.setattr(cdp, "selected_conversation", lambda p: next((c for _, c, a in p.rows if a), None))
        monkeypatch.setattr(cdp, "_chat_finished", lambda conv, r=turn_running: not r)
        monkeypatch.setattr(cdp, "EXISTING_SENT_WAIT_S", 0.0)
        original = page.call

        def call(method: str, page: FakePage = page, original: Any = original, **params: Any) -> Any:
            result = original(method, **params)
            if method == "Input.dispatchKeyEvent" and params.get("type") == "keyUp":
                page.body = "ancien chat"  # message parti (champ vidé) mais pas encore affiché
            return result

        page.call = call  # type: ignore[method-assign]
        result = cdp.send_in_new_chat(
            page,  # type: ignore[arg-type]
            cdp.wake_text([ID]),
            ID,
            sleep=lambda _: None,
            report={},
            existing="conv-sujet",  # type: ignore[arg-type]
        )
        assert result["status"] == expected


@pytest.mark.parametrize(("running", "said"), [(True, "n'écoute pas : le relancer"), (False, "fermé ou page absente")])
def test_reperes_antigravity_ouvert_sans_son_port(monkeypatch: pytest.MonkeyPatch, running: bool, said: str):
    # vu le 22/10 : relancé par sa mise à jour, Antigravity tournait sans son port ; le dire, au lieu de « fermé »
    monkeypatch.setattr(cdp, "page_url", lambda port=0, url="": None)
    monkeypatch.setattr(cdp, "agy_page_url", lambda port=0: None)
    monkeypatch.setattr(cdp, "agy_running", lambda: running)
    assert said in cdp.landmarks()["antigravity"]["reason"]


def test_reveil_quitte_les_parametres_pour_trouver_le_projet(monkeypatch: pytest.MonkeyPatch):
    """23/10 (appli 26.1016.8622) : laissée sur ses Paramètres, l'appli masque la liste des projets ; le relais ouvre
    l'Accueil (barre de navigation) et trouve le projet, sans toucher à la barre latérale."""
    state, clicks = _wake_setup(monkeypatch, lambda *a, **k: {"status": "started", "seconds": 1.0}, relay_button=False)
    pressed: list[str] = []

    def press(page: Any, element_js: str) -> bool:
        pressed.append(element_js)
        state["home"] = True
        return True

    monkeypatch.setattr(cdp, "_press", press)
    result = cdp.wake_codex([ID])
    assert pressed == [cdp._HOME]
    assert result["status"] == "started" and state["sent"] == 1
    assert cdp.SHOW_SIDEBAR not in clicks


@pytest.mark.parametrize(
    ("user", "working", "relaunched"), [(False, False, True), (True, False, False), (False, True, False)]
)
def test_antigravity_ouvert_sans_port_relance_avec_gardes(
    monkeypatch: pytest.MonkeyPatch, user: bool, working: bool, relaunched: bool
):
    """23/10 : Antigravity ouvert sans port ni sidecar restait injoignable sans fin. Relancé avec son port, jamais quand
    l'utilisateur est dedans ni quand un de ses agents travaille."""
    calls: list[str] = []

    class Lock:
        def acquire(self) -> bool:
            return True

        def release(self) -> None:
            pass

    monkeypatch.setattr(cdp, "agy_running", lambda: True)
    monkeypatch.setattr(cdp, "user_in_agy", lambda: user)
    monkeypatch.setattr(cdp, "agy_working", lambda now=None: working)
    monkeypatch.setattr(cdp, "agy_exe", lambda: Path("C:/Antigravity/Antigravity.exe"))
    monkeypatch.setattr(cdp, "_foreground_lock", Lock)
    monkeypatch.setattr(cdp, "_ps", lambda command, timeout=30.0: calls.append("stop") or "")
    monkeypatch.setattr(cdp, "_start_detached", lambda path, port: calls.append(f"start:{port}"))
    monkeypatch.setattr(cdp.time, "sleep", lambda s: None)
    result = cdp.start_antigravity()
    assert (calls[:1] == ["stop"] and calls[1:2] == ["start:9227"]) is relaunched  # son vrai port
    assert result.get("launched", False) is relaunched
    if not relaunched:
        assert result["status"] == "deferred" and calls == []


def test_reveil_reporte_si_le_chat_du_sujet_reste_introuvable(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex : chat du sujet ouvert dans l'appli mais introuvable → réveil reporté, jamais un second chat."""
    state, clicks = _wake_setup(monkeypatch, lambda *a, **k: pytest.fail("aucun nouveau chat"))
    monkeypatch.setattr(cdp, "_topic_chat", lambda ids, page: ("nexuswindows", None, True))
    rendus: list[Any] = []
    monkeypatch.setattr(cdp, "_restore_view", lambda page, before, present=None: rendus.append(present) or True)
    result = cdp.wake_codex([ID])
    assert result["status"] == "deferred" and result["injected"] is False and state["sent"] == 0
    assert rendus == [cdp.user_in_codex]  # vue rendue dans le finally, avec la garde de présence


def test_vue_non_rendue_si_utilisateur_est_revenu():
    clics: list[Any] = []

    class Page:
        def eval(self, expression: str) -> Any:
            return ["Accueil"]

    import nexus_relay.cdp as module

    vieux = module._click
    module._click = lambda page, labels: clics.append(labels) or True
    try:
        assert cdp._restore_view(Page(), ["Paramètres"], lambda: True) is False
    finally:
        module._click = vieux
    assert clics == []


def test_rien_n_est_repris_de_force_si_utilisateur_est_revenu(monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex : l'utilisateur revenu pendant la recherche du chat → ni barre latérale, ni vue, ni mode touchés."""
    state, clicks = _wake_setup(monkeypatch, lambda *a, **k: pytest.fail("aucun envoi"))
    present = {"oui": False}

    def topic_chat(ids: Any, page: Any) -> tuple[str, None, bool]:
        present["oui"] = True  # l'utilisateur revient pendant la recherche
        return "nexuswindows", None, True

    monkeypatch.setattr(cdp, "_topic_chat", topic_chat)
    monkeypatch.setattr(cdp, "user_in_codex", lambda: present["oui"])
    monkeypatch.setattr(cdp, "_restore_view", lambda *a, **k: pytest.fail("vue reprise malgré l'utilisateur"))
    result = cdp.wake_codex([ID])
    assert result["status"] == "deferred" and result["restore_skipped"]
    assert state["mode"] == "Codex" and cdp.HIDE_SIDEBAR not in clicks
