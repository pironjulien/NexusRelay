"""Recherche plein texte dans le salon et résumé d'un sujet (24/10, comparaison avec MCP Agent Mail)."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest
from conftest import as_agent, call, payload

from nexus_relay import search as search_mod
from nexus_relay import server
from nexus_relay.box import Box


def _fill(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    ids = {}
    ids["a"] = call(
        server.send("codex", "Relecture du dépôt NexusBrowser : sécurité des jetons", topic="nexusbrowser")
    )["id"]
    ids["b"] = call(server.send("antigravity", "Banc NexusWindows : mesure des clics", topic="nexuswindows"))["id"]
    time.sleep(1.1)  # horodatage à la seconde : c vient après a et b
    ids["c"] = call(
        server.send("codex", "Seconde relecture : la SECURITE du pont, jetons et cookies", topic="nexusbrowser")
    )["id"]
    as_agent(monkeypatch, "codex")
    ids["r"] = call(server.reply(ids["a"], "src/pont.py:12 jeton visible, gravité haute", vote="désaccord"))["id"]
    as_agent(monkeypatch, "claude")
    return ids


def test_recherche_sans_accents_ni_casse_tous_les_mots(monkeypatch: pytest.MonkeyPatch):
    ids = _fill(monkeypatch)
    found = call(server.search("securite jetons"))
    assert [h["id"] for h in found["hits"]] == [ids["c"], ids["a"]]  # le plus pertinent puis le plus récent
    first = found["hits"][0]
    assert first["topic"] == "nexusbrowser" and first["from"] == "claude" and "⟦relay:" in first["snippet"]
    assert "SECURITE" in first["snippet"] and found["note"].startswith("Texte entre")
    assert call(server.search("clics"))["hits"][0]["id"] == ids["b"]
    assert call(server.search("jetons inexistant"))["hits"] == []


def test_recherche_filtre_sujet_expediteur_phrase(monkeypatch: pytest.MonkeyPatch):
    ids = _fill(monkeypatch)
    assert [h["id"] for h in call(server.search("jeton", sender="codex"))["hits"]] == [ids["r"]]
    assert {h["id"] for h in call(server.search("relecture", topic="nexusbrowser"))["hits"]} == {ids["a"], ids["c"]}
    assert call(server.search("relecture", topic="nexuswindows"))["hits"] == []
    assert [h["id"] for h in call(server.search('"pont, jetons"'))["hits"]] == [ids["c"]]
    assert call(server.search("relecture", limit=1))["total"] == 2
    assert len(call(server.search("relecture", limit=1))["hits"]) == 1


def test_recherche_vide_refusee():
    assert payload(server.search("   "))["status"] == "error"


def test_resume_sujet_demandes_ouvertes_et_closes(monkeypatch: pytest.MonkeyPatch):
    ids = _fill(monkeypatch)
    summary = call(server.summary("nexusbrowser"))
    assert summary["topic"] == "nexusbrowser" and summary["messages"] == 3 and not summary["closed"]
    assert summary["participants"] == {"claude": 2, "codex": 1}
    assert [o["id"] for o in summary["open"]] == [ids["c"]] and summary["open"][0]["to"] == "codex"
    assert summary["answered_total"] == 1
    done = summary["answered"][0]
    assert done["id"] == ids["a"] and done["outcome"] == "terminé" and done["vote"] == "désaccord"
    assert "gravité haute" in done["reply"] and "⟦relay:" in done["reply"]
    assert [m["id"] for m in summary["last"]] == [ids["a"], ids["c"], ids["r"]]
    assert summary["first_at"] <= summary["last_at"]


def test_resume_borne_les_demandes_ouvertes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "SUMMARY_OPEN", 2)
    sent = [call(server.send("codex", f"question {n}", topic="t"))["id"] for n in range(4)]
    summary = call(server.summary("t"))
    assert summary["open_total"] == 4 and [o["id"] for o in summary["open"]] == sent[-2:]


def test_resume_sujet_inconnu_refuse():
    assert "inconnu" in payload(server.summary("rien"))["error"]


# --- 1.6.0 : index SQLite FTS5 à la place du parcours en mémoire


def _legacy(box: Box, query: str, topic: str | None = None, sender: str | None = None) -> list[tuple[str, int, str]]:
    """Le parcours en mémoire de la 1.5.3, référence des résultats attendus de l'index."""
    wanted = search_mod.terms(query)
    pool = box.topic_messages(topic) if topic else box.all()
    hits = []
    for message in pool:
        if sender and message.sender != sender:
            continue
        plain = search_mod.bare(message.text)
        counts = [plain.count(term) for term in wanted]
        if not all(counts):
            continue
        folded, where = search_mod.fold(message.text)
        first = min(i for term in wanted if (i := folded.find(term)) >= 0)
        hits.append((message.id, sum(counts), search_mod.snippet(message.text, where[first] if where else 0)))
    hits.sort(key=lambda hit: (hit[1], hit[0]), reverse=True)
    return hits


