"""Réservation de fichiers avant de les modifier (24/10 : manque relevé face à MCP Agent Mail et aux équipes de Claude
Code ; utile dès que Codex ou Antigravity écrivent, pas seulement relisent).

Une réservation couvre des chemins ABSOLUS : fichier, dossier (tout ce qu'il contient) ou motif (`*`, `?`), comparés
sans casse ni séparateur propre à Windows. Exclusive (défaut) : personne d'autre ne réserve ce qui la recouvre ;
partagée : seules les exclusives des autres la bloquent. Elle expire seule (ttl) ; son agent la prolonge en la
redemandant, ou la libère. Un fichier par réservation, réécrit d'un coup par le démon (seul écrivain) ; libérée ou
expirée, elle reste sur disque, marquée (jamais supprimée).

    <racine>/reservations/<id>.json   {id, agent, paths, reason, exclusive, created, expires, released?}

Garde de commit : `python -m nexus_relay.reservations [--agent <moi>] [--staged] <fichiers…>` rend 1 si un fichier est
réservé par un AUTRE agent ; sans --agent, l'agent est reconnu par ses processus parents. Branchée dans les dépôts par
scripts/install_commit_guard.ps1 (hook scripts/pre-commit).
"""

from __future__ import annotations

import argparse
import fnmatch
import ntpath
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from typing import Any

from .box import Box, RelayError, read_json, write_state
from .lang import tr
from .paths import normalize_party

MAX_PATHS = 32
# contrôle puis écriture d'un seul tenant : le démon (seul écrivain) sert plusieurs appels à la fois
_LOCK = threading.Lock()
TTL_MIN, TTL_MAX = 1, 24 * 60


def now() -> float:
    return time.time()


def _iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def _split(path: str) -> tuple[str, list[str], bool]:
    """(lecteur, noms, forme longue à garder). Forme longue « \\\\?\\ » (marqueur UNC sans casse, tour 2 de Codex)
    ramenée à l'ordinaire ; Windows retire points et espaces finaux de chaque nom à l'ouverture ordinaire (« a.py. »
    ouvre « a.py ») ; en forme longue ce sont de vrais noms : le préfixe est alors gardé, pour qu'un chemin rangé se
    relise à l'identique (tour 3 de Codex : « \\\\?\\C:\\repo\\a. » devenait « a » à la comparaison suivante)."""
    path = path.lstrip().replace("/", "\\")
    extended = path.startswith("\\\\?\\")
    if not extended:
        path = path.rstrip()  # forme longue : l'espace final est un vrai caractère du nom (tour 4 de Codex)
    if path[:8].upper() == "\\\\?\\UNC\\":
        path = "\\\\" + path[8:]
    elif extended:
        path = path[4:]
    if not ntpath.isabs(path):
        raise RelayError(
            tr(f"reservation: absolute path expected ({path})", f"réservation : chemin absolu attendu ({path})")
        )
    drive, rest = ntpath.splitdrive(ntpath.normpath(path))
    parts = [p for p in rest.split("\\") if p]
    if not extended:
        parts = [p.rstrip(". ") or p for p in parts]
    return drive, parts, extended and any(p != p.rstrip(". ") for p in parts)


def clean(path: str) -> str:
    """Chemin rangé tel qu'écrit (casse gardée) : absolu, séparateurs de Windows, sans séparateur final sauf à la
    racine (« C:\\ ») ; forme longue seulement quand un nom finit par un point ou une espace."""
    drive, parts, keep = _split(path)
    body = drive + "\\" + "\\".join(parts)
    if not keep:
        return body
    return "\\\\?\\UNC\\" + body[2:] if body.startswith("\\\\") else "\\\\?\\" + body


def norm(path: str) -> str:
    """Chemin comparable : comme clean, sans casse."""
    return ntpath.normcase(clean(path))


def _segments(path: str) -> list[str]:
    drive, parts, _ = _split(path)
    return [ntpath.normcase(drive), *(ntpath.normcase(p) for p in parts)]


def _wild(segment: str) -> bool:
    return any(c in segment for c in "*?[")


