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
import contextlib
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
from .paths import normalize_party, relay_host

MAX_PATHS = 32
# contrôle puis écriture d'un seul tenant : le démon sert plusieurs appels à la fois (_LOCK), et les hooks d'édition
# (edit_guard, processus à part) réservent aussi (_between_processes)
_LOCK = threading.Lock()
TTL_MIN, TTL_MAX = 1, 24 * 60
# motif historique ; les nouveaux hooks posent aussi le marqueur automatic et suivent les appels en vol
AUTO_REASON = "modification en cours (réservation automatique)"


def now() -> float:
    return time.time()


def _iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


_BLANKS = " \t\r\n\f\v"  # blancs ASCII


def _split(path: str) -> tuple[str, list[str], bool]:
    """(lecteur, noms, forme longue à garder). Forme longue « \\\\?\\ » (marqueur UNC sans casse, tour 2 de Codex)
    ramenée à l'ordinaire ; Windows retire points et espaces finaux de chaque nom à l'ouverture ordinaire (« a.py. »
    ouvre « a.py ») ; en forme longue ce sont de vrais noms : le préfixe est alors gardé, pour qu'un chemin rangé se
    relise à l'identique (tour 3 de Codex : « \\\\?\\C:\\repo\\a. » devenait « a » à la comparaison suivante)."""
    # blancs ASCII seulement : Windows garde les espaces Unicode (insécable…) d'un nom (trouvé par le fuzzing)
    path = path.lstrip(_BLANKS).replace("/", "\\")
    extended = path.startswith("\\\\?\\")
    if not extended:
        path = path.rstrip(_BLANKS)  # forme longue : l'espace final est un vrai caractère du nom (tour 4 de Codex)
    if path[:8].upper() in ("\\\\?\\UNC\\", "\\\\.\\UNC\\"):
        path = "\\\\" + path[8:]
    elif extended or path.startswith("\\\\.\\"):
        # « \\.\C:\a » est le même fichier que « C:\a » (chemin de périphérique DOS, relecture de Codex)
        path = path[4:]
    # Refusés, car aucun fichier n'est désigné et le chemin rangé ne se relisait pas à l'identique (trouvé par le
    # fuzzing) : caractère de contrôle (interdit dans un nom Windows), préfixe restant après le premier
    # (« \\?\\\?\ », « \\.\\\.\ »), partage réseau incomplet ou à nom vide (« \\?\UNC\ », « \\serveur », « \\a\. »),
    # nom fait seulement de points ou d'espaces hors forme longue (Windows le réduit à rien), chemin sans vraie lettre
    # de lecteur ni partage (« \:\ », «  :\ », que ntpath dit absolus).
    drive, rest = ntpath.splitdrive(ntpath.normpath(path))
    lettered = len(drive) == 2 and drive[0].isascii() and drive[0].isalpha() or drive.startswith("\\\\")
    if any(c < " " for c in path) or path[:4] in ("\\\\?\\", "\\\\.\\") or not ntpath.isabs(path) or not lettered:
        raise RelayError(
            tr(f"reservation: absolute path expected ({path})", f"réservation : chemin absolu attendu ({path})")
        )
    if drive.startswith("\\\\") and not _share(drive):
        raise RelayError(
            tr(
                f"reservation: network path without server and share ({path})",
                f"réservation : chemin réseau sans serveur ni partage ({path})",
            )
        )
    parts = [p for p in rest.split("\\") if p]
    if not extended:
        parts = [p.rstrip(". ") for p in parts]
        if not all(parts):
            raise RelayError(
                tr(f"reservation: empty name in the path ({path})", f"réservation : nom vide dans le chemin ({path})")
            )
    return drive, parts, extended and any(p != p.rstrip(". ") for p in parts)


def _share(drive: str) -> bool:
    """« \\\\serveur\\partage » (motifs permis : « \\\\nas\\projet* ») : deux noms, chacun avec au moins un caractère
    autre que point ou espace."""
    names = drive[2:].split("\\")
    return len(names) == 2 and all(n.strip(". ") for n in names)


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


@contextlib.contextmanager
def _between_processes(box: Box, wait_s: float = 10.0) -> Any:
    """Verrou de fichier entre le démon et les hooks : un seul contrôle-puis-écriture à la fois sur le poste."""
    folder = _folder(box)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / ".lock", "a+b") as handle:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # pyright: ignore[reportAttributeAccessIssue]
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RelayError(tr("reservations busy, try again", "réservations occupées, réessayer")) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # flock : libéré à la fermeture


def active(box: Box, at: float | None = None) -> list[dict[str, Any]]:
    at = now() if at is None else at
    folder = _folder(box)
    found = [r for p in sorted(folder.glob("*.json")) if (r := read_json(p))] if folder.is_dir() else []
    return [r for r in found if not r.get("released") and float(r.get("expires_at") or 0) > at]


