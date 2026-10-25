"""Chats du relais dans l'appli Codex : reconnus par leur dossier de travail, rangés ensemble, preuve par les journaux."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from nexus_relay import cdp, codex_app

RELAY_ID = "20261020T100000000-abcdef"


def _rollout(
    root: Path, chat_id: str, cwd: str, *events: dict[str, Any], archived: bool = False, parent: str | None = None
) -> Path:
    """Journal Codex : `session_meta` en première ligne (cwd), puis les évènements."""
    folder = root / ("archived_sessions" if archived else "sessions/2026/10/20")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-10-20T10-00-00-{chat_id}.jsonl"
    meta: dict[str, Any] = {"id": chat_id, "cwd": cwd, **({"parent_thread_id": parent} if parent else {})}
    lines = [{"type": "session_meta", "payload": meta}]
    lines += [{"type": "event_msg", "payload": event} for event in events]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _turn(done_at: float | None = 1.0) -> list[dict[str, Any]]:
    started = {"type": "task_started"}
    return [started] if done_at is None else [started, {"type": "task_complete", "completed_at": done_at}]


def test_dossier_du_projet_du_relais_sous_relay_home(isolated: Path):
    assert codex_app.relay_dir() == isolated / "relay" / "codex-projet"


def test_seul_le_dossier_de_travail_fait_un_chat_du_relais(tmp_path: Path):
    """Relectures du 20/10 : ni le titre, ni un id cité, ni une ligne apparue au bon moment ne prouvent qu'un chat est
    au relais ; un chat de l'utilisateur a pu être pris."""
    root = tmp_path / "codex"
    relay = str(codex_app.relay_dir())
    _rollout(root, "019a-relais", "\\\\?\\" + relay.upper() + "\\", *_turn())  # préfixe long, casse, barre finale
    user = _rollout(
        root,
        "019a-user",
        str(tmp_path / "OneDrive" / "Documents"),
        {"type": "user_message", "message": f'Nexus Relay : message d\'un pair pour toi, read(id="{RELAY_ID}")'},
        *_turn(),
    )
    (root / "session_index.jsonl").write_text(
        json.dumps({"id": "019a-user", "thread_name": "Nexus Relay · relais"}) + "\n", encoding="utf-8"
    )
    _rollout(root, "019a-range", relay, *_turn(), archived=True)  # déjà archivé : plus actif
    chats = codex_app.relay_chats(root)
    assert [c.id for c in chats] == ["019a-relais"]
    assert user.exists()  # jamais touché


def test_un_tour_arrete_finit_aussi_le_tour(tmp_path: Path):
    """Vu le 20/10 : un tour arrêté finit par turn_aborted ; le chat restait « en cours » et bloquait le rangement."""
    path = _rollout(tmp_path, "019a-stop", "x", {"type": "task_started"})
    assert codex_app.turn_state(path) == (False, None)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted", "completed_at": 42}}) + "\n")
    assert codex_app.turn_state(path) == (True, 42.0)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
    assert codex_app.turn_state(path) == (False, None)  # nouveau tour


def test_un_sous_agent_n_est_pas_un_chat_du_relais(tmp_path: Path):
    """Tour 2 : un sous-agent né dans le dossier du relais (parent_thread_id) n'a pas de ligne à lui ; le compter
    bloquait le rangement de son parent."""
    relay = str(codex_app.relay_dir())
    _rollout(tmp_path, "019a-parent", relay, *_turn())
    _rollout(tmp_path, "019a-enfant", relay, {"type": "task_started"}, parent="019a-parent")
    assert [c.id for c in codex_app.relay_chats(tmp_path)] == ["019a-parent"]
    assert codex_app.chat_meta(next((tmp_path / "sessions").rglob("*019a-enfant.jsonl")))["parent_thread_id"]


def test_pret_relu_dans_le_journal(tmp_path: Path):
    """`ready` relit le journal : un chat vu fini puis reparti n'est plus prêt ; fini depuis moins que la grâce non plus."""
    now = 1_000_000.0
    path = _rollout(tmp_path, "019a-un", "x", *_turn(now - 60))
    chat = codex_app.Chat("019a-un", path, True, now - 60)
    assert codex_app.ready(chat, now, 10.0)
    assert not codex_app.ready(chat, now - 55, 10.0)  # fini il y a 5 s
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
    assert not codex_app.ready(chat, now, 10.0)  # l'objet dit « fini », le journal dit « en cours »


