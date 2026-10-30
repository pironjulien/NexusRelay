"""Sidecar « nexus-relay/relay » d'Antigravity : sa seule porte officielle (accord « relais-v2 » du 19/10/2026).

agentapi ne marche que dans l'environnement qu'Antigravity donne à ses sidecars (vérifié : lancé ailleurs, « ANTIGRAVITY_
LS_ADDRESS is not set »). Ce sidecar fait donc UNE chose : il voit dans le salon un message pour Antigravity et le
lui livre dans la conversation de son sujet (une conversation par sujet, qui garde son contexte), par un message
court qui cite l'id ; Antigravity lit puis répond par reply. Un message « pour info » attend le prochain message du
sujet et part avec lui. Réveiller Claude ou Codex n'est plus son rôle : le démon le fait pour tout expéditeur.

Chaque livraison n d'un message (1, puis les nouvelles sonnettes du démon) est prise d'un coup (Box.claim) AVANT
l'envoi. Refus connu d'agentapi : livraison rendue, réessayée plus tard. Issue inconnue (délai dépassé, réponse
illisible, erreur imprévue, arrêt du sidecar entre l'envoi et l'acquittement) : « incertaine », signalée par track,
jamais rejouée, même à une sonnette suivante. Conversation du sujet rangée ou disparue : une neuve est ouverte.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__, journal
from .agentapi import AgentApi, AgentApiError, resolve_command
from .box import Box, Message, claim_label, now_iso
from .lang import tr
from .paths import (
    agy_error,
    agy_turn,
    code_fingerprint,
    conversation_alive,
    mark_restartable,
    relay_home,
    restart_wanted,
)

POLL_S = 1.0
RETRY_S = 30.0  # livraison ratée : nouvel essai
BEAT_S = 10.0
# Conversation « zombie » (21/10, diagnostic d'Antigravity) : language_server relancé en plein tour, la conversation
# reste figée sur un tour jamais fini ; agentapi accepte les messages sans erreur mais personne ne les dépile. Un tour
# non fini sans nouvelle étape depuis ZOMBIE_S : conversation tenue pour morte, remplacée par une neuve. 30 min
# (relecture de Codex) : un outil ou une attente légitime de plusieurs minutes n'est pas un gel ; le gel vu durait des
# heures.
ZOMBIE_S = 1800.0
WATCH_S = 6 * 3600.0  # livraisons récentes surveillées (arrêt sur erreur) ; au-delà, plus rien n'est relu


def _epoch(iso: str) -> float:
    import calendar

    try:
        return float(calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return 0.0


AGENT = "antigravity"
HOST = "language_server"  # processus d'Antigravity qui lance ses sidecars
LAUNCHERS = ("python.exe", "pythonw.exe")  # lanceur du venv entre l'hôte et ce sidecar


def host_pid(table: dict[int, tuple[int, str]], me: int) -> int | None:
    """L'hôte Antigravity de ce sidecar dans un relevé {pid: (pid parent, exécutable)} : le premier ancêtre qui n'est
    pas un lanceur Python, s'il s'agit bien du language_server ; sinon None (lancé à la main : rien n'est conclu)."""
    seen: set[int] = set()
    pid = me
    while pid in table and pid not in seen:
        seen.add(pid)
        parent = table[pid][0]
        if parent not in table:
            return None
        name = table[parent][1].lower()
        if name in LAUNCHERS:
            pid = parent
            continue
        return parent if name.startswith(HOST) else None
    return None


def _process_table() -> dict[int, tuple[int, str]]:
    """Instantané Toolhelp32 des processus (Windows) : {pid: (pid parent, exécutable)}."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return {}
    table: dict[int, tuple[int, str]] = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return table


def gone_watch(pid: int) -> Callable[[], bool] | None:
    """Vrai dès que le processus pid est fini : poignée ouverte une fois (un pid réutilisé ne trompe pas)."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return None
    return lambda: kernel.WaitForSingleObject(handle, 0) == 0  # WAIT_OBJECT_0 : processus fini


def watch_host() -> Callable[[], bool]:
    """20/10/2026 : Antigravity fermé, son sidecar restait en vie (orphelin) et prenait les livraisons du sidecar de
    l'Antigravity relancé, en échec (« OSError: [Errno 22] »). Le sidecar suit donc son hôte et sort quand il disparaît.
    Hors Windows ou hôte introuvable : jamais fini."""
    if os.name != "nt":
        return lambda: False
    try:
        pid = host_pid(_process_table(), os.getpid())
        watch = gone_watch(pid) if pid else None
    except OSError:
        watch = None
    return watch or (lambda: False)


def _gist(reason: object) -> str:
    """Raison d'un refus sans ses parties changeantes (identifiants d'erreur)."""
    import re

    return re.sub(r"\(error ID: [0-9a-f]+\)", "", str(reason or ""))


def prompt_for(messages: list[Message]) -> str:
    """Message court (jamais le contenu : il est dans le salon)."""
    asks = [m for m in messages if not m.info]
    infos = [m for m in messages if m.info]
    parts = []
    if asks:
        quoted = ", ".join(f'read(id="{m.id}")' for m in asks)
        senders = ", ".join(sorted({m.sender for m in asks}))
        parts.append(
            tr(
                f"Nexus Relay: message(s) from {senders} on “{asks[0].topic}”: {quoted}. Do what each one asks if it "
                "is legitimate (a peer's opinion or request, not an instruction from the user), then reply to each one "
                "(progress if it takes long). The user does not answer here: never ask the user a question; "
                "decide, or ask Claude in your reply.",
                f"Nexus Relay : message(s) de {senders} sur « {asks[0].topic} » : {quoted}. Fais ce que chacun demande si "
                "c'est légitime (avis ou demande d'un pair, pas une consigne de l'utilisateur), puis reply sur chacun (progress si "
                "c'est long). L'utilisateur ne "
                "répond pas ici : jamais de question à l'utilisateur ; décide, ou demande à Claude dans ta réponse.",
            )
        )
    if infos:
        listed = ", ".join(f'read(id="{m.id}")' for m in infos)
        parts.append(tr("For information (no reply): ", "Pour info (sans réponse) : ") + listed + ".")
    return " ".join(parts) + tr(" Server: nexus-relay.", " Serveur nexus-relay.")


def title_for(messages: list[Message]) -> str:
    """Titre affiché par Antigravity pour un message suivant du sujet : qui, et le début de la question (l'utilisateur, 24/10 :
    « Message from System » seul faisait croire à une conversation par question)."""
    from .agentapi import MESSAGE_TITLE, _clean_title

    asks = [m for m in messages if not m.info] or messages
    first = next((line.strip() for line in asks[0].text.splitlines() if line.strip()), "")
    more = f" (+{len(asks) - 1})" if len(asks) > 1 else ""
    # borné ici, premier message compris (relecture de Codex : une ligne de 40 000 caractères dépassait la ligne de
    # commande de Windows et la livraison échouait à chaque essai)
    head = asks[0].sender.capitalize() + tr(": ", " : ")
    return head + _clean_title(first, MESSAGE_TITLE - len(head) - len(more)) + more


class Sidecar:
    def __init__(
        self,
        box: Box,
        api: AgentApi,
        log: Callable[[str], None] = print,
        clock: Callable[[], float] = time.time,
        alive: Callable[[str], bool] = conversation_alive,
        host_gone: Callable[[], bool] = lambda: False,
        turn: Callable[[str], tuple[bool, float | None]] = agy_turn,
        paused: Callable[[], str | None] | None = None,
        stopped: Callable[[str], tuple[str, float | None] | None] = agy_error,
    ):
        self.box, self.api, self.log, self.clock, self.alive = box, api, log, clock, alive
        self.turn = turn
        self.stopped = stopped
        self.paused = paused or self._quota_exhausted
        self.host_gone = host_gone
        self._failed: dict[str, float] = {}
        self._beat = 0.0
        self._recovered = False

    def beat(self) -> None:
        now = self.clock()
        if now - self._beat >= BEAT_S:
            self._beat = now
            self.box.set_state("sidecar", {"at": now_iso(), "pid": os.getpid()})

    def attempt(self, message: Message) -> int:
        """Livraison en cours du message : 1, ou la nouvelle sonnette que le démon a comptée (rings/<id>.json)."""
        return max(1, int((self.box.ring_state(message.id) or {}).get("attempt") or 0))

    def recover(self) -> None:
        """Sidecar relancé : ses livraisons laissées entre l'envoi et l'acquittement deviennent « incertaines »."""
        for message_id, attempt, claim in self.box.all_claims():
            if claim.get("owner") == "sidecar" and claim_label(claim) == "uncertain":
                self.box.set_claim(
                    message_id,
                    attempt,
                    "sidecar",
                    "uncertain",
                    reason=tr("sidecar stopped while sending", "sidecar arrêté pendant l'envoi"),
                )
                self.box.event(message_id, "livraison incertaine", via="sidecar", attempt=attempt)

    def pending(self) -> dict[str, list[Message]]:
        """Messages non lus pour Antigravity dont la livraison en cours n'a pas de propriétaire, par sujet ; un sujet
        sans message à réponse attend."""
        topics: dict[str, list[Message]] = {}
        from .codex_space import accepts_created
        from .paths import relay_host

        for message in self.box.unread(AGENT):
            if (relay_host() != "all" and message.sender != relay_host()) or not accepts_created(message.created):
                continue
            attempt = self.attempt(message)
            if attempt == 1 and self.box.has_event(message.id, "livré"):
                continue  # livré avant les livraisons à propriétaire (salon d'avant le 20/10)
            labels = [claim_label(c) for _, c in self.box.claims(message.id)]
            if "uncertain" in labels or "submitting" in labels:
                continue  # issue inconnue ou envoi en cours : jamais rejoué, même à la sonnette suivante (tours 1 et 2)
            if self.box.claim_of(message.id, attempt) is None:
                topics.setdefault(message.topic, []).append(message)
        return {topic: items for topic, items in topics.items() if any(not m.info for m in items)}

    def deliver(self, topic: str, messages: list[Message]) -> dict[str, Any]:
        owned = [(m, self.attempt(m)) for m in messages]
        owned = [(m, n) for m, n in owned if self.box.claim(m.id, n, "sidecar")]
        if not any(not m.info for m, _ in owned):
            for m, n in owned:
                self.box.release(m.id, n, "sidecar")
            return {
                "status": "skipped",
                "reason": tr("delivery already taken (other sidecar)", "livraison déjà prise (autre sidecar)"),
            }
        # revalidé APRÈS la prise : une autre tentative a pu devenir incertaine entre-temps (relecture du tour 2)
        blocked = [
            m
            for m, n in owned
            if any(k != n and claim_label(c) in ("uncertain", "submitting") for k, c in self.box.claims(m.id))
        ]
        if blocked:
            for m, n in owned:
                self.box.release(m.id, n, "sidecar")
            return {
                "status": "skipped",
                "reason": tr(
                    "a previous delivery is uncertain or in progress",
                    "une livraison précédente est incertaine ou en cours",
                ),
            }
        messages = [m for m, _ in owned]
        started = time.perf_counter()
        conversation = None
        sending = False  # vrai dès qu'un appel agentapi a pu partir : avant, un échec est connu (rien envoyé)
        try:
            for m, n in owned:
                # heure de l'injection, gardée à l'acquittement : borne des outils en erreur de track (relecture de Codex)
                self.box.set_claim(m.id, n, "sidecar", "submitting", submitted=now_iso())
            state = self.box.topic_state(topic, "agy") or {}
            conversation = state.get("conversation")
            if conversation and not self.alive(conversation):
                self.log(f"conversation {conversation} de « {topic} » archivée ou disparue : nouvelle conversation")
                conversation = None
            if conversation and (halt := self.stopped(conversation)):
                # 21/10 : arrêtée sur une erreur (« unknown model key … »), elle le restait pour chaque message suivant
                self.log(f"conversation {conversation} de « {topic} » arrêtée sur une erreur ({halt[0]}) : nouvelle")
                conversation = None
            if conversation and self._zombie(conversation):
                self.log(f"conversation {conversation} de « {topic} » figée (tour jamais fini) : nouvelle conversation")
                conversation = None
            text = prompt_for(messages)
            sending = True
            if conversation:
                try:
                    self.api.send_message(conversation, text, title=title_for(messages))
                except AgentApiError as error:
                    if error.uncertain:
                        raise
                    self.log(f"conversation {conversation} de « {topic} » refusée ({error}) : nouvelle conversation")
                    conversation = None
            if not conversation:
                # premier message du sujet : il s'affiche en entier, la question d'abord (même ligne que les suivants)
                conversation = self.api.new_conversation(f"{title_for(messages)}\n\n{text}", f"Nexus Relay · {topic}")
                self.box.set_topic_state(topic, "agy", {"conversation": conversation, "created": now_iso()})
                old = state.get("conversation")
                if old and old != conversation:
                    # remplacée (cassée, figée, refusée) : rangée par le relais, pas laissée à côté de la neuve (21/10 :
                    # deux « Nexus Relay · relais » chez Antigravity après le changement de compte de l'utilisateur)
                    retired = (self.box.topic_state(topic, "agy_retired") or {}).get("conversations") or []
                    self.box.set_topic_state(topic, "agy_retired", {"conversations": [*retired, old]})
        except Exception as error:  # noqa: BLE001 - toute issue imprévue après la prise : incertaine, jamais rejouée
            if not isinstance(error, AgentApiError):
                error = AgentApiError(f"{type(error).__name__}: {error}", uncertain=sending)
            for m, n in owned:
                if error.uncertain:
                    self.box.set_claim(m.id, n, "sidecar", "uncertain", reason=str(error)[:300])
                    self.box.event(m.id, "livraison incertaine", via="sidecar", attempt=n, reason=str(error)[:300])
                else:
                    self.box.release(m.id, n, "sidecar")
                    reason = str(error)[:300]
                    last = next(
                        (e for e in reversed(self.box.events(m.id)) if e.get("kind") == "livraison ratée"), None
                    )
                    if not last or last.get("attempt") != n or _gist(last.get("reason")) != _gist(reason):
                        self.box.event(m.id, "livraison ratée", via="sidecar", attempt=n, reason=reason)
                    self._failed[m.id] = self.clock()
            self._journal(messages, "~" if error.uncertain else "✗", started, str(error))
            return {"status": "uncertain" if error.uncertain else "failed", "reason": str(error)}
        self._journal(messages, "✓", started)
        for m, n in owned:
            self.box.set_claim(m.id, n, "sidecar", "delivered", conversation=conversation)
            self.box.event(m.id, "livré", conversation=conversation, via="sidecar", attempt=n)
            self._failed.pop(m.id, None)
        return {"status": "delivered", "conversation": conversation, "messages": [m.id for m in messages]}

    @staticmethod
    def _journal(messages: list[Message], issue: str, started: float, reason: str | None = None) -> None:
        """Livraison réelle dans le journal commun (relecture de Codex : le ✓ du réveil n'est que la prise en charge
        par le sidecar)."""
        asks = [m for m in messages if not m.info] or messages
        journal.note(asks[0].sender, "livraison", AGENT, issue, started, reason, asks[0].id, "sidecar")

    def watch_errors(self) -> None:
        """Demande livrée, sans réponse, dont la conversation s'est arrêtée sur une erreur APRÈS la livraison : l'évènement
        « arrêt sur erreur » le dit (track, wait_reply « unavailable ») au lieu de « lu, sans réponse » sans fin. Jamais
        de renvoi automatique : l'hôte renvoie, et le message suivant ouvre une conversation neuve."""
        horizon = self.clock() - WATCH_S
        for message_id, _, claim in self.box.all_claims():
            conversation = claim.get("conversation")
            if claim.get("owner") != "sidecar" or claim_label(claim) != "delivered" or not conversation:
                continue
            since = _epoch(str(claim.get("updated") or claim.get("at") or ""))
            if since < horizon or self.box.has_event(message_id, "arrêt sur erreur"):
                continue
            message = self.box.get(message_id)
            if message is None or any(r.sender == AGENT for r in self.box.replies(message_id)):
                continue
            halt = self.stopped(conversation)
            if halt and (halt[1] is None or halt[1] >= since - 5):
                self.box.event(message_id, "arrêt sur erreur", via="sidecar", conversation=conversation, reason=halt[0])
                journal.note(message.sender, "tour", AGENT, "✗", erreur=halt[0], appel=message_id, technique="sidecar")
                self.log(f"{message_id} : conversation {conversation} arrêtée sur une erreur ({halt[0]})")

    def _zombie(self, conversation: str) -> bool:
        try:
            finished, last = self.turn(conversation)
        except Exception:  # noqa: BLE001 - journal illisible : rien ne prouve qu'elle est figée
            return False
        return not finished and last is not None and self.clock() - last > ZOMBIE_S

    def _quota_exhausted(self) -> str | None:
        from .credits import cached_antigravity_usage, exhausted

        return exhausted("antigravity", cached_antigravity_usage(self.box))

    def step(self) -> list[dict[str, Any]]:
        if not self._recovered:
            self.recover()
            self._recovered = True
        self.box.set_state("sidecar-loop", {"at": now_iso()})  # boucle vivante (le battement seul ne le prouve pas)
        # Quota Gemini épuisé (relevé par le relais) : rien n'est livré, les messages attendent dans le salon au lieu
        # d'échouer en boucle la nuit (souhait d'Antigravity, 21/10).
        if reason := self.paused():
            self.box.set_state("sidecar-paused", {"at": now_iso(), "reason": reason})
            return []
        self.watch_errors()
        done = []
        now = self.clock()
        for topic, messages in self.pending().items():
            if any(now - self._failed.get(m.id, -RETRY_S) < RETRY_S for m in messages):
                continue
            self.box.set_state("sidecar-loop", {"at": now_iso()})  # tour 4 : chaque livraison peut durer ~2 min
            done.append(self.deliver(topic, messages))
        return done

    def run(self) -> None:
        self.log(f"nexus-relay sidecar {__version__} : livraison à Antigravity par agentapi")

        def heartbeat() -> None:  # battement même pendant un appel agentapi de 2 min (relecture du tour 2)
            while True:
                try:
                    self.box.set_state("sidecar", {"at": now_iso(), "pid": os.getpid()})
                except OSError:
                    pass  # battement manqué (fichier pris) : le suivant le remplace, le fil ne meurt jamais
                time.sleep(BEAT_S)

        threading.Thread(target=heartbeat, name="nexus-relay-sidecar-battement", daemon=True).start()
        loaded = code_fingerprint()
        try:
            mark_restartable("sidecar")
        except OSError:
            pass  # marque facultative : sans elle, install.ps1 arrête ce sidecar de force au lieu de le prévenir
        while True:
            try:
                self.step()
            except Exception as error:  # noqa: BLE001 - un pas raté n'arrête jamais le sidecar
                self.log(f"pas raté : {type(error).__name__}: {error}")
            if self.restart_due(loaded):
                return
            if self.host_gone():  # entre deux livraisons, comme la relance
                self.log("Antigravity fermé : sortie (sidecar sans hôte)")
                return
            time.sleep(POLL_S)

    def restart_due(self, loaded: str) -> bool:
        """Demande d'install.ps1, lue entre deux livraisons seulement (tour 5 : l'arrêt forcé pendant une livraison la
        laissait « incertaine », jamais rejouée) ; Antigravity relance le sidecar (restart_policy « always »)."""
        try:
            wanted = restart_wanted(relay_home() / "sidecar-restart.request", loaded)
        except OSError:
            return False
        if wanted:
            self.log("nouveau code : sortie pour relance par Antigravity")
        return wanted


def main() -> None:
    from .settings import apply

    apply()  # Same explicit host/workspace/cutoff settings as the shared daemon, including after a hot reload.
    data_dir = Path(os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR") or Path.home() / ".nexus" / "relay" / "sidecar")
    data_dir.mkdir(parents=True, exist_ok=True)
    log_file = data_dir / "sidecar.log"

    def log(line: str) -> None:
        stamped = f"{now_iso()} {line}"
        print(stamped, file=sys.stderr, flush=True)
        try:
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(stamped + "\n")
        except OSError:
            pass  # journal fichier indisponible : la ligne est déjà sur stderr

    Sidecar(Box(), AgentApi(resolve_command()), log, host_gone=watch_host()).run()


if __name__ == "__main__":
    main()
