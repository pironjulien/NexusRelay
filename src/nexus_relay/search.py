"""Recherche plein texte dans le salon et résumé d'un sujet (24/10 : manque relevé face à MCP Agent Mail).

1.6.0 (27/10) : index SQLite FTS5 (tokeniseur trigram) au lieu d'un parcours de tous les messages à chaque recherche
(924 messages, 4,4 Mo : ~0,2 s ; la boîte grossit sans fin). Index dans `<boîte>/index/search.sqlite3`, une ligne par
fichier de message, texte replié (sans accents ni casse). Les messages sont immuables : à chaque recherche, seuls les
fichiers absents de l'index sont lus et ajoutés (l'historique existant y entre tout seul à la première recherche, ou au
démarrage du démon) ; un fichier disparu en sort. Le score (occurrences) est compté sur le texte replié de l'index,
l'extrait sur le message, comme avant : mêmes résultats ; seuls les messages rendus (`limit`) sont relus et repliés.
Plus pertinent d'abord, puis plus récent.
Sans accents ni casse ; tous les mots demandés doivent y être ; "entre guillemets" = expression exacte.
"""

from __future__ import annotations

import random
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any

from .box import Box, Message, read_json
from .lang import tr

SNIPPET = 89  # caractères de part et d'autre du premier mot trouvé
PREVIEW = 233
TRIGRAM = 3  # le tokeniseur trigram ne cherche que des chaînes de 3 caractères au moins ; en dessous : instr()
OPEN_WAIT_S = 30  # attente maximale d'une base occupée (verrou SQLite, initialisation simultanée)
SCHEMA = 1  # PRAGMA user_version : un autre numéro = index reconstruit
_SYNC_LOCK = threading.Lock()  # un seul rattrapage à la fois dans ce processus (les autres : verrou de SQLite)
_DDL = (
    "DROP TABLE IF EXISTS fts",
    "DROP TABLE IF EXISTS messages",
    "CREATE TABLE messages (rowid INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, id TEXT NOT NULL,"
    " sender TEXT NOT NULL, topic TEXT NOT NULL, plain TEXT NOT NULL)",
    "CREATE VIRTUAL TABLE fts USING fts5(plain, content='messages', content_rowid='rowid',"
    " tokenize='trigram case_sensitive 1')",
    "CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN"
    " INSERT INTO fts(rowid, plain) VALUES (new.rowid, new.plain); END",
    "CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN"
    " INSERT INTO fts(fts, rowid, plain) VALUES ('delete', old.rowid, old.plain); END",
)


def fold(text: str) -> tuple[str, list[int]]:
    """Texte sans accents ni casse, et pour chaque caractère rendu sa position dans l'original (extrait juste)."""
    out: list[str] = []
    where: list[int] = []
    for index, char in enumerate(text):
        bare = "".join(c for c in unicodedata.normalize("NFD", char) if not unicodedata.combining(c)).casefold()
        out.append(bare)
        where.extend([index] * len(bare))
    return "".join(out), where


def bare(text: str) -> str:
    """Comme fold, sans les positions : le tri rapide de tous les messages (fold seulement pour ceux trouvés)."""
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)).casefold()


def terms(query: str) -> list[str]:
    """Mots et "expressions" de la requête, repliés comme le texte."""
    found = [phrase or word for phrase, word in re.findall(r'"([^"]+)"|(\S+)', query)]
    return [folded for item in found if (folded := fold(item.strip())[0])]


def snippet(text: str, start: int) -> str:
    lo, hi = max(0, start - SNIPPET), min(len(text), start + SNIPPET)
    return ("…" if lo else "") + " ".join(text[lo:hi].split()) + ("…" if hi < len(text) else "")


def preview(text: str) -> str:
    text = " ".join(text.split())
    return text[:PREVIEW] + ("…" if len(text) > PREVIEW else "")


def index_path(box: Box) -> Path:
    return box.root / "index" / "search.sqlite3"


def _busy(error: sqlite3.OperationalError) -> bool:
    text = str(error).lower()
    return "locked" in text or "busy" in text


def _prepare(db: sqlite3.Connection) -> None:
    """WAL et schéma. Passer en WAL demande un verrou exclusif que SQLite n'attend pas toujours (busy_timeout ignoré) :
    plusieurs ouvertures simultanées d'une base neuve → « database is locked » (relecture de Codex, 27/10)."""
    if str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
        db.execute("PRAGMA journal_mode=WAL")  # lecteurs jamais bloqués par l'écrivain (démon, sessions stdio)
    if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA:
        db.execute("BEGIN IMMEDIATE")
        try:  # relu sous le verrou : un autre processus a pu le créer entre-temps
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA:
                for statement in _DDL:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version = {SCHEMA}")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def _connect(box: Box) -> sqlite3.Connection:
    """Connexion prête ; l'initialisation occupée par un autre fil ou processus est retentée, dans la limite de
    OPEN_WAIT_S (valable entre processus : seul SQLite arbitre)."""
    path = index_path(box)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + OPEN_WAIT_S
    while True:
        db = sqlite3.connect(path, timeout=OPEN_WAIT_S, isolation_level=None)
        try:
            _prepare(db)
            return db
        except sqlite3.OperationalError as error:
            db.close()
            if not _busy(error) or time.monotonic() >= deadline:
                raise
        except BaseException:
            db.close()
            raise
        time.sleep(random.uniform(0.01, 0.0618))


def _inventory(box: Box) -> dict[str, Path]:
    return {path.name: path for path in box.root.joinpath("messages").glob("*.json")}