class Sidebar:
    """Barre latérale simulée : conversation → ligne ; « Archiver » déplace le journal dans archived_sessions."""

    def __init__(self, root: Path, rows: dict[str, str], moves: bool = True) -> None:
        self.root, self.rows, self.moves, self.clicked = root, dict(rows), moves, []

    def mapping(self) -> dict[str, str]:
        return dict(self.rows)

    def archive_row(self, row: str, guard: Any) -> tuple[str, str | None]:
        if reason := guard():  # garde relue juste avant chaque clic
            return "skipped", reason
        self.clicked.append(row)
        conversation = next(c for c, r in self.rows.items() if r == row)
        journal = next(iter((self.root / "sessions").rglob(f"rollout-*{conversation}.jsonl")), None)
        if self.moves and journal is not None:
            (self.root / "archived_sessions").mkdir(exist_ok=True)
            journal.rename(self.root / "archived_sessions" / journal.name)
        return "clicked", None


def _archive(root: Path, side: Sidebar, **kw: Any) -> dict[str, Any]:
    return codex_app.archive_project(
        side.mapping,
        side.archive_row,
        lambda: codex_app.relay_chats(root),
        root,
        10.0,
        **{"verify_s": 0.0, "sleep": lambda s: None, **kw},
    )


def test_rangement_par_identite_jamais_un_chat_de_utilisateur_du_bloc(tmp_path: Path):
    """Relectures du 20/10 : une ligne du bloc « Nexus Relay » peut être un chat de l'utilisateur (né ailleurs, déplacé) ;
    seules les lignes dont la conversation est un chat du relais sont cliquées."""
    relay = str(codex_app.relay_dir())
    for chat_id in ("019a-un", "019a-deux"):
        _rollout(tmp_path, chat_id, relay, *_turn())
    user = _rollout(tmp_path, "019a-user", str(tmp_path / "Documents"), *_turn())
    side = Sidebar(tmp_path, {"019a-un": "t1", "019a-deux": "t2", "019a-user": "t3"})
    result = _archive(tmp_path, side)
    assert result == {"status": "archived", "clicked": 2, "archived": ["019a-deux", "019a-un"]}
    assert sorted(side.clicked) == ["t1", "t2"] and user.exists()
    assert codex_app.relay_chats(tmp_path) == []


def test_rien_a_ranger(tmp_path: Path):
    relay = str(codex_app.relay_dir())
    _rollout(tmp_path, "019a-un", relay, *_turn())
    assert _archive(tmp_path, Sidebar(tmp_path, {"019a-user": "t3"})) == {"status": "nothing"}  # ligne introuvable
    _rollout(tmp_path, "019a-deux", relay, {"type": "task_started"})
    side = Sidebar(tmp_path, {"019a-deux": "t2"})
    assert _archive(tmp_path, side) == {"status": "nothing"} and side.clicked == []  # seul chat visible : en cours


def test_un_chat_bloque_n_empeche_plus_de_ranger_les_autres(tmp_path: Path):
    """Tour 2 : un seul chat figé (tour jamais fini) bloquait le rangement de tous les autres pendant des jours."""
    relay = str(codex_app.relay_dir())
    stuck = _rollout(tmp_path, "019a-fige", relay, {"type": "task_started"})
    _rollout(tmp_path, "019a-fini", relay, *_turn())
    side = Sidebar(tmp_path, {"019a-fige": "t1", "019a-fini": "t2"})
    result = _archive(tmp_path, side)
    assert result["status"] == "archived" and result["archived"] == ["019a-fini"] and side.clicked == ["t2"]
    assert stuck.exists()


