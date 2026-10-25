"""Réglages du poste : `~\\.nexus\\nexus-relay\\settings.json` (propre à ce PC), puis `settings.local.json` à la racine du
dépôt installé (partagé par les PC d'une installation synchronisée), clé par clé, le premier qui la donne l'emporte.

Les valeurs acceptent les variables d'environnement (`%OneDrive%`, `%LOCALAPPDATA%`…) ; une valeur dont une variable
manque sur ce PC est ignorée. Une clé peut être une liste : `token_file` prend le premier fichier qui existe, `vaults`
et `codex_router_start` gardent toutes les valeurs.

    token_file          fichier du jeton du démon                       → NEXUS_MCP_TOKEN_FILE
    vaults              dossiers de secrets jamais joints à un message  → NEXUS_RELAY_VAULTS (séparés par os.pathsep)
    codex_router        adresse de santé du routeur devant Codex        → NEXUS_RELAY_CODEX_ROUTER
    codex_router_start  commandes qui le lancent, dans l'ordre          → NEXUS_RELAY_CODEX_ROUTER_START (séparées par ;)

`apply()` (démarrage du serveur) pose ces variables quand l'environnement ne les donne pas déjà ; `python -m
nexus_relay.settings` rend les réglages effectifs en JSON (lu par install.ps1).
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

KEYS = ("token_file", "vaults", "codex_router", "codex_router_start")
ENV = {
    "token_file": "NEXUS_MCP_TOKEN_FILE",
    "vaults": "NEXUS_RELAY_VAULTS",
    "codex_router": "NEXUS_RELAY_CODEX_ROUTER",
    "codex_router_start": "NEXUS_RELAY_CODEX_ROUTER_START",
}
_UNEXPANDED = re.compile(r"%[^%\s]+%|\$\{?\w+")


def files() -> list[Path]:
    return [
        Path.home() / ".nexus" / "nexus-relay" / "settings.json",
        Path(__file__).resolve().parents[2] / "settings.local.json",
    ]


def _expand(value: object) -> str | None:
    text = os.path.expandvars(str(value)).strip()
    return None if not text or _UNEXPANDED.search(text) else text


def _values(raw: object) -> list[str]:
    items = raw if isinstance(raw, list) else [raw]
    return [text for item in items if item is not None and (text := _expand(item))]


def load(paths: list[Path] | None = None) -> dict[str, Any]:
    """Réglages effectifs, valeurs développées (clés absentes : non réglées)."""
    found: dict[str, Any] = {}
    for path in paths if paths is not None else files():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        for key in KEYS:
            if key in found or key not in data:
                continue
            values = _values(data[key])
            if key == "token_file":
                existing = [v for v in values if Path(v).is_file()]
                if existing:
                    found[key] = existing[0]
            elif key == "codex_router":
                if values:
                    found[key] = values[0]
            elif values:
                found[key] = list(dict.fromkeys(values))
    return found


def apply(environ: MutableMapping[str, str] | None = None, paths: list[Path] | None = None) -> dict[str, str]:
    """Pose les variables des réglages absentes de l'environnement ; rend celles posées."""
    environ = os.environ if environ is None else environ
    applied = {}
    for key, value in load(paths).items():
        name = ENV[key]
        if environ.get(name):
            continue
        sep = os.pathsep if key == "vaults" else ";"
        environ[name] = sep.join(value) if isinstance(value, list) else value
        applied[name] = environ[name]
    return applied


if __name__ == "__main__":
    json.dump(load(), sys.stdout, ensure_ascii=False)
