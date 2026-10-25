"""Trace des échanges du relais : pour chaque demande de Claude à un pair, durées (dépôt → réveil → lu → en cours →
réponse) et jetons consommés par le pair pour la traiter, lus dans ses propres journaux. Lecture seule.

- Codex : le journal (rollout-*.jsonl, ~/.codex/sessions et archived_sessions) dont un message cite l'id de la demande ;
  jetons = différence des compteurs cumulés (token_count) entre le premier message citant l'id et la réponse.
- Antigravity : la conversation de la livraison (claims), étapes du modèle de son transcript.jsonl entre la livraison
  et la réponse (input_tokens, cache_read_tokens, output_tokens).

Lancer avec le Python du relais (~/.nexus/nexus-relay/venv) :
  python -B bench/trace.py [--since 2026-10-21T12:00] [--topic relais] [--to codex|antigravity] [--json]
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nexus_relay.box import Box, Message  # noqa: E402
from nexus_relay.codex_app import home as codex_home  # noqa: E402
from nexus_relay.paths import brain_dir  # noqa: E402


def epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    text = str(iso).replace("Z", "").split(".")[0]
    try:
        return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def codex_rollouts() -> list[Path]:
    home = codex_home()  # CODEX_HOME respecté, comme le relais
    found = [*home.glob("sessions/*/*/*/rollout-*.jsonl"), *home.glob("archived_sessions/rollout-*.jsonl")]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def codex_usage(message_id: str, until: float | None, rollouts: list[Path]) -> dict[str, Any] | None:
    """Jetons de Codex pour la demande : compteurs cumulés du journal qui la cite, du premier appel citant l'id à la
    réponse (ou à la fin du journal)."""
    for path in rollouts:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if message_id not in text:
            continue
        start = end = None
        seen = False
        for line in text.splitlines():
            if not seen and message_id in line:
                seen = True
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            payload = entry.get("payload") or {}
            if payload.get("type") != "token_count" or not payload.get("info"):
                continue
            stamp = epoch(entry.get("timestamp"))
            total = payload["info"].get("total_token_usage") or {}
            if not seen:
                start = total  # dernier compteur AVANT la demande : base de la différence
                continue
            if until is not None and stamp is not None and stamp > until + 5:
                break
            end = total
        if end is None:
            return {"journal": path.name, "jetons": None}
        base = start or {}
        keys = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
        return {"journal": path.name, **{k: int(end.get(k, 0)) - int(base.get(k, 0)) for k in keys}}
    return None


def agy_usage(conversation: str, since: float, until: float | None) -> dict[str, Any] | None:
    path = brain_dir() / conversation / ".system_generated" / "logs" / "transcript.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    sums = {"input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0, "étapes": 0}
    for line in lines:
        try:
            step = json.loads(line)
        except ValueError:
            continue
        at = epoch(step.get("created_at"))
        if at is None or at < since - 2 or (until is not None and at > until + 5):
            continue
        if step.get("source") == "MODEL":
            sums["étapes"] += 1
            for key in ("input_tokens", "cache_read_tokens", "output_tokens"):
                sums[key] += int(step.get(key) or 0)
    return {"conversation": conversation, **sums}


def trace(box: Box, message: Message, rollouts: list[Path]) -> dict[str, Any]:
    created = epoch(message.created) or 0.0
    events = box.events(message.id)

    def first(kind: str) -> float | None:
        return next((epoch(e.get("at")) for e in events if e.get("kind") == kind), None)

    replies = sorted((r for r in box.replies(message.id) if r.sender == message.to), key=lambda r: r.created)
    answered = epoch(replies[0].created) if replies else None
    read = epoch(box.read_at(message))
    woken = first("réveil") or first("livré")
    working = first("en cours")

    def after(at: float | None) -> float | None:
        return round(at - created, 1) if at is not None else None

    row: dict[str, Any] = {
        "id": message.id,
        "à": message.to,
        "sujet": message.topic,
        "déposé": message.created,
        "réveil_s": after(woken),
        "lu_s": after(read),
        "en_cours_s": after(working),
        "réponse_s": after(answered),
        "issue": (replies[0].extra.get("state") or "terminé") if replies else None,
        "texte_car": len(message.text),
    }
    if message.to == "codex":
        row["jetons"] = codex_usage(message.id, answered, rollouts)
    elif message.to == "antigravity":
        conversation = next(
            (c.get("conversation") for _, c in box.claims(message.id) if c.get("conversation")),
            next((e.get("conversation") for e in events if e.get("conversation")), None),
        )
        row["jetons"] = agy_usage(conversation, created, answered) if conversation else None
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0] if __doc__ else None)
    parser.add_argument("--since", help="ISO, ex. 2026-10-21T12:00")
    parser.add_argument("--topic")
    parser.add_argument("--to", choices=("codex", "antigravity"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    box = Box()
    since = epoch(args.since + (":00" if args.since and args.since.count(":") == 1 else "")) if args.since else 0.0
    asks = [
        m
        for m in box.all()
        if m.sender == "claude"
        and m.to in ("codex", "antigravity")
        and not m.info
        and m.topic != "selftest"
        and (epoch(m.created) or 0) >= (since or 0)
        and (not args.topic or m.topic == args.topic)
        and (not args.to or m.to == args.to)
    ]
    rollouts = codex_rollouts()
    rows = [trace(box, m, rollouts) for m in sorted(asks, key=lambda m: m.created)]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    print("| déposé | à | sujet | réveil s | lu s | en cours s | réponse s | issue | entrée | dont cache | sortie |")
    print("|---|---|---|---:|---:|---:|---:|---|---:|---:|---:|")
    for r in rows:
        t = r.get("jetons") or {}
        cached = t.get("cached_input_tokens", t.get("cache_read_tokens"))
        print(
            f"| {r['déposé'][11:19]} | {r['à']} | {r['sujet']} | {r['réveil_s']} | {r['lu_s']} | {r['en_cours_s']} | "
            f"{r['réponse_s']} | {r['issue'] or '—'} | {t.get('input_tokens', '—')} | {cached if cached is not None else '—'} | "
            f"{t.get('output_tokens', '—')} |"
        )
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    raise SystemExit(main())
