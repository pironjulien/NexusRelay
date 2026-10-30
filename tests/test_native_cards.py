"""Native UI maps share the stable snapshot/guards and read stored maps without any app navigation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from test_cartes import _asar
from test_native_csharp import NO_WINDOW, _native_exe, _stdio_rpc, _tool_call_data

from nexus_relay import cartes
from nexus_relay.box import Box

HOST_PROFILE = os.environ.get("USERPROFILE")
HOST_HOME = os.environ.get("HOME")


def test_native_card_semantics_and_real_local_dom_survey_matrix():
    script = Path(__file__).with_name("native_cards") / "verify.py"
    environment = os.environ.copy()
    # Chromium's Windows sandbox requires the actual OS profile identity. Its browser data still goes exclusively
    # to the driver's fresh --user-data-dir; Relay/oracle mailboxes and files remain isolated by verify.py.
    if HOST_PROFILE:
        environment["USERPROFILE"] = HOST_PROFILE
    if HOST_HOME:
        environment["HOME"] = HOST_HOME
    result = subprocess.run(
        [sys.executable, "-B", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        creationflags=NO_WINDOW,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Native cards: 18 passed, 0 failed" in result.stdout


def test_native_map_scripts_and_landmarks_match_the_proven_source():
    contracts = json.loads(subprocess.check_output([str(_native_exe()), "--ui-contracts"], creationflags=NO_WINDOW))
    assert contracts["snapshot"] == cartes._SNAPSHOT
    assert contracts["danger"] == cartes.DANGER.pattern
    assert contracts["landmarks"] == cartes.LANDMARKS
    for app, spec in cartes.SPECS.items():
        assert contracts["apps"][app]["pages"] == spec.pages
        assert contracts["apps"][app]["subpages"] == spec.subpages


def test_native_reads_localized_asar_index_with_unicode_and_escapes(tmp_path):
    asar = _asar(
        tmp_path / "app.asar",
        {
            "webview/assets/fr-FR-fixture.js": rb'export default {"archive":"Archiver","mode":`Changer de mode\xA0: {mode}`,"emoji":"\uD83D\uDE00"}',
            "native-menu-locales/fr-FR.json": json.dumps({"quit": "Quitter"}).encode(),
        },
    )
    native = json.loads(
        subprocess.check_output([str(_native_exe()), "--card-index", str(asar), "fr-FR"], creationflags=NO_WINDOW)
    )
    assert native["archive"] == "Archiver" and native["mode"] == "Changer de mode\u00a0: {mode}"
    assert native["emoji"] == "😀" and native["menu:quit"] == "Quitter"


def test_native_where_reads_historical_complete_cards_without_the_application(tmp_path):
    relay = tmp_path / "relay"
    box = Box(relay / "v1")
    folder = box.root / "cartes"
    folder.mkdir(parents=True)
    element = {
        "role": "menuitem",
        "aria": "Archiver",
        "text": "",
        "data": {"data-testid": "archive-chat"},
        "path": ["vue de départ", "menu « Actions du chat »"],
    }
    older = {
        "app": "codex",
        "version": "1",
        "surveyed_at": "2026-10-20T10:00:00Z",
        "elements": [element],
        "index": {"archive": "Archiver"},
    }
    recent = {
        "app": "codex",
        "version": "2",
        "surveyed_at": "2026-10-21T10:00:00Z",
        "elements": [],
        "index": {"archive": "Archiver"},
    }
    (folder / "codex-1.json").write_text(json.dumps(older), encoding="utf-8")
    (folder / "codex-2.json").write_text(json.dumps(recent), encoding="utf-8")
    (folder / "codex-3.partiel.json").write_text(
        json.dumps({**older, "version": "3", "surveyed_at": "2099-01-01T00:00:00Z"}), encoding="utf-8"
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "where", "arguments": {"app": "codex", "what": "ARCHIVE"}},
    }
    response = _stdio_rpc(_native_exe(), {**os.environ, "NEXUS_RELAY_HOME": str(relay)}, "claude", [request])[0]
    data = _tool_call_data(response)
    assert data["status"] == "success" and data["version"] == "2"
    assert data["from_older_card"].startswith("1 (")
    assert data["found"][0]["landmark"] == "ARCHIVE"
    assert data["found"][0]["path"].endswith("menu « Actions du chat »")
    assert data["found"][0]["how"][-1]["locator"]["css"] == '[data-testid="archive-chat"]'
