"""Cartes des interfaces : index, repères, différence, stockage et where, hors ligne (aucune vraie appli touchée)."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

from nexus_relay import cartes


def _asar(path: Path, files: dict[str, bytes]) -> Path:
    """Petit app.asar : en-tête JSON (format Electron) puis contenus."""
    tree: dict[str, Any] = {"files": {}}
    blob, offset = b"", 0
    for name, data in files.items():
        node = tree
        *folders, leaf = name.strip("/").split("/")
        for folder in folders:
            node = node["files"].setdefault(folder, {"files": {}})
        node["files"][leaf] = {"size": len(data), "offset": str(offset)}
        blob, offset = blob + data, offset + len(data)
    header = json.dumps(tree).encode()
    pickled = struct.pack("<I", len(header)) + header
    pickled = struct.pack("<I", len(pickled)) + pickled
    path.write_bytes(struct.pack("<I", 4) + struct.pack("<I", len(pickled)) + pickled + blob)
    return path


def test_codex_index_reads_translations_from_the_package(tmp_path: Path) -> None:
    locale = b'const e={"chatgptConversations.sidebar.archive":`Archiver le chat`,"a.b":"Nouveau chat"};export{e as default};'
    menu = json.dumps({"codex.command.archiveThread": "Archiver le chat"}).encode()
    asar = _asar(
        tmp_path / "app.asar",
        {
            "/webview/assets/fr-FR-5068e215a06b.js": locale,
            "/webview/assets/en-US-1111.js": b'{"a.b":"New chat"}',
            "/native-menu-locales/fr-FR.json": menu,
            "/webview/assets/main.js": b"x",
        },
    )
    labels = cartes.codex_index("fr-FR", asar)
    assert labels == {
        "chatgptConversations.sidebar.archive": "Archiver le chat",
        "a.b": "Nouveau chat",
        "menu:codex.command.archiveThread": "Archiver le chat",
    }


def test_bundle_labels_keep_ui_strings_only() -> None:
    source = 'x({"aria-label":"Archive conversation"}),y({title:"New Conversation",label:"a-b",children:"{x}"})'
    assert set(cartes.parse_bundle(source)) == {"Archive conversation", "New Conversation"}


def test_danger_never_opens_actions() -> None:
    for label in (
        "Envoyer",
        "Send message",
        "Archiver",
        "Supprimer",
        "Se déconnecter",
        "Log out",
        "Nouvel onglet",
        "Clear chat",
        "Effacer l'historique",
        "Run now",
        "Exécuter",
        "Export",
        "Télécharger",
        "Revoke",
    ):
        assert cartes.DANGER.search(label), label
    for label in ("Accueil", "Plugins", "Paramètres", "Actions du chat", "Changer de mode, mode actuel : Codex"):
        assert not cartes.DANGER.search(label), label


def test_repeated_rows_are_collapsed_without_titles() -> None:
    rows = [
        {"role": "link", "aria": "", "text": f"chat {i}", "data": {"data-row": ""}, "region": "nav"} for i in range(6)
    ]
    out = cartes._collapse([*rows, {"role": "button", "aria": "Nouveau chat", "text": "", "data": {}, "region": ""}])
    assert len(out) == 2
    assert out[0]["repeated"] == 6 and out[0]["text"] == ""


def test_locator_prefers_stable_attributes() -> None:
    assert cartes.locator_of({"role": "button", "aria": "X", "data": {"data-testid": "send-button"}}) == {
        "css": '[data-testid="send-button"]'
    }
    assert cartes.locator_of({"role": "button", "aria": "Nouveau chat", "data": {"data-color": "x"}}) == {
        "role": "button",
        "aria": "Nouveau chat",
    }


def _card(version: str, elements: list[dict[str, Any]], at: str, index: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "app": "codex",
        "version": version,
        "surveyed_at": at,
        "elements": elements,
        "landmarks": cartes.landmark_status("codex", elements),
        "index": index or {},
    }


NEW_CHAT = {"role": "button", "aria": "Nouveau chat", "text": "", "data": {}, "path": ["vue de départ"]}
MODE = {
    "role": "button",
    "aria": "Changer de mode, mode actuel : Codex",
    "popup": "menu",
    "data": {},
    "path": ["vue de départ"],
}
ROW_ARCHIVE = {"role": "button", "aria": "Archiver le chat", "text": "", "data": {}, "path": ["vue de départ"]}
ACTIONS = {"role": "button", "aria": "Actions du chat", "popup": "menu", "data": {}, "path": ["vue de départ"]}
MENU_ARCHIVE = {
    "role": "menuitem",
    "aria": "",
    "text": "Archiver",
    "data": {},
    "path": ["vue de départ", "menu « Actions du chat »"],
}


def test_diff_shows_archive_moved_into_chat_actions_menu() -> None:
    """Le cas du 20/10 : « Archiver » était un bouton de la ligne, il est passé dans le menu « Actions du chat »."""
    old = _card("26.1001.0", [NEW_CHAT, ROW_ARCHIVE], "2026-10-01T10:00:00Z", {"k.archive": "Archiver le chat"})
    new = _card(
        "26.1013.4894.0", [NEW_CHAT, MODE, ACTIONS, MENU_ARCHIVE], "2026-10-20T10:00:00Z", {"k.archive": "Archiver"}
    )
    change = cartes.diff(old, new)
    assert "repère ARCHIVE maintenant aussi : vue de départ > menu « Actions du chat »" in change["landmarks"]
    assert "repère ARCHIVE n'est plus : vue de départ" in change["landmarks"]  # plus de bouton sur la ligne
    assert "repère CHAT_ACTIONS trouvé : vue de départ" in change["landmarks"]
    assert "repère MODE trouvé : vue de départ" in change["landmarks"]
    assert any("Archiver le chat" in r for r in change["removed"])
    assert any("« Archiver »" in a and "Actions du chat" in a for a in change["added"])
    assert change["labels"]["changed"] == ["k.archive : « Archiver le chat » → « Archiver »"]
    assert "Actions du chat" in cartes.summary(change)


def test_diff_flags_a_vanished_landmark() -> None:
    old = _card("1", [NEW_CHAT, MODE, ACTIONS, MENU_ARCHIVE], "2026-10-20T10:00:00Z")
    new = _card("2", [NEW_CHAT, MODE], "2026-10-21T10:00:00Z")
    landmarks = cartes.diff(old, new)["landmarks"]
    assert "REPÈRE DISPARU ARCHIVE (était : vue de départ > menu « Actions du chat »)" in landmarks
    assert "REPÈRE DISPARU CHAT_ACTIONS (était : vue de départ)" in landmarks


def test_diff_reports_moved_elements() -> None:
    old = _card("1", [{**MODE, "path": ["page « Paramètres »"]}], "a")
    new = _card("2", [MODE], "b")
    assert cartes.diff(old, new)["moved"] == [
        "button « Changer de mode, mode actuel : Codex » : page « Paramètres » → vue de départ"
    ]


def test_save_versions_cards_and_writes_the_diff(isolated: Path) -> None:
    first = cartes.save(_card("26.1001.0", [NEW_CHAT, ROW_ARCHIVE], "2026-10-01T10:00:00Z"))
    assert "diff" not in first
    second = cartes.save(_card("26.1013.4894.0", [NEW_CHAT], "2026-10-20T10:00:00Z"))
    folder = isolated / "relay" / "v1" / "cartes"
    assert sorted(p.name for p in folder.iterdir()) == [
        "codex-26.1001.0.json",
        "codex-26.1013.4894.0.diff.json",
        "codex-26.1013.4894.0.json",
    ]
    assert second["alerts"] == ["REPÈRE DISPARU ARCHIVE (était : vue de départ)"]
    assert cartes.latest("codex")["version"] == "26.1013.4894.0"  # type: ignore[index]


def test_where_gives_path_landmark_and_gestures(isolated: Path) -> None:
    cartes.save(_card("26.1013.4894.0", [NEW_CHAT, ACTIONS, MENU_ARCHIVE], "2026-10-20T10:00:00Z", {"x.a": "Archiver"}))
    found = cartes.where("codex", "archiver")
    hit = found["found"][0]
    assert hit["path"] == "vue de départ > menu « Actions du chat »"
    assert hit["landmark"] == "ARCHIVE"
    assert hit["how"] == [
        {
            "step": "menu « Actions du chat »",
            "gesture": "clic émulé (cdp._press)",
            "locator": {"role": "button", "aria": "Actions du chat"},
        },
        {"step": "élément", "locator": {"role": "menuitem", "text": "Archiver"}},
    ]
    assert cartes.where("codex", "CHAT_ACTIONS")["found"][0]["label"] == "Actions du chat"
    missing = cartes.where("codex", "Archiver")  # libellé aussi dans l'index du paquet
    assert missing["labels"] == [{"key": "x.a", "text": "Archiver"}]
    assert cartes.where("codex", "zzz")["note"] == "pas vu au relevé"
    assert cartes.where("antigravity", "x")["status"] == "error"
    with pytest.raises(ValueError):
        cartes.where("word", "x")


def test_refresh_only_when_the_version_changes(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(cartes, "VERSIONS", {"codex": lambda: "2", "antigravity": lambda: None})
    monkeypatch.setattr(cartes.SPECS["codex"], "port", lambda: "ws://x")
    monkeypatch.setattr(cartes.SPECS["codex"], "present", lambda: False)
    monkeypatch.setattr(cartes, "survey", lambda app: started.append(app))
    monkeypatch.setattr(cartes, "_CHECKED", {})
    cartes.save(
        {**_card("2", [NEW_CHAT], "2026-10-20T10:00:00Z"), "live": {"status": "complet", "view_restored": True}}
    )
    assert cartes.refresh_due(clock=lambda: 10_000.0) == []  # même version, carte complète : rien
    monkeypatch.setattr(cartes, "VERSIONS", {"codex": lambda: "3", "antigravity": lambda: None})
    assert cartes.refresh_due(clock=lambda: 10_001.0) == []  # version relue au plus toutes les 30 min
    started_now = cartes.refresh_due(clock=lambda: 20_000.0)
    assert started_now == [{"app": "codex", "version": "3"}]


def test_refresh_waits_while_user_is_in_the_app(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cartes, "VERSIONS", {"codex": lambda: "3", "antigravity": lambda: None})
    monkeypatch.setattr(cartes.SPECS["codex"], "port", lambda: "ws://x")
    monkeypatch.setattr(cartes.SPECS["codex"], "present", lambda: True)
    monkeypatch.setattr(cartes, "_CHECKED", {})
    assert cartes.refresh_due(clock=lambda: 10_000.0) == []
    assert cartes.refresh_due(clock=lambda: 10_100.0) == []  # revu 5 min plus tard, pas avant
    monkeypatch.setattr(cartes.SPECS["codex"], "present", lambda: False)
    monkeypatch.setattr(cartes, "survey", lambda app: None)
    assert cartes.refresh_due(clock=lambda: 10_301.0) == [{"app": "codex", "version": "3"}]


def test_server_where_and_survey_tools(isolated: Path) -> None:
    from nexus_relay import server

    cartes.save(_card("26.1013.4894.0", [NEW_CHAT, ACTIONS, MENU_ARCHIVE], "2026-10-20T10:00:00Z"))
    found = json.loads(server.where("codex", "ARCHIVE"))
    assert found["status"] == "success" and found["found"][0]["landmark"] == "ARCHIVE"
    assert json.loads(server.where("chatgpt", "Nouveau chat"))["found"][0]["label"] == "Nouveau chat"
    assert json.loads(server.where("claude", "x"))["status"] == "error"
    overview = cartes.overview()
    assert overview["codex"]["missing"] == ["COMPOSER", "MODE"]
    assert overview["antigravity"] == {"status": "pas encore de carte"}


NO_CHAT = {"role": "listitem", "aria": "", "text": "Aucun chat", "data": {}, "path": ["vue de départ"]}


def test_landmark_unverifiable_without_any_chat_and_where_uses_an_older_card(isolated: Path) -> None:
    """Barre latérale vide : « Actions du chat » et « Archiver » ne se voient pas. Pas « disparus » ; where reprend
    l'emplacement de la carte précédente."""
    cartes.save(_card("26.1013.4894.0", [NEW_CHAT, ACTIONS, MENU_ARCHIVE], "2026-10-20T10:00:00Z"))
    empty = _card("26.1013.4894.0", [NEW_CHAT, NO_CHAT], "2026-10-20T12:00:00Z")
    assert empty["landmarks"]["ARCHIVE"] == {"unverified": "non vérifiable : « Aucun chat » (vue de départ)"}
    change = cartes.diff(_card("x", [NEW_CHAT, ACTIONS, MENU_ARCHIVE], "a"), empty)
    assert not [line for line in change["landmarks"] if "DISPARU" in line]
    assert (
        "repère ARCHIVE non vérifiable : « Aucun chat » (vue de départ) (était : vue de départ > menu « Actions du chat »)"
        in change["landmarks"]
    )
    cartes.save({**empty, "version": "26.1013.4894.1"})  # relevé suivant, barre latérale vide
    found = cartes.where("codex", "ARCHIVE")
    assert found["version"] == "26.1013.4894.1"
    assert found["from_older_card"] == "26.1013.4894.0 (2026-10-20T10:00:00Z) : pas vu au dernier relevé"
    assert found["found"][0]["path"] == "vue de départ > menu « Actions du chat »"
    assert cartes.overview()["codex"]["unverified"] == ["ARCHIVE", "CHAT_ACTIONS"]


