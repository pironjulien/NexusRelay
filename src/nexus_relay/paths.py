"""Emplacements : boîte aux lettres (~\\.nexus\\relay) et données d'Antigravity (~\\.gemini), surchargeables pour les tests."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from .lang import tr

AGENTS = ("claude", "antigravity", "codex")
# Boîte de l'utilisateur : une boîte aux lettres, pas un agent (son panneau de Chrome, qui y déposait ses demandes, a été retiré
# le 20/10, jamais utilisé ; la boîte reste lisible pour les anciens messages).
USER = "user"
PARTIES = (*AGENTS, USER)
_ALIASES = {
    "claude": "claude",
    "antigravity": "antigravity",
    "agy": "antigravity",
    "gemini": "antigravity",
    "codex": "codex",
    "chatgpt": "codex",
    "openai": "codex",
}
DEFAULT_HOST = "claude"


def relay_host() -> str:
    """L'hôte du relais : le seul agent qui ouvre un échange, jamais sonné (il attend par wait_reply). Claude par défaut ;
    NEXUS_RELAY_HOST (réglage `host`) pour un poste sans Claude Code ou qui préfère un autre chef d'orchestre. Une valeur
    inconnue retombe sur Claude (jamais deux hôtes, jamais aucun)."""
    raw = os.environ.get("NEXUS_RELAY_HOST", "").strip().lower()
    if raw in ("all", "peers", "trinite", "*"):
        return "all"
    try:
        return normalize_agent(raw) if raw else DEFAULT_HOST
    except ValueError:
        return DEFAULT_HOST


_CONVERSATION = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def code_fingerprint() -> str:
    """Empreinte du code exécuté : le paquet (installation éditable) et uv.lock (dépendances)."""
    package = Path(__file__).resolve().parent
    project = package.parents[1]
    digest = hashlib.sha256()
    for path in [*sorted(package.rglob("*")), project / "uv.lock"]:
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(project).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def restart_wanted(request: Path, loaded: str) -> bool:
    """Demande de relance d'install.ps1 (démon ou sidecar), consommée : vrai si le code a changé depuis `loaded`, ou si
    la demande est forcée (« force » : réglages du démon changés, tour 5)."""

    def elsewhere(text: str) -> bool:
        target = re.search(r"(?:^|\s)pid:(\d+)(?:\s|$)", text)
        return bool(target and int(target.group(1)) != os.getpid())

    try:
        if elsewhere(request.read_text(encoding="utf-8")):
            return False
    except FileNotFoundError:
        return False
    taken = request.with_name(request.name + ".lue")
    try:
        os.replace(request, taken)  # prise d'un coup : une demande déposée pendant l'examen reste entière (tour 6)
    except FileNotFoundError:
        return False
    try:
        text = taken.read_text(encoding="utf-8").strip()
        if elsewhere(text):
            _give_back(taken, request)
            return False
        wanted = text.startswith("force") or code_fingerprint() != loaded
    except OSError:
        _give_back(taken, request)  # examen raté : la demande reste pour le tour suivant (tour 5)
        raise
    taken.unlink(missing_ok=True)
    return wanted


def _give_back(taken: Path, request: Path) -> None:
    try:
        forced = taken.read_text(encoding="utf-8").strip().startswith("force")
    except OSError:
        forced = False
    if forced or not request.exists():
        os.replace(taken, request)  # « force » couvre aussi une demande « code » plus récente
    else:
        taken.unlink(missing_ok=True)


def mark_restartable(name: str) -> None:
    """« Je suis la relance par demande » : install.ps1 n'arrête de force que les processus d'avant ce protocole
    (tour 5 : un ancien démon ou sidecar ne lit aucune demande et gardait l'ancien code)."""
    path = relay_home() / f"{name}-restart.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")


def relay_home() -> Path:
    """Racine de la boîte aux lettres (jamais dans OneDrive)."""
    override = os.environ.get("NEXUS_RELAY_HOME")
    return Path(override) if override else Path.home() / ".nexus" / "relay"


def gemini_home() -> Path:
    override = os.environ.get("NEXUS_RELAY_GEMINI_HOME")
    return Path(override) if override else Path.home() / ".gemini"


def brain_dir() -> Path:
    return gemini_home() / "antigravity" / "brain"


