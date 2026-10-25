"""Le salon commun de Nexus Relay (accord Claude–Antigravity « relais-v2 » du 19/10/2026).

Un fichier JSON IMMUABLE par message, créé d'un coup (fichier temporaire puis os.replace sous un nom neuf) : trois
écrivains simultanés ne se gênent jamais, aucun verrou n'est nécessaire. Tout ce qui change ensuite (lu, livré,
réveils) est un fichier-marqueur à part, lui aussi créé d'un coup. Le Markdown du sujet est régénéré pour l'utilisateur.

    <racine>/messages/<id>.<de>.<à>.<sujet>.json   message (id, from, to, topic, created, text, reply_to, info, vote) ;
                                     le nom suffit pour trier et filtrer sans ouvrir les fichiers
    <racine>/read/<agent>/<id>       lu par son destinataire
    <racine>/events/<id>/<n>.json    suivi : livré, réveil tenté, pris en charge
    <racine>/topics/<sujet>/*.json   état du sujet (débat, conversation d'Antigravity), un seul écrivain chacun
    <racine>/state/<nom>.json        état d'un service (réveils), un seul écrivain
    <racine>/rings/<id>.json         sonnettes d'un message : compteur et échéance (le démon, seul écrivain)
    <racine>/claims/<id>/<n>.json    voie propriétaire de la livraison n d'un message à Antigravity (créée d'un coup,
                                     O_EXCL : un seul propriétaire ; réécrite par lui seul)
    <racine>/salon/<sujet>.md        le sujet lisible par l'utilisateur
    <racine>/attachments/<id>/<n>-<nom>  pièces jointes du message, copiées avant lui (extra["attachments"])

Le texte d'un message est une donnée, jamais exécutée ; il n'est jamais injecté dans une interface.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import stat
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .paths import USER, normalize_party, relay_home

TOPIC = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
MESSAGE_ID = re.compile(r"^\d{8}T\d{9}-[0-9a-f]{6}$")
MAX_TEXT = 100_000
VOTES = ("accord", "désaccord")
# pièces jointes (24/10) : copiées dans la boîte à l'envoi, immuables comme le message, lues par read
MAX_ATTACHMENTS = 8
MAX_ATTACHMENT = 10 * 1024 * 1024
MAX_ATTACHMENTS_TOTAL = 25 * 1024 * 1024


class RelayError(ValueError):
    pass


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id() -> str:
    stamp = time.time()
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(stamp)) + f"{int(stamp * 1000) % 1000:03d}-{secrets.token_hex(3)}"


def check_topic(topic: str | None) -> str:
    topic = (topic or "").strip().lower()
    if not TOPIC.match(topic):
        raise RelayError("sujet obligatoire : minuscules, chiffres et tirets (ex. « nexuswindows », « relais »)")
    return topic


def write_new(path: Path, data: dict[str, Any]) -> None:
    """Crée `path` d'un coup ; refuse s'il existe déjà (un message ne se réécrit jamais)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RelayError(f"{path.name} existe déjà")
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def write_state(path: Path, data: dict[str, Any]) -> None:
    """Réécrit un état (un seul écrivain par fichier) d'un coup : un lecteur voit l'ancien ou le nouveau, jamais un mélange."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def pid_alive(pid: int | None) -> bool:
    """Le processus `pid` tourne-t-il encore ? (propriétaire d'une livraison mort = arrêt brutal)."""
    if not pid:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def _plain(path: str) -> str:
    """Chemin comparable : sans préfixe \\\\?\\ (ni \\\\?\\UNC\\), séparateurs et casse de Windows."""
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normcase(os.path.normpath(path))


def _vaults() -> list[str]:
    """Coffres de secrets (NEXUS_RELAY_VAULTS, dossiers séparés par « ; » sous Windows) : jamais une pièce jointe.
    Relecture de Codex du 24/10."""
    roots = [entry.strip() for entry in os.environ.get("NEXUS_RELAY_VAULTS", "").split(os.pathsep) if entry.strip()]
    return sorted({_plain(os.path.realpath(Path(root).expanduser())) for root in roots})


def _opened_path(fd: int) -> str:
    """Chemin réel du fichier OUVERT (jonctions, liens et préfixes résolus par Windows) : le contrôle porte sur ce qui
    est lu, pas sur le nom donné (course entre contrôle et copie, relecture de Codex du 24/10)."""
    if os.name != "nt":
        return os.path.realpath(f"/proc/self/fd/{fd}")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetFinalPathNameByHandleW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    size = kernel.GetFinalPathNameByHandleW(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
    if not size or size >= len(buffer):
        raise RelayError("pièce jointe : chemin réel illisible")
    return buffer.value


def check_files(files: list[str]) -> list[Path]:
    """Noms des fichiers joints : chemins absolus, en nombre borné. Le reste (fichier ordinaire, hors coffre, taille)
    se contrôle sur le fichier ouvert, par read_attachment."""
    if len(files) > MAX_ATTACHMENTS:
        raise RelayError(f"pièces jointes : {MAX_ATTACHMENTS} au plus")
    paths = []
    for name in files:
        path = Path(name).expanduser()
        if not path.is_absolute():
            raise RelayError(f"pièce jointe : chemin absolu attendu ({name})")
        paths.append(path)
    return paths


def read_attachment(path: Path, budget: int) -> bytes:
    """Contenu d'une pièce jointe, lu par UNE ouverture : fichier ordinaire, un seul nom (un lien dur vers le coffre
    porterait un autre nom), chemin réel hors du coffre, au plus min(MAX_ATTACHMENT, budget) octets lus."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise RelayError(f"pièce jointe introuvable ou illisible : {path} ({error.strerror})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RelayError(f"pièce jointe : pas un fichier ordinaire ({path.name})")
        if info.st_nlink > 1:
            raise RelayError(f"pièce jointe refusée : fichier à plusieurs noms (lien dur) ({path.name})")
        opened = _opened_path(fd)
        if opened.startswith("\\\\?\\UNC\\") or (opened.startswith("\\\\") and not opened.startswith("\\\\?\\")):
            # \\localhost\C$\… mène au coffre sans lettre de lecteur (relecture d'Antigravity du 24/10) ; les trois
            # agents sont sur ce PC : aucune pièce jointe ne vient d'un partage
            raise RelayError(f"pièce jointe refusée : partage réseau ({path.name})")
        real = _plain(opened)
        if any(real == vault or real.startswith(vault + os.sep) for vault in _vaults()):
            raise RelayError(f"pièce jointe refusée : coffre de secrets ({path.name})")
        limit = min(MAX_ATTACHMENT, budget)
        chunks, read = [], 0
        while chunk := os.read(fd, min(1 << 20, limit + 1 - read)):
            chunks.append(chunk)
            read += len(chunk)
            if read > limit:
                if limit < MAX_ATTACHMENT:
                    raise RelayError(f"pièces jointes trop grosses ensemble ({MAX_ATTACHMENTS_TOTAL} octets au plus)")
                raise RelayError(f"pièce jointe trop grosse : {path.name} ({MAX_ATTACHMENT} octets au plus)")
        return b"".join(chunks)
    finally:
        os.close(fd)


def claim_label(claim: dict[str, Any]) -> str:
    """Phase lisible d'une livraison : « incertaine » si son propriétaire est mort pendant l'injection."""
    phase = str(claim.get("phase") or "claimed")
    if phase == "submitting" and not pid_alive(claim.get("pid")):
        return "uncertain"
    return phase


@dataclass(frozen=True)
class Message:
    id: str
    sender: str
    to: str
    topic: str
    created: str
    text: str
    reply_to: str | None = None
    info: bool = False
    vote: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["from"] = data.pop("sender")
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Message:
        return cls(
            id=str(data["id"]),
            sender=str(data["from"]),
            to=str(data["to"]),
            topic=str(data["topic"]),
            created=str(data["created"]),
            text=str(data["text"]),
            reply_to=data.get("reply_to"),
            info=bool(data.get("info")),
            vote=data.get("vote"),
            extra=dict(data.get("extra") or {}),
        )


class Box:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or relay_home() / "v1"

    # --- messages
    def _folder(self) -> Path:
        return self.root / "messages"

    def _find(self, message_id: str) -> Path | None:
        if not MESSAGE_ID.match(message_id):
            raise RelayError(f"identifiant de message invalide : {message_id!r}")
        return next(iter(self._folder().glob(f"{message_id}.*.json")), None)

    def _load(self, paths: list[Path]) -> list[Message]:
        return [Message.from_json(data) for path in sorted(paths) if (data := read_json(path))]

    def post(
        self,
        sender: str,
        to: str,
        text: str,
        topic: str,
        reply_to: str | None = None,
        info: bool = False,
        vote: str | None = None,
        extra: dict[str, Any] | None = None,
        files: list[str] | None = None,
    ) -> Message:
        sender, to, topic = normalize_party(sender), normalize_party(to), check_topic(topic)
        if sender == to:
            raise RelayError("on n'écrit pas à soi-même")
        if not text.strip():
            raise RelayError("message vide")
        if len(text) > MAX_TEXT:
            raise RelayError(
                f"message trop long ({len(text)} caractères, {MAX_TEXT} au plus) : mets le détail dans un fichier"
            )
        if vote is not None and vote not in VOTES:
            raise RelayError(f"vote : {' ou '.join(VOTES)}")
        sources = check_files(files or [])
        contents, budget = [], MAX_ATTACHMENTS_TOTAL
        for path in sources:  # tout est lu et contrôlé AVANT d'écrire quoi que ce soit
            contents.append((path, data := read_attachment(path, budget)))
            budget -= len(data)
        message_id, extra = new_id(), dict(extra or {})
        folder = self.root.absolute() / "attachments" / message_id  # racine relative : chemins rendus absolus
        try:
            if contents:
                extra["attachments"] = [self._attach(folder, n, *item) for n, item in enumerate(contents, 1)]
            message = Message(message_id, sender, to, topic, now_iso(), text, reply_to, info, vote, extra)
            write_new(self._folder() / f"{message.id}.{sender}.{to}.{topic}.json", message.to_json())
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)  # jamais une pièce jointe sans son message
            raise
        self.render(topic)
        return message

    @staticmethod
    def _attach(folder: Path, number: int, source: Path, data: bytes) -> dict[str, Any]:
        """Copie immuable dans la boîte des octets contrôlés (l'original peut changer ou disparaître)."""
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.\-]+", "_", source.name)[:96] or "fichier"
        target = folder / f"{number:02d}-{safe}"
        with open(target, "xb") as out:
            out.write(data)
        return {
            "name": source.name,
            "path": str(target),
            "size": len(data),
            "mime": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def get(self, message_id: str) -> Message | None:
        path = self._find(message_id)
        data = read_json(path) if path else None
        return Message.from_json(data) if data else None

    def all(self) -> list[Message]:
        return self._load(list(self._folder().glob("*.json")))

    def _since(self, pattern: str, since: float) -> list[Message]:
        """Messages dont le NOM correspond, déposés depuis `since` (époque Unix) : seuls ceux-là sont ouverts."""
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(since)) if since > 0 else ""
        return self._load([p for p in self._folder().glob(pattern) if p.name[:15] >= stamp])

    def addressed_to(self, agent: str, since: float = 0.0) -> list[Message]:
        return self._since(f"*.*.{normalize_party(agent)}.*.json", since)

    def sent_by(self, agent: str, since: float = 0.0) -> list[Message]:
        return self._since(f"*.{normalize_party(agent)}.*.*.json", since)

    def topic_messages(self, topic: str) -> list[Message]:
        return self._load(list(self._folder().glob(f"*.*.*.{check_topic(topic)}.json")))

    def new_in_topic(self, topic: str, seen: set[str]) -> list[Message]:
        """Messages du sujet pas encore vus (noms de fichiers ajoutés à `seen`) : on n'ouvre que les nouveaux."""
        fresh = [p for p in self._folder().glob(f"*.*.*.{check_topic(topic)}.json") if p.name not in seen]
        seen.update(p.name for p in fresh)
        return self._load(fresh)

    def replies(self, message_id: str) -> list[Message]:
        original = self.get(message_id)
        pool = self.topic_messages(original.topic) if original else self.all()
        return [m for m in pool if m.reply_to == message_id]

    # --- lu / non lu
    def _read_mark(self, agent: str, message_id: str) -> Path:
        return self.root / "read" / agent / message_id

    def is_read(self, message: Message) -> bool:
        return self._read_mark(message.to, message.id).exists()

    def mark_read(self, message: Message) -> None:
        mark = self._read_mark(message.to, message.id)
        if not mark.exists():
            # d'un coup (relecture de Codex, 24/10) : un marqueur créé puis une écriture en échec (disque plein)
            # faisait passer pour lue une réponse jamais rendue
            mark.parent.mkdir(parents=True, exist_ok=True)
            tmp = mark.with_name(f".{mark.name}.{secrets.token_hex(4)}.tmp")
            tmp.write_text(now_iso(), encoding="utf-8")
            os.replace(tmp, mark)

    def unread(self, agent: str) -> list[Message]:
        agent = normalize_party(agent)
        marks = self.root / "read" / agent
        done = {p.name for p in marks.iterdir()} if marks.is_dir() else set()
        paths = [p for p in self._folder().glob(f"*.*.{agent}.*.json") if p.name.split(".", 1)[0] not in done]
        return self._load(paths)

    # --- suivi
    def event(self, message_id: str, kind: str, **data: Any) -> None:
        folder = self.root / "events" / message_id
        write_new(folder / f"{time.time_ns()}-{secrets.token_hex(2)}.json", {"at": now_iso(), "kind": kind, **data})

    def events(self, message_id: str) -> list[dict[str, Any]]:
        folder = self.root / "events" / message_id
        return [e for p in sorted(folder.glob("*.json")) if (e := read_json(p))] if folder.is_dir() else []

    def has_event(self, message_id: str, kind: str) -> bool:
        return any(e.get("kind") == kind for e in self.events(message_id))

    # --- états (un seul écrivain par fichier)
    def topic_state(self, topic: str, name: str) -> dict[str, Any] | None:
        return read_json(self.root / "topics" / check_topic(topic) / f"{name}.json")

    def set_topic_state(self, topic: str, name: str, data: dict[str, Any]) -> None:
        write_state(self.root / "topics" / check_topic(topic) / f"{name}.json", data)

    def project_topic(self, topic: str | None) -> str:
        """Sujet = PROJET, jamais une sous-tâche (l'utilisateur, 23/10 : une conversation par question dans Antigravity).
        Un sujet nouveau qui prolonge un sujet existant (tirets ignorés : « relais-cartes » → « relais »,
        « nexuswindows-banc » → « nexuswindows », « nexusandroid-sota » → « nexus-android ») est ramené au plus long de
        ceux-là ; même nom à un tiret près (« nexus-windows » / « nexuswindows ») : le plus récemment servi."""
        topic = check_topic(topic)
        if topic == "selftest":
            return topic

        def key(name: str) -> str:
            return name.replace("-", "")

        dossier = self.root / "topics"
        connus = [p.name for p in dossier.iterdir() if p.is_dir() and p.name != "selftest"] if dossier.is_dir() else []
        parents = [t for t in connus if len(key(t)) >= 5 and key(topic) != key(t) and key(topic).startswith(key(t))]
        if parents:
            # parent lui-même ramené à son propre parent s'il en a un (« relais-v2-cartes » → « relais »)
            return self.project_topic(max(parents, key=lambda t: len(key(t))))
        jumeaux = [t for t in connus if key(t) == key(topic)]
        if len(jumeaux) > 1 or (jumeaux and jumeaux[0] != topic):
            return max(jumeaux, key=lambda t: self.topic_last_at(t) or 0.0)
        return topic

    def topic_last_at(self, topic: str) -> float | None:
        """Heure (époque Unix) du dernier message du sujet, lue dans les noms de fichiers (id horodaté en UTC)."""
        import calendar

        stamps = [p.name[:15] for p in self._folder().glob(f"*.*.*.{check_topic(topic)}.json")]
        if not stamps:
            return None
        try:
            return float(calendar.timegm(time.strptime(max(stamps), "%Y%m%dT%H%M%S")))
        except ValueError:
            return None

    def topic_closed(self, topic: str) -> bool:
        """Sujet clos par l'hôte (outil close) et pas rouvert depuis par un nouveau message."""
        return bool((self.topic_state(topic, "closed") or {}).get("closed"))

    def topic_idle(self, topic: str, now: float, idle_s: float) -> bool:
        """Conversation du sujet à ranger : sujet clos, ou aucun message depuis `idle_s`."""
        if self.topic_closed(topic):
            return True
        last = self.topic_last_at(topic)
        return last is not None and now - last >= idle_s

    def topic_finished_for(self, agent: str, topic: str) -> bool:
        """True si `agent` a répondu à tous les messages qui lui étaient adressés sur `topic`."""
        agent_norm = normalize_party(agent)
        top = check_topic(topic)
        if any(m.topic == top for m in self.unread(agent_norm)):
            return False
        messages = self.topic_messages(top)
        asks = [m for m in messages if m.to == agent_norm and not m.info]
        if not asks:
            return False
        return all(any(r.sender == agent_norm for r in self.replies(m.id)) for m in asks)

    def state(self, name: str) -> dict[str, Any] | None:
        return read_json(self.root / "state" / f"{name}.json")

    def set_state(self, name: str, data: dict[str, Any]) -> None:
        write_state(self.root / "state" / f"{name}.json", data)

    # --- sonnettes d'un message (point B du 20/10) : survivent au redémarrage du démon
    def ring_state(self, message_id: str) -> dict[str, Any] | None:
        return read_json(self.root / "rings" / f"{message_id}.json")

    def set_ring_state(self, message_id: str, data: dict[str, Any]) -> None:
        write_state(self.root / "rings" / f"{message_id}.json", data)

    def ring_states(self) -> list[tuple[str, dict[str, Any]]]:
        folder = self.root / "rings"
        return [(p.stem, d) for p in sorted(folder.glob("*.json")) if (d := read_json(p))] if folder.is_dir() else []

    # --- voie propriétaire vers Antigravity (point G du 20/10) : sidecar OU port, jamais les deux pour une livraison
    def _claim_path(self, message_id: str, attempt: int) -> Path:
        if not MESSAGE_ID.match(message_id):
            raise RelayError(f"identifiant de message invalide : {message_id!r}")
        return self.root / "claims" / message_id / f"{int(attempt)}.json"

    def claim(self, message_id: str, attempt: int, owner: str, **data: Any) -> bool:
        """Prend la livraison `attempt` du message pour `owner`, d'un coup (O_EXCL) ; False si elle a déjà un
        propriétaire. Prise AVANT toute injection."""
        path = self._claim_path(message_id, attempt)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        record = {"owner": owner, "phase": "claimed", "at": now_iso(), "pid": os.getpid(), **data}
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False)
        return True

    def claim_of(self, message_id: str, attempt: int) -> dict[str, Any] | None:
        """Propriétaire de la livraison, ou None si libre ; {"phase": "claimed"} si le fichier est en cours d'écriture."""
        path = self._claim_path(message_id, attempt)
        if not path.exists():
            return None
        return read_json(path) or {"phase": "claimed"}

    def set_claim(self, message_id: str, attempt: int, owner: str, phase: str, **data: Any) -> None:
        """Le propriétaire (et lui seul) note la phase : submitting (injection en cours), delivered, uncertain."""
        current = self.claim_of(message_id, attempt) or {}
        if current.get("owner") != owner:
            raise RelayError(f"livraison {message_id}/{attempt} : {owner} n'en est pas propriétaire")
        write_state(self._claim_path(message_id, attempt), {**current, **data, "phase": phase, "updated": now_iso()})

    def release(self, message_id: str, attempt: int, owner: str) -> None:
        """Rend la livraison (échec CONNU avant l'injection) : l'autre voie peut la prendre (repli exclusif)."""
        if (self.claim_of(message_id, attempt) or {}).get("owner") == owner:
            self._claim_path(message_id, attempt).unlink(missing_ok=True)

    def claims(self, message_id: str) -> list[tuple[int, dict[str, Any]]]:
        folder = self.root / "claims" / message_id
        if not folder.is_dir():
            return []
        found = [(int(p.stem), read_json(p) or {"phase": "claimed"}) for p in folder.glob("*.json") if p.stem.isdigit()]
        return sorted(found, key=lambda item: item[0])

    def all_claims(self) -> list[tuple[str, int, dict[str, Any]]]:
        folder = self.root / "claims"
        if not folder.is_dir():
            return []
        return [(d.name, n, c) for d in sorted(folder.iterdir()) if d.is_dir() for n, c in self.claims(d.name)]

    def read_at(self, message: Message) -> str | None:
        try:
            return self._read_mark(message.to, message.id).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    # --- le sujet, lisible par l'utilisateur
    def render(self, topic: str) -> Path:
        lines = [f"# Nexus Relay · {topic}", ""]
        for m in self.topic_messages(topic):
            head = f"## {m.created} · {m.sender} → {m.to}"
            if m.vote:
                head += f" · vote : {m.vote}"
            if m.info:
                head += " · pour info"
            lines += [head, "", m.text.strip(), ""]
            for item in m.extra.get("attachments") or []:
                target = Path(str(item.get("path", ""))).as_uri() if item.get("path") else ""
                lines.append(f"- Pièce jointe : [{item.get('name', '?')}]({target}) ({item.get('size', 0)} octets)")
            if m.extra.get("attachments"):
                lines.append("")
        path = self.root / "salon" / f"{topic}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        try:
            os.replace(tmp, path)
        except (
            PermissionError
        ):  # l'utilisateur l'a ouvert dans un éditeur qui le verrouille : régénéré au prochain message
            tmp.unlink(missing_ok=True)
        return path


__all__ = ["USER", "Box", "Message", "RelayError", "check_topic", "claim_label", "new_id", "now_iso", "pid_alive"]
