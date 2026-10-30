"""Hook PreToolUse commun à Claude Code, Codex et Antigravity : avant qu'un agent modifie un fichier, refus si un AUTRE
agent l'a réservé ; sinon le fichier lui est réservé (AUTO_REASON, AUTO_TTL_MIN, prolongé à chaque modification),
libéré par le hook post-commit une fois commité. Les hooks de fin d'outil et de session libèrent aussi les tentatives
sans changement, après preuve que les fichiers sont propres ; une autre session et les réservations manuelles restent.
Le 27/10, la copie de travail d'Antigravity, jamais réservée, s'était
glissée dans un commit de Claude : la réservation ne dépend plus de la mémoire de l'agent. La garde de commit
(reservations --staged) reste le second rideau.

Usage : python -m nexus_relay.edit_guard <claude|codex|antigravity>, l'événement du hook sur l'entrée standard.
Relais illisible ou en panne : la modification passe (jamais bloquer le travail sur une erreur du relais).
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

from . import reservations
from .box import Box, RelayError
from .lang import tr
from .paths import normalize_agent

AUTO_TTL_MIN = 60
# Outils d'écriture de fichiers. Claude Code : Edit, Write, MultiEdit, NotebookEdit (file_path / notebook_path).
# Codex : apply_patch (le patch dans tool_input.command). Antigravity : edit_file et write_file (args.Path, vus dans
# son journal le 27/10), write_to_file, replace_file_content, multi_replace_file_content (args.TargetFile, sa doc).
AGY_PATH_ARGS = ("Path", "TargetFile")
PATCH_LINE = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*$", re.MULTILINE)


def _absolute(path: str, base: str | None) -> str:
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(base or os.getcwd(), path))


def targets(agent: str, event: dict[str, Any]) -> list[str]:
    """Fichiers que l'appel d'outil va écrire, chemins absolus ; [] : pas une écriture de fichier."""
    if agent == "antigravity":
        args = (event.get("toolCall") or {}).get("args") or {}
        path = next((args[k] for k in AGY_PATH_ARGS if args.get(k)), None)
        workspaces = event.get("workspacePaths") or [None]
        return [_absolute(str(path), workspaces[0])] if path else []
    data = event.get("tool_input") or {}
    cwd = event.get("cwd")
    if event.get("tool_name") == "apply_patch":
        return [_absolute(p, cwd) for p in PATCH_LINE.findall(str(data.get("command") or ""))]
    path = data.get("file_path") or data.get("notebook_path")
    return [_absolute(str(path), cwd)] if path else []


def session(event: dict[str, Any]) -> str | None:
    """Conversation de l'agent : session_id (Claude Code, Codex), conversationId (Antigravity)."""
    value = event.get("session_id") or event.get("conversationId")
    return str(value) if value else None


def invocation(agent: str, event: dict[str, Any]) -> str | None:
    """Stable id shared by the before/after event; no guessed id from file names or timestamps."""
    if agent == "antigravity":
        value = event.get("stepIdx")
        return f"step:{value}" if isinstance(value, int) and not isinstance(value, bool) else None
    value = event.get("tool_use_id") or event.get("tool_call_id")
    return str(value) if value else None


def decide(
    agent: str,
    files: list[str],
    box: Box | None = None,
    conversation: str | None = None,
    tool: str | None = None,
) -> str | None:
    """Raison du refus, ou None : fichiers libres, désormais réservés à `agent` et à sa conversation (une autre
    conversation du même agent est refusée comme un autre agent)."""
    if not files:
        return None
    box = box or Box()
    if hits := reservations.blocking(agent, files, box, conversation):
        lines = "; ".join(
            tr(
                f"{h['file']} reserved by {h['agent']} ({h['reason']}) until {h['expires']}",
                f"{h['file']} réservé par {h['agent']} ({h['reason']}) jusqu'à {h['expires']}",
            )
            for h in hits[:5]
        )
        if hits[0]["agent"].startswith(agent + " "):  # une autre conversation du même agent : le relais ne la joint pas
            return tr(
                f"Nexus Relay: {lines}. Do not edit it: another conversation of {agent} is working on it; wait for its "
                "commit (automatic release) or the expiry",
                f"Nexus Relay : {lines}. Ne pas le modifier : une autre conversation de {agent} y travaille ; attendre "
                "son commit (libération automatique) ou l'expiration",
            )
        return tr(
            f"Nexus Relay: {lines}. Do not edit it: ask {hits[0]['agent']} through the relay to release it, or wait",
            f"Nexus Relay : {lines}. Ne pas le modifier : demander à {hits[0]['agent']} par le relais de le libérer, "
            "ou attendre",  # sans point final : Codex en ajoute un (« attendre.. » vu le 27/10)
        )
    try:
        reservations.reserve(
            box,
            agent,
            files[: reservations.MAX_PATHS],
            reservations.AUTO_REASON,
            AUTO_TTL_MIN,
            session=conversation,
            automatic=True,
            tool=tool,
        )
    except RelayError as error:  # réservé entre-temps par un autre agent
        return str(error)
    return None


def answer(agent: str, reason: str) -> dict[str, Any]:
    """Refus dans le format de l'agent (Antigravity : decision ; Claude Code et Codex : hookSpecificOutput)."""
    if agent == "antigravity":
        return {"decision": "deny", "reason": reason}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        agent = normalize_agent(args[0]) if args else reservations.whoami()
        event = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        if "--finish-tool" in args or "--finish-session" in args:
            reservations.finish_edits(
                agent,
                session(event),
                tool=invocation(agent, event),
                session_ended="--finish-session" in args,
            )
            sys.stdout.write("{}")  # neutral JSON object accepted by all three hook schemas
            return 0
        reason = decide(agent, targets(agent, event), conversation=session(event), tool=invocation(agent, event))
    except Exception as error:  # noqa: BLE001 - un hook ne bloque jamais le travail sur une erreur du relais
        print(f"Nexus Relay edit_guard: {error}", file=sys.stderr)
        return 0
    if reason:
        sys.stdout.write(json.dumps(answer(agent, reason)))  # ASCII échappé : sûr quel que soit l'encodage du tube
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
