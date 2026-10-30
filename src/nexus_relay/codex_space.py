"""Explicit Codex execution space and verified conversation ownership. No app state is edited here."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .box import Box, now_iso
from .paths import relay_home


def configured() -> dict[str, Any]:
    workspace = os.environ.get("NEXUS_RELAY_CODEX_WORKSPACE", "").strip()
    label = os.environ.get("NEXUS_RELAY_CODEX_PROJECT", "").strip()
    project_id = os.environ.get("NEXUS_RELAY_CODEX_PROJECT_ID", "").strip()
    explicit = bool(workspace or label or project_id)
    if explicit and not (workspace and label and project_id):
        raise ValueError("Codex execution space requires workspace path, project label and official project ID")
    return {
        "path": str(Path(workspace)) if workspace else str(relay_home() / "codex-projet"),
        "label": label if explicit else "Nexus Relay",
        "project_id": project_id or None,
        "explicit": explicit,
        "ownership": "verified Relay conversation IDs only",
    }


def labels() -> tuple[str, str]:
    label = configured()["label"]
    return f"Démarrer un nouveau chat dans {label}", f"Start new chat in {label}"


def protected_ids() -> set[str]:
    return {value for key in ("CODEX_SESSION_ID", "CODEX_THREAD_ID") if (value := os.environ.get(key, "").strip())}


def registered(conversation: str, box: Box | None = None) -> bool:
    item = ((box or Box()).state("codex-owned") or {}).get("threads", {}).get(conversation)
    return isinstance(item, dict) and item.get("verified") is True and bool(item.get("workspace"))


def owns(conversation: str, box: Box | None = None) -> bool:
    from . import codex_app

    if conversation in protected_ids() or not registered(conversation, box):
        return False
    row = (codex_app._db_threads(codex_app.home()) or {}).get(conversation)
    if not row or row.get("child"):
        return False
    record = ((box or Box()).state("codex-owned") or {})["threads"][conversation]
    return codex_app._norm(row["cwd"]) == codex_app._norm(record["workspace"])


def remember(topic: str, conversation: str, box: Box | None = None) -> bool:
    """Called only after verified delivery; the app DB must confirm workspace and top-level identity."""
    from . import codex_app

    box = box or Box()
    row = (codex_app._db_threads(codex_app.home()) or {}).get(conversation)
    if not row or row.get("child") or conversation in protected_ids():
        return False
    if codex_app._norm(row["cwd"]) != codex_app._norm(configured()["path"]):
        return False
    registry = box.state("codex-owned") or {"threads": {}}
    registry.setdefault("threads", {})[conversation] = {
        "verified": True,
        "topic": topic,
        "workspace": row["cwd"],
        "verified_at": now_iso(),
        "origin": "verified-relay-wake",
    }
    box.set_state("codex-owned", registry)
    return True


def accepts_created(created: str) -> bool:
    cutoff = os.environ.get("NEXUS_RELAY_WAKE_SINCE", "").strip()
    if not cutoff:
        return True
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00")) >= datetime.fromisoformat(
            cutoff.replace("Z", "+00:00")
        )
    except ValueError:
        return False
