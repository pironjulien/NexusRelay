"""Chats du relais dans l'appli Codex (ChatGPT.exe) : reconnus et archivés dès leur tour fini.

Codex n'a pas de réveil autonome : le relais ouvre un chat dans le projet Codex « Nexus Relay » (dossier vide
`~\\.nexus\\relay\\codex-projet`, hors OneDrive) avec un message court qui cite l'id d'un message du relais.
Consigne de l'utilisateur (19/10) : rangés aussitôt la réponse récupérée, ~10 s de grâce au plus.

Identité (relectures du 20/10 : ni le titre, ni un id cité, ni une ligne apparue au bon moment ne prouvent qu'un chat
est au relais ; un chat de l'utilisateur a pu être pris) :
- un chat est au relais si et seulement si son journal (`~/.codex/sessions/…/rollout-…-<id>.jsonl`) est né dans le
  dossier du projet du relais : `cwd` de sa première ligne (`session_meta`) ;
- les sous-agents d'un chat (``parent_thread_id`` dans ``session_meta``) n'ont pas de ligne : écartés ;
- chaque ligne de la barre latérale porte l'id de sa conversation (props de l'appli, relevé le 20/10) : on ne range
  QUE les lignes dont l'id est un chat du relais au tour fini depuis TIDY_S, relu juste avant chaque clic, jamais
  quand l'utilisateur est dans Codex (relectures du tour 2 : le bloc du projet seul ne prouvait rien) ;
- un tour commence par `task_started` et finit par `task_complete` ou `turn_aborted` ;
- archivé, le journal passe dans `~/.codex/archived_sessions/` : c'est la PREUVE d'archivage.
Rien n'est jamais supprimé ici ; un échec reste un échec (réessayé plus tard, de plus en plus rarement).
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import relay_home

PROCESS = "chatgpt.exe"
VERIFY_S = 8.0
POLL_S = 0.4
RECENT_S = 7 * 24 * 3600.0  # journaux plus vieux : ignorés (un chat du relais vit quelques minutes)
TAIL_BYTES = 262144  # fin du journal relue pour l'état du tour (évènements de tour : quelques centaines d'octets)
TURN_END = ("task_complete", "turn_aborted")  # vu le 20/10 : un tour arrêté finit par turn_aborted
_meta: dict[Path, dict[str, Any]] = {}


def home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def relay_dir() -> Path:
    """Dossier du projet Codex du relais : le `cwd` de ses chats."""
    return relay_home() / "codex-projet"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.removeprefix("\\\\?\\"))).rstrip("\\/")


@dataclass
class Chat:
    id: str
    path: Path
    finished: bool = True  # dernier tour terminé
    completed_at: float | None = None  # fin du dernier tour (completed_at de task_complete), époque Unix

    def idle_s(self, now: float) -> float | None:
        """Temps depuis la fin du dernier tour. Pas la date du fichier : Windows ne la met pas à jour tant que
        Codex garde le journal ouvert (relevé le 19/10 : 7 min de retard sur un tour fini)."""
        if self.completed_at is not None:
            return now - self.completed_at
        try:
            return now - self.path.stat().st_mtime
        except OSError:
            return None


def _payloads(lines: list[str]) -> list[dict[str, Any]]:
    found = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # ligne coupée (début de la fin lue, écriture en cours)
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if isinstance(payload, dict):
            found.append(payload)
    return found


def chat_meta(path: Path) -> dict[str, Any]:
    """``session_meta`` du chat (première ligne du journal) ; ne change jamais : en cache."""
    if path in _meta:
        return _meta[path]
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
    except OSError:
        return {}
    payloads = _payloads([first])
    meta = payloads[0] if payloads else {}
    if meta.get("cwd"):
        _meta[path] = meta
    return meta


def turn_state(path: Path) -> tuple[bool, float | None]:
    """(tour fini ?, fin du dernier tour) d'après la FIN du journal, relue à chaque appel : ni la date ni la taille
    du fichier ne font foi pendant que Codex l'écrit. Aucun évènement de tour dans la fin lue : « en cours »."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return False, None
    started = completed = -1
    completed_at: float | None = None
    entries = []
    for line in tail:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("payload"), dict):
            entries.append(entry)
    for index, entry in enumerate(entries):
        payload = entry["payload"]
        if payload.get("type") == "task_started":
            started = index
        elif payload.get("type") in TURN_END:
            completed = index
            stamp = payload.get("completed_at")
            # turn_aborted sans completed_at : l'heure de l'évènement, pas la date du fichier (en retard sous Windows)
            completed_at = float(stamp) if isinstance(stamp, int | float) else _iso(entry.get("timestamp"))
    return completed > started, completed_at if completed > started else None


