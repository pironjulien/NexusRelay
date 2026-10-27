"""Superviseur du seul démon nexus-relay, pour un poste sans superviseur des démons Nexus (tâche planifiée « Nexus Relay »
posée par install.ps1, lancée à l'ouverture de session par pythonw).

Lance `python -B -m nexus_relay.server --http <port>` avec la spec écrite par install.ps1
(`~\\.nexus\\daemons\\specs\\nexus-relay.json` : Python, arguments, port, environnement), relue à chaque lancement, et
le relance quand il s'arrête : aussitôt après une relance par demande (sortie propre après au moins MIN_UP_S), avec une
pause croissante (2 s, 4 s… 60 s au plus) après un plantage. Une seule instance par session (verrou exclusif). Sortie du
démon dans `~\\.nexus\\nexus-relay\\daemon.log`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .paths import _give_back, relay_home

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MIN_UP_S = 10.0  # plus court : compté comme un plantage (port pris, jeton absent…)
MAX_PAUSE_S = 60.0
LOG_MAX = 5 * 1024 * 1024


def spec_path() -> Path:
    return Path.home() / ".nexus" / "daemons" / "specs" / "nexus-relay.json"


def state_dir() -> Path:
    return Path.home() / ".nexus" / "nexus-relay"


def command(spec: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """Commande et environnement du démon d'après sa spec."""
    argv = [str(spec["python"]), *[str(arg) for arg in spec.get("args", [])], "--http", str(int(spec["port"]))]
    env = {**os.environ, **{str(k): str(v) for k, v in (spec.get("env") or {}).items()}}
    return argv, env


def lock(path: Path) -> Any:
    """Verrou exclusif tenu tant que le processus vit (libéré par le système à sa mort) ; None : déjà pris."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115 - gardé ouvert toute la vie du superviseur
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # pyright: ignore[reportAttributeAccessIssue]
    except OSError:
        handle.close()
        return None
    return handle


def _log(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_MAX:
        path.replace(path.with_suffix(".log.1"))
    return open(path, "ab")  # noqa: SIM115 - confié au démon lancé


def take_request() -> Path | None:
    """Demande de relance déposée AVANT ce lancement (install.ps1 l'écrit avant de lancer la tâche) : satisfaite par lui,
    prise d'un coup AVANT de relire la spec (relecture de Codex : prise après, elle effaçait une demande plus récente
    que la spec lue). Vu le 27/10 en CI : le démon tout neuf la suivait, se relançait 2 s après avoir répondu, et
    l'étape suivante trouvait le port fermé. Rend la prise (à jeter une fois lancé, à rendre sinon)."""
    request = relay_home() / "daemon-restart.request"
    taken = request.with_name(request.name + ".lancement")
    try:
        os.replace(request, taken)
    except FileNotFoundError:
        return None
    except OSError as error:  # demande gardée : au pire une relance de trop, comme avant
        print(f"nexus-relay daemon : demande de relance non prise : {error}", file=sys.stderr, flush=True)
        return None
    return taken


def settle(taken: Path | None, launched: bool) -> None:
    """Lancé : la prise est satisfaite, jetée. Sinon rendue (une demande plus récente, ou « force », l'emporte)."""
    if taken is None:
        return
    try:
        if launched:
            taken.unlink(missing_ok=True)
        else:
            _give_back(taken, taken.with_name("daemon-restart.request"))
    except OSError as error:
        print(f"nexus-relay daemon : demande de relance non réglée : {error}", file=sys.stderr, flush=True)


def run(
    spec_file: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    rounds: int | None = None,
) -> None:
    """Boucle de supervision (`rounds` : nombre de lancements, pour les tests ; None = sans fin)."""
    spec_file = spec_file or spec_path()
    pause = 0.0
    done = 0
    while rounds is None or done < rounds:
        done += 1
        taken = take_request()
        try:
            argv, env = command(json.loads(spec_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError) as error:
            settle(taken, launched=False)
            print(f"nexus-relay daemon : spec illisible ({spec_file}) : {error}", file=sys.stderr, flush=True)
            pause = min(MAX_PAUSE_S, max(2.0, pause * 2))
            sleep(pause)
            continue
        started = clock()
        with _log(state_dir() / "daemon.log") as log:
            try:
                proc = subprocess.Popen(
                    argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=NO_WINDOW
                )
            except BaseException:
                settle(taken, launched=False)
                raise
            settle(taken, launched=True)
            code = proc.wait()
        up = clock() - started
        if code == 0 and up >= MIN_UP_S:
            pause = 0.0  # relance par demande : nouveau code aussitôt
        else:
            pause = min(MAX_PAUSE_S, max(2.0, pause * 2))
        sleep(pause or 0.5)


def main() -> None:
    held = lock(state_dir() / "daemon.lock")
    if held is None:
        print("nexus-relay daemon : déjà lancé", file=sys.stderr)
        return
    run()


if __name__ == "__main__":
    main()
