"""CLI officielle `agentapi` d'Antigravity, fournie aux sidecars (doc : antigravity.google/docs/sidecars).

Commandes de la version installée (vérifié : `agentapi help`, chaînes de language_server.exe) : new-conversation,
send-message, get-conversation-metadata. Ni liste, ni archivage, ni suppression de conversation : le rangement passe
par le port d'Antigravity (cdp.archive_antigravity_conversation).

Le texte des messages part en argument d'un exécutable, JAMAIS à travers cmd.exe : le raccourci agentapi.bat est lu
pour retrouver l'exécutable qu'il appelle (un .bat interpréterait &, |, % ou " contenus dans un message).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .lang import tr
from .paths import is_conversation_id

_BAT_LINE = re.compile(r'^\s*"([^"]+\.exe)"\s+([A-Za-z0-9_-]+)\s+%\*\s*$', re.IGNORECASE)
CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x4 if os.name == "nt" else 0  # schéma du superviseur des démons (nexus-mcp-daemons.py)


class AgentApiError(RuntimeError):
    """`uncertain` : la demande a pu partir (délai dépassé, réponse illisible ou sans identifiant) ; sinon, refus connu."""

    def __init__(self, message: str, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


def _from_bat(bat: Path) -> list[str]:
    for line in bat.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _BAT_LINE.match(line)
        if match:
            return [match.group(1), match.group(2)]
    raise AgentApiError(
        tr(f"unexpected format of {bat}: executable not found", f"format inattendu de {bat} : exécutable introuvable")
    )


def resolve_command(env: Mapping[str, str] | None = None) -> list[str]:
    """Commande à lancer pour agentapi (liste d'arguments, sans shell)."""
    env = os.environ if env is None else env
    override = env.get("NEXUS_RELAY_AGENTAPI")
    if override:
        command = json.loads(override)
        if not (isinstance(command, list) and command and all(isinstance(part, str) for part in command)):
            raise AgentApiError(
                tr(
                    "NEXUS_RELAY_AGENTAPI must be a JSON list of strings",
                    "NEXUS_RELAY_AGENTAPI doit être une liste JSON de chaînes",
                )
            )
        return command
    candidate = env.get("ANTIGRAVITY_AGENTAPI_EXE") or shutil.which("agentapi", path=env.get("PATH")) or ""
    if not candidate:
        raise AgentApiError(
            tr(
                "agentapi not found: this program must run as an Antigravity sidecar",
                "agentapi introuvable : ce programme doit tourner comme sidecar d'Antigravity",
            )
        )
    path = Path(candidate)
    suffix = path.suffix.lower()
    if suffix in (".bat", ".cmd"):
        return _from_bat(path)
    if path.name.lower() == "language_server.exe":
        return [str(path), "agentapi"]
    return [str(path)]


def _clean_title(title: str, limit: int = 80) -> str:
    # caractères de contrôle (NUL compris : Popen le refuse, tour 2 de Codex) remplacés par une espace
    title = " ".join("".join(c if c >= " " else " " for c in title).split())
    return (title[: limit - 1] + "…" if len(title) > limit else title) or "Nexus Relay"


MESSAGE_TITLE = 144  # titre d'un message suivant dans la conversation (une ligne lisible, essai réel du 24/10)


def _job(process: subprocess.Popen[str]) -> int | None:
    """Objet tâche Windows contenant agentapi et TOUTE sa descendance, même quand agentapi est déjà sorti (tour 4 :
    taskkill /T ne trouvait plus l'arbre d'un parent sorti, ses enfants gardaient les tubes)."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        return None
    handle = getattr(process, "_handle", None)  # poignée du processus (CPython sous Windows)
    if handle is None or not kernel.AssignProcessToJobObject(job, int(handle)):
        _close_job(job)
        return None
    return int(job)


def _resume(process: subprocess.Popen[str]) -> bool:
    handle = getattr(process, "_handle", None)
    if os.name != "nt" or handle is None:
        return True
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    ntdll.NtResumeProcess.restype = ctypes.c_long
    return ntdll.NtResumeProcess(int(handle)) == 0  # NTSTATUS : 0 = succès


def _kill_job(job: int | None) -> None:
    if job:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.TerminateJobObject(job, 1)


def _close_job(job: int | None) -> None:
    if job:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle(job)


class AgentApi:
    def __init__(self, command: list[str], timeout: float = 120.0) -> None:
        self.command = command
        self.timeout = timeout

    def _run(self, args: list[str]) -> dict[str, Any]:
        try:
            process = subprocess.Popen(
                [*self.command, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW | CREATE_SUSPENDED,
            )
        except (OSError, ValueError) as error:  # ValueError : argument refusé (NUL) avant tout lancement
            raise AgentApiError(tr(f"agentapi {args[0]}: {error}", f"agentapi {args[0]} : {error}")) from error
        job = None
        try:
            job = _job(process)
            if not _resume(process):  # relâché seulement une fois dans l'objet tâche : aucun enfant né avant (tour 5)
                raise OSError(tr("NtResumeProcess refused", "NtResumeProcess refusé"))
        except Exception as error:  # tour 6 : jamais un agentapi suspendu pour toujours ; rien n'a tourné
            process.kill()
            _close_job(job)
            raise AgentApiError(
                tr(f"agentapi {args[0]}: launch: {error}", f"agentapi {args[0]} : lancement : {error}")
            ) from error
        try:
            stdout, stderr = process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired as error:
            # toute la descendance tuée (objet tâche), puis les tubes vidés : plus aucun lecteur ni processus bloqué
            if job:
                _kill_job(job)
            else:  # objet tâche indisponible : l'arbre par taskkill (parent encore vivant)
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    capture_output=True,
                    creationflags=CREATE_NO_WINDOW,
                    check=False,
                    timeout=15,
                )
            process.kill()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass  # déjà tué : on n'attend pas plus ses tubes, l'erreur de délai suit
            raise AgentApiError(
                tr(
                    f"agentapi {args[0]}: timed out ({self.timeout:.0f} s)",
                    f"agentapi {args[0]} : délai dépassé ({self.timeout:.0f} s)",
                ),
                uncertain=True,
            ) from error
        finally:
            _close_job(job)
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        output = (result.stdout or "").strip()
        if result.returncode:
            detail = ((result.stderr or "") + "\n" + output).strip()[:618]
            raise AgentApiError(
                tr(
                    f"agentapi {args[0]}: code {result.returncode}: {detail}",
                    f"agentapi {args[0]} : code {result.returncode} : {detail}",
                )
            )
        start = output.find("{")
        try:
            data = json.loads(output[start:]) if start >= 0 else {}
        except json.JSONDecodeError as error:  # sortie coupée : la demande a pu être acceptée (relecture du 20/10)
            raise AgentApiError(
                tr(
                    f"agentapi {args[0]}: unreadable response: {output[:200]}",
                    f"agentapi {args[0]} : réponse illisible : {output[:200]}",
                ),
                uncertain=True,
            ) from error
        if isinstance(data, dict) and data.get("error"):
            # agentapi rend certaines erreurs en JSON avec le code 0 (vérifié : {"error": "…"}).
            raise AgentApiError(
                tr(
                    f"agentapi {args[0]}: {str(data['error'])[:618]}",
                    f"agentapi {args[0]} : {str(data['error'])[:618]}",
                )
            )
        return data if isinstance(data, dict) else {}

    def new_conversation(self, prompt: str, title: str) -> str:
        data = self._run(["new-conversation", f"--title={_clean_title(title)}", "--", prompt])
        conversation = str(((data.get("response") or {}).get("newConversation") or {}).get("conversationId", ""))
        if not is_conversation_id(conversation):
            raise AgentApiError(
                tr(
                    f"agentapi new-conversation: no conversation id ({data})",
                    f"agentapi new-conversation : pas d'identifiant de conversation ({data})",
                ),
                uncertain=True,
            )
        return conversation

    def send_message(self, conversation: str, prompt: str, title: str | None = None) -> None:
        """`title` : ce qu'Antigravity affiche du message dans la conversation (sinon « Message from System »)."""
        if not is_conversation_id(conversation):
            raise AgentApiError(
                tr(f"invalid conversation: {conversation!r}", f"conversation invalide : {conversation!r}")
            )
        if prompt.startswith("-"):
            raise AgentApiError(
                tr(
                    "a message cannot start with “-” (taken for an option)",
                    "un message ne peut pas commencer par « - » (pris pour une option)",
                )
            )
        named = [f"--title={_clean_title(title, MESSAGE_TITLE)}"] if title else []
        self._run(["send-message", *named, conversation, prompt])