def _entry(line: str) -> dict[str, Any] | None:
    try:
        entry = json.loads(line)
    except ValueError:
        return None  # ligne coupée (début de la fin lue, écriture en cours)
    return entry if isinstance(entry, dict) else None


def turn_after(path: Path, since: float) -> dict[str, Any] | None:
    """Dernier tour du chat commencé à `since` ou après (réveil d'une demande) : {"ended", "at" (fin), "error",
    "last" (dernière ligne écrite)} ; None : aucun tour depuis. `error` : message d'erreur de task_complete (filtre de
    contenu, panne du modèle : vu le 24/10) ou raison d'un turn_aborted."""
    # fin du journal relue de plus en plus loin jusqu'à couvrir `since` (relecture de Codex : un tour de plus de
    # 256 Kio perdait son task_started, et l'arrêt passait inaperçu)
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            span = TAIL_BYTES
            while True:
                handle.seek(max(0, size - span))
                tail = handle.read().decode("utf-8", errors="replace").splitlines()
                first = next((_iso(e.get("timestamp")) for e in map(_entry, tail[1:]) if e), None)
                if span >= size or (first is not None and first < since):
                    break
                span *= 4
    except OSError:
        return None
    found: dict[str, Any] | None = None
    last: float | None = None
    for entry in map(_entry, tail):
        payload = entry.get("payload") if entry else None
        if entry is None or not isinstance(payload, dict):
            continue
        at = _iso(entry.get("timestamp"))
        last = at if at is not None else last
        kind = payload.get("type")
        if kind == "task_started" and at is not None and at >= since:
            found = {"ended": False, "at": None, "error": None}
        elif kind in TURN_END and found is not None:
            error = payload.get("error")
            reason = error.get("message") if isinstance(error, dict) else error or payload.get("reason")
            found = {"ended": True, "at": at, "error": str(reason) if reason else None}
    return {**found, "last": last} if found is not None else None


def _iso(stamp: Any) -> float | None:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _id_of(path: Path) -> str:
    """« rollout-2026-10-19T11-43-11-<id>.jsonl » → <id> (forme « <id>_<id> » : le premier)."""
    stem = path.name.split(".", 1)[0]
    match = re.match(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)$", stem)
    return match.group(1).split("_", 1)[0] if match else ""


UNREADABLE: dict[str, dict[str, Any]] = {}  # sentinelle : base de l'appli présente mais illisible


def _db_threads(root: Path) -> dict[str, dict[str, Any]] | None:
    """Conversations connues de l'appli (state_5.sqlite, lu en lecture seule) : id → {path, cwd, archived, parent}.
    Depuis l'appli 26.1016.8622 (23/10), les journaux vivent aussi dans archived_sessions et l'état « archivé » est
    dans cette base, plus dans l'emplacement du fichier. Base absente ou illisible : None (ancien repérage)."""
    import sqlite3

    base = root / "state_5.sqlite"
    if not base.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{base.as_posix()}?mode=ro", uri=True, timeout=2.0)
        try:
            enfants = {r[0] for r in con.execute("select child_thread_id from thread_spawn_edges")}
            rows = con.execute("select id, rollout_path, cwd, archived from threads").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return UNREADABLE  # base présente mais illisible : rien n'est confirmé (jamais l'ancien critère du dossier)
    return {
        str(i): {"path": Path(p) if p else None, "cwd": c or "", "archived": bool(a), "child": str(i) in enfants}
        for i, p, c, a in rows
    }