def conversation_alive(conversation: str) -> bool:
    """Conversation d'Antigravity encore vivante : sa base existe et elle n'est pas archivée. Vu le 20/10 : agentapi
    accepte un message pour une conversation archivée (ou disparue) sans erreur, puis le garde « undelivered » pour
    toujours ; la conversation du sujet doit alors être remplacée par une neuve."""
    root = gemini_home() / "antigravity"
    if not (root / "conversations" / f"{conversation}.db").exists():
        return False
    try:
        note = (root / "annotations" / f"{conversation}.pbtxt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return "archived:true" not in note.replace(" ", "")


AGY_ERROR_STEP = 17  # type d'étape « erreur » de la base d'Antigravity (conversations/<id>.db), relevé le 21/10


def agy_error(conversation: str) -> tuple[str, float | None] | None:
    """(raison, heure) si la conversation d'Antigravity s'est ARRÊTÉE sur une erreur d'exécution (« Agent execution
    terminated due to error ») ; None sinon. Vu le 21/10 : cette étape n'est écrite que dans sa base
    (conversations/<id>.db), pas dans transcript.jsonl : le tour paraissait en cours (« lu, sans réponse »), et la
    conversation restait cassée (« unknown model key … model not found ») pour tous les messages suivants du sujet.
    Lecture seule, une seule ligne : la dernière étape."""
    import sqlite3

    if not is_conversation_id(conversation):
        return None
    db = gemini_home() / "antigravity" / "conversations" / f"{conversation}.db"
    if not db.is_file():
        return None
    try:
        with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=1.0) as connection:
            row = connection.execute("SELECT step_type, step_payload FROM steps ORDER BY idx DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    # Étape d'ERREUR seulement (type 17, relevé le 21/10) : une sortie d'outil qui cite ce texte (Antigravity relisant
    # ce fichier même) n'est pas un arrêt ; le détail suit la phrase exacte « terminated due to error. ».
    if not row or row[0] != AGY_ERROR_STEP or not isinstance(row[1], bytes):
        return None
    payload = row[1]
    marker = b"Agent execution terminated due to error"
    if marker not in payload:
        return None
    reason = _agy_error_detail(payload[payload.index(marker) + len(marker) :]) or tr(
        "execution error", "erreur d'exécution"
    )
    stamp = re.search(rb"\n\x0c\x08([\x80-\xff]*[\x00-\x7f])", payload)  # Timestamp protobuf : secondes (varint)
    at = None
    if stamp:
        value = 0
        for shift, byte in enumerate(stamp.group(1)):
            value |= (byte & 0x7F) << (7 * shift)
        at = float(value)
    return reason, at


def _agy_error_detail(tail: bytes) -> str | None:
    """Détail qui suit « terminated due to error. » : champ protobuf 2 (0x12), longueur en varint, puis le texte."""
    if len(tail) < 3 or tail[0:1] != b"." or tail[1:2] != b"":
        return None
    length, shift, at = 0, 0, 2
    while at < len(tail):
        byte = tail[at]
        length |= (byte & 0x7F) << shift
        at += 1
        if byte < 0x80:
            break
        shift += 7
    text = tail[at : at + min(length, 300)].decode("utf-8", errors="replace").strip()
    return text or None


TOOL_ERRORS_WINDOW = 2 * 1024 * 1024  # fin du journal relue pour les outils en erreur (lignes d'outil parfois longues)
_TOOL_ERROR_HINTS = (  # (marqueur, anglais, français) : la langue est choisie à l'appel
    (
        "denied by pre-tool hook",
        'an Antigravity PreToolUse hook refuses its tools (it must answer {"decision": "allow"})',
        'un hook PreToolUse d\'Antigravity refuse ses outils (il doit répondre {"decision": "allow"})',
    ),
    ("is not enabled for server", "MCP tool disabled in Antigravity", "outil MCP désactivé dans Antigravity"),
)


def agy_tool_errors(conversation: str, since: float) -> dict[str, Any] | None:
    """Appels d'outil en ERREUR d'affilée à la fin de la conversation d'Antigravity, depuis `since` (la livraison) :
    {count, reason, hint, at, ended} ; None s'il n'y en a pas. Vu le 23/10 : un hook PreToolUse tiers refusait tous ses
    outils (« tool call denied by pre-tool hook ») ; Antigravity, réveillé trois fois, ne pouvait ni lire ni répondre,
    et le relais disait seulement « livré, lecture non confirmée ». Ces étapes (« GENERIC », statut « ERROR ») sont dans
    transcript.jsonl ; un outil réussi (ou en cours) depuis remet le compte à zéro : erreur ordinaire, rattrapée.
    `ended` : le tour s'est fini sur ces erreurs (dernière étape : réponse du modèle sans appel d'outil). Résultat gardé
    par état du journal (taille, date) : status relit chaque conversation une fois (relecture de Codex)."""
    if not is_conversation_id(conversation):
        return None
    path = transcript_path(conversation, gemini_home() / "antigravity" / "brain")
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), since, stat.st_mtime_ns, stat.st_size)
    if key not in _TOOL_ERRORS_SEEN:
        try:
            found = _tool_errors(path, since)
        except OSError:
            return None  # lecture ratée (verrou passager) : jamais gardée, relue la prochaine fois (tour 2)
        if len(_TOOL_ERRORS_SEEN) >= 64:
            _TOOL_ERRORS_SEEN.clear()
        _TOOL_ERRORS_SEEN[key] = found
    return _TOOL_ERRORS_SEEN[key]


_TOOL_ERRORS_SEEN: dict[tuple[str, float, int, int], dict[str, Any] | None] = {}