def _indexed(box: Box, query: str, topic: str | None = None, sender: str | None = None) -> list[tuple[str, int, str]]:
    return [(m.id, score, extract) for m, score, extract in search_mod.search(box, query, topic, sender)]


TEXTS = [
    ("claude", "codex", "relais", "Réveil de Codex : ÉCHEC du port 9223, relance à 10 h"),
    ("codex", "claude", "relais", "Ok : le réveil marche, café « crème » et Straße ; a b c"),
    ("antigravity", "claude", "nexuswindows", "Banc NexusWindows : 44 sur 51, réveil réveil réveil"),
    ("claude", "antigravity", "nexuswindows", 'Le "pont, jetons" reste ouvert ; py 3.14 ; ok'),
    ("claude", "codex", "nexusbrowser", "Sécurité des jetons : SECURITE haute, sécurité, securite"),
]
QUERIES = [
    "reveil",
    "RÉVEIL",
    "securite jetons",
    "a",
    "é",
    "ok",
    "py",
    "10",
    "ss",
    "strasse",
    '"pont, jetons"',
    "echec 9223",
    "reveil codex",
    "rien du tout",
    "ab",
    "e",
]


def _corpus(box: Box) -> None:
    for sender, to, topic, text in TEXTS:
        box.post(sender, to, text, topic)


def test_index_memes_resultats_que_le_parcours(isolated: Path):
    box = Box(isolated / "boite")
    _corpus(box)
    for query in QUERIES:
        for topic, sender in [(None, None), ("relais", None), (None, "claude"), ("nexuswindows", "antigravity")]:
            expected = _legacy(box, query, topic, sender)
            assert _indexed(box, query, topic, sender) == expected, (query, topic, sender)
            total, page = search_mod.search_page(box, query, topic, sender, limit=2)
            assert total == len(expected) and [(m.id, n, x) for m, n, x in page] == expected[:2]


def test_index_migre_l_historique_existant(isolated: Path):
    box = Box(isolated / "boite")
    _corpus(box)  # messages déposés avant toute recherche : pas d'index
    assert not search_mod.index_path(box).exists()
    assert search_mod.sync(box) == len(TEXTS)  # migration de l'historique
    assert search_mod.index_path(box).exists() and search_mod.sync(box) == 0
    expected = _legacy(box, "reveil")
    assert _indexed(box, "reveil") == expected and len(expected) == 3
    search_mod.index_path(box).unlink()  # index perdu : reconstruit à la recherche suivante
    assert _indexed(box, "reveil") == expected


def test_index_suit_les_nouveaux_messages_et_les_disparus(isolated: Path):
    box = Box(isolated / "boite")
    _corpus(box)
    assert len(_indexed(box, "jetons")) == 2
    added = box.post("codex", "claude", "Troisième avis sur les JETONS", "nexusbrowser")
    assert _indexed(box, "jetons")[0][0] == added.id  # vu sans relancer quoi que ce soit
    next((box.root / "messages").glob(f"{added.id}.*.json")).unlink()
    assert [hit[0] for hit in _indexed(box, "jetons")] == [hit[0] for hit in _legacy(box, "jetons")]
    assert added.id not in [hit[0] for hit in _indexed(box, "jetons")]