def _meet(a: str, b: str) -> bool:
    """Deux segments (noms ou motifs) peuvent désigner le même nom. Motif contre motif : leurs débuts et fins fixes
    s'accordent (a*.py et *b.py : oui, ab.py ; a*.py et b*.md : non) — prudent, jamais un faux « libre »."""
    if not _wild(a) and not _wild(b):
        return a == b
    if not _wild(a) or not _wild(b):
        return fnmatch.fnmatchcase(a, b) if _wild(b) else fnmatch.fnmatchcase(b, a)
    head_a, head_b = re.split(r"[*?\[]", a, maxsplit=1)[0], re.split(r"[*?\[]", b, maxsplit=1)[0]
    tail_a, tail_b = re.split(r".*[*?\]]", a, maxsplit=1)[-1], re.split(r".*[*?\]]", b, maxsplit=1)[-1]
    return (head_a.startswith(head_b) or head_b.startswith(head_a)) and (
        tail_a.endswith(tail_b) or tail_b.endswith(tail_a)
    )


def overlap(a: str, b: str) -> bool:
    """Deux réservations peuvent couvrir un même fichier (relecture de Codex : l'intersection des ensembles, pas la
    comparaison des chaînes). Segment par segment ; `*` ne traverse pas un dossier, `**` couvre tout ce qui suit ; le
    plus court qui s'accorde jusqu'au bout est un dossier qui contient l'autre."""
    sa, sb = _segments(a), _segments(b)
    for x, y in zip(sa, sb, strict=False):
        if x == "**" or y == "**":
            return True
        if not _meet(x, y):
            return False
    return True


def _folder(box: Box) -> Any:
    return box.root / "reservations"


def active(box: Box, at: float | None = None) -> list[dict[str, Any]]:
    at = now() if at is None else at
    folder = _folder(box)
    found = [r for p in sorted(folder.glob("*.json")) if (r := read_json(p))] if folder.is_dir() else []
    return [r for r in found if not r.get("released") and float(r.get("expires_at") or 0) > at]


def _conflicts(box: Box, agent: str, paths: list[str], exclusive: bool) -> list[tuple[dict[str, Any], str, str]]:
    found = []
    for held in active(box):
        if held["agent"] == agent or not (exclusive or held.get("exclusive", True)):
            continue
        for mine in paths:
            for theirs in held["paths"]:
                if overlap(mine, theirs):
                    found.append((held, mine, theirs))
    return found


def reserve(
    box: Box, agent: str, paths: list[str], reason: str, ttl_min: int = 60, exclusive: bool = True
) -> dict[str, Any]:
    agent = normalize_party(agent)
    if not paths or len(paths) > MAX_PATHS:
        raise RelayError(tr(f"reservation: 1 to {MAX_PATHS} paths", f"réservation : de 1 à {MAX_PATHS} chemins"))
    if not reason.strip():
        raise RelayError(tr("reservation: say why (reason)", "réservation : dis pourquoi (reason)"))
    if not TTL_MIN <= int(ttl_min) <= TTL_MAX:
        raise RelayError(
            tr(f"reservation: ttl_min from {TTL_MIN} to {TTL_MAX}", f"réservation : ttl_min de {TTL_MIN} à {TTL_MAX}")
        )
    wanted = sorted({norm(p): clean(p) for p in paths}.values(), key=norm)
    with _LOCK:
        return _reserve(box, agent, wanted, reason, ttl_min, exclusive)


def _reserve(box: Box, agent: str, wanted: list[str], reason: str, ttl_min: int, exclusive: bool) -> dict[str, Any]:
    if clash := _conflicts(box, agent, wanted, exclusive):
        lines = [
            tr(
                f"{mine} overlaps {theirs}, reserved by {held['agent']} ({held['reason']}) until {held['expires']}",
                f"{mine} recouvre {theirs}, réservé par {held['agent']} ({held['reason']}) jusqu'à {held['expires']}",
            )
            for held, mine, theirs in clash[:5]
        ]
        raise RelayError(tr("reservation refused: ", "réservation refusée : ") + tr("; ", " ; ").join(lines))
    at = now()
    # même agent, mêmes chemins : la réservation existante est prolongée, pas doublée
    key = [norm(p) for p in wanted]
    same = next(
        (r for r in active(box, at) if r["agent"] == agent and sorted(norm(p) for p in r["paths"]) == key), None
    )
    record = {
        **(same or {"id": f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime(at))}-{secrets.token_hex(3)}"}),
        "agent": agent,
        "paths": wanted,
        "reason": reason.strip()[:300],
        "exclusive": bool(exclusive),
        "created": (same or {}).get("created") or _iso(at),
        "expires": _iso(at + int(ttl_min) * 60),
        "expires_at": at + int(ttl_min) * 60,
    }
    write_state(_folder(box) / f"{record['id']}.json", record)
    return record


