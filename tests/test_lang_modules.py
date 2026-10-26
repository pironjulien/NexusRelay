"""Textes rendus aux agents par les modules du relais : anglais par défaut, français avec lang=fr."""

from __future__ import annotations

import pytest

from nexus_relay import journal, reservations
from nexus_relay.box import Box, RelayError
from nexus_relay.sidecar import prompt_for, title_for


@pytest.fixture
def english(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_RELAY_LANG", "en")


def test_texte_de_reveil_du_sidecar_en_anglais(english: None):
    box = Box()
    ask = box.post("claude", "antigravity", "Relis le diff, s'il te plaît", "relais")
    note = box.post("codex", "antigravity", "pour info", "relais", info=True)
    text = prompt_for([ask, note])
    assert text.startswith("Nexus Relay: message(s) from claude on “relais”:")
    assert f'read(id="{ask.id}")' in text and f'For information (no reply): read(id="{note.id}")' in text
    assert "Fais ce que" not in text and "Serveur" not in text
    assert len(text) < 500 and "Relis le diff" not in text  # jamais le contenu
    assert title_for([ask]).startswith("Claude: Relis le diff")


def test_texte_de_reveil_du_sidecar_en_francais(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEXUS_RELAY_LANG", "fr")
    ask = Box().post("claude", "antigravity", "Relis", "relais")
    assert prompt_for([ask]).startswith("Nexus Relay : message(s) de claude sur « relais » :")
    assert title_for([ask]).startswith("Claude : ")


def test_refus_de_reservation_en_anglais(english: None, tmp_path):
    box = Box()
    reservations.reserve(box, "claude", [str(tmp_path / "src" / "a.py")], "fix")
    with pytest.raises(RelayError) as refused:
        reservations.reserve(box, "codex", [str(tmp_path / "src")], "refactor")
    message = str(refused.value)
    assert message.startswith("reservation refused: ") and "reserved by claude (fix) until" in message
    with pytest.raises(RelayError, match="^reservation: say why"):
        reservations.reserve(box, "codex", [str(tmp_path / "b.py")], " ")


def test_causes_du_journal_reconnues_en_anglais(english: None):
    assert journal.cause("Antigravity open but sidecar silent: it restarts by itself, retrying") == "sidecar muet"
    assert journal.cause("turn ended without a reply") == "tour fini sans réponse"
    assert journal.cause("turn silent for 21 min (app frozen?)") == "appli figée"
    assert journal.cause("Codex: 95 % used (5 h), reset at 1792819398") == "quota"
    assert journal.cause("agentapi send-message: timed out (120 s)") == "délai dépassé"
    assert journal.cause("Antigravity closed and not found on this PC") == "appli fermée"
