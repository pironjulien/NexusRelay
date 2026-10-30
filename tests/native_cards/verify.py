"""Offline Python-oracle parity plus real DOM survey in a fresh, headless, local-only Chrome fixture."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src")]
state = Path(tempfile.mkdtemp(prefix="relay-card-contracts-", dir=Path(os.environ["LOCALAPPDATA"]) / "NexusRelay"))
os.environ["NEXUS_RELAY_HOME"] = str(state / "relay")
os.environ["NEXUS_RELAY_LANG"] = "fr"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True
from nexus_relay import cartes  # noqa: E402 - isolate profile and bytecode before importing the Python oracle

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
subprocess.run(
    ["dotnet", "build", str(Path(__file__).with_name("CardsFixture.csproj")), "--nologo", "-v:q"],
    check=True,
    creationflags=NO_WINDOW,
)
exe = Path(os.environ["LOCALAPPDATA"]) / "NexusRelay/card-contracts/bin/Debug/net10.0-windows/CardsFixture.exe"
driver = subprocess.Popen(
    [str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", creationflags=NO_WINDOW
)


def call(op: str, **values):
    assert driver.stdin and driver.stdout
    driver.stdin.write(json.dumps({"op": op, **values}, ensure_ascii=False) + "\n")
    driver.stdin.flush()
    answer = json.loads(driver.stdout.readline())
    if "error" in answer:
        raise RuntimeError(f"{answer['error']}: {answer['message']}")
    return answer["result"]


passed = 0


def test(name, run):
    global passed
    run()
    passed += 1
    print("PASS", name, flush=True)


def equal(actual, expected):
    assert actual == expected, f"\nactual: {actual!r}\nexpected: {expected!r}"


def rejects(op, **values):
    try:
        call(op, **values)
    except RuntimeError:
        return
    raise AssertionError("operation unexpectedly succeeded")


NEW = {"role": "button", "aria": "Nouveau chat", "text": "", "data": {}, "path": ["vue de départ"]}
ACTIONS = {
    "role": "button",
    "aria": "Actions du chat",
    "text": "",
    "popup": "menu",
    "data": {},
    "path": ["vue de départ"],
}
ARCHIVE = {
    "role": "menuitem",
    "aria": "",
    "text": "Archiver",
    "data": {},
    "path": ["vue de départ", "menu « Actions du chat »"],
}
MODE = {
    "role": "button",
    "aria": "Changer de mode, mode actuel : {mode}",
    "text": "",
    "popup": "menu",
    "data": {},
    "path": ["vue de départ"],
}


def card(version, items, at="2026-10-20T10:00:00Z", live=None):
    value = {
        "app": "codex",
        "version": version,
        "surveyed_at": at,
        "elements": items,
        "landmarks": cartes.landmark_status("codex", items),
        "index": {"archive": "Archiver"},
    }
    if live is not None:
        value["live"] = live
    return value


try:

    def locators():
        for item in [
            NEW,
            ARCHIVE,
            MODE,
            {"role": "button", "aria": "Change ${model} (mode)", "data": {}},
            {"role": "button", "aria": "Visible", "data": {"data-testid": "stable"}},
            {"role": "link", "text": "Choisir /{name}", "data": {"data-color": "blue"}},
        ]:
            equal(call("locator", item=item), cartes.locator_of(item))

    test("locators and template expressions agree with Python, including CSS priority", locators)

    def masks():
        labels = [
            "Nouveau chat",
            "/c",
            "navigation",
            "Changer de mode, mode actuel : {mode}",
            "Select model, current: ${m}",
            "${a} · ${b}",
        ]
        items = [
            NEW,
            {"role": "button", "aria": "Changer de mode, mode actuel\u00a0: Codex", "text": ""},
            {
                "role": "link",
                "text": "Synthetic private chat",
                "data": {"data-testid": "row", "data-chat-id": "synthetic-id"},
                "href": "/c/synthetic-id?secret=fixture#test",
            },
            {
                "role": "button",
                "aria": "Synthetic Person <fixture@example.org>",
                "text": "",
                "region": "navigation:Synthetic private chat",
            },
            {"role": "button", "aria": "Select model, current: Fiction", "text": ""},
        ]
        equal(call("mask", items=items, labels=labels), cartes.Known(labels).mask(items))

    test("known labels, substitutions, identity masking and routes match Python", masks)

    def collapses():
        rows = [{"role": "link", "aria": "", "text": f"fixture row {n}", "data": {}, "region": "nav"} for n in range(6)]
        equal(call("collapse", items=rows + [NEW]), cartes._collapse(rows + [NEW]))

    test("repeated rows collapse without preserving private titles", collapses)

    def landmarks():
        for items, context in [
            ([NEW, MODE, ACTIONS, ARCHIVE], None),
            ([NEW, {"role": "listitem", "text": "Aucun chat", "data": {}, "path": ["vue de départ"]}], None),
            ([NEW], {"chat_open": False}),
            ([NEW], {"chat_open": True}),
        ]:
            equal(
                call("marks", app="codex", items=items, context=context),
                cartes.landmark_status("codex", items, context),
            )

    test("missing and unverified landmarks retain typed context and all paths", landmarks)

    old = card("1", [NEW, MODE, ACTIONS, ARCHIVE])
    new = card("2", [NEW, {**MODE, "path": ["page « Paramètres »"]}], "2026-10-21T10:00:00Z")
    new["index"] = {"mode": "Changer de mode", "archive": "Archiver le chat"}
    change = cartes.diff(old, new)
    test(
        "added removed moved landmarks and translation changes match Python",
        lambda: equal(call("diff", before=old, after=new), change),
    )
    test(
        "readable difference summary matches Python",
        lambda: equal(call("summary", diff=change), cartes.summary(change)),
    )
    test(
        "search result types, navigation gestures and template selectors match Python",
        lambda: equal(
            call("search", app="codex", card=old, what="ARCHIVE"), cartes._search("codex", old, "ARCHIVE", 5)
        ),
    )

    def stores():
        first = call("save", card=old)
        assert "diff" not in first
        saved = call("save", card=new)
        equal(json.loads(Path(saved["diff"]).read_text(encoding="utf-8")), change)
        equal(saved["summary"], cartes.summary(change))
        assert saved["alerts"] and saved["alerts"][0].startswith("REPÈRE DISPARU")
        equal(call("where", app="codex", what="ARCHIVE"), cartes.where("codex", "ARCHIVE"))

    test("storage writes real differences and historical fallback agrees with Python", stores)
    test(
        "index-only missing label explanation agrees with Python",
        lambda: equal(call("where", app="codex", what="Changer de mode"), cartes.where("codex", "Changer de mode")),
    )
    test(
        "empty app map is a typed error",
        lambda: equal(call("where", app="antigravity", what="x"), cartes.where("antigravity", "x")),
    )
    test("unknown app is refused before reading or navigation", lambda: rejects("where", app="../codex", what="x"))

    def freshness():
        for live in [
            None,
            {"status": "interrompu : fixture", "view_restored": True},
            {"status": "complet", "view_restored": False},
            {"status": "complet", "view_restored": True},
        ]:
            value = card("3", [NEW], live=live)
            equal(call("fresh", card=value, version="3"), cartes.complete(value))
            equal(call("fresh", card=value, version="4"), False)
        complete = card("3", [NEW], "2026-10-22T10:00:00Z", live={"status": "complet", "view_restored": True})
        call("save", card=complete)
        partial = card(
            "3", [NEW], "2099-01-01T00:00:00Z", live={"status": "interrompu : fixture", "view_restored": False}
        )
        saved = call("save", card=partial)
        assert saved["partial"] is True and Path(saved["card"]).name.endswith(".partiel.json")
        equal(call("where", app="codex", what="Nouveau chat")["surveyed_at"], complete["surveyed_at"])
        call("save", card=complete)
        assert not Path(saved["card"]).exists()

    test("partial surveys never replace a complete card or count as fresh", freshness)

    def asar(path, files):
        tree = {"files": {}}
        blob = b""
        for name, data in files.items():
            node = tree
            *folders, leaf = name.split("/")
            for folder in folders:
                node = node["files"].setdefault(folder, {"files": {}})
            node["files"][leaf] = {"size": len(data), "offset": str(len(blob))}
            blob += data
        header = json.dumps(tree).encode()
        pickled = struct.pack("<I", len(header)) + header
        pickled = struct.pack("<I", len(pickled)) + pickled
        path.write_bytes(struct.pack("<II", 4, len(pickled)) + pickled + blob)
        return path

    asset = asar(
        state / "app.asar",
        {
            "webview/assets/fr-FR-z.js": rb'const x={"b":"dernier","emoji":"\uD83D\uDE00"}',
            "webview/assets/fr-FR-a.js": rb'const x={"b":"premier","mode":`Mode\xA0: {mode}`,"wide":"\u{1F431}"}',
            "native-menu-locales/fr-FR.json": json.dumps({"quit": "Quitter", "numeric": 3}).encode(),
            "webview/assets/en-US-a.js": b'{"b":"English"}',
        },
    )

    def indexing():
        actual = call("index", path=str(asset), lang="fr-FR")
        expected = cartes.codex_index("fr-FR", asset)
        # Python represents escaped UTF-16 surrogate pairs as two code units; JSON joins them into one scalar.
        expected = json.loads(json.dumps(expected, ensure_ascii=True))
        equal(actual, expected)

    test("ASAR indexing handles deterministic order Unicode escapes and native menus", indexing)

    def malformed():
        required = asar(state / "required-locale.asar", {"webview/assets/fr-FR-a.js": b'{"label":"required locale"}'})
        for name, contents in [
            ("short.asar", b"short"),
            ("bad-header.asar", struct.pack("<IIII", 4, 8, 4, 100000)),
            ("truncated.asar", required.read_bytes()[:-2]),
        ]:
            path = state / name
            path.write_bytes(contents)
            rejects("index", path=str(path), lang="fr-FR")

    test("truncated archive and assets are errors instead of partial success", malformed)

    fixture = str(Path(__file__).with_name("fixture.html"))

    def browser_normal():
        result = call("browser", path=fixture)
        assert result["card"]["live"]["status"] == "complet" and result["card"]["live"]["view_restored"] is True, result
        assert "navigate:settings" in result["events"] and "navigate:notifications" in result["events"], result
        assert not any(e.startswith("FORBIDDEN") for e in result["events"]), result
        equal(result["view"], {"hash": "", "menu": True})

    test("real local DOM menus pages subpages and exact restoration, no action clicked", browser_normal)

    def browser_abort():
        result = call("browser", path=fixture, scenario="user-return")
        equal(result["events"], ["menu"])
        assert result["card"]["live"]["view_restored"] is False and result["card"]["live"]["status"].startswith(
            "interrompu"
        ), result
        assert not call("fresh", card=result["card"], version="fixture-version")

    test("user return ends gestures including cleanup and preserves partial evidence", browser_abort)

    def browser_unknown():
        result = call("browser", path=fixture, scenario="transport-loss")
        equal(result["events"], ["menu"])
        assert "unknown" in result["card"]["live"]["status"] and result["card"]["live"]["view_restored"] is False, (
            result
        )

    test("transport failure after a real gesture stays unknown and is never replayed", browser_unknown)

    def browser_preserves_existing_work():
        for scenario in ["draft", "running"]:
            result = call("browser", path=fixture, scenario=scenario)
            assert result["card"] is None and result["refusal"], result
            equal(result["events"], [])

    test("existing draft or active AGY turn refuses before any actual gesture", browser_preserves_existing_work)
    print(f"Native cards: {passed} passed, 0 failed")
finally:
    if driver.stdin:
        driver.stdin.close()
    driver.wait(timeout=10)