class FakePage:
    """Page factice : enregistre le JavaScript envoyé (aucune vraie appli)."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def eval(self, expression: str) -> Any:
        self.sent.append(expression)
        return {"href": "app://-/index.html", "current": []} if "aria-current" in expression else None

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self.sent.append(method)
        return {}


def test_generated_javascript_has_no_doubled_braces(monkeypatch: pytest.MonkeyPatch) -> None:
    """Régression du 20/10 : une ligne non-f-string après une f-string gardait « }} » (JavaScript invalide), la vue
    d'Antigravity n'était pas rendue."""
    monkeypatch.setattr(cartes.SPECS["antigravity"], "present", lambda: False)  # pas le vrai premier plan du poste
    page = FakePage()
    surveyor = cartes.Surveyor(page, cartes.SPECS["antigravity"], sleep=lambda s: None)
    surveyor.hover({"css": "a"})
    surveyor.open({"aria": "More options"})
    surveyor.escape()
    surveyor.view()
    page.eval = lambda expression: page.sent.append(expression) or {"href": "x"}  # type: ignore[method-assign]
    surveyor.restore({"href": "y", "current": []})
    assert page.sent and all("}}" not in js and "{{" not in js for js in page.sent), [
        js for js in page.sent if "}}" in js
    ]


def test_survey_and_wakes_never_drive_the_port_together(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vu le 20/10 : un relevé qui naviguait pendant un réveil de Codex a laissé le texte du réveil dans son champ ;
    11 réveils ont ensuite refusé d'écrire (« brouillon de l'utilisateur ? »). Un seul pilote à la fois PAR appli (tour 2 :
    un réveil de Codex ne bloque plus le rangement d'Antigravity, et inversement)."""
    from nexus_relay import cdp, codex_app

    monkeypatch.setattr(
        cdp, "page_url", lambda port=0, url="": "ws://codex"
    )  # port « ouvert » : seul le verrou retient
    monkeypatch.setattr(cdp, "user_in_agy", lambda: False)
    monkeypatch.setattr(cdp, "user_in_codex", lambda: False)  # jamais l'écran réel (Codex au premier plan)
    monkeypatch.setattr(cdp, "Page", lambda url: type("P", (), {"close": lambda self: None})())
    monkeypatch.setattr(cdp, "codex_mode", lambda page: None)
    monkeypatch.setattr(cdp, "ensure_codex_mode", lambda page: False)  # Codex libre : va jusqu'à la page, échoue
    monkeypatch.setattr(cartes, "UI_WAIT_S", 0.01)
    monkeypatch.setattr(cartes, "VERSIONS", {"codex": lambda: "1", "antigravity": lambda: "1"})
    assert cdp.UI_LOCK is cdp.UI_LOCKS["codex"] and cdp.UI_LOCKS["codex"] is not cdp.UI_LOCKS["antigravity"]
    assert cdp.UI_LOCKS["codex"].acquire(blocking=False)
    try:  # Codex tenu : tout geste dans Codex est reporté
        assert cdp.wake_codex(["m1"]) == {"status": "deferred", "reason": cdp.BUSY_REASON, "injected": False}
        assert codex_app.archive_chats([]) == {"status": "deferred", "reason": cdp.BUSY_REASON}
        assert cartes.survey("codex")["status"] == "deferred"
        # … mais Antigravity reste libre (port fermé dans les tests : le geste va jusqu'au port, pas reporté)
        status, reason = cdp.archive_antigravity_conversation("x")
        assert status == "skipped" and reason != cdp.BUSY_REASON and "fermé" in (reason or "")
        assert cdp.read_antigravity_usage()["reason"] != cdp.BUSY_REASON
    finally:
        cdp.UI_LOCKS["codex"].release()
    assert cdp.UI_LOCKS["antigravity"].acquire(blocking=False)
    try:  # Antigravity tenu : ses gestes sont reportés, Codex reste libre
        assert cdp.archive_antigravity_conversation("x") == ("skipped", cdp.BUSY_REASON)
        assert cdp.read_antigravity_usage() == {"status": "failed", "reason": cdp.BUSY_REASON}
        assert cartes.survey("antigravity")["status"] == "deferred"
        assert codex_app.archive_chats([]) == {"status": "failed", "reason": "mode Codex de l'appli non sélectionné"}
    finally:
        cdp.UI_LOCKS["antigravity"].release()


