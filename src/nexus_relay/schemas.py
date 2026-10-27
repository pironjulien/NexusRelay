"""Schémas de sortie des outils qui rendent des données (outputSchema + structuredContent, MCP 2025-06-18 et suivants).

Audit d'état de l'art du 27/10 : les 19 outils déclaraient un faux schéma `{result: string}` (fonctions `-> str`), le
client recevait du texte à relire. Les outils de lecture (inbox, read, status, track, search, reservations, summary)
rendent désormais des données typées ; les autres n'annoncent plus aucun schéma. Le bloc texte reste le même JSON
(clients sans structuredContent). Une erreur est rendue isError=True, sans structuredContent : elle ne suit pas le
schéma. Les clés facultatives sont NotRequired (absentes plutôt que nulles), comme dans le JSON d'avant.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class Attachment(TypedDict):
    name: NotRequired[str]
    mime: NotRequired[str]
    size: NotRequired[int]
    path: NotRequired[str]


# « from » est un mot réservé : forme fonctionnelle
RenderedMessage = TypedDict(
    "RenderedMessage",
    {
        "id": str,
        "from": str,
        "to": str,
        "topic": str,
        "created": str,
        "text": str,  # encadré ⟦relay:NONCE …⟧ … ⟦/relay:NONCE⟧
        "reply_to": NotRequired[str],
        "vote": NotRequired[str],
        "state": NotRequired[str],
        "due": NotRequired[str],
        "quality": NotRequired[str],
        "info": NotRequired[bool],
        "attachments": NotRequired[list[Attachment]],
        "cancelled": NotRequired[str],
    },
)


class _Base(TypedDict):
    status: Literal["success"]
    late_replies: NotRequired[list[RenderedMessage]]  # réponses arrivées après un wait_reply abandonné
    late_note: NotRequired[str]


class InboxOut(_Base):
    unread: list[RenderedMessage]
    note: str
    hidden: NotRequired[str]
    error: NotRequired[str]  # liste interrompue : ce qui est déjà rendu reste lu, la suite non lue


class ReadOut(_Base):
    message: RenderedMessage
    note: str


class TrackOut(_Base):
    id: str
    to: str
    topic: str
    state: str
    events: list[dict[str, Any]]
    replies: list[str]
    rings: dict[str, Any] | None
    wake: dict[str, Any] | None
    delivery: NotRequired[list[dict[str, Any]]]
    tool_errors: NotRequired[dict[str, Any]]


class Reservation(TypedDict):
    id: str
    agent: str
    paths: list[str]
    reason: NotRequired[str]
    exclusive: NotRequired[bool]
    expires: NotRequired[str]


class ReservationsOut(_Base):
    active: list[Reservation]


SearchHit = TypedDict(
    "SearchHit",
    {
        "id": str,
        "from": str,
        "to": str,
        "topic": str,
        "created": str,
        "score": float,
        "snippet": str,
        "reply_to": NotRequired[str],
    },
)


class SearchOut(_Base):
    total: int
    hits: list[SearchHit]
    note: str


class OpenAsk(TypedDict):
    id: str
    to: str
    created: str
    state: str
    text: str


class AnsweredAsk(TypedDict):
    id: str
    to: str
    created: str
    outcome: str
    vote: NotRequired[str]
    text: str
    reply: str


LastMessage = TypedDict("LastMessage", {"id": str, "from": str, "created": str, "text": str})


class SummaryOut(_Base):
    topic: str
    closed: bool
    messages: int
    participants: dict[str, int]
    first_at: str
    last_at: str
    open_total: int
    open: list[OpenAsk]
    answered_total: int
    answered: list[AnsweredAsk]
    last: list[LastMessage]
    note: str


class Incident(TypedDict):
    id: str
    to: str | None
    incident: str
    topic: str
    state: str


class StatusOut(_Base):
    me: str
    host: str
    version: str
    bell: dict[str, Any]
    en_cours: list[dict[str, Any]]
    agents: dict[str, dict[str, Any]]
    incidents: list[Incident]
    reservations: list[Reservation]
    selftest: list[dict[str, Any]]
