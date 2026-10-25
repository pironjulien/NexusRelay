"""Recherche plein texte dans le salon et résumé d'un sujet (24/10 : manque relevé face à MCP Agent Mail).

Le salon tient en quelques centaines de messages (496, 1 Mo le 24/10) : un parcours en mémoire, sans index ni
dépendance, répond en quelques dizaines de millisecondes. Sans accents ni casse ; tous les mots demandés doivent y être ;
"entre guillemets" = expression exacte. Plus pertinent d'abord (occurrences), puis plus récent.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .box import Box, Message

SNIPPET = 89  # caractères de part et d'autre du premier mot trouvé
PREVIEW = 233


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


def search(box: Box, query: str, topic: str | None = None, sender: str | None = None) -> list[tuple[Message, int, str]]:
    """(message, score, extrait) de tous les messages contenant chaque mot, plus pertinent puis plus récent d'abord."""
    wanted = terms(query)
    if not wanted:
        raise ValueError("recherche vide : donne au moins un mot")
    pool = box.topic_messages(topic) if topic else box.all()
    hits = []
    for message in pool:
        if sender and message.sender != sender:
            continue
        plain = bare(message.text)
        counts = [plain.count(term) for term in wanted]
        if not all(counts):
            continue
        folded, where = fold(message.text)
        first = min(i for term in wanted if (i := folded.find(term)) >= 0)
        hits.append((message, sum(counts), snippet(message.text, where[first] if where else 0)))
    hits.sort(key=lambda hit: (hit[1], hit[0].id), reverse=True)
    return hits


def summary(box: Box, topic: str) -> dict[str, Any]:
    """État d'un sujet sans le relire : qui, combien, demandes ouvertes, issues des demandes répondues, derniers mots.
    Les textes rendus sont des `Message` ; l'appelant les encadre (⟦relay:…⟧)."""
    messages = sorted(box.topic_messages(topic), key=lambda m: m.id)
    if not messages:
        raise ValueError(f"sujet « {topic} » inconnu (aucun message)")
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
