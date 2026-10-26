"""Crédits et disponibilité des pairs (point I du 20/10 : « et si une IA n'est pas lancée ? et si elle n'a plus de
crédits ? »).

- Codex : lu en PASSIF dans ses journaux (`~/.codex/sessions/…/rollout-*.jsonl`, évènement `token_count` →
  `rate_limits` : fenêtre `primary` de 5 h, `secondary` d'une semaine, `used_percent`, `resets_at` en secondes Unix).
  Le dernier journal se choisit par la date écrite dans son nom. Rien n'est appelé chez Codex.
- Antigravity : sa page « Settings › Models » lue par son port (cdp.read_antigravity_usage), au plus toutes les
  10 min, jamais quand l'utilisateur y est ; dernière lecture gardée dans le salon (state/antigravity-usage.json).

Règle (accord du 19/10, reprise) : un quota INCONNU n'exclut personne ; un pair n'est écarté d'un choix automatique
que si une lecture encore valable le dit épuisé (≥ 95 % utilisés, ≤ 5 % restants). Un `to` explicite n'est jamais
modifié : l'absence est seulement dite.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .box import Box
from .lang import tr

EXHAUSTED_USED_PCT = 95.0
SPENT_USED_PCT = 100.0  # quota vide : seul seuil qui dit qu'un pair NE répondra PAS (attente en cours)
TAIL_BYTES = 262144
AGY_CACHE_S = 600.0
_STAMP = re.compile(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-")


def _newest_rollouts(root: Path, limit: int = 8) -> list[Path]:
    """Journaux les plus récents par naissance ET par écriture : un vieux chat repris peut porter le dernier relevé."""
    born: list[tuple[str, Path]] = []
    written: list[tuple[float, Path]] = []
    for folder in (root / "sessions", root / "archived_sessions"):
        if folder.is_dir():
            for path in folder.rglob("rollout-*.jsonl"):
                match = _STAMP.search(path.name)
                if match:
                    born.append((match.group(1), path))
                try:
                    written.append((path.stat().st_mtime, path))
                except OSError:
                    pass
    picked = [p for _, p in sorted(born, reverse=True)[:limit]] + [p for _, p in sorted(written, reverse=True)[:limit]]
    return list(dict.fromkeys(picked))


def _last_rate_limits(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - TAIL_BYTES))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        payload = entry.get("payload") if isinstance(entry, dict) else None
        limits = payload.get("rate_limits") if isinstance(payload, dict) else None
        if isinstance(limits, dict):
            return {"limits": limits, "observed": entry.get("timestamp")}
    return None


def codex_usage(root: Path | None = None, now: float | None = None) -> dict[str, Any] | None:
    """Dernières limites connues de Codex par fenêtre (% utilisé, remise à zéro, lecture encore valable) ; None :
    aucune lecture."""
    from . import codex_app

    root = root or codex_app.home()
    now = time.time() if now is None else now
    # le relevé le plus récemment OBSERVÉ (relecture de Codex du 20/10 : le premier trouvé pouvait dater de 8 h)
    readings = [r for r in (_last_rate_limits(p) for p in _newest_rollouts(root)) if r]
    for found in sorted(readings, key=lambda r: str(r.get("observed") or ""), reverse=True)[:1]:
        windows = {}
        for name in ("primary", "secondary"):
            window = found["limits"].get(name)
            if not isinstance(window, dict):
                continue
            resets = window.get("resets_at")
            current = isinstance(resets, int | float) and resets > now
            windows[name] = {
                "minutes": window.get("window_minutes"),
                "used_pct": window.get("used_percent") if current else 0.0,
                "resets_at": resets,
                "current": current,
            }
        return {
            "source": "journal Codex (lecture passive)",
            "observed": found["observed"],
            "windows": windows,
            "credits": found["limits"].get("credits"),
        }
    return None


def _epoch(stamp: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return 0.0


def antigravity_usage(box: Box, read: Any = None, now: float | None = None) -> dict[str, Any] | None:
    """Dernière lecture des quotas d'Antigravity (relue si elle a plus de 10 min et que la lecture est possible)."""
    now = time.time() if now is None else now
    cached = box.state("antigravity-usage")
    if cached and now - _epoch(str(cached.get("read_at") or "")) < AGY_CACHE_S:
        return {**cached, "cached": True}
    if read is None:
        from .cdp import read_antigravity_usage as read
    fresh = read()
    if fresh.get("status") == "success":
        box.set_state("antigravity-usage", fresh)
        return fresh
    return {**(cached or {}), "last_try": fresh, "cached": bool(cached)} if cached else fresh


def exhausted(
    agent: str, usage: dict[str, Any] | None, now: float | None = None, used_pct: float = EXHAUSTED_USED_PCT
) -> str | None:
    """Raison d'écarter ce pair d'un choix AUTOMATIQUE (None : rien ne le prouve épuisé) ; `used_pct` : seuil, 95 %
    pour choisir un pair, SPENT_USED_PCT pour abandonner une attente."""
    if not usage:
        return None
    now = time.time() if now is None else now
    if agent == "codex":
        for name, window in (usage.get("windows") or {}).items():
            used = window.get("used_pct")
            if window.get("current") and isinstance(used, int | float) and used >= used_pct:
                label = "5 h" if name == "primary" else tr("week", "semaine")
                resets = window.get("resets_at")
                return tr(
                    f"Codex: {used:.0f} % used ({label}), reset at {resets}",
                    f"Codex : {used:.0f} % utilisés ({label}), remise à zéro à {resets}",
                )
        return None
    if agent == "antigravity":
        if usage.get("status") != "success" or now - _epoch(str(usage.get("read_at") or "")) > 6 * 3600:
            return None  # lecture absente ou trop vieille : inconnu
        for limit in usage.get("limits") or []:
            remaining = limit.get("remaining_pct")
            family = str(limit.get("family") or "")
            if "Gemini" in family and isinstance(remaining, int | float) and remaining <= 100 - used_pct:
                cap = limit.get("limit")
                return tr(
                    f"Antigravity: {remaining:.0f} % left ({family}, {cap})",
                    f"Antigravity : {remaining:.0f} % restants ({family}, {cap})",
                )
        return None
    return None


def availability(box: Box, agent: str, used_pct: float = EXHAUSTED_USED_PCT) -> str | None:
    """Raison CONNUE pour laquelle `agent` ne peut pas répondre maintenant (None : disponible ou inconnu) ; `used_pct` :
    seuil du quota (voir `exhausted`)."""
    if agent == "codex":
        return exhausted("codex", codex_usage(), used_pct=used_pct)
    if agent == "antigravity":
        from . import cdp, wake

        if not wake.sidecar_alive(box) and not cdp.agy_page_url() and cdp.agy_exe() is None:
            return tr("Antigravity closed and not found on this PC", "Antigravity fermé et introuvable sur ce PC")
        return exhausted("antigravity", box.state("antigravity-usage"), used_pct=used_pct)
    return None