def _sync(box: Box, db: sqlite3.Connection) -> int:
    """Ajoute les fichiers de message absents de l'index, retire ceux qui ont disparu ; rend le nombre ajouté.
    Les nouveaux messages (immuables) sont lus hors verrou ; l'inventaire qui décide des retraits et la lecture de
    l'index se refont sous BEGIN IMMEDIATE, exclusif entre processus (relecture de Codex, 27/10 : un inventaire pris
    avant le verrou retirait de l'index un message qu'un autre processus venait d'y ajouter)."""
    with _SYNC_LOCK:
        names = _inventory(box)
        known = {row[0] for row in db.execute("SELECT name FROM messages")}
        if names.keys() == known:
            return 0
        rows = {}
        for name in sorted(set(names) - known):
            data = read_json(names[name])
            try:
                message = Message.from_json(data) if data else None
            except (KeyError, TypeError, ValueError):
                message = None
            if message is None:
                continue  # illisible : réessayé à la prochaine recherche (la boîte l'ignore aussi)
            rows[name] = (name, message.id, message.sender, message.topic, bare(message.text))
        db.execute("BEGIN IMMEDIATE")
        try:
            names = _inventory(box)  # sous le verrou : ce qui existe vraiment au moment d'écrire
            known = {row[0] for row in db.execute("SELECT name FROM messages")}
            gone = known - names.keys()
            fresh = [row for name, row in rows.items() if name in names and name not in known]
            db.executemany("DELETE FROM messages WHERE name = ?", [(name,) for name in gone])
            db.executemany(
                "INSERT OR IGNORE INTO messages (name, id, sender, topic, plain) VALUES (?, ?, ?, ?, ?)", fresh
            )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return len(fresh)


def sync(box: Box) -> int:
    """Index à jour avec la boîte (migration de l'historique au premier appel) ; rend le nombre de messages ajoutés."""
    with closing(_connect(box)) as db:
        return _sync(box, db)


def _candidates(box: Box, wanted: list[str], topic: str | None, sender: str | None) -> list[tuple[str, str, str]]:
    """(nom du fichier, id, texte replié) des messages qui contiennent chaque terme (FTS5 dès 3 caractères, instr
    en dessous)."""
    # NUL (U+0000) : FTS5 coupe la chaîne de requête (« unterminated string », relecture de Codex 27/10) → instr()
    long = [term for term in wanted if len(term) >= TRIGRAM and "\x00" not in term]
    where, args = [], []
    if long:
        where.append("rowid IN (SELECT rowid FROM fts WHERE fts MATCH ?)")
        args.append(" AND ".join('"' + term.replace('"', '""') + '"' for term in long))
    for term in wanted:
        if term not in long:
            where.append("instr(plain, ?) > 0")
            args.append(term)
    if topic:
        where.append("topic = ?")
        args.append(topic)
    if sender:
        where.append("sender = ?")
        args.append(sender)
    with closing(_connect(box)) as db:
        _sync(box, db)
        return db.execute(f"SELECT name, id, plain FROM messages WHERE {' AND '.join(where)}", args).fetchall()


def search_page(
    box: Box, query: str, topic: str | None = None, sender: str | None = None, limit: int | None = None
) -> tuple[int, list[tuple[Message, int, str]]]:
    """(nombre trouvé, `limit` premiers (message, score, extrait)) : plus pertinent puis plus récent d'abord. Le score
    vient du texte replié de l'index ; seuls les messages rendus sont relus et repliés pour leur extrait."""
    wanted = terms(query)
    if not wanted:
        raise ValueError(tr("empty search: give at least one word", "recherche vide : donne au moins un mot"))
    try:
        rows = _candidates(box, wanted, topic, sender)
    except sqlite3.Error as error:
        raise OSError(tr(f"search index: {error}", f"index de recherche : {error}")) from error
    scored = []
    for name, message_id, plain in rows:
        counts = [plain.count(term) for term in wanted]
        if all(counts):
            scored.append((sum(counts), message_id, name))
    scored.sort(reverse=True)
    folder = box.root / "messages"
    hits = []
    for score, _, name in scored[:limit]:
        data = read_json(folder / name)
        if data is None:
            continue  # disparu depuis la recherche
        message = Message.from_json(data)
        folded, where = fold(message.text)
        first = min(i for term in wanted if (i := folded.find(term)) >= 0)
        hits.append((message, score, snippet(message.text, where[first] if where else 0)))
    return len(scored), hits


def search(box: Box, query: str, topic: str | None = None, sender: str | None = None) -> list[tuple[Message, int, str]]:
    """(message, score, extrait) de tous les messages contenant chaque mot, plus pertinent puis plus récent d'abord."""
    return search_page(box, query, topic, sender)[1]


def summary(box: Box, topic: str) -> dict[str, Any]:
    """État d'un sujet sans le relire : qui, combien, demandes ouvertes, issues des demandes répondues, derniers mots.
    Les textes rendus sont des `Message` ; l'appelant les encadre (⟦relay:…⟧)."""
    messages = sorted(box.topic_messages(topic), key=lambda m: m.id)
    if not messages:
        raise ValueError(tr(f"unknown topic “{topic}” (no message)", f"sujet « {topic} » inconnu (aucun message)"))
    participants: dict[str, int] = {}
    for message in messages:
        participants[message.sender] = participants.get(message.sender, 0) + 1
    replies: dict[str, list[Message]] = {}
    for message in messages:
        if message.reply_to:
            replies.setdefault(message.reply_to, []).append(message)
    open_asks, answered = [], []
    for ask in (m for m in messages if not m.info and not m.reply_to):
        own = [r for r in replies.get(ask.id, []) if r.sender == ask.to]
        if own:
            answered.append((ask, own[0]))
        else:
            open_asks.append(ask)
    return {
        "messages": messages,
        "participants": participants,
        "open": open_asks,
        "answered": answered,
    }