def test_garde_avant_chaque_clic(tmp_path: Path):
    relay = str(codex_app.relay_dir())
    _rollout(tmp_path, "019a-un", relay, *_turn())
    _rollout(tmp_path, "019a-deux", relay, *_turn())
    rows = {"019a-un": "t1", "019a-deux": "t2"}
    side = Sidebar(tmp_path, rows)
    result = _archive(tmp_path, side, present=lambda: True)
    assert result == {"status": "skipped", "reason": "L'utilisateur est dans Codex"} and side.clicked == []
    moved = Sidebar(tmp_path, rows)
    calls = iter([rows, {"019a-un": "t1", "019a-deux": "t9"}])  # la ligne a changé entre la lecture et le clic
    result = codex_app.archive_project(
        lambda: next(calls, rows), moved.archive_row, lambda: codex_app.relay_chats(tmp_path), tmp_path, 10.0
    )
    assert result == {"status": "skipped", "reason": "ligne du chat changée"} and moved.clicked == []


def test_un_chat_qui_repart_arrete_la_suite(tmp_path: Path):
    """Un chat du relais qui repart (nouveau réveil) entre deux clics arrête tout : sa ligne n'est pas touchée."""
    relay = str(codex_app.relay_dir())
    _rollout(tmp_path, "019a-deux", relay, *_turn())  # rangé en premier (ordre des journaux)
    restarting = _rollout(tmp_path, "019a-un", relay, *_turn())
    side = Sidebar(tmp_path, {"019a-deux": "t2", "019a-un": "t1"})
    archive_row = side.archive_row

    def then_busy(row: str, guard: Any) -> tuple[str, str | None]:
        outcome = archive_row(row, guard)
        if outcome[0] == "clicked":
            with restarting.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
        return outcome

    result = codex_app.archive_project(
        side.mapping, then_busy, lambda: codex_app.relay_chats(tmp_path), tmp_path, 10.0, verify_s=0.0
    )
    assert side.clicked == ["t2"] and result["archived"] == ["019a-deux"]
    assert result["status"] == "archived" and result["reason"] == "ce chat du relais travaille"
    assert restarting.exists()


def test_clic_sans_effet_est_un_echec(tmp_path: Path):
    _rollout(tmp_path, "019a-un", str(codex_app.relay_dir()), *_turn())
    side = Sidebar(tmp_path, {"019a-un": "t1"}, moves=False)
    result = _archive(tmp_path, side)
    assert side.clicked == ["t1"] and result["status"] == "failed" and result["archived"] == []


def _codex_page(monkeypatch: pytest.MonkeyPatch, mode_ok: bool = True) -> dict[str, Any]:
    state: dict[str, Any] = {"mode": "ChatGPT", "closed": 0, "modes": []}

    class CodexPage:
        def __init__(self, url: str) -> None:
            pass

        def close(self) -> None:
            state["closed"] += 1

    def set_mode(page: Any, wanted: str, sleep: Any = None) -> bool:
        state["modes"].append(wanted)
        state["mode"] = wanted
        return True

    monkeypatch.setattr(cdp, "page_url", lambda port=0, url="": "ws://codex")
    monkeypatch.setattr(cdp, "Page", CodexPage)
    monkeypatch.setattr(cdp, "codex_mode", lambda page: state["mode"])
    monkeypatch.setattr(cdp, "ensure_codex_mode", lambda page, sleep=None: mode_ok and set_mode(page, "Codex"))
    monkeypatch.setattr(cdp, "set_codex_mode", set_mode)
    monkeypatch.setattr(cdp, "user_in_codex", lambda: False)
    monkeypatch.setattr(cdp, "selection_size", lambda page: 0)
    return state