def release(box: Box, agent: str, reservation: str | None = None) -> list[str]:
    """Libère une réservation de `agent` (par son id), ou toutes les siennes."""
    agent = normalize_party(agent)
    with _LOCK:
        return _release(box, agent, reservation)


def _release(box: Box, agent: str, reservation: str | None) -> list[str]:
    held = active(box)
    if reservation:
        target = next((r for r in held if r["id"] == reservation), None)
        if target is None:
            raise RelayError(
                tr(
                    f"reservation {reservation} not found or already ended",
                    f"réservation {reservation} introuvable ou déjà finie",
                )
            )
        if target["agent"] != agent:
            raise RelayError(
                tr(
                    f"reservation {reservation}: not yours ({target['agent']})",
                    f"réservation {reservation} : pas à toi ({target['agent']})",
                )
            )
        held = [target]
    done = []
    for record in (r for r in held if r["agent"] == agent):
        write_state(_folder(box) / f"{record['id']}.json", {**record, "released": _iso(now())})
        done.append(record["id"])
    return done


def blocking(agent: str, files: list[str], box: Box | None = None) -> list[dict[str, Any]]:
    """Réservations EXCLUSIVES d'autres agents qui couvrent ces fichiers (garde de commit)."""
    box = box or Box()
    agent = normalize_party(agent)
    return [
        {"agent": r["agent"], "reason": r["reason"], "expires": r["expires"], "file": f}
        for r in active(box)
        if r["agent"] != agent and r.get("exclusive", True)
        for f in files
        if any(overlap(theirs, os.path.abspath(f)) for theirs in r["paths"])
    ]


# Exécutables dont descend une commande → agent qui la lance. Git ne le dit pas : les trois commitent sous l'identité
# de Julien. Claude.exe (appli et claude-code), Codex.exe et ses services, Antigravity.exe.
_ANCESTORS = (("claude", "claude"), ("codex", "codex"), ("antigravity", "antigravity"))


def _processes() -> dict[int, tuple[int, str]]:
    """pid → (pid parent, exécutable en minuscules), d'après un instantané Toolhelp de Windows ; vide ailleurs."""
    if sys.platform != "win32":
        return {}
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return {}
    table: dict[int, tuple[int, str]] = {}
    entry = Entry(dwSize=ctypes.sizeof(Entry))
    try:
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return table


def whoami(table: dict[int, tuple[int, str]] | None = None, pid: int | None = None) -> str:
    """Agent qui lance la commande : NEXUS_RELAY_AGENT, sinon le plus proche ancêtre reconnu ; « user » (Julien à la
    main) quand aucun ne l'est."""
    if named := os.environ.get("NEXUS_RELAY_AGENT", "").strip():
        return normalize_party(named)
    table = _processes() if table is None else table
    pid, seen = os.getpid() if pid is None else pid, set()
    while pid in table and pid not in seen:  # pid déjà vu : cycle d'un pid réutilisé, on s'arrête
        seen.add(pid)
        pid, exe = table[pid]
        for prefix, agent in _ANCESTORS:
            if exe.startswith(prefix):
                return agent
    return normalize_party("user")


def staged() -> list[str]:
    """Fichiers indexés du dépôt courant, chemins absolus ; un renommage compte ses deux noms (--no-renames)."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout

    top = git("rev-parse", "--show-toplevel").strip()
    return [
        os.path.join(top, name)
        for name in git("diff", "--cached", "--name-only", "--no-renames", "-z").split("\0")
        if name
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Garde de commit Nexus Relay : fichiers réservés par un autre agent")
    parser.add_argument(
        "--agent", help="toi : claude, codex ou antigravity (par défaut : reconnu par les processus parents)"
    )
    parser.add_argument("--staged", action="store_true", help="fichiers indexés du dépôt courant (hook pre-commit)")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    agent = normalize_party(args.agent) if args.agent else whoami()
    hits = blocking(agent, [*args.files, *(staged() if args.staged else [])])
    for hit in hits:
        line = tr(
            f"{hit['file']}: reserved by {hit['agent']} ({hit['reason']}) until {hit['expires']}",
            f"{hit['file']} : réservé par {hit['agent']} ({hit['reason']}) jusqu'à {hit['expires']}",
        )
        print(line, file=sys.stderr)
    if hits:
        print(
            tr(
                f"Nexus Relay: commit refused for {agent}; wait for the release or ask the holder through the relay.",
                f"Nexus Relay : commit refusé pour {agent} ; attendre la libération ou demander au détenteur par le "
                "relais.",
            ),
            file=sys.stderr,
        )
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
