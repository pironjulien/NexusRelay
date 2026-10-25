"""Journal commun des outils Nexus (même format que Nexus Windows), produit « relay » : une ligne par action
(send, reply, read, wait_reply, wake, livraison, tour), en plus des évènements détaillés du salon.

`agent` : l'IA qui agit (pour un réveil, une livraison ou un tour arrêté : celle qui a envoyé la demande) ; `app` : le
pair visé ; `appel` : l'id du message, seulement s'il a la forme d'un id (relecture de Codex : un argument refusé
pouvait contenir un extrait de message). JAMAIS le texte d'un message : `erreur` n'est qu'une cause courte (filtre,
quota, appli figée…) ou le début d'un message d'erreur coupé au premier guillemet, deux-points, parenthèse ou crochet.

Jamais bloquant : les lignes partent dans une file bornée, écrite par un fil à part (relecture de Codex : un disque
lent bloquait la boucle du démon) ; file pleine, la ligne est perdue plutôt que le relais ralenti.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .box import MESSAGE_ID

QUEUE_MAX = 1000
_QUEUE: queue.Queue[tuple[Path, dict[str, Any]]] = queue.Queue(maxsize=QUEUE_MAX)
_WRITER: threading.Thread | None = None
_WRITER_LOCK = threading.Lock()

_CUT = re.compile(r"[«\"“'’(\[:{]")
# causes connues, de la plus précise à la plus générale (raisons rendues par les réveils et la surveillance des tours)
CAUSES = (
    (re.compile(r"flagged|filtre|content policy|cybersecurity", re.IGNORECASE), "filtre"),
    (re.compile(r"quota|utilisés|épuisé|usage limit|rate limit", re.IGNORECASE), "quota"),
    # essai réel du 24/10 : « l'interface … » coupé à l'apostrophe donnait « l » ; « sidecar muet » n'est pas figé
    (re.compile(r"déjà pilotée|interface .*occupée", re.IGNORECASE), "interface occupée"),
    (re.compile(r"sidecar muet|sidecar mort", re.IGNORECASE), "sidecar muet"),
    (re.compile(r"jeton|premier plan tenu", re.IGNORECASE), "jeton pris"),
    (re.compile(r"routeur", re.IGNORECASE), "routeur arrêté"),
    (re.compile(r"projet absent|projet .*introuvable|chat du sujet", re.IGNORECASE), "chat introuvable"),
    (re.compile(r"délai dépassé|timeout", re.IGNORECASE), "délai dépassé"),
    (re.compile(r"figé|muet|ne répond pas|not responding|hung", re.IGNORECASE), "appli figée"),
    (re.compile(r"fermée?\b|port .*fermé|introuvable sur ce PC", re.IGNORECASE), "appli fermée"),
    (re.compile(r"utilisateur", re.IGNORECASE), "L'utilisateur actif"),
    (re.compile(r"sans réponse", re.IGNORECASE), "tour fini sans réponse"),
)


def _root() -> Path:
    base = os.environ.get("NEXUS_JOURNAL_HOME") or os.path.join(
        os.environ.get("LOCALAPPDATA", str(Path.home())), "NexusJournal"
    )
    return Path(base) / "relay"


def erreur_propre(texte: Any) -> str | None:
    if not texte:
        return None
    debut = _CUT.split(" ".join(str(texte).split()), maxsplit=1)[0].strip(" .,;-")
    return debut[:80] or "error"


def cause(reason: Any) -> str | None:
    """Cause courte d'un échec, sans texte venu d'un message ni d'un écran."""
    if not reason:
        return None
    text = str(reason)
    return next((label for pattern, label in CAUSES if pattern.search(text)), None) or erreur_propre(text)


def _write_to(path: Path, line: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")  # échappé : jamais d'erreur d'encodage


def _drain() -> None:
    while True:
        path, line = _QUEUE.get()
        try:
            _write_to(path, line)
        except Exception:  # noqa: BLE001 - journal : jamais bloquant
            pass
        finally:
            _QUEUE.task_done()


def _writer() -> None:
    global _WRITER
    with _WRITER_LOCK:
        if _WRITER is None or not _WRITER.is_alive():
            _WRITER = threading.Thread(target=_drain, name="journal commun", daemon=True)
            _WRITER.start()
            atexit.register(flush, 2.0)  # sortie propre (relance du démon, du sidecar) : file vidée, 2 s au plus


def flush(timeout: float = 5.0) -> None:
    """Attend que les lignes en file soient écrites (tests, arrêt propre), `timeout` secondes au plus."""
    deadline = time.monotonic() + timeout
    while _QUEUE.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)


def note(
    agent: str,
    action: str,
    app: str | None,
    issue: str,
    started: float | None = None,
    erreur: Any = None,
    appel: str | None = None,
    technique: str | None = None,
    ms: float | None = None,
    premier_plan: bool = False,
) -> None:
    """Une action dans le journal commun ; `started` (time.perf_counter) ou `ms` donnent la durée. `premier_plan` :
    vrai quand une activation de fenêtre a servi (Codex relancé et activé par un réveil)."""
    try:
        if ms is None and started is not None:
            ms = (time.perf_counter() - started) * 1000
        line = {
            "v": 1,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "agent": agent,
            "outil": __version__,
            "appel": appel if isinstance(appel, str) and MESSAGE_ID.match(appel) else None,
            "app": app,
            "cadre": None,
            "action": action,
            "technique": technique,
            "issue": issue,
            "ms": round(ms) if ms is not None else None,
            "premier_plan": bool(premier_plan),
            "erreur": cause(erreur) if erreur else None,
        }
        # destination figée à l'enfilage, pas au drainage (relecture de Codex : une ligne en attente partait ailleurs)
        path = _root() / agent / f"{datetime.now(UTC):%Y-%m}.jsonl"
        _writer()
        _QUEUE.put_nowait((path, line))
    except Exception:  # noqa: BLE001 - file pleine ou journal en panne : jamais bloquant
        pass