def test_never_reads_field_values_nor_unknown_labels() -> None:
    """Relecture de Codex (P1) : ni valeur de champ (mot de passe, brouillon), ni titre de chat, nom ou adresse."""
    assert "e.value" not in cartes._SNAPSHOT  # seule la valeur d'attributs data-* est lue (a.value)
    known = cartes.Known(
        ["Nouveau chat", "Changer de mode, mode actuel : {mode}", "Select model, current: ${x}", "${a} · ${b}"]
    )
    assert "Nexus Relay · mon sujet" not in known  # modèle trop vague : ignoré
    items = [
        {"role": "button", "aria": "Nouveau chat", "text": ""},
        {"role": "button", "aria": "Changer de mode, mode actuel\xa0: Codex", "text": ""},
        {"role": "button", "aria": "Select model, current: Gemini 3.8 Flash", "text": ""},
        {"role": "link", "aria": "", "text": "Mon projet secret"},
        {"role": "menuitem", "aria": "", "text": "user@example.com"},
    ]
    assert [(i["aria"], i["text"]) for i in known.mask(items)] == [
        ("Nouveau chat", ""),
        ("Changer de mode, mode actuel : {mode}", ""),  # le modèle, jamais la valeur substituée
        ("Select model, current: ${x}", ""),
        ("", "*"),
        ("", "*"),
    ]
    assert all(i["aria"] in ("", "*") and i["text"] in ("", "*") for i in cartes.Known(()).mask(items))