def test_archivage_par_l_appli_rend_le_mode_de_utilisateur(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Vu le 20/10 : le mode ChatGPT de l'utilisateur restait en mode Codex après un rangement."""
    _rollout(tmp_path, "019a-un", str(codex_app.relay_dir()), *_turn(time.time() - 60))
    state = _codex_page(monkeypatch)
    side = Sidebar(tmp_path, {"019a-un": "t1"})
    seen: list[str] = []
    present: list[bool] = []

    def rows(page: Any) -> list[tuple[str, str, bool]]:
        seen.append(state["mode"])  # la barre est lue en mode Codex
        return [(row, conv, False) for conv, row in side.rows.items()]

    monkeypatch.setattr(cdp, "row_conversations", rows)
    monkeypatch.setattr(cdp, "user_in_codex", lambda: present.append(True) or False)
    monkeypatch.setattr(cdp, "archive_thread", lambda page, row, guard: side.archive_row(row, guard))
    result = codex_app.archive_chats(codex_app.relay_chats(tmp_path), tmp_path, grace_s=10.0)
    assert result["status"] == "archived" and set(seen) == {"Codex"} and present  # garde : l'utilisateur dans Codex ?
    assert state["mode"] == "ChatGPT" and state["modes"] == ["Codex", "ChatGPT"] and state["closed"] == 1
    assert cdp.UI_LOCK.acquire(blocking=False)  # verrou rendu
    cdp.UI_LOCK.release()


def test_archivage_par_l_appli_echoue_proprement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state = _codex_page(monkeypatch, mode_ok=False)
    result = codex_app.archive_chats([], tmp_path)
    assert result["status"] == "failed" and "mode Codex" in result["reason"] and state["closed"] == 1
    assert cdp.UI_LOCK.acquire(blocking=False)
    cdp.UI_LOCK.release()
    monkeypatch.setattr(cdp, "page_url", lambda port=0, url="": None)
    assert codex_app.archive_chats([], tmp_path)["status"] == "failed"  # port fermé


def test_tour_coupe_date_par_son_evenement(tmp_path: Path):
    """Tour 3 : turn_aborted n'a pas de completed_at ; l'heure de l'évènement, pas la date du fichier."""
    path = tmp_path / "rollout.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": "x", "cwd": "c"}},
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {"timestamp": "2026-10-20T10:00:00.000Z", "type": "event_msg", "payload": {"type": "turn_aborted"}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    finished, at = codex_app.turn_state(path)
    assert finished and at == 1792490400.0


def test_selection_multiple_reporte_le_rangement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Tour 3 : avec plusieurs chats sélectionnés, « Archiver » agirait sur toute la sélection de l'utilisateur."""
    state = _codex_page(monkeypatch)
    monkeypatch.setattr(cdp, "selection_size", lambda page: 2)
    result = codex_app.archive_chats([], tmp_path)
    assert result["status"] == "deferred" and "sélectionnés" in result["reason"]
    assert state["mode"] == "ChatGPT" and state["closed"] == 1  # mode rendu


def _base(root, rows, enfants=()):
    import sqlite3

    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root / "state_5.sqlite")
    con.execute("create table threads (id text primary key, rollout_path text, cwd text, archived integer)")
    con.execute(
        "create table thread_spawn_edges (parent_thread_id text, child_thread_id text primary key, status text)"
    )
    con.executemany("insert into threads values (?, ?, ?, ?)", rows)
    con.executemany("insert into thread_spawn_edges values ('p', ?, 'ok')", [(e,) for e in enfants])
    con.commit()
    con.close()


def test_recent_codex_app_state_comes_from_its_database(tmp_path):
    """23/10 (appli 26.1016.8622) : journaux aussi dans archived_sessions, « archivé » dans la base de l'appli."""
    root = tmp_path / "codex-home"
    relay = str(codex_app.relay_dir())
    actif = root / "archived_sessions" / "rollout-2026-10-23T05-49-05-actif.jsonl"
    range_ = root / "archived_sessions" / "rollout-2026-10-23T05-50-00-range.jsonl"
    for f in (actif, range_):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text('{"type":"event_msg","payload":{"type":"task_complete"}}\n', encoding="utf-8")
    _base(root, [("actif", str(actif), relay, 0), ("range", str(range_), relay, 1), ("autre", str(actif), r"C:\Dev", 0),
                 ("enfant", str(actif), relay, 0)], enfants=("enfant",))  # fmt: skip
    assert [c.id for c in codex_app.relay_chats(root)] == ["actif"]
    assert codex_app.is_archived(root, "range") and not codex_app.is_archived(root, "actif")
    assert codex_app.chat_path("actif", root) == actif


def test_unreadable_app_database_confirms_nothing(tmp_path):
    """Relecture de Codex : base présente mais illisible → jamais l'ancien critère du dossier (faux « archivé »)."""
    root = tmp_path / "codex-home"
    (root / "archived_sessions").mkdir(parents=True)
    (root / "archived_sessions" / "rollout-2026-10-23T05-49-05-actif.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "state_5.sqlite").write_bytes(b"pas une base sqlite" * 10)
    assert codex_app.is_archived(root, "actif") is False
    assert codex_app.relay_chats(root) == []
