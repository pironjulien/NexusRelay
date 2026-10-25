"""Mise à jour du relais par lui-même (contrat commun des produits Nexus, appli Nexus du Store, 26/10/2026).

L'appli ne lance aucun installeur : elle lit `~\\.nexus\\status\\relay.json` et déclenche la mise à jour par
`POST /api/update` sur le démon (même jeton que /mcp). C'est le démon, déjà lancé hors de tout conteneur par sa tâche,
qui lance son propre `get.ps1` (`powershell -File`, NEXUS_PACK=1 : ni question ni fenêtre). get.ps1 recopie la dernière
version dans `%LOCALAPPDATA%\\NexusRelay\\app`, puis install.ps1 demande la relance douce du démon, qui repart sur le
nouveau code et note l'issue.

Seule une installation publique (dossier installé = `%LOCALAPPDATA%\\NexusRelay\\app`, avec son get.ps1) se met à jour
ainsi ; un dépôt de développement suit git. La dernière version vient de la redirection de `releases/latest` (pas de
l'API GitHub), au plus une fois par jour ; NEXUS_RELAY_UPDATE_CHECK=0 coupe cette vérification.

    ~/.nexus/status/relay.json   {product, version, latest, state, checked_at, message}
    ~/.nexus/nexus-relay/update.json   mise à jour en cours {target, started_at, pid}
    ~/.nexus/nexus-relay/update.log    sortie de get.ps1
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__

PRODUCT = "relay"
REPO = "pironjulien/NexusRelay"
CHECK_EVERY_S = 24 * 3600
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
BREAKAWAY = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB : survit à l'arrêt du démon pour relance
_LOCK = threading.Lock()


def status_path() -> Path:
    return Path.home() / ".nexus" / "status" / f"{PRODUCT}.json"


def _state_dir() -> Path:
    return Path.home() / ".nexus" / "nexus-relay"


def marker_path() -> Path:
    return _state_dir() / "update.json"


def install_root() -> Path:
    return Path(__file__).resolve().parents[2]


def managed_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "NexusRelay" / "app"


def self_updatable(root: Path | None = None) -> bool:
    root = root or install_root()
    same = os.path.normcase(os.path.realpath(root)) == os.path.normcase(os.path.realpath(managed_root()))
    return same and (root / "get.ps1").is_file()


def parse_version(text: str | None) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text or "")[:3])


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def fetch_latest(timeout: float = 10.0) -> str | None:
    """Dernière version publiée, lue dans la redirection de releases/latest (…/tag/vX.Y.Z)."""
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(f"https://github.com/{REPO}/releases/latest", method="HEAD")
    try:
        opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        match = re.search(r"/tag/v?([\d.]+)$", error.headers.get("Location", ""))
        return match.group(1) if match else None
    except OSError:
        return None
    return None


def read_status() -> dict[str, Any]:
    try:
        data = json.loads(status_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(**changes: Any) -> dict[str, Any]:
    with _LOCK:
        data = {"product": PRODUCT, "version": __version__, "latest": None, "state": "idle", "checked_at": None}
        data.update({k: v for k, v in read_status().items() if k in data or k == "message"})
        data.update(changes, product=PRODUCT, version=__version__)
        path = status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return data


def _checked_age(data: dict[str, Any]) -> float:
    try:
        checked = datetime.strptime(str(data.get("checked_at")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return float("inf")
    return (datetime.now(UTC) - checked).total_seconds()


def check(force: bool = False, fetch: Any = fetch_latest) -> dict[str, Any]:
    """Dernière version (au plus une fois par jour sauf force) ; rend l'état écrit."""
    data = read_status()
    if os.environ.get("NEXUS_RELAY_UPDATE_CHECK") == "0" or (not force and _checked_age(data) < CHECK_EVERY_S):
        return write_status()
    latest = fetch()
    if latest is None:
        return write_status(message="latest version unavailable (offline?)")
    return write_status(latest=latest, checked_at=now_iso(), message=None)


def _pid_alive(pid: int) -> bool:
    from .box import pid_alive

    return pid_alive(pid)


def resume() -> dict[str, Any]:
    """Au démarrage du démon : issue d'une mise à jour lancée avant la relance."""
    try:
        marker = json.loads(marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return write_status(state="idle")  # aucune mise à jour en cours (message de la dernière issue gardé)
    target = str(marker.get("target") or "")
    if target and parse_version(__version__) >= parse_version(target):
        marker_path().unlink(missing_ok=True)
        return write_status(state="done", message=f"updated to {__version__}")
    if _pid_alive(int(marker.get("pid") or 0)):
        _watch(int(marker["pid"]), None)
        return write_status(state="updating")
    marker_path().unlink(missing_ok=True)
    return write_status(state="failed", message=f"update to {target or 'latest'} did not complete; see update.log")


def _watch(pid: int, proc: subprocess.Popen[bytes] | None) -> None:
    """Fin de get.ps1 sans relance du démon (échec, ou rien à relancer) : l'issue est notée ici."""

    def wait() -> None:
        if proc is not None:
            code = proc.wait()
        else:
            while _pid_alive(pid):
                time.sleep(2)
            code = 0
        if not marker_path().exists():
            return
        if code:
            marker_path().unlink(missing_ok=True)
            write_status(state="failed", message=f"get.ps1 exited with code {code}; see update.log")

    threading.Thread(target=wait, name="nexus-relay-maj", daemon=True).start()


def start(fetch: Any = fetch_latest, root: Path | None = None) -> tuple[int, dict[str, Any]]:
    """POST /api/update : (code HTTP, état). 202 lancée, 200 déjà à jour, 409 impossible ici."""
    root = root or install_root()
    current = read_status()
    if current.get("state") == "updating" and marker_path().exists():
        return 202, write_status()
    if not self_updatable(root):
        return 409, write_status(message="this install is not self-updating (development checkout: use git)")
    data = check(force=True, fetch=fetch)
    latest = data.get("latest")
    if latest and parse_version(latest) <= parse_version(__version__):
        return 200, write_status(state="idle", message="already up to date")
    log = _state_dir() / "update.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
    with open(log, "ab") as out:
        try:
            proc = subprocess.Popen(
                [*command, str(root / "get.ps1")],
                env={**os.environ, "NEXUS_PACK": "1"},
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                creationflags=NO_WINDOW | DETACHED | BREAKAWAY,
            )
        except OSError:  # tâche sans droit de sortir de son job : lancé dedans
            proc = subprocess.Popen(
                [*command, str(root / "get.ps1")],
                env={**os.environ, "NEXUS_PACK": "1"},
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                creationflags=NO_WINDOW | DETACHED,
            )
    marker = {"target": latest, "started_at": now_iso(), "pid": proc.pid}
    marker_path().write_text(json.dumps(marker), encoding="utf-8")
    _watch(proc.pid, proc)
    return 202, write_status(state="updating", message=f"updating to {latest or 'latest'}")


def watch_latest(delay_s: float = 60.0) -> None:
    """Fil du démon : dernière version vérifiée peu après le démarrage, puis toutes les 6 h si l'état a plus d'un jour."""

    def loop() -> None:
        time.sleep(delay_s)
        while True:
            try:
                check()
            except Exception:  # noqa: BLE001 - une vérification ratée n'arrête jamais le démon
                pass
            time.sleep(6 * 3600)

    threading.Thread(target=loop, name="nexus-relay-version", daemon=True).start()