def test_bundle_strings_feed_the_known_labels() -> None:
    assert cartes.bundle_strings('a("Archive conversation"),b(`Select model, current: ${m}`)') == {
        "Archive conversation",
        "Select model, current: ${m}",
    }


class ScriptedPage(FakePage):
    def __init__(self, answers: dict[str, Any]) -> None:
        super().__init__()
        self.answers = answers

    def eval(self, expression: str) -> Any:
        self.sent.append(expression)
        return next((v for k, v in self.answers.items() if k in expression), None)


def test_navigation_only_to_internal_routes() -> None:
    """Relecture de Codex (P1) : un lien externe, mailto ou d'authentification n'est jamais une page à ouvrir."""
    page = ScriptedPage({"e.matches": True})
    surveyor = cartes.Surveyor(page, cartes.SPECS["antigravity"], sleep=lambda s: None)
    assert surveyor.allowed({"role": "link", "href": "/customizations"}, "a")
    for href in ("externe", "#x", "mailto:x"):
        assert not surveyor.allowed({"role": "link", "href": href}, "a") or href == "#"


def test_no_gesture_once_user_is_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relecture de Codex (P1/P2) : l'utilisateur revenu, ni Échap, ni remise en place de la vue."""
    spec = cartes.AppSpec("x", lambda: "ws://x", lambda: True, pages="a", subpages="b", menu_pages=())
    page = ScriptedPage({"aria-current": {"href": "now", "current": []}, "role=menu": 1})
    surveyor = cartes.Surveyor(page, spec, sleep=lambda s: None)
    assert surveyor.restore({"href": "before", "current": []}) is False
    assert not [js for js in page.sent if "pushState" in js]
    with pytest.raises(cartes.Abort):
        surveyor.close_layers(0)
    assert "Input.dispatchKeyEvent" not in page.sent and not [js for js in page.sent if "Escape" in js]


