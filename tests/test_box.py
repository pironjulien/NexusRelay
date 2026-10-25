"""Le salon : un fichier immuable par message, sûr à plusieurs écrivains simultanés."""

from __future__ import annotations

import threading

import pytest

from nexus_relay.box import Box, RelayError


def test_message_depose_relu_et_lu():
    box = Box()
    message = box.post("claude", "codex", "Avis ?", "nexuswindows")
    assert box.get(message.id) == message
    assert [m.id for m in box.unread("codex")] == [message.id]
    box.mark_read(message)
    assert box.unread("codex") == [] and box.is_read(message)


def test_sujet_obligatoire_et_regles():
    box = Box()
    for bad in ("", "Avec Espace", "a.b"):
        with pytest.raises(RelayError):
            box.post("claude", "codex", "x", bad)
    with pytest.raises(RelayError):
        box.post("claude", "claude", "x", "t")
    with pytest.raises(RelayError):
        box.post("claude", "codex", "   ", "t")
    with pytest.raises(RelayError):
        box.post("claude", "codex", "x", "t", vote="peut-être")


def test_trois_ecrivains_simultanes_sans_perte():
    box = Box()
    errors: list[Exception] = []

    def write(sender: str, to: str) -> None:
        try:
            for i in range(40):
                box.post(sender, to, f"{sender} {i}", "course")
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [
        threading.Thread(target=write, args=pair)
        for pair in (("claude", "codex"), ("codex", "antigravity"), ("antigravity", "claude"))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    messages = box.topic_messages("course")
    assert len(messages) == 120 and len({m.id for m in messages}) == 120


def test_un_message_ne_se_reecrit_jamais():
    from nexus_relay.box import write_new

    box = Box()
    message = box.post("claude", "codex", "original", "t")
    path = next((box.root / "messages").glob(f"{message.id}.*.json"))
    with pytest.raises(RelayError):
        write_new(path, {"id": message.id, "text": "falsifié"})
    assert box.get(message.id).text == "original"  # type: ignore[union-attr]


def test_markdown_du_sujet_pour_utilisateur():
    box = Box()
    box.post("claude", "antigravity", "Première idée", "salon-md")
    box.post("antigravity", "claude", "Contre-idée", "salon-md", vote="désaccord")
    text = (box.root / "salon" / "salon-md.md").read_text(encoding="utf-8")
    assert "claude → antigravity" in text and "Contre-idée" in text and "vote : désaccord" in text


def test_evenements_et_etats():
    box = Box()
    message = box.post("claude", "antigravity", "x", "t")
    box.event(message.id, "livré", conversation="c")
    assert box.has_event(message.id, "livré") and not box.has_event(message.id, "répondu")
    box.set_topic_state("t", "agy", {"conversation": "c"})
    assert box.topic_state("t", "agy") == {"conversation": "c"}


def test_sujet_termine_pour_le_repondant():
    box = Box()
    assert not box.topic_finished_for("antigravity", "debat-arch")  # aucune demande : rien à ranger
    m1 = box.post("claude", "antigravity", "Question à AGY", "debat-arch")
    assert not box.topic_finished_for("antigravity", "debat-arch")  # non lu, sans réponse
    box.mark_read(m1)
    assert not box.topic_finished_for("antigravity", "debat-arch")  # lu, sans réponse
    box.post("antigravity", "claude", "Réponse d'AGY", "debat-arch", reply_to=m1.id)
    assert box.topic_finished_for("antigravity", "debat-arch")
    box.post("claude", "antigravity", "Relance", "debat-arch")
    assert not box.topic_finished_for("antigravity", "debat-arch")  # nouvelle demande : de nouveau ouvert


def test_un_sujet_par_projet(tmp_path):
    """L'utilisateur, 23/10 : une conversation par projet, jamais par sous-tâche."""
    box = Box(tmp_path / "relay")
    for t in ("relais", "nexuswindows", "nexus-android"):
        box.set_topic_state(t, "codex", {"conversation": t})
    assert box.project_topic("relais-cartes") == "relais"
    assert box.project_topic("nexuswindows-banc") == "nexuswindows"
    assert box.project_topic("nexus-windows") == "nexuswindows"
    assert box.project_topic("nexusandroid-sota") == "nexus-android"
    assert box.project_topic("nexuswindows") == "nexuswindows"
    assert box.project_topic("selftest") == "selftest"
