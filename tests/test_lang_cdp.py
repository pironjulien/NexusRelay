"""Textes du port (cdp) et des cartes rendus aux agents : anglais par défaut (lang=en), le réveil reconnu dans les deux
langues."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_relay import cartes, cdp

ID = "20261026T100000Z-claude-0001"


@pytest.fixture(autouse=True)
def english(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_RELAY_LANG", "en")


def test_texte_de_reveil_en_anglais() -> None:
    text = cdp.wake_text([ID])
    assert text.startswith(f'Nexus Relay: message from a peer for you, read(id="{ID}"). Do what it asks')
    assert "pair" not in text and "inbox()" in cdp.wake_text([])


def test_reveil_reconnu_dans_les_deux_langues(monkeypatch: pytest.MonkeyPatch) -> None:
    english = cdp.wake_text([ID])
    monkeypatch.setenv("NEXUS_RELAY_LANG", "fr")
    french = cdp.wake_text([ID])
    assert french.startswith("Nexus Relay : message d'un pair pour toi")
    # un reste laissé dans le champ avant un changement de langue reste reconnu (et effacé) ; un ajout, jamais
    assert cdp.is_wake_text(english) and cdp.is_wake_text(french)
    assert not cdp.is_wake_text(english + " and my addition")


def test_raisons_du_port_en_anglais() -> None:
    assert cdp.BUSY_REASON.startswith("the app's interface is busy")
    reason = cdp.landmarks()["codex"]  # port fermé (conftest)
    assert reason == {"status": "not tested", "reason": f"port {cdp.CODEX_PORT} closed or page missing"}


def test_where_et_resume_des_cartes_en_anglais() -> None:
    assert cartes.where("codex", "x")["error"] == "no map yet for codex: run a survey"
    menu = {"role": "button", "aria": "Actions du chat", "popup": "menu", "data": {}, "path": ["vue de départ"]}
    item = {
        "role": "menuitem",
        "aria": "",
        "text": "Archiver",
        "data": {},
        "path": ["vue de départ", "menu « Actions du chat »"],
    }
    elements = [menu, item]
    card = {"app": "codex", "version": "1", "surveyed_at": "a", "elements": elements, "index": {}}
    cartes.save({**card, "landmarks": cartes.landmark_status("codex", elements)})
    found = cartes.where("codex", "archiver")
    assert found["found"][0]["how"][0]["gesture"] == "emulated click (cdp._press)"
    assert found["found"][0]["how"][-1]["step"] == "element"
    assert cartes.where("codex", "zzz")["note"] == "not seen in the survey"
    saved = cartes.save({**card, "version": "2", "surveyed_at": "b", "elements": [], "landmarks": {}})
    assert "LANDMARK GONE ARCHIVE (was: vue de départ > menu « Actions du chat »)" in saved["alerts"]
    assert "0 elements appeared, 2 gone" in saved["summary"]