def _tool_errors(path: Path, since: float) -> dict[str, Any] | None:
    import calendar
    import json
    import time

    with open(path, "rb") as handle:  # OSError remonte : agy_tool_errors ne la garde pas
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - TOOL_ERRORS_WINDOW))
        text = handle.read().decode("utf-8", errors="replace")
    lines = text.split("\n")
    if size > TOOL_ERRORS_WINDOW:
        lines = lines[1:]  # première ligne coupée par la fenêtre
    writing = lines.pop() != ""  # après le dernier retour à la ligne : étape en cours d'écriture
    count, last, ended = 0, None, False
    for index, line in enumerate(reversed(lines)):
        try:
            step = json.loads(line)
        except ValueError:
            continue
        if not isinstance(step, dict):
            continue
        kind, status = step.get("type"), step.get("status")
        if index == 0 and not writing:  # fin certifiée par la DERNIÈRE ligne seule, jamais une plus ancienne (tour 1)
            ended = kind == "PLANNER_RESPONSE" and status == "DONE" and not step.get("tool_calls")
        if kind != "GENERIC":
            continue
        if status != "ERROR":
            break  # outil réussi ou en cours depuis (ordre du fichier) : les erreurs d'avant sont rattrapées
        try:
            at = float(calendar.timegm(time.strptime(str(step.get("created_at") or ""), "%Y-%m-%dT%H:%M:%SZ")))
        except ValueError:
            continue
        if at < since:  # horodatages pas toujours dans l'ordre du fichier (tour 1) : écartée, la lecture continue
            continue
        count += 1
        if last is None:
            last = (
                str(step.get("error") or "").strip().rstrip(":").strip()
                or tr("tool execution error", "erreur d'exécution d'outil"),
                at,
            )
    if not count or last is None:
        return None
    reason = last[0][:300]
    hint = next((tr(en, fr) for marker, en, fr in _TOOL_ERROR_HINTS if marker in reason), None)
    return {"count": count, "reason": reason, "hint": hint, "at": last[1], "ended": bool(ended)}


def agy_turn(conversation: str) -> tuple[bool, float | None]:
    """(tour fini ?, heure de la dernière étape) d'une conversation d'Antigravity, d'après la fin de son journal
    (brain/<id>/.system_generated/logs/transcript.jsonl, relevé le 20/10) : fini quand la dernière étape est une
    réponse du modèle terminée, sans appel d'outil (relecture de Codex : rangée dès « reply », parfois en plein travail)."""
    import calendar
    import json
    import time

    if stopped := agy_error(conversation):  # arrêt sur erreur : tour fini (jamais « figé » 30 min)
        return True, stopped[1]
    path = gemini_home() / "antigravity" / "brain" / conversation / ".system_generated" / "logs" / "transcript.jsonl"
    # Seule la DERNIÈRE ligne compte (relecture du tour 2) : fin sans retour à la ligne = étape en cours d'écriture,
    # jamais l'ancienne fin ressuscitée ; ligne plus longue que la fenêtre lue : fenêtre agrandie (8 Mio au plus).
    window = 65536
    while True:
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - window))
                text = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return False, None
        if not text.endswith("\n"):
            return False, None
        lines = text.splitlines()
        try:
            step = json.loads(lines[-1]) if lines else None
        except ValueError:
            if window >= size or window >= 8 * 1024 * 1024:
                return False, None
            window *= 4
            continue
        if not isinstance(step, dict):
            return False, None
        try:
            at = float(calendar.timegm(time.strptime(str(step.get("created_at") or ""), "%Y-%m-%dT%H:%M:%SZ")))
        except ValueError:
            at = None
        answered = (
            step.get("type") == "PLANNER_RESPONSE" and step.get("status") == "DONE" and not step.get("tool_calls")
        )
        done = answered or step.get("type") == "ERROR_MESSAGE"  # vu le 20/10 : des tours finissent sur une erreur
        return done, at


def is_conversation_id(value: str) -> bool:
    return bool(_CONVERSATION.match(value))


def transcript_path(conversation: str, brain: Path | None = None) -> Path:
    if not is_conversation_id(conversation):
        raise ValueError(
            tr(f"invalid conversation id: {conversation!r}", f"identifiant de conversation invalide : {conversation!r}")
        )
    return (brain or brain_dir()) / conversation / ".system_generated" / "logs" / "transcript.jsonl"


def normalize_agent(name: str) -> str:
    """« claude », « antigravity » (alias : agy, gemini) ou « codex » (alias : chatgpt, openai) ; tout autre nom est refusé."""
    agent = _ALIASES.get(str(name).strip().lower())
    if not agent:
        raise ValueError(
            tr(
                f"unknown agent: {name!r} (claude, antigravity or codex)",
                f"agent inconnu : {name!r} (claude, antigravity ou codex)",
            )
        )
    return agent


def normalize_party(name: str) -> str:
    """Un agent (normalize_agent) ou « user » (boîte de l'ancien panneau de Chrome, retiré le 20/10) : pour la boîte
    aux lettres seulement."""
    return USER if str(name).strip().lower() == USER else normalize_agent(name)
