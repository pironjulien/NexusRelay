"""La sonnette de Nexus Relay (accord « relais-v2 » du 19/10/2026, fiabilisée par l'accord « relais-ameliorer » du
20/10 : points B, B bis et G).

Le démon nexus-relay (le serveur MCP que Claude, Codex et Antigravity partagent) sonne lui-même le destinataire dès
qu'un message est déposé (seul Claude ouvre un échange) ; un fil par destinataire et un fil de rangement.

- Codex : port de débogage de son appli (cdp.wake_codex) : nouveau chat, message court qui cite les ids, Entrée.
- Antigravity : son sidecar (agentapi, la porte officielle), seule voie de livraison (l'utilisateur, 20/10 : le port 9227 sert
  au rangement, aux crédits et au relancement, plus à taper un message). Antigravity fermé : relancé.
- Claude n'est pas sonné : hôte et seul à ouvrir un échange, il attend les réponses (wait_reply) dans sa propre
  conversation (l'utilisateur, 20/10 ; son réveil par tâche planifiée échouait dès que l'utilisateur touchait la souris).

Pas de réveil : message « pour info », message à l'utilisateur, réponse qu'une session attend déjà (wait_reply en cours).
Une rafale est groupée en un seul réveil. Un réveil reporté ou raté est retenté de plus en plus rarement (20 s, puis
×1,618 jusqu'à 5 min), abandonné après GIVE_UP_S.
Un réveil réussi (« started ») que le destinataire ne lit pas dans CONFIRM_S est resonné, MÊME id, 2 fois au plus ;
compteur et échéance sont écrits dans le salon (rings/<id>.json) et survivent au redémarrage du démon. Un message lu
n'est jamais resonné, même sans réponse."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import journal
from .box import Box, Message, now_iso, pid_alive

WAKEABLE = ("codex", "antigravity")  # ceux qu'une appli permet de réveiller (Claude Code : aucune)
GROUP_S = 2.0
RETRY_S = 20.0
RETRY_MAX_S = 300.0
PHI = 1.618  # constante harmonique : chaque report allonge l'attente suivante
GIVE_UP_S = 3600.0
CONFIRM_S = 300.0  # réveil réussi sans lecture : nouvelle sonnette après 5 min
MAX_RINGS = 3  # le réveil, puis 2 nouvelles sonnettes au plus (point B)
SIDECAR_FRESH_S = 30.0  # battement du sidecar plus vieux : sidecar tenu pour mort
SIDECAR_LOOP_S = 300.0  # boucle principale du sidecar sans nouveau tour depuis 5 min : figée (appel bloqué)
TIDY_S = 10.0  # chats de réveil de Codex/Antigravity rangés 10 s après leur tour fini
# Une conversation par sujet (l'utilisateur, 21/10 : contexte et cache gardés d'une demande à l'autre) : rangée seulement quand
# l'hôte clôt le sujet (close) ou après IDLE_S sans message du sujet ; jamais après chaque réponse.
IDLE_S = 12 * 3600.0
AGY_ARCHIVE_TRIES = 3  # rangement de la discussion d'Antigravity : vrais échecs avant abandon (reports non comptés)
AGY_STALE_S = 7200.0  # dernière étape vieille de 2 h sans fin propre : tour tenu pour fini (une commande longue non)
AGY_GIVE_UP_S = 24 * 3600.0  # ligne jamais trouvée (« absent ») : abandon après 24 h, pas après 3 passages
TIDY = "rangement"  # nom du fil de rangement
STUCK_S = 420.0  # fil sans nouveau tour depuis 7 min : figé (une relance de Codex peut durer ~3 min)

Waker = Callable[..., dict[str, Any]]


class Activity:
    """Ce que le démon est en train de faire : appels MCP, réveils, rangements (tour 5 : la relance ne comptait que les
    appels MCP et coupait un réveil de Codex en pleine séquence, d'où un projet non rendu ou un réveil en double).
    Une fois fermé pour la relance, plus aucun réveil ni rangement ne commence."""

    def __init__(self) -> None:
        self.count = 0
        self.closed = False
        self._lock = threading.Lock()
        self._next = 0
        self._running: dict[int, tuple[str, float]] = {}  # jeton → (quoi, depuis) : ce qui retient une relance

    def enter(self, label: str = "?") -> int | None:
        """Jeton de l'activité (None : démon fermé pour relance, rien ne commence)."""
        with self._lock:
            if self.closed:
                return None
            self.count += 1
            self._next += 1
            self._running[self._next] = (label, time.time())
            return self._next

    def leave(self, token: int | None = None) -> None:
        with self._lock:
            self.count -= 1
            self._running.pop(token, None) if token is not None else None

    def running(self, now: float | None = None) -> list[dict[str, Any]]:
        """Activités en cours, la plus ancienne d'abord (21/10 : une relance restait retenue sans qu'on sache par quoi)."""
        now = time.time() if now is None else now
        with self._lock:
            items = sorted(self._running.values(), key=lambda item: item[1])
        return [{"quoi": label, "depuis_s": round(now - since, 1)} for label, since in items]

    def close_if_idle(self, force: bool = False) -> bool:
        with self._lock:
            if self.count == 0 or force:
                self.closed = True
            return self.closed


ACTIVE = Activity()


def _counted(action: Callable[[], Any], label: str = "pas de la sonnette") -> None:
    token = ACTIVE.enter(label)
    if token is None:
        return  # démon en arrêt pour relance : le pas reprendra au redémarrage (état dans le salon)
    try:
        action()
    finally:
        ACTIVE.leave(token)


WAITED: dict[str, float] = {}  # questions dont une session attend la réponse (wait_reply) → échéance
_WAITED_LOCK = threading.Lock()


# Réservations aussi écrites dans le salon (state/waited.json) : un wait_reply rendu « restarting » garde sa réponse
# réservée à travers la relance du démon (relecture de Codex, 21/10 : WAITED seul, en mémoire, se perdait).
def _persist(change: Callable[[dict[str, float]], object]) -> None:
    """Appelé SOUS _WAITED_LOCK (relecture d'Antigravity : verrou relâché entre la mémoire et le salon, un wait_on
    pouvait réécrire une réservation qu'un wait_done venait de retirer)."""
    box = Box()
    kept = {k: float(v) for k, v in (box.state("waited") or {}).items() if float(v) > time.time()}
    change(kept)
    box.set_state("waited", kept)


def wait_on(question_id: str, until: float) -> None:
    with _WAITED_LOCK:
        WAITED[question_id] = until
        _persist(lambda kept: kept.__setitem__(question_id, until))


def wait_done(question_id: str) -> None:
    with _WAITED_LOCK:
        WAITED.pop(question_id, None)
        _persist(lambda kept: kept.pop(question_id, None))


def _until(question_id: str) -> float | None:
    until = WAITED.get(question_id)
    if until is None:
        until = (Box().state("waited") or {}).get(question_id)
    return None if until is None else float(until)


def awaited(question_id: str | None, now: float | None = None) -> bool:
    if not question_id:
        return False
    now = time.time() if now is None else now
    with _WAITED_LOCK:
        until = _until(question_id)
    return until is not None and until > now


def unless_awaited(question_id: str, action: Callable[[], object]) -> bool:
    """Lance `action` SOUS le verrou des réservations si aucune attente ne réserve `question_id` (False sinon) : une
    collecte de réponses tardives ne prend pas celle qu'un wait_reply vient de réserver (relecture de Codex, 24/10)."""
    with _WAITED_LOCK:
        until = _until(question_id)
        if until is not None and until > time.time():
            return False
        action()
        return True


def wakeable() -> tuple[str, ...]:
    """Agents sonnés pour un message : les réveillables, sauf l'hôte (il attend ses réponses par wait_reply)."""
    from .paths import relay_host

    host = relay_host()
    return tuple(agent for agent in WAKEABLE if agent != host)


def needs_wake(message: Message) -> bool:
    return (
        message.to in wakeable() and message.sender != message.to and not message.info and not awaited(message.reply_to)
    )


def _codex(ids: list[str]) -> dict[str, Any]:
    from .cdp import wake_codex

    return wake_codex(ids)


def sidecar_alive(box: Box, now: float | None = None) -> bool:
    beat = box.state("sidecar") or {}
    now = time.time() if now is None else now
    fresh = now - _epoch(str(beat.get("at") or "")) < SIDECAR_FRESH_S
    loop = box.state("sidecar-loop") or {}
    looping = now - _epoch(str(loop.get("at") or beat.get("at") or "")) < SIDECAR_LOOP_S
    return fresh and looping and pid_alive(beat.get("pid"))


def _antigravity(ids: list[str]) -> dict[str, Any]:
    """Livraison par le sidecar seul ; Antigravity fermé : relancé (son sidecar repart avec lui)."""
    from . import cdp
    from .credits import exhausted

    if reason := exhausted("antigravity", Box().state("antigravity-usage")):
        return {"status": "deferred", "reason": f"quota épuisé, livraison en pause : {reason}"}
    # Figé (fenêtre « ne répond pas ») : le sidecar peut battre encore, mais agentapi ne livrerait rien (21/10, l'utilisateur :
    # relancer une IA qui ne répond plus, jamais pendant qu'il s'en sert).
    frozen = cdp.hung_for("antigravity", cdp.app_hung(cdp.AGY_PROCESS))
    if frozen >= cdp.HUNG_RESTART_S:
        return cdp.restart_frozen_antigravity()
    if sidecar_alive(Box()):
        return {
            "status": "started",
            "via": "sidecar",
            "confirmed": False,
            "note": "confié au sidecar (livraison : track)",
        }
    if cdp.targets(cdp.AGY_PORT):
        return {"status": "deferred", "reason": "Antigravity ouvert mais sidecar muet : il redémarre seul, réessai"}
    if cdp.user_in_agy():
        return {"status": "deferred", "reason": "Antigravity sans sidecar ni port, l'utilisateur y travaille"}
    return cdp.start_antigravity()


WAKERS: dict[str, Waker] = {"codex": _codex, "antigravity": _antigravity}


def peers_verdict(box: Box) -> dict[str, Any]:
    """Verdict du relais sur ses pairs, écrit pour la barre d'état de l'utilisateur (state/peers.json) : les MÊMES signaux que
    `status` et le réveil (Codex : page de son port 9223 et routeur prêt ; Antigravity : sidecar vivant ; quota épuisé
    connu), jamais une règle à part (l'utilisateur, 22/10 : le point de la barre restait vert Codex fermé)."""
    from . import cdp, credits

    def card(ok: bool, why: str | None) -> dict[str, Any]:
        return {"ok": ok, **({"why": why} if why else {})}

    codex_page, lb = cdp.page_url(), cdp.router_ready(1.0)
    codex_why = credits.availability(box, "codex") or (
        None if codex_page and lb else "appli Codex fermée" if not codex_page else "routeur arrêté"
    )
    agy_alive = sidecar_alive(box)
    agy_why = credits.availability(box, "antigravity") or (None if agy_alive else "sidecar muet")
    verdict = {
        "at": now_iso(),
        "codex": card(codex_why is None, codex_why),
        "antigravity": card(agy_why is None, agy_why),
    }
    from .paths import DEFAULT_HOST, relay_host

    if (
        relay_host() != DEFAULT_HOST
    ):  # par défaut, fichier inchangé (barre et pad) ; sinon le pad sait qui est au centre
        verdict["host"] = relay_host()
    return verdict


def retry_delay(state: dict[str, Any]) -> float:
    """Attente avant de retenter un réveil reporté ou raté : 20 s, puis ×1,618 à chaque nouveau report, 5 min au plus."""
    streak = max(1, int(state.get("streak") or 1))
    return min(RETRY_S * PHI ** (streak - 1), RETRY_MAX_S)


DONE = ("started", "uncertain")  # une sonnette a eu lieu (comptée)
# issue d'un réveil dans le journal commun (FORMAT.md) : reporté parce que l'utilisateur est là ou le jeton pris = différé
WAKE_ISSUES = {"started": "✓", "uncertain": "~", "deferred": "différé", "failed": "✗", "abandoned": "✗"}


class Bell:
    """Un fil par destinataire, dans le démon : rassemble les messages en attente et sonne jusqu'au succès."""

    def __init__(self, box: Box, wakers: dict[str, Waker] | None = None, clock: Callable[[], float] = time.time):
        self.box, self.wakers, self.clock = box, wakers or WAKERS, clock
        self._pending: dict[str, dict[str, float]] = {agent: {} for agent in WAKEABLE}
        self._watch: dict[str, dict[str, float]] = {agent: {} for agent in WAKEABLE}  # id → échéance de la relance
        self._lock = threading.Lock()
        # Un fil par destinataire et un pour le rangement (relectures du 20/10 : un seul fil pour tout, figé par un
        # réveil de Codex bloqué, arrêtait aussi Antigravity et les rangements, sans rien dire).
        self._events = {name: threading.Event() for name in (*WAKEABLE, TIDY)}
        self._threads: dict[str, threading.Thread] = {}
        self.beats: dict[str, float] = {}  # dernier tour de chaque fil (status : un fil figé se voit)

    def ring(self, message: Message) -> bool:
        if not needs_wake(message):
            return False
        with self._lock:
            self._pending[message.to].setdefault(message.id, self.clock())
        self._events[message.to].set()
        self.start()
        return True

    def tidy_soon(self) -> None:
        """Rangement au prochain tour du fil (sujet clos à l'instant)."""
        self._events[TIDY].set()
        self.start()

    def start(self) -> None:
        with self._lock:
            for name in (*WAKEABLE, TIDY):
                thread = self._threads.get(name)
                if thread is None or not thread.is_alive():
                    self._threads[name] = threading.Thread(
                        target=self._run, args=(name,), name=f"nexus-relay-{name}", daemon=True
                    )
                    self._threads[name].start()

    def _run(self, name: str) -> None:
        while True:
            self.beats[name] = time.time()
            if name == TIDY:
                self._events[name].wait(TIDY_S)
                self._events[name].clear()
                for label, tidy in (
                    ("tours de Codex", lambda: watch_codex(self.box)),
                    ("rangement Codex", lambda: tidy_codex(self.box)),
                    ("rangement Antigravity", lambda: tidy_antigravity(self.box)),
                    ("verdict des pairs", lambda: self.box.set_state("peers", peers_verdict(self.box))),
                ):
                    try:
                        _counted(tidy, label)
                    except Exception:  # noqa: BLE001 - rangement : réessayé au pas suivant
                        pass
                continue
            with self._lock:
                busy = bool(self._pending[name])
            self._events[name].wait(GROUP_S if busy else TIDY_S)
            self._events[name].clear()
            try:
                _counted(lambda: self.step(name), f"réveil de {name}")
            except Exception as error:  # noqa: BLE001 - un pas raté n'arrête jamais la sonnette
                self.box.set_state(f"wake-{name}", {"at": now_iso(), "status": "failed", "reason": repr(error)})

    def health(self, now: float | None = None) -> dict[str, Any]:
        """Âge du dernier tour de chaque fil : au-delà de STUCK_S, figé (un geste ne rend pas la main)."""
        now = time.time() if now is None else now
        ages = {name: round(now - at, 1) for name, at in self.beats.items()}
        missing = sorted(name for name in (*WAKEABLE, TIDY) if name not in ages)  # jamais démarré : pas « sain »
        return {
            "ages_s": ages,
            "stuck": sorted(name for name, age in ages.items() if age > STUCK_S),
            "missing": missing,
        }

    def _incident(self, message_id: str, ring: dict[str, Any], reason: str) -> None:
        self.box.set_ring_state(message_id, {**ring, "incident": reason, "incident_at": now_iso()})
        self.box.event(message_id, "incident", reason=reason)

    def _due(self, agent: str, now: float) -> None:
        """Relances échues : message toujours non lu → resonné (même id), au plus MAX_RINGS sonnettes en tout."""
        with self._lock:
            watched = list(self._watch[agent].items())
        for message_id, due in watched:
            message = self.box.get(message_id)
            if message is None or self.box.is_read(message):
                with self._lock:
                    self._watch[agent].pop(message_id, None)  # lu : jamais rejoué, même sans réponse
                continue
            if due > now:
                continue
            if awaited(message.reply_to):
                with self._lock:
                    self._watch[agent][message_id] = now + CONFIRM_S  # une session l'attend : elle le lira
                continue
            ring = self.box.ring_state(message_id) or {}
            with self._lock:
                self._watch[agent].pop(message_id, None)
            if int(ring.get("attempt") or 0) >= MAX_RINGS:
                self._incident(message_id, ring, f"non lu après {MAX_RINGS} réveils : prise en charge non confirmée")
                continue
            self.box.event(message_id, "nouvelle sonnette", attempt=int(ring.get("attempt") or 0) + 1)
            with self._lock:
                self._pending[agent].setdefault(message_id, now - GROUP_S)

    def step(self, agent: str) -> dict[str, Any] | None:
        """Un pas pour `agent` : relances échues, puis sonne si des messages attendent depuis GROUP_S et que le dernier
        essai est assez vieux."""
        now = self.clock()
        self._due(agent, now)
        with self._lock:
            pending = self._pending[agent]
            for message_id in [i for i in pending if (m := self.box.get(i)) is None or self.box.is_read(m)]:
                del pending[message_id]  # lu entre-temps (wait_reply, inbox) : plus rien à sonner
            if not pending:
                return None
            # Un sujet par réveil (une conversation par sujet chez le destinataire), en rotation : le sujet au plus
            # ancien message parmi ceux qui ne sont pas en pause ; la pause de reprise est PAR SUJET (relecture de
            # Codex du 21/10 : le chat de A « en plein tour » bloquait B, jamais tenté, puis A et B abandonnés).
            groups: dict[str, list[str]] = {}
            first_at: dict[str, float] = {}
            for message_id, at in pending.items():
                message = self.box.get(message_id)
                topic = message.topic if message else ""
                groups.setdefault(topic, []).append(message_id)
                first_at[topic] = min(first_at.get(topic, at), at)
        state = self.box.state(f"wake-{agent}") or {}
        holds: dict[str, Any] = dict(state.get("holds") or {})
        legacy = state if "holds" not in state else {}  # pause d'avant ce format : vaut pour tous les sujets

        def hold_of(topic: str) -> dict[str, Any]:
            return holds.get(topic) or legacy

        def paused(topic: str) -> bool:
            hold = hold_of(topic)
            return hold.get("status") in ("deferred", "failed") and now - float(hold.get("tried") or 0) < retry_delay(
                hold
            )

        ready = [t for t in sorted(groups, key=lambda t: (first_at[t], t)) if now - first_at[t] >= GROUP_S]
        topic = next((t for t in ready if not paused(t)), None)
        if topic is None:
            return None
        ids = sorted(groups[topic])
        oldest = first_at[topic]
        hold = hold_of(topic)
        held = hold.get("status") in ("deferred", "failed")
        if now - oldest > GIVE_UP_S:
            result: dict[str, Any] = {
                "status": "abandoned",
                "reason": "pas de réveil en 1 h : message laissé dans la boîte (non livré)",
            }
        else:
            tried_at = time.time()  # heure réelle avant le réveil : un tour commencé pendant ce réveil reste « après »
            from . import cdp

            activated = cdp.activations()
            try:
                result = self.wakers[agent](ids)
            except Exception as error:  # noqa: BLE001 - interface : jamais fatale au relais
                result = {"status": "failed", "reason": f"{type(error).__name__}: {error}"}
            result = {**result, "tried_at": round(tried_at, 3)}
            if cdp.activations() != activated:  # une fenêtre activée pendant ce réveil, réussi ou non
                result["activated"] = True
        status = result.get("status")
        first = self.box.get(ids[0])
        journal.note(
            first.sender if first else "claude",  # celle qui a demandé : le réveil agit pour elle
            "wake",
            agent,
            WAKE_ISSUES.get(str(status), "✗"),
            ms=(time.time() - float(result["tried_at"])) * 1000 if result.get("tried_at") else None,
            erreur=None if status == "started" else result.get("reason"),
            appel=ids[0],
            technique=result.get("via"),
            premier_plan=bool(result.get("activated")),  # compté à la source (cdp.ACTIVATIONS), pas « launched »
        )
        streak = int(hold.get("streak") or 1) + 1 if held and status in ("deferred", "failed") else 1
        holds = {t: h for t, h in holds.items() if t in groups and t != topic}  # sujets sans attente : pause oubliée
        holds[topic] = {"status": status, "tried": now, "streak": streak, "reason": result.get("reason")}
        record = {**result, "at": now_iso(), "tried": now, "messages": ids, "streak": streak, "topic": topic}
        record["holds"] = holds
        self.box.set_state(f"wake-{agent}", record)
        for message_id in ids:
            last = next((e for e in reversed(self.box.events(message_id)) if e.get("kind") == "réveil"), None)
            same = last is not None and (last.get("status"), last.get("reason")) == (status, result.get("reason"))
            if same and status in ("deferred", "failed"):
                continue  # même report que le précédent : le compte est dans wake-<agent> (streak), pas 87 lignes
            self.box.event(
                message_id,
                "réveil",
                status=status,
                reason=result.get("reason"),
                via=result.get("via"),
                **({"conversation": result["conversation"]} if result.get("conversation") else {}),
                **({"tried_at": result["tried_at"]} if result.get("tried_at") else {}),
            )
        if status in DONE:
            for message_id in ids:
                ring = self.box.ring_state(message_id) or {}
                due = now + CONFIRM_S
                self.box.set_ring_state(
                    message_id,
                    {
                        "agent": agent,
                        "attempt": int(ring.get("attempt") or 0) + 1,
                        "max": MAX_RINGS,
                        "rung_at": now_iso(),
                        "recheck_at": due,
                        "via": result.get("via"),
                        "status": status,
                    },
                )
                with self._lock:
                    self._pending[agent].pop(message_id, None)
                    self._watch[agent][message_id] = due
        elif status == "abandoned":
            for message_id in ids:
                self._incident(message_id, self.box.ring_state(message_id) or {}, result["reason"])
                with self._lock:
                    self._pending[agent].pop(message_id, None)
        return record

    def recover(self) -> int:
        """Démon relancé : les messages non lus reprennent où ils en étaient (compteur et échéance du salon)."""
        count = 0
        now = self.clock()
        for agent in WAKEABLE:
            for message in self.box.unread(agent):
                if now - _epoch(message.created) >= GIVE_UP_S or not needs_wake(message):
                    continue
                ring = self.box.ring_state(message.id)
                if ring is None:  # salon d'avant le 20/10 : un réveil réussi vaut une sonnette
                    rung = any(
                        e.get("kind") == "réveil" and e.get("status") == "started" for e in self.box.events(message.id)
                    )
                    ring = {"attempt": 1, "recheck_at": now + CONFIRM_S} if rung else {}
                if ring.get("incident"):
                    continue
                attempt = int(ring.get("attempt") or 0)
                with self._lock:
                    if attempt == 0:
                        self._pending[agent].setdefault(message.id, now - GROUP_S)
                    else:
                        self._watch[agent][message.id] = float(ring.get("recheck_at") or now + CONFIRM_S)
                count += 1
        if count:
            for event in self._events.values():
                event.set()
            self.start()
        return count

    def pending(self, agent: str) -> list[str]:
        with self._lock:
            return sorted(self._pending.get(agent, {}))

    def watching(self, agent: str) -> dict[str, float]:
        with self._lock:
            return dict(self._watch.get(agent, {}))


def _epoch(stamp: str) -> float:
    import calendar

    try:
        return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return 0.0


def codex_topics(box: Box) -> dict[str, str]:
    """Chat de Codex → sujet, d'après la conversation mémorisée de chaque sujet (cdp.wake_codex)."""
    out: dict[str, str] = {}
    topics_dir = box.root / "topics"
    for topic_dir in sorted(topics_dir.iterdir()) if topics_dir.is_dir() else []:
        conversation = (box.topic_state(topic_dir.name, "codex") or {}).get("conversation")
        if conversation:
            out[str(conversation)] = topic_dir.name
    return out


CODEX_SILENT_S = 1200.0  # tour de Codex sans ligne de journal depuis 20 min : figé (il écrit en continu)
CODEX_END_GRACE_S = 5.0  # fin de tour toute fraîche : la réponse peut être en train d'arriver
CODEX_WATCH_S = 6 * 3600.0  # demandes plus vieilles : plus surveillées
CODEX_WAKE_SLACK_S = 60.0  # réveil d'avant ce format (sans tried_at) : le tour a pu commencer pendant le réveil


def _resumed(events: list[dict[str, Any]]) -> bool:
    """Signe de vie après le dernier « arrêt sur erreur » : progress du pair ou nouveau réveil (renvoi, sonnette)."""
    at = next((i for i in range(len(events) - 1, -1, -1) if events[i].get("kind") == "arrêt sur erreur"), None)
    return at is not None and any(
        e.get("kind") == "en cours" or (e.get("kind") == "réveil" and e.get("status") in DONE) for e in events[at + 1 :]
    )


def watch_codex(box: Box, now: float | None = None) -> list[str]:
    """Demandes à Codex PRISES EN CHARGE (lues, ou « en cours ») dont le tour s'est fini sans réponse (filtre de
    contenu, erreur, abandon) ou n'écrit plus rien depuis CODEX_SILENT_S (appli figée) : évènement « arrêt sur
    erreur » et incident (status, track, wait_reply « unavailable »), puis le chat du sujet est oublié pour qu'un
    renvoi parte dans un chat neuf. Non lue : les sonnettes s'en chargent. Jamais de renvoi automatique : l'hôte
    renvoie, ou confie la demande à Antigravity. Vu le 24/10 : filtre de Codex, « en cours » affiché 40 min."""
    from . import codex_app

    now = time.time() if now is None else now
    since = now - CODEX_WATCH_S
    # une lecture par passage : réponses de Codex de la fenêtre (relecture de Codex : 2 009 JSON relus par passage)
    answered = {m.reply_to for m in box.sent_by("codex", since=since) if m.reply_to}
    paths: dict[str, Path | None] = {}
    flagged = []
    recent = box.addressed_to("codex", since=since)
    # dernier réveil de chaque chat, toutes demandes confondues : un chat réveillé depuis pour une autre demande vit
    # encore, il n'est jamais oublié (vu en vrai le 24/10 : le chat nexuswindows d'une autre session l'a été)
    last_wake: dict[str, float] = {}
    for other in recent:
        for event in box.events(other.id):
            conv = event.get("conversation")
            if event.get("kind") == "réveil" and event.get("status") in DONE and conv:
                at = float(event.get("tried_at") or _epoch(str(event.get("at"))))
                last_wake[str(conv)] = max(last_wake.get(str(conv), 0.0), at)
    for message in recent:
        if message.info or message.id in answered:
            continue
        ring = box.ring_state(message.id) or {}
        events = box.events(message.id)
        if ring.get("incident"):
            if ring.get("halt") and _resumed(events):  # il a repris : incident levé (relecture de Codex)
                box.set_ring_state(
                    message.id, {k: v for k, v in ring.items() if k not in ("incident", "incident_at", "halt")}
                )
            continue
        woken = next((e for e in reversed(events) if e.get("kind") == "réveil" and e.get("status") in DONE), None)
        taken = box.is_read(message) or any(e.get("kind") == "en cours" for e in events)
        if woken is None or not taken:
            continue
        conversation = woken.get("conversation") or (box.topic_state(message.topic, "codex") or {}).get("conversation")
        if not conversation:
            continue
        if conversation not in paths:
            paths[str(conversation)] = codex_app.chat_path(str(conversation))
        path = paths[str(conversation)]
        start = float(woken.get("tried_at") or _epoch(str(woken.get("at"))) - CODEX_WAKE_SLACK_S)
        if path is None:
            continue
        turn = codex_app.turn_after(path, start - 2)
        if turn is None:
            continue
        if turn["ended"] and now - float(turn["at"] or now) >= CODEX_END_GRACE_S:
            reason = turn["error"] or "tour fini sans réponse"
        elif not turn["ended"] and turn["last"] is not None and now - turn["last"] > CODEX_SILENT_S:
            reason = f"tour muet depuis {int((now - turn['last']) // 60)} min (appli figée ?)"
        else:
            continue
        # revalidé juste avant d'écrire (relecture de Codex : un progress, une réponse ou un nouveau réveil arrivés
        # pendant le passage faisaient déclarer arrêté un chat vivant) : rien n'a bougé depuis le cliché
        if (
            len(box.events(message.id)) != len(events)
            or any(r.sender == "codex" for r in box.replies(message.id))
            or codex_app.turn_after(path, start - 2) != turn
        ):
            continue
        box.event(message.id, "arrêt sur erreur", via="journal Codex", conversation=conversation, reason=reason)
        journal.note(message.sender, "tour", "codex", "✗", erreur=reason, appel=message.id, technique="journal Codex")
        box.set_ring_state(
            message.id,
            {
                **ring,
                "agent": "codex",
                "incident": f"codex s'est arrêté : {reason}",
                "incident_at": now_iso(),
                "halt": True,
            },
        )
        state = box.topic_state(message.topic, "codex") or {}
        if (
            woken.get(
                "conversation"
            )  # réveil d'avant ce format : chat déduit du sujet, jamais oublié sur une déduction
            and state.get("conversation") == conversation
            and last_wake.get(str(conversation), 0.0) <= start + 1
        ):
            box.set_topic_state(
                message.topic, "codex", {"conversation": None, "dropped": conversation, "at": now_iso(), "why": reason}
            )
        flagged.append(message.id)
    return flagged


def tidy_codex(box: Box | None = None, now: float | None = None) -> dict[str, Any]:
    """Range les chats du relais dans Codex dont le tour est fini depuis TIDY_S, chacun par l'id de sa ligne ; jamais
    quand l'utilisateur est dans Codex ; après un VRAI échec, pause croissante (un report ne compte pas : tour 2)."""
    from . import cdp, codex_app

    box = box or Box()
    fixed = now is not None  # heure imposée (tests) : aussi pour l'éligibilité relue avant chaque clic
    now = time.time() if now is None else now
    chats = codex_app.relay_chats(now=now)

    # Chat d'un sujet encore vivant (ni clos, ni 12 h sans message) : gardé, avec son contexte ; un chat sans sujet
    # connu (réveils d'avant le 21/10) : rangé une fois fini, comme avant.
    def eligible(chat: codex_app.Chat, at: float | None = None) -> bool:
        topics = codex_topics(box)  # relu à chaque appel : un sujet rouvert entre-temps n'est pas rangé
        return chat.id not in topics or box.topic_idle(topics[chat.id], time.time() if at is None else at, IDLE_S)

    chats = [c for c in chats if eligible(c, now)]
    if not any(codex_app.ready(c, now, TIDY_S) for c in chats):
        return {}
    state = box.state("codex-tidy") or {}
    if now < float(state.get("next_at") or 0):
        return {}
    if cdp.user_in_codex():
        return {"status": "deferred", "reason": "L'utilisateur est dans Codex"}
    result = codex_app.archive_chats(
        chats, grace_s=TIDY_S, eligible=lambda chat: eligible(chat, now if fixed else None)
    )
    status = result.get("status")
    failures = int(state.get("failures") or 0)
    if status == "archived":
        failures = 0
    elif status == "failed":
        failures += 1
    delay = min(RETRY_S * PHI ** (failures - 1), 3600.0) if status == "failed" else 0.0
    if status in ("nothing", "deferred", "skipped"):
        delay = 60.0  # chats prêts mais lignes cachées ou l'utilisateur là : pas un échec, mais pas toutes les 10 s (tour 3)
    box.set_state("codex-tidy", {**result, "at": now_iso(), "failures": failures, "next_at": now + delay})
    return result


def _tidy_retired(box: Box, topic: str, now: float) -> None:
    """Conversations d'Antigravity REMPLACÉES dans ce sujet (cassée, figée, refusée) : rangées dès que leur tour n'avance
    plus, sans attendre la fin du sujet ; jamais l'utilisateur dans l'appli. Disparues ou déjà archivées : oubliées."""
    from . import cdp
    from .paths import agy_turn, conversation_alive

    retired = (box.topic_state(topic, "agy_retired") or {}).get("conversations") or []
    if not retired:
        return
    left = []
    for conversation in retired:
        if not conversation_alive(conversation):
            continue

        def ready(conversation: str = conversation) -> str | None:
            finished, last = agy_turn(conversation)
            if not finished and last is not None and now - last < AGY_STALE_S:
                return "Antigravity travaille encore"
            return "L'utilisateur est dans Antigravity" if cdp.user_in_agy() else None

        if ready():
            left.append(conversation)
            continue
        try:
            status, _ = cdp.archive_antigravity_conversation(conversation, ready)
        except Exception:  # noqa: BLE001 - réessayé au pas suivant
            status = "failed"
        if status != "clicked":
            left.append(conversation)
    box.set_topic_state(topic, "agy_retired", {"conversations": left})


def tidy_antigravity(box: Box | None = None, now: float | None = None, verify_s: float = 8.0) -> dict[str, Any]:
    """Range la conversation d'Antigravity de chaque sujet fini, par son identifiant, quand son tour est fini depuis
    TIDY_S ; vérifié (conversation archivée) ; un report (l'utilisateur dans l'appli, port fermé, port occupé) ne compte pas
    comme un échec (relecture de Codex du 20/10 : trois reports abandonnaient le rangement pour toujours)."""
    from . import cdp
    from .paths import agy_turn, conversation_alive

    box = box or Box()
    fixed = now is not None  # heure imposée (tests) : aussi pour la garde relue avant le clic
    now = time.time() if now is None else now
    results: dict[str, Any] = {}
    topics_dir = box.root / "topics"
    for topic_dir in sorted(topics_dir.iterdir()) if topics_dir.is_dir() else []:
        topic = topic_dir.name
        _tidy_retired(box, topic, now)
        conversation = (box.topic_state(topic, "agy") or {}).get("conversation")
        done = box.topic_state(topic, "agy_archived") or {}
        if not conversation or done.get("conversation") == conversation:
            continue
        if not conversation_alive(conversation):
            box.set_topic_state(topic, "agy_archived", {"at": now_iso(), "conversation": conversation, "by": "déjà"})
            continue

        def ready(topic: str = topic, conversation: str = conversation) -> str | None:
            if not box.topic_finished_for("antigravity", topic):
                return "échange pas fini"
            if not box.topic_idle(topic, now if fixed else time.time(), IDLE_S):
                return "sujet actif (ni clos, ni 12 h sans message)"
            finished, last = agy_turn(conversation)
            moment = now if fixed else time.time()
            stale = last is not None and moment - last > AGY_STALE_S  # tour coupé sans fin propre (tour 3)
            if not stale and (not finished or last is None or moment - last < TIDY_S):
                return "Antigravity travaille encore"
            if cdp.user_in_agy():
                return "L'utilisateur est dans Antigravity"
            return None

        if ready():
            continue
        try:
            status, reason = cdp.archive_antigravity_conversation(conversation, ready)
        except Exception as error:  # noqa: BLE001 - interface muette : échec noté, les autres sujets continuent
            status, reason = "failed", f"{type(error).__name__}: {error}"
        if status == "clicked":
            deadline = time.monotonic() + verify_s
            while conversation_alive(conversation) and time.monotonic() < deadline:
                time.sleep(0.4)
            status = "archived" if not conversation_alive(conversation) else "failed"
            reason = None if status == "archived" else "toujours active après « Archive »"
        results[topic] = {"status": status, "reason": reason}
        first = box.topic_state(topic, "agy_archive_first") or {}
        if first.get("conversation") != conversation:
            first = {"conversation": conversation, "at": now}
            box.set_topic_state(topic, "agy_archive_first", first)
        if status == "absent" and now - float(first.get("at") or now) < AGY_GIVE_UP_S:
            continue  # ligne pas affichée (autre projet ouvert, barre repliée) : report, abandon après 24 h (tour 2)
        if status == "skipped":
            continue  # report : réessayé au pas suivant, sans compter
        previous = box.topic_state(topic, "agy_archive_tries") or {}
        tries = int(previous.get("count", 0)) + 1 if previous.get("conversation") == conversation else 1
        if status == "archived" or tries >= AGY_ARCHIVE_TRIES:
            box.set_topic_state(
                topic, "agy_archived", {"at": now_iso(), "conversation": conversation, "tries": tries, **results[topic]}
            )
        else:
            box.set_topic_state(
                topic,
                "agy_archive_tries",
                {"count": tries, "at": now_iso(), "conversation": conversation, **results[topic]},
            )
    return results