def chat_path(chat_id: str, root: Path | None = None) -> Path | None:
    """Journal d'un chat, où qu'il soit (base de l'appli, puis sessions, puis archived_sessions)."""
    root = root or home()
    threads = _db_threads(root)
    row = (threads or {}).get(chat_id)
    if row and row["path"] is not None and row["path"].is_file():
        return row["path"]
    return _find(root / "sessions", chat_id) or _find(root / "archived_sessions", chat_id)


def relay_chats(root: Path | None = None, now: float | None = None) -> list[Chat]:
    """Chats ACTIFS (non archivés) nés dans le dossier du projet du relais, sans leurs sous-agents."""
    root = root or home()
    now = time.time() if now is None else now
    threads = _db_threads(root)
    if threads is UNREADABLE:
        return []  # base illisible : aucun chat sûr à ranger
    if threads is not None:
        mine_db = _norm(str(relay_dir()))
        found = []
        for chat_id, row in threads.items():
            path = row["path"]
            if row["archived"] or row["child"] or _norm(row["cwd"]) != mine_db or path is None or not path.is_file():
                continue
            try:
                if now - path.stat().st_mtime > RECENT_S:
                    continue
            except OSError:
                continue
            finished, completed_at = turn_state(path)
            found.append(Chat(chat_id, path, finished, completed_at))
        return found
    folder = root / "sessions"
    mine = _norm(str(relay_dir()))
    chats = []
    for path in sorted(folder.rglob("rollout-*.jsonl")) if folder.is_dir() else []:
        try:
            if now - path.stat().st_mtime > RECENT_S:
                continue
        except OSError:
            continue
        chat_id = _id_of(path)
        meta = chat_meta(path)
        if chat_id and not meta.get("parent_thread_id") and _norm(str(meta.get("cwd") or "")) == mine:
            finished, completed_at = turn_state(path)
            chats.append(Chat(chat_id, path, finished, completed_at))
    return chats


def _find(folder: Path, chat_id: str) -> Path | None:
    if not folder.is_dir():
        return None
    return next(iter(sorted(folder.rglob(f"rollout-*{chat_id}*.jsonl*"))), None)


def is_archived(root: Path, chat_id: str) -> bool:
    threads = _db_threads(root)
    if threads is UNREADABLE:
        return False  # non confirmé : un clic sans effet n'est jamais compté comme archivage réussi
    row = (threads or {}).get(chat_id)
    if row is not None:
        return row["archived"]  # appli récente : l'état est dans sa base, pas dans l'emplacement du journal
    return _find(root / "sessions", chat_id) is None and _find(root / "archived_sessions", chat_id) is not None


def ready(chat: Chat, now: float, grace_s: float) -> bool:
    """Tour fini depuis au moins `grace_s` (relu dans le journal)."""
    finished, completed_at = turn_state(chat.path)
    fresh = Chat(chat.id, chat.path, finished, completed_at)
    return finished and (fresh.idle_s(now) or 0) >= grace_s