def test_index_d_un_autre_schema_reconstruit(isolated: Path):
    box = Box(isolated / "boite")
    _corpus(box)
    search_mod.sync(box)
    with closing(sqlite3.connect(search_mod.index_path(box))) as db:
        db.execute("PRAGMA user_version = 99")
        db.commit()
    assert _indexed(box, "reveil") == _legacy(box, "reveil")
    with closing(sqlite3.connect(search_mod.index_path(box))) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == search_mod.SCHEMA
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == len(TEXTS)


def test_message_illisible_ignore_comme_avant(isolated: Path):
    box = Box(isolated / "boite")
    _corpus(box)
    (box.root / "messages" / "20260101T000000000-abcdef.claude.codex.relais.json").write_text("{", encoding="utf-8")
    assert _indexed(box, "reveil") == _legacy(box, "reveil")


def test_inventaire_refait_sous_le_verrou(isolated: Path, monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex, 27/10 : l'inventaire pris avant BEGIN IMMEDIATE retirait de l'index un message qu'un autre
    processus venait de déposer et d'indexer entre l'inventaire et la lecture de l'index."""
    box = Box(isolated / "boite")
    _corpus(box)
    search_mod.sync(box)
    folder = box.root / "messages"
    real = type(folder).glob
    added: list[str] = []

    def glob(self: Path, pattern: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        found = list(real(self, pattern, *args, **kwargs))
        if self == folder and pattern == "*.json" and not added:
            added.append("")  # une seule fois (le dépôt et l'autre rattrapage relisent le dossier)
            added[0] = box.post("codex", "claude", "Arrivé pendant l'inventaire", "relais").id
            message = box.get(added[0])
            assert message is not None
            name = next(folder.glob(f"{message.id}.*.json")).name
            with closing(search_mod._connect(box)) as other:  # « autre processus » : sa connexion, pas notre verrou
                other.execute(
                    "INSERT INTO messages (name, id, sender, topic, plain) VALUES (?, ?, ?, ?, ?)",
                    (name, message.id, message.sender, message.topic, search_mod.bare(message.text)),
                )
        return iter(found)

    monkeypatch.setattr(type(folder), "glob", glob)
    with closing(search_mod._connect(box)) as db:
        search_mod._sync(box, db)
        monkeypatch.setattr(type(folder), "glob", real)
        # l'index, sans nouveau rattrapage (qui le remettrait) : le message existe, il doit y être resté
        assert [row[0] for row in db.execute("SELECT id FROM messages WHERE id = ?", added)] == added


def test_ouvertures_simultanees_d_une_base_neuve(isolated: Path):
    """Relecture de Codex, 27/10 : PRAGMA journal_mode=WAL sans protection, « database is locked » à 12 fils."""
    import threading

    errors: list[BaseException] = []
    for trial in range(21):
        box = Box(isolated / f"boite{trial}")
        barrier = threading.Barrier(12)

        def open_once(box: Box = box, barrier: threading.Barrier = barrier) -> None:
            barrier.wait()
            try:
                search_mod._connect(box).close()
            except BaseException as error:  # noqa: BLE001 - relevée par l'assertion
                errors.append(error)

        threads = [threading.Thread(target=open_once) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert errors == []


def test_terme_avec_nul_cherche_par_instr(isolated: Path):
    """Relecture de Codex, 27/10 : un caractère NUL faisait planter FTS5 (« unterminated string »)."""
    box = Box(isolated / "boite")
    _corpus(box)
    nul = box.post("claude", "codex", "avant\x00apres", "relais")
    assert search_mod.search_page(box, "reveil\x00codex") == (0, [])
    total, hits = search_mod.search_page(box, "avant\x00apres")
    assert total == 1 and hits[0][0].id == nul.id