def other_holder(held: dict[str, Any], agent: str, session: str | None) -> bool:
    """La réservation est à quelqu'un d'autre : un autre agent, ou une autre conversation du même agent quand les deux
    sont connues (hooks : Claude Code, Codex et Antigravity donnent l'id de conversation). Une réservation faite par
    l'outil reserve, sans conversation, vaut pour tout l'agent."""
    if held["agent"] != agent:
        return True
    theirs = held.get("session")
    return bool(session and theirs and theirs != session)


def _conflicts(
    box: Box, agent: str, paths: list[str], exclusive: bool, session: str | None = None
) -> list[tuple[dict[str, Any], str, str]]:
    found = []
    for held in active(box):
        if not other_holder(held, agent, session) or not (exclusive or held.get("exclusive", True)):
            continue
        for mine in paths:
            for theirs in held["paths"]:
                if overlap(mine, theirs):
                    found.append((held, mine, theirs))
    return found


def reserve(
    box: Box,
    agent: str,
    paths: list[str],
    reason: str,
    ttl_min: int = 60,
    exclusive: bool = True,
    session: str | None = None,
    *,
    automatic: bool = False,
    tool: str | None = None,
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
    session = str(session).strip()[:120] if session else None
    with _LOCK, _between_processes(box):
        return _reserve(box, agent, wanted, reason, ttl_min, exclusive, session, automatic, tool)


def holder(record: dict[str, Any], agent: str) -> str:
    """Qui tient la réservation, vu de `agent` : « codex », ou « claude (autre conversation) »."""
    same = record["agent"] == agent
    return record["agent"] + (tr(" (other conversation)", " (autre conversation)") if same else "")


def _reserve(
    box: Box,
    agent: str,
    wanted: list[str],
    reason: str,
    ttl_min: int,
    exclusive: bool,
    session: str | None,
    automatic: bool = False,
    tool: str | None = None,
) -> dict[str, Any]:
    if clash := _conflicts(box, agent, wanted, exclusive, session):
        lines = [
            tr(
                f"{mine} overlaps {theirs}, reserved by {holder(held, agent)} ({held['reason']}) until "
                f"{held['expires']}",
                f"{mine} recouvre {theirs}, réservé par {holder(held, agent)} ({held['reason']}) jusqu'à "
                f"{held['expires']}",
            )
            for held, mine, theirs in clash[:5]
        ]
        raise RelayError(tr("reservation refused: ", "réservation refusée : ") + tr("; ", " ; ").join(lines))
    at = now()
    # même agent, même conversation, mêmes chemins : la réservation existante est prolongée, pas doublée
    key = [norm(p) for p in wanted]
    same = next(
        (
            r
            for r in active(box, at)
            if r["agent"] == agent
            and r.get("session") == session
            and bool(r.get("automatic")) == automatic
            and (r.get("reason") == AUTO_REASON) == (reason == AUTO_REASON)
            and sorted(norm(p) for p in r["paths"]) == key
        ),
        None,
    )
    record = {
        **(same or {"id": f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime(at))}-{secrets.token_hex(3)}"}),
        "agent": agent,
        **({"session": session} if session else {}),
        "paths": wanted,
        "reason": reason.strip()[:300],
        "exclusive": bool(exclusive),
        "created": (same or {}).get("created") or _iso(at),
        "expires": _iso(at + int(ttl_min) * 60),
        "expires_at": at + int(ttl_min) * 60,
    }
    if automatic:
        # A manual reservation is never converted to an automatic one. Pending calls accumulate: completion of
        # one parallel edit must not release a second edit which has not run yet.
        record["automatic"] = True
        record["absent_at_start"] = (same or {}).get(
            "absent_at_start", [norm(p) for p in wanted if not os.path.lexists(p)]
        )
        record["pending_tools"] = sorted({*((same or {}).get("pending_tools") or []), *([tool] if tool else [])})
        if not tool:
            record["pending_unknown"] = True
    write_state(_folder(box) / f"{record['id']}.json", record)
    _purge(box, at)
    return record


KEEP_ENDED_S = 24 * 3600


def _purge(box: Box, at: float) -> None:
    """Réservations finies (libérées ou expirées) depuis plus d'un jour effacées : le hook d'édition en pose une par
    fichier modifié, le dossier ne grossit pas sans fin."""
    for path in _folder(box).glob("*.json"):
        record = read_json(path) or {}
        ended = float(record.get("expires_at") or 0)
        if record.get("released"):
            try:
                ended = min(ended, path.stat().st_mtime)
            except OSError:
                continue
        if ended and at - ended > KEEP_ENDED_S:
            with contextlib.suppress(OSError):
                path.unlink()


def release(box: Box, agent: str, reservation: str | None = None, force: bool = False) -> list[str]:
    """Libère une réservation de `agent` (par son id), ou toutes les siennes. `force` : l'hôte reprend, à la demande de
    l'utilisateur, la réservation d'un autre agent absent (30/10 : réservations automatiques de Codex bloquant un commit
    pendant une heure) ; tracée (released_by), les fichiers restent tels quels."""
    agent = normalize_party(agent)
    if force and (not reservation or relay_host() not in (agent, "all")):
        raise RelayError(
            tr(
                "release force: host only, with the reservation id",
                "release force : réservé à l'hôte, avec l'id de la réservation",
            )
        )
    with _LOCK, _between_processes(box):
        return _release(box, agent, reservation, force)


def _release(box: Box, agent: str, reservation: str | None, force: bool = False) -> list[str]:
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
        if force and target["agent"] != agent:
            write_state(
                _folder(box) / f"{target['id']}.json", {**target, "released": _iso(now()), "released_by": agent}
            )
            return [target["id"]]
        if target["agent"] != agent:
            raise RelayError(
                tr(
                    f"reservation {reservation}: not yours ({target['agent']})",
                    f"réservation {reservation} : pas à toi ({target['agent']})",
                )
            )
        held = [target]
    else:  # « toutes les miennes » : jamais celles des conversations (hook d'édition), libérées par id ou au commit
        held = [r for r in held if not r.get("session")]
    done = []
    for record in (r for r in held if r["agent"] == agent):
        write_state(_folder(box) / f"{record['id']}.json", {**record, "released": _iso(now())})
        done.append(record["id"])
    return done


def blocking(agent: str, files: list[str], box: Box | None = None, session: str | None = None) -> list[dict[str, Any]]:
    """Réservations EXCLUSIVES d'autres agents, ou d'autres conversations du même agent, qui couvrent ces fichiers
    (hook d'édition, garde de commit)."""
    box = box or Box()
    agent = normalize_party(agent)
    return [
        {"agent": holder(r, agent), "reason": r["reason"], "expires": r["expires"], "file": f}
        for r in active(box)
        if other_holder(r, agent, session) and r.get("exclusive", True)
        for f in files
        if any(overlap(theirs, os.path.abspath(f)) for theirs in r["paths"])
    ]


def _unchanged(path: str, absent_at_start: bool) -> bool:
    """Positive proof only: an unwritten creation, or an ordinary tracked file clean in both Git indexes.

    Unknown repositories, untracked/ignored changes, directories, symlinks, skip-worktree and assume-unchanged
    entries remain reserved. No Git refresh/index lock, external diff or fsmonitor is needed. Content hashing uses
    the repository's normal clean conversion (including its configured Git filters).
    """
    if any(c in path for c in "*?") or os.path.isdir(path) or os.path.islink(path):
        return False
    if os.path.normcase(os.path.realpath(path)) != os.path.normcase(os.path.abspath(path)):
        return False
    directory = os.path.dirname(path)
    while not os.path.isdir(directory):
        parent = os.path.dirname(directory)
        if parent == directory:
            return False
        directory = parent

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "--literal-pathspecs", "-c", "core.fsmonitor=false", "-C", directory, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=5,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout

    try:
        tracked = git("ls-files", "--stage", "-v", "-z", "--", path)
        unwritten = not tracked and absent_at_start and not os.path.lexists(path)
        ordinary = tracked.startswith("H ") and len(tracked.split("\0")) == 2
        if not (unwritten or ordinary):
            return False
        if git("status", "--porcelain=v1", "--untracked-files=all", "--ignored", "--", path):
            return False
        if unwritten:
            return True
        fields = tracked.split("\t", 1)[0].split()
        if len(fields) != 4 or fields[1] not in {"100644", "100755"} or fields[3] != "0":
            return False
        # Git status can trust cached size/mtime even when bytes changed (reproduced on Windows). Hash the real
        # content through Git's clean conversion, so normal CRLF/attributes do not become false dirty changes.
        return git("hash-object", f"--path={path}", "--", path).strip() == fields[2]
    except (OSError, subprocess.SubprocessError):
        return False


def finish_edits(
    agent: str,
    session: str | None,
    *,
    tool: str | None = None,
    session_ended: bool = False,
    box: Box | None = None,
) -> list[str]:
    """Owner completion releases only proven clean automatic reservations, never another session's work.

    A tool completion needs the invocation id recorded before execution. Stop only rechecks completed calls: some
    hosts give a parent session id to live subagents, so Stop must never clear a pending or unidentified call.
    """
    if not session or not (session_ended or tool):
        return []
    box, agent = box or Box(), normalize_party(agent)
    session = str(session).strip()[:120]
    ended = []
    with _LOCK, _between_processes(box):
        for record in active(box):
            if (
                record["agent"] != agent
                or record.get("session") != session
                or not record.get("automatic")
                or record.get("reason") != AUTO_REASON
            ):
                continue
            pending = set(record.get("pending_tools") or [])
            if not session_ended:
                if tool not in pending:
                    continue
                pending.remove(tool)
            record["pending_tools"] = sorted(pending)
            clean = (
                not record["pending_tools"]
                and not record.get("pending_unknown")
                and all(_unchanged(path, norm(path) in record.get("absent_at_start", [])) for path in record["paths"])
            )
            if clean:
                record["released"] = _iso(now())
                ended.append(record["id"])
            write_state(_folder(box) / f"{record['id']}.json", record)
    return ended


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


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _repo_files(*args: str) -> list[str]:
    top = _git("rev-parse", "--show-toplevel").strip()
    return [os.path.join(top, name) for name in _git(*args).split("\0") if name]


def staged() -> list[str]:
    """Fichiers indexés du dépôt courant, chemins absolus ; un renommage compte ses deux noms (--no-renames)."""
    return _repo_files("diff", "--cached", "--name-only", "--no-renames", "-z")


def committed() -> list[str]:
    """Fichiers du dernier commit (HEAD), chemins absolus ; --root : le premier commit d'un dépôt compte aussi."""
    return _repo_files("diff-tree", "--root", "--no-commit-id", "-r", "--name-only", "--no-renames", "-z", "HEAD")


# Variable d'environnement qui porte, dans les commandes d'un agent, l'id de conversation que ses hooks reçoivent
# (session_id, conversationId). NEXUS_RELAY_SESSION d'abord, pour tous. Relevés le 27/10 dans les shells des trois.
SESSION_VARS = {
    "claude": "CLAUDE_CODE_SESSION_ID",
    "codex": "CODEX_THREAD_ID",
    "antigravity": "ANTIGRAVITY_CONVERSATION_ID",
}


def session_from_env(agent: str) -> str | None:
    """Conversation de l'agent qui lance la commande (git et ses hooks), ou None : inconnue."""
    # Codex emits session_id=CODEX_SESSION_ID for hooks, including calls routed from its subagents.
    # Their CODEX_THREAD_ID names the child execution, not the reservation owner. Keep the same canonical
    # owner as the hook while retaining the CLI's older thread-only environment as a fallback.
    names = [
        "NEXUS_RELAY_SESSION",
        *(["CODEX_SESSION_ID"] if agent == "codex" else []),
        *([SESSION_VARS[agent]] if agent in SESSION_VARS else []),
    ]
    return next((value.strip() for name in names if (value := os.environ.get(name, "")).strip()), None)


def release_committed(agent: str, files: list[str], box: Box | None = None, session: str | None = None) -> list[str]:
    """Release completed automatic edits of this exact session, only if committed paths have no dirty remainder.

    Unknown/mismatched session and pending calls cannot prove ownership/completion (notably inherited parent ids
    in child shells). Manual and legacy reservations without lifecycle metadata keep their owner/TTL fallback.
    """
    if not session or not (session := str(session).strip()[:120]):
        return []
    box = box or Box()
    agent = normalize_party(agent)
    done = set(map(norm, files))
    with _LOCK, _between_processes(box):
        ended = [
            r["id"]
            for r in active(box)
            if r["agent"] == agent
            and r.get("session") == session
            and r.get("automatic")
            and r.get("reason") == AUTO_REASON
            and not r.get("pending_tools")
            and not r.get("pending_unknown")
            and all(norm(p) in done for p in r["paths"])
            and all(_unchanged(p, True) for p in r["paths"])
        ]
        return [done_id for rid in ended for done_id in _release(box, agent, rid)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Garde de commit Nexus Relay : fichiers réservés par un autre agent")
    parser.add_argument(
        "--agent", help="toi : claude, codex ou antigravity (par défaut : reconnu par les processus parents)"
    )
    parser.add_argument("--staged", action="store_true", help="fichiers indexés du dépôt courant (hook pre-commit)")
    parser.add_argument(
        "--release-committed",
        action="store_true",
        help="libère mes réservations automatiques des fichiers du dernier commit (hook post-commit)",
    )
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    agent = normalize_party(args.agent) if args.agent else whoami()
    session = session_from_env(agent)
    if args.release_committed:
        if agent != normalize_party("user"):
            release_committed(agent, [*args.files, *committed()], session=session)
        return 0
    hits = blocking(agent, [*args.files, *(staged() if args.staged else [])], session=session)
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