def test_partial_card_never_counts_for_its_version(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Relecture de Codex (P2) : un relevé interrompu est rangé à part et refait plus tard."""
    full = {**_card("3", [NEW_CHAT], "2026-10-20T10:00:00Z"), "live": {"status": "complet", "view_restored": True}}
    cartes.save(full)
    partial = {**_card("4", [NEW_CHAT], "2026-10-21T10:00:00Z"), "live": {"status": "interrompu : l'utilisateur"}}
    saved = cartes.save(partial)
    assert saved["partial"] and saved["card"].endswith("codex-4.partiel.json")
    assert cartes.latest("codex")["version"] == "3"  # type: ignore[index]
    monkeypatch.setattr(cartes, "VERSIONS", {"codex": lambda: "4", "antigravity": lambda: None})
    monkeypatch.setattr(cartes.SPECS["codex"], "port", lambda: "ws://x")
    monkeypatch.setattr(cartes.SPECS["codex"], "present", lambda: False)
    monkeypatch.setattr(cartes, "survey", lambda app: None)
    monkeypatch.setattr(cartes, "_CHECKED", {})
    assert cartes.refresh_due(clock=lambda: 10_000.0) == [{"app": "codex", "version": "4"}]


def test_archive_lock_released_when_the_page_cannot_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relecture de Codex (P1) : appli fermée pendant le rangement, verrou rendu quand même ; tour 2 : un échec noté,
    jamais une exception qui interrompt le rangement."""
    from nexus_relay import cdp, codex_app

    monkeypatch.setattr(cdp, "page_url", lambda *a, **k: "ws://x")

    def broken(url: str) -> None:
        raise OSError("appli fermée")

    monkeypatch.setattr(cdp, "Page", broken)
    result = codex_app.archive_chats([])
    assert result["status"] == "failed" and "appli fermée" in result["reason"]
    assert not cdp.UI_LOCK.locked()


def test_every_stored_field_follows_the_policy() -> None:
    """2e relecture de Codex (P1) : région, data-* et routes aussi ; un modèle est stocké sans sa valeur."""
    known = cartes.Known(["Supprimer {filename}", "Actions pour {path}", "/customizations", "Settings"])
    [item] = known.mask(
        [
            {
                "role": "button",
                "aria": "Supprimer SYNTHETIC_PRIVATE_ATTACHMENT.txt",
                "text": "",
                "region": "dialog:SYNTHETIC_PRIVATE_DIALOG_TITLE",
                "data": {
                    "data-path": "C:/private/SYNTHETIC_PRIVATE_FILE.txt",
                    "data-testid": "send-button",
                    "data-x": "",
                },
                "href": "/projects/SYNTHETIC_PRIVATE_SLUG",
            }
        ]
    )
    assert item["aria"] == "Supprimer {filename}"
    assert item["region"] == "dialog:*"
    assert item["data"] == {"data-path": "*", "data-testid": "send-button", "data-x": ""}
    assert item["href"] == "/*/*"
    assert "SYNTHETIC" not in json.dumps(item)
    assert known.route("/customizations") == "/customizations" and known.route("externe") == "externe"
    assert known.mask([{"role": "dialog", "aria": "", "region": "dialog:Settings"}])[0]["region"] == "dialog:Settings"


def test_user_back_during_a_wait_stops_the_next_gesture(monkeypatch: pytest.MonkeyPatch) -> None:
    """2e relecture de Codex (P1/P2) : présent pendant l'attente d'un menu → le clic suivant n'est pas fait."""
    state = {"present": False}
    spec = cartes.AppSpec("x", lambda: "ws://x", lambda: state["present"], pages="a", subpages="b", menu_pages=("P",))
    page = ScriptedPage({"e.matches": True, "role=menu": 0})

    def sleep(seconds: float) -> None:
        state["present"] = True  # l'utilisateur revient pendant l'attente

    surveyor = cartes.Surveyor(page, spec, cartes.Known(["P"]), sleep=sleep)
    from nexus_relay import cdp

    monkeypatch.setattr(cdp, "_press", lambda page_, js: page.sent.append("PRESS") or True)
    target = {"via_menu": {"role": "button", "aria": "Menu"}, "entry": {"role": "menuitem", "text": "P"}}
    with pytest.raises(cartes.Abort):
        surveyor.visit(target, "P", "", [])
    assert page.sent.count("PRESS") == 1  # le menu a été ouvert ; l'entrée, jamais cliquée


def test_card_with_view_not_restored_is_partial(isolated: Path) -> None:
    """2e relecture de Codex (P2) : vue non rendue = relevé à refaire, rangé à part."""
    card = {**_card("5", [NEW_CHAT], "2026-10-22T10:00:00Z"), "live": {"status": "complet", "view_restored": False}}
    assert not cartes.complete(card)
    assert cartes.save(card)["partial"]


def test_package_escapes_and_the_most_precise_template() -> None:
    """3e relecture de Codex (P2) : le paquet écrit « mode actuel\xa0: {mode} » ; deux modèles concurrents."""
    labels = cartes.parse_locale(
        r'{"a.trigger":`Changer de mode, mode actuel\xA0: {mode}`,"b.model":"Changer de {model}","c":"L’onglet"}'
    )
    assert labels["a.trigger"] == "Changer de mode, mode actuel\xa0: {mode}" and labels["c"] == "L’onglet"
    for order in (list(labels.values()), list(reversed(labels.values()))):
        known = cartes.Known(order)
        assert known.canon("Changer de mode, mode actuel\xa0: Codex") == "Changer de mode, mode actuel : {mode}"
    shown = {"role": "button", "aria": "Changer de mode, mode actuel\xa0: Codex", "popup": "menu"}
    item = cartes.Known(labels.values()).mask([shown])[0]
    marks = cartes.landmark_status("codex", [{**item, "path": ["vue de départ"]}])
    assert marks["MODE"] is not None


def test_template_labels_become_template_selectors() -> None:
    """3e relecture de Codex (P2) : un modèle n'est jamais comparé tel quel ; sélecteur « aria_re » vérifié."""
    import re

    locator = cartes.locator_of({"role": "button", "aria": "Changer de mode, mode actuel : {mode}", "data": {}})
    assert locator == {"role": "button", "aria_re": r"^Changer de mode, mode actuel : .+$"}
    assert re.fullmatch(locator["aria_re"][1:-1], "Changer de mode, mode actuel : Codex")
    weird = cartes.template_regex("Coût (x) ${n} [beta]?")
    assert re.fullmatch(weird[1:-1], "Coût (x) 12 [beta]?") and not re.fullmatch(weird[1:-1], "Coût x 12 beta")
    assert "aria_re" in cartes._element_js(locator)


def test_restore_leaves_codex_settings_by_destination_then_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vu le 20/10 : relevé fini sur les Paramètres de Codex (pas de barre latérale de chats) ; vue d'avant rendue en
    recliquant la destination, puis le chat."""
    from nexus_relay import cdp

    home = {"data": {"data-sidebar-destination": "builtin:home"}}
    chat = {"data": {"data-app-action-sidebar-thread-id": "t1"}}
    before = {"href": "app://-/index.html", "current": [home, chat], "layers": 0}
    views = [
        {"href": "app://-/index.html", "current": [{"aria": "Général", "text": "Général", "data": {}}], "layers": 0},
        {"href": "app://-/index.html", "current": [home], "layers": 0},
        before,
    ]
    state = {"view": views[0]}
    pressed: list[str] = []

    def press(page: Any, js: str) -> bool:
        if "builtin:home" in js:
            pressed.append("home")
            state["view"] = views[1]
            return True
        if "t1" in js and state["view"] is views[1]:
            pressed.append("chat")
            state["view"] = views[2]
            return True
        return False

    monkeypatch.setattr(cdp, "_press", press)
    page = ScriptedPage({})
    page.eval = lambda expression: state["view"] if "aria-current" in expression else 0  # type: ignore[method-assign]
    surveyor = cartes.Surveyor(page, cartes.SPECS["codex"], sleep=lambda s: None)
    monkeypatch.setattr(cartes.SPECS["codex"], "present", lambda: False)
    assert surveyor.restore(before, steps=5) is True
    assert pressed == ["home", "chat"]


def test_chat_actions_unverifiable_without_an_open_chat() -> None:
    assert cartes.landmark_status("codex", [NEW_CHAT], {"chat_open": False})["CHAT_ACTIONS"] == {
        "unverified": "non vérifiable : aucun chat ouvert au départ du relevé"
    }
    assert cartes.landmark_status("codex", [NEW_CHAT], {"chat_open": True})["CHAT_ACTIONS"] is None


def test_escaped_bundle_labels_reach_where(isolated: Path) -> None:
    """4e relecture de Codex (P2) : libellés réels du bundle d'Antigravity avec \u2026 ou \u2190, de la source à where."""
    source = (
        r'const x = {placeholder:"Search skills\u2026"}, y = {"aria-label":"Previous (\u2190)"}, z = `Next (\u2192)`;'
    )
    assert {"Search skills…", "Previous (←)", "Next (→)"} <= cartes.bundle_strings(source)
    assert set(cartes.parse_bundle(source)) == {"Search skills…", "Previous (←)"}
    known = cartes.Known(cartes.bundle_strings(source))
    element = known.mask([{"role": "textbox", "aria": "Search skills…", "text": "", "data": {}}])[0]
    assert element["aria"] == "Search skills…"
    card = {
        "app": "antigravity",
        "version": "2.24.0.0",
        "surveyed_at": "2026-10-20T10:00:00Z",
        "elements": [{**element, "path": ["vue de départ"]}],
        "landmarks": {},
        "live": {"status": "complet", "view_restored": True},
    }
    cartes.save(card)
    assert cartes.where("antigravity", "Search skills…")["found"][0]["label"] == "Search skills…"