def archive_project(
    rows: Callable[[], dict[str, str]],
    archive_row: Callable[[str, Callable[[], str | None]], tuple[str, str | None]],
    chats: Callable[[], list[Chat]],
    root: Path,
    grace_s: float,
    present: Callable[[], bool] = lambda: False,
    verify_s: float = VERIFY_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    eligible: Callable[[Chat], bool] = lambda chat: True,
) -> dict[str, Any]:
    """Range, un par un, les chats du relais au tour fini dont la ligne est trouvée par son id (``rows`` : conversation
    → ligne). Garde relue juste avant CHAQUE clic : l'utilisateur dans Codex, ce chat qui reprend, ou sa ligne qui a changé
    arrêtent tout. Preuve : le journal passé dans archived_sessions."""
    targets = [c for c in chats() if ready(c, clock(), grace_s) and eligible(c)]
    mapping = rows() if targets else {}
    targets = [c for c in targets if c.id in mapping]
    if not targets:
        return {"status": "nothing"}
    clicked: list[str] = []
    skipped = None
    for chat in targets:
        row = mapping[chat.id]

        def guard(chat: Chat = chat, row: str = row) -> str | None:
            if present():
                return "L'utilisateur est dans Codex"
            if not ready(chat, clock(), grace_s):
                return "ce chat du relais travaille"
            if not eligible(chat):  # sujet rouvert entre-temps (relu avant chaque clic)
                return "sujet de ce chat redevenu actif"
            if rows().get(chat.id) != row:
                return "ligne du chat changée"
            return None

        outcome, why = archive_row(row, guard)
        if outcome == "skipped":
            skipped = why
            break
        if outcome == "clicked":
            clicked.append(chat.id)
    deadline = time.monotonic() + verify_s
    while any(not is_archived(root, c) for c in clicked) and time.monotonic() < deadline:
        sleep(POLL_S)
    archived = [c for c in clicked if is_archived(root, c)]
    if skipped and not clicked:
        return {"status": "skipped", "reason": skipped}
    status = "archived" if archived and len(archived) == len(clicked) else "failed"
    return {"status": status, "clicked": len(clicked), "archived": archived, **({"reason": skipped} if skipped else {})}


def archive_chats(
    chats: list[Chat],
    root: Path | None = None,
    grace_s: float = 10.0,
    eligible: Callable[[Chat], bool] = lambda chat: True,
) -> dict[str, Any]:
    """Archivage réel par l'appli Codex (clic sur la ligne de chaque chat du relais, par son port), vérifié par les
    journaux ; mode de l'appli rendu tel que l'utilisateur l'avait ; une interface muette est un échec noté, jamais une
    exception qui interrompt le rangement (tour 2). Seuls les chats REÇUS (`chats`) et toujours `eligible` au moment
    du clic sont rangés (relecture de Codex du 21/10 : tous les chats du relais l'étaient, sujets actifs compris)."""
    selected = {c.id for c in chats}
    from . import cdp

    root = root or home()
    url = cdp.page_url()
    if not url:
        return {"status": "failed", "reason": "port de débogage de Codex fermé"}
    lock = cdp.UI_LOCKS["codex"]
    if not lock.acquire(blocking=False):
        return {"status": "deferred", "reason": cdp.BUSY_REASON}
    page = None
    mode = None
    try:  # verrou rendu même si la page ne s'ouvre pas (appli fermée ou mise à jour : relecture de Codex)
        page = cdp.Page(url)
        if cdp.user_in_codex():  # relu juste avant de changer le mode (tour 3)
            return {"status": "deferred", "reason": "L'utilisateur est dans Codex"}
        mode = cdp.codex_mode(page)
        if not cdp.ensure_codex_mode(page):  # les chats du relais sont dans la barre du mode Codex
            return {"status": "failed", "reason": "mode Codex de l'appli non sélectionné"}
        if cdp.selection_size(page) > 1:  # « Archiver » agirait sur toute la sélection de l'utilisateur (tour 3)
            return {"status": "deferred", "reason": "plusieurs chats sélectionnés dans Codex"}
        return archive_project(
            lambda: {conv: row for row, conv, _ in cdp.row_conversations(page)},
            lambda row, guard: cdp.archive_thread(page, row, guard),
            lambda: [c for c in relay_chats(root) if c.id in selected],
            root,
            grace_s,
            present=cdp.user_in_codex,
            eligible=eligible,
        )
    except Exception as error:  # noqa: BLE001 - port muet, page fermée : échec noté, pause croissante
        return {"status": "failed", "reason": f"{type(error).__name__}: {error}"}
    finally:
        if page is not None:
            try:
                if mode and cdp.codex_mode(page) != mode:
                    cdp.set_codex_mode(
                        page, mode
                    )  # vu le 20/10 : le mode ChatGPT de l'utilisateur restait en mode Codex
            except Exception:  # noqa: BLE001
                pass
            page.close()
        lock.release()
