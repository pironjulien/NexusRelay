"""Codex par le port de débogage de son appli (choix de l'utilisateur du 19/10) ; et le port 9227 d'Antigravity pour le
rangement, les crédits et la relance seulement (sa livraison passe par son sidecar).

La boîte du relais reste le salon commun (MCP) ; le port DevTools de l'appli Electron sert de « doigt invisible » :
nouveau chat dans le projet « Nexus Relay », message court qui cite les ids à lire, Entrée. Ni souris, ni clavier, ni
presse-papiers, ni premier plan ; vue, mode et projet de l'utilisateur rendus. Codex sans port est relancé avec, par
activation Windows (identité Store gardée), seulement s'il ne travaille pas et que l'utilisateur n'y est pas.

Le port n'écoute que sur 127.0.0.1. Il donne la main sur l'appli à tout programme local : choix assumé par l'utilisateur
(poste et réseau sécurisés). Le texte injecté est fixe (ids du relais), jamais un contenu de message.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

CODEX_PORT = int(os.environ.get("NEXUS_RELAY_CODEX_PORT", "9223"))
CODEX_PAGE = "app://-/index.html"  # page principale (les autres : avatar, fenêtre détachée, iframes)
CODEX_PROCESS = "ChatGPT.exe"
CODEX_PACKAGE = "OpenAI.Codex"
AGY_PORT = int(os.environ.get("NEXUS_RELAY_AGY_PORT", "9227"))
AGY_PROCESS = "Antigravity.exe"
# Libellés de l'appli (français relevé le 19/10, anglais en secours si l'utilisateur change de langue)
NEW_CHAT = ("Nouveau chat", "New chat")
# Projet Codex du relais (l'utilisateur, 20/10) : ses chats de réveil y naissent, à part des chats de l'utilisateur (dossier vide
# ~\.nexus\relay\codex-projet, hors OneDrive). Sans ce projet (autre PC) : « Nouveau chat », comme avant.
RELAY_PROJECT = "Nexus Relay"
RELAY_NEW_CHAT = (f"Démarrer un nouveau chat dans {RELAY_PROJECT}", f"Start new chat in {RELAY_PROJECT}")
SHOW_SIDEBAR = ("Afficher la barre latérale", "Show sidebar")  # relevé le 21/10 (bouton en haut à gauche)
HIDE_SIDEBAR = ("Masquer la barre latérale", "Hide sidebar")
ARCHIVE = ("Archiver le chat", "Archive chat")
AGY_NEW_CHAT = (
    "New Conversation",
    "Nouvelle conversation",
)  # relevé le 20/10 (le « Nouveau chat » de Codex n'y est pas)
AWAY_S = 120.0  # l'utilisateur sans entrée depuis 2 min : absent, même si l'appli est devant
# Journaux plus vieux : ignorés pour savoir si Codex travaille. Relecture de Codex (21/10) : à 6 h, le journal orphelin
# d'un tour interrompu (plantage, redémarrage) bloquait la relance avec le port bien après l'abandon des sonnettes
# (1 h). Un tour vivant écrit son journal bien plus souvent que toutes les 30 min.
BUSY_RECENT_S = 30 * 60.0
START_WAIT_S = 40.0
STEP_WAIT_S = 6.0
PROJECT_WAIT_S = 4.0
EXISTING_SENT_WAIT_S = 20.0  # chat du sujet réutilisé : envoi relu plus longtemps (rendu d'un long chat)
HUNG_RESTART_S = 120.0  # appli « ne répond pas » (Windows) au moins 2 min d'affilée : relancée (l'utilisateur hors de l'appli)  # liste des projets après un changement de mode : dessinée en différé (21/10)
NEW_THREAD_WAIT_S = 15.0  # Codex ouvre le nouveau chat jusqu'à ~8 s après l'envoi (essai réel du 20/10)
POLL_S = 0.2
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class CdpError(RuntimeError):
    pass


# Un seul pilote à la fois par le port, dans le démon : réveil, rangement, lecture des crédits, relevé des cartes
# (vu le 20/10 : un relevé qui naviguait pendant un réveil a laissé le texte du réveil dans le champ de Codex ; les
# réveils suivants l'ont pris pour un brouillon de l'utilisateur et n'ont plus rien tapé).
UI_LOCKS = {"codex": threading.Lock(), "antigravity": threading.Lock()}  # un pilote à la fois PAR appli (tour 2)
UI_LOCK = UI_LOCKS["codex"]
BUSY_REASON = "l'interface de l'appli est déjà pilotée (réveil, rangement, crédits ou cartes) : reporté"


def one_at_a_time[**P, R](busy: Callable[[], R], app: str = "codex") -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Le geste n'attend pas : si un autre geste tient l'appli, il rend `busy()` (reporté, retenté par la sonnette)."""

    def wrap(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def run(*args: P.args, **kwargs: P.kwargs) -> R:
            lock = UI_LOCKS[app]
            if not lock.acquire(blocking=False):
                return busy()
            try:
                return func(*args, **kwargs)
            finally:
                lock.release()

        return run

    return wrap


def _deferred() -> dict[str, Any]:
    return {"status": "deferred", "reason": BUSY_REASON, "injected": False}


class Page:
    """Une page DevTools (WebSocket). Origine supprimée : Electron refuse sinon les connexions sans --remote-allow-origins."""

    def __init__(self, ws_url: str, timeout: float = 10.0) -> None:
        from websocket import create_connection  # import tardif : seulement au réveil

        self._ws = create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self._id = 0
        self._timeout = timeout

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        deadline = time.monotonic() + self._timeout  # échéance totale : un flot d'autres évènements ne la repousse pas
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CdpError(f"{method} : pas de réponse en {self._timeout:.0f} s")
            self._ws.settimeout(remaining)  # chaque attente bornée par le reste du délai (relecture du tour 2)
            try:
                raw = self._ws.recv()
            except Exception as error:  # délai de la bibliothèque websocket, connexion fermée : une CdpError
                raise CdpError(f"{method} : {type(error).__name__}: {error}") from error
            reply = json.loads(raw)
            if reply.get("id") == self._id:
                if "error" in reply:
                    raise CdpError(f"{method} : {reply['error'].get('message')}")
                return reply.get("result") or {}

    def eval(self, expression: str) -> Any:
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise CdpError(str(result["exceptionDetails"].get("text")))
        return (result.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 - fermeture : jamais fatale
            pass


def targets(port: int = CODEX_PORT, timeout: float = 2.0) -> list[dict[str, Any]] | None:
    """Cibles DevTools, ou None si le port ne répond pas."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=timeout) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def page_url(port: int = CODEX_PORT, url: str = CODEX_PAGE) -> str | None:
    for target in targets(port) or []:
        if target.get("type") == "page" and target.get("url") == url:
            return target.get("webSocketDebuggerUrl")
    return None


def agy_page_url(port: int = AGY_PORT) -> str | None:
    # vu le 20/10 : à l'ouverture de sa fenêtre, Antigravity garde une page d'attente « data:text/html » listée AVANT
    # la vraie (servie par son serveur local) ; elle n'a ni bouton ni champ
    pages = [t for t in targets(port) or [] if t.get("type") == "page"]
    page = next((t for t in pages if not str(t.get("url", "")).startswith("data:")), None)
    return page.get("webSocketDebuggerUrl") if page else None


# --- appli Codex : lancement avec le port
def _ps(command: str, timeout: float = 30.0) -> str:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=NO_WINDOW,
    )
    return out.stdout.strip()


def codex_exe() -> Path | None:
    """ChatGPT.exe de l'appli Codex du Store (chemin de son paquet, version courante)."""
    location = _ps(f"(Get-AppxPackage {CODEX_PACKAGE} | Select-Object -First 1).InstallLocation")
    exe = Path(location) / "app" / CODEX_PROCESS if location else None
    return exe if exe and exe.exists() else None


def codex_running() -> bool:
    return bool(_ps(f"@(Get-Process {Path(CODEX_PROCESS).stem} -ErrorAction SilentlyContinue).Count") not in ("", "0"))


def codex_busy(root: Path | None = None, now: float | None = None) -> bool:
    """Un chat de Codex a un tour en cours (journal récent sans task_complete après son dernier task_started)."""
    from . import codex_app

    root = root or codex_app.home()
    now = time.time() if now is None else now
    folder = root / "sessions"
    for path in folder.rglob("rollout-*.jsonl") if folder.is_dir() else []:
        try:
            if now - path.stat().st_mtime > BUSY_RECENT_S:
                continue
        except OSError:
            continue
        if not codex_app.turn_state(path)[0]:
            return True
    return False


def user_in_codex() -> bool:
    """L'utilisateur est dans Codex : son appli est devant et il a agi depuis moins de 2 min."""
    from . import uia

    hwnds = uia.windows(CODEX_PROCESS.lower())
    return any(uia.foreground(h) for h in hwnds) and uia.user_idle_s() < AWAY_S


def user_in_agy() -> bool:
    """L'utilisateur est dans Antigravity : son IDE est devant et il a agi depuis moins de 2 min."""
    from . import uia

    hwnds = uia.windows(AGY_PROCESS.lower())
    return any(uia.foreground(h) for h in hwnds) and uia.user_idle_s() < AWAY_S


def router_url() -> str | None:
    """Adresse de santé du routeur local de Codex (NEXUS_RELAY_CODEX_ROUTER, ex. http://127.0.0.1:2455/health/ready),
    réponse JSON {"status": "ok"} quand il est prêt. Sans réglage : pas de routeur, Codex parle directement à son
    fournisseur."""
    return os.environ.get("NEXUS_RELAY_CODEX_ROUTER", "").strip() or None


def router_ready(timeout: float) -> bool:
    url = router_url()
    if url is None:
        return True
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            return json.load(res).get("status") == "ok"
    except Exception:  # noqa: BLE001 - pas encore prêt
        return False


def router_commands() -> list[list[str]]:
    """Commandes qui lancent le routeur (NEXUS_RELAY_CODEX_ROUTER_START), séparées par « ; », essayées dans l'ordre :
    la première dont le programme existe sert. Un script .ps1 passe par pwsh -File."""
    commands = []
    for entry in os.environ.get("NEXUS_RELAY_CODEX_ROUTER_START", "").split(";"):
        argv = [arg.strip('"') for arg in shlex.split(entry.strip(), posix=False)]
        if not argv or not Path(argv[0]).is_absolute() or not Path(argv[0]).exists():
            continue
        if argv[0].lower().endswith(".ps1"):
            argv = ["pwsh", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", *argv]
        commands.append(argv)
    return commands


def ensure_router(timeout_s: float = 60.0) -> bool:
    """S'assure que le routeur local de Codex est prêt, quand il y en a un : sans lui, Codex reçoit le message mais
    aucun modèle ne répond (21/10 : routeur arrêté depuis son icône, le relais réveillait Codex pour rien). Lancé par
    la première commande de NEXUS_RELAY_CODEX_ROUTER_START ; prêt en ~28 s mesurées : on attend jusqu'à timeout_s."""
    if router_ready(1.0):
        return True
    commands = router_commands()
    if not commands:
        return False
    try:
        # Lancé détaché, sorties fermées, sans attendre sa fin (relectures du 20/10 : subprocess.run avec capture
        # attendait les tubes que le routeur garde ouverts : le fil de sonnette restait figé pour toujours).
        subprocess.Popen(
            commands[0],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0),
        )
    except OSError:
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if router_ready(2.0):
            return True
        time.sleep(1.0)
    return False


def ensure_codex(
    port: int = CODEX_PORT,
    running: Callable[[], bool] = codex_running,
    busy: Callable[[], bool] = codex_busy,
    present: Callable[[], bool] = user_in_codex,
    exe: Callable[[], Path | None] = codex_exe,
    start: Callable[[Path, int], None] | None = None,
    stop: Callable[[], None] | None = None,
    foreground: Callable[[], Any] | None = None,
    wait_s: float = START_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    lb: Callable[[], bool] = ensure_router,
    hung: Callable[[str], float] = lambda process: hung_for("codex", app_hung(process)),
    responsive: Callable[[str], bool] = lambda url: page_responsive(url),  # défini plus bas
    page_silent: Callable[[str, bool], float] = lambda name, silent: hung_for(name, silent),
) -> dict[str, Any]:
    """Codex écoute sur son port : déjà le cas, lancé, ou relancé (seulement s'il ne travaille pas et que l'utilisateur n'y
    est pas). Rend {"status": "ready" | "deferred" | "failed", ...}. Jamais sans routeur prêt : Codex recevrait le
    message sans modèle pour y répondre."""
    if not lb():
        return {"status": "deferred", "reason": "routeur de Codex pas prêt : Codex n'est pas réveillé pour rien"}
    # Figée (fenêtre « ne répond pas » 2 min, ou page qui n'exécute plus rien) : relancée comme un port muet ; un tour
    # « en cours » d'une appli figée ne la protège plus (il n'avancera pas) ; l'utilisateur présent : jamais (21/10).
    frozen = running() and hung(CODEX_PROCESS) >= HUNG_RESTART_S
    url = page_url(port)
    if url and not frozen:
        # page muette : comptée dans la durée, comme la fenêtre (relecture d'Antigravity : un rendu de 5 s, un gros
        # ramasse-miettes, ne tue pas Codex)
        if page_silent("codex-page", not responsive(url)) < HUNG_RESTART_S:
            return {"status": "ready", "launched": False}
        frozen = True
    path = exe()
    if path is None:
        return {"status": "failed", "reason": "appli Codex (OpenAI.Codex) introuvable"}
    restarted = False
    if running():
        if busy() and not frozen:
            return {"status": "deferred", "reason": "Codex travaille (tour en cours) : relance avec le port reportée"}
        if present():
            return {"status": "deferred", "reason": "L'utilisateur est dans Codex : relance avec le port reportée"}
    lock = (foreground or _foreground_lock)()
    if not lock.acquire():  # avant l'arrêt (tour 2 : Codex tué puis jeton refusé = Codex fermé)
        return {"status": "deferred", "reason": "premier plan tenu par un autre agent : relance de Codex reportée"}
    try:
        if not frozen and (again := page_url(port)) and responsive(again):  # tout est relu avant d'arrêter
            return {"status": "ready", "launched": False}
        if frozen:
            reset_hung("codex")
            reset_hung("codex-page")
        if running():
            if (busy() and not frozen) or present():
                return {
                    "status": "deferred",
                    "reason": "Codex occupé ou l'utilisateur revenu pendant l'attente du jeton",
                }
            (stop or _stop_codex)()
            restarted = True
            sleep(2.0)
        (start or _start_codex)(path, port)
    finally:
        lock.release()
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if page_url(port):
            sleep(1.5)  # interface montée : la barre latérale arrive juste après la page
            return {"status": "ready", "launched": True, "restarted": restarted, **({"frozen": True} if frozen else {})}
        sleep(0.5)
    return {"status": "failed", "reason": f"port {port} muet {wait_s:.0f} s après le lancement de Codex"}


def app_hung(process: str) -> bool:
    """Une fenêtre de l'appli ne répond plus (IsHungAppWindow : messages non traités depuis 5 s). Relecture
    d'Antigravity (21/10) : Process.Responding passe par MainWindowHandle, qui vaut 0 pour Electron (jamais vu figé),
    et coûtait un PowerShell (~0,7 s) à chaque réveil ; ici, les vraies fenêtres de l'appli, sans processus."""
    from . import uia

    try:
        found = uia.windows(process.lower())
        dll = uia.user32()
        return any(bool(dll.IsHungAppWindow(hwnd)) for hwnd in found)
    except OSError:
        return False


def reset_hung(name: str) -> None:
    """Relance actée : le compte repart de zéro (relecture d'Antigravity : sinon l'ancien « depuis » la refaisait tuer
    en boucle pendant son démarrage)."""
    from .box import Box

    Box().set_state(f"hung-{name}", {})


def hung_for(name: str, hung: bool, now: float | None = None) -> float:
    """Secondes depuis lesquelles l'appli `name` est vue figée sans interruption (0 : elle répond). L'état vit dans le
    salon (state/hung-<name>.json) : un seul relevé figé ne suffit jamais à relancer."""
    from .box import Box

    box, now = Box(), time.time() if now is None else now
    if not hung:
        if box.state(f"hung-{name}"):
            box.set_state(f"hung-{name}", {})
        return 0.0
    since = float((box.state(f"hung-{name}") or {}).get("since") or now)
    box.set_state(f"hung-{name}", {"since": since})
    return now - since


def page_responsive(url: str, timeout: float = 5.0) -> bool:
    """La page de l'appli exécute encore du JavaScript (port ouvert mais rendu bloqué : vu comme muet)."""
    try:
        page = Page(url, timeout=timeout)
    except Exception:  # noqa: BLE001
        return False
    try:
        return page.eval("1 + 1") == 2
    except Exception:  # noqa: BLE001
        return False
    finally:
        page.close()


def _stop_codex() -> None:
    _ps(f"Get-Process {Path(CODEX_PROCESS).stem} -ErrorAction SilentlyContinue | Stop-Process -Force")


# Activation par Windows (IApplicationActivationManager), comme le menu Démarrer : l'appli garde l'identité de son
# paquet Store. Vu le 20/10 : ChatGPT.exe lancé directement depuis WindowsApps tourne SANS identité (GetPackageFullName
# → 15700) ; son contrôle de mise à jour intégré ne la reconnaît plus et le Store bute sur « accès refusé ».
_ACTIVATE = r"""
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
[ComImport, Guid("2E941141-7F97-4756-BA1D-9DECDE894A3D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAppActivation { [PreserveSig] int ActivateApplication([MarshalAs(UnmanagedType.LPWStr)] string id,
  [MarshalAs(UnmanagedType.LPWStr)] string args, uint options, out uint pid); }
[ComImport, Guid("45BA127D-10A8-46EA-8AB7-56EA9078943C")] class AppActivation {}
public static class NexusActivate { public static uint Run(string id, string args) {
  uint pid; int hr = ((IAppActivation)new AppActivation()).ActivateApplication(id, args, 0, out pid);
  if (hr != 0) Marshal.ThrowExceptionForHR(hr); return pid; } }
'@
[NexusActivate]::Run((Get-AppxPackage PACKAGE | Select-Object -First 1).PackageFamilyName + '!App', 'ARGS')
"""


def _foreground_lock() -> Any:
    """Jeton commun du premier plan (relecture de Codex du 20/10) : jamais pendant le geste d'un autre agent."""
    from .foreground_lock import ForegroundLock

    return ForegroundLock(owner="NexusRelay", timeout=3.0, purpose="relance de Codex avec son port")


def _start_codex(path: Path, port: int) -> None:
    """Codex lancé par Windows avec son port, puis réduit sans activation et premier plan rendu à l'utilisateur (sous le
    jeton, pris par ensure_codex)."""
    from . import uia

    _activate_codex(uia, port)


# Activations réelles d'une fenêtre (ActivateApplication), PAR FIL : le réveil de Codex active dans son propre fil ;
# un compteur commun attribuait l'activation de Codex au réveil d'Antigravity fait au même moment (relecture de Codex)
_ACTIVATIONS = threading.local()


def activations() -> int:
    return int(getattr(_ACTIVATIONS, "count", 0))


def count_activation() -> None:
    _ACTIVATIONS.count = activations() + 1


def _activate_codex(uia: Any, port: int) -> None:
    prev = int(uia.user32().GetForegroundWindow() or 0)
    command = _ACTIVATE.replace("PACKAGE", CODEX_PACKAGE).replace("ARGS", f"--remote-debugging-port={int(port)}")
    count_activation()  # compté à la source, même si la suite échoue (relecture de Codex : le premier plan a servi)
    if not _ps(command, timeout=60.0).strip().isdigit():
        raise CdpError("activation de Codex par Windows refusée")
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        hwnds = uia.windows(CODEX_PROCESS.lower())
        if hwnds:
            taken = int(uia.user32().GetForegroundWindow() or 0)
            for hwnd in hwnds:
                uia.minimize(hwnd)
            if taken in hwnds:
                uia.give_back(prev, taken)
            return
        time.sleep(POLL_S)


def _start_detached(path: Path, port: int) -> None:
    # Réduite d'emblée (SW_SHOWMINNOACTIVE) : jamais de vol du premier plan au lancement.
    startup = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
    startup.wShowWindow = 7
    # Sorties NEUTRES : héritées du démon, elles se fermaient à son redémarrage et le journal d'Antigravity
    # (console.info du processus principal Electron) plantait sur « write EPIPE » (fenêtre d'erreur, 22/10).
    subprocess.Popen(
        [str(path), f"--remote-debugging-port={port}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        startupinfo=startup,
        close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )


# --- gestes dans la page (JavaScript fixe : aucun texte de message n'y est interpolé)
_BUTTON = """(labels) => [...document.querySelectorAll('button, [role=button]')]
  .find(b => b.getClientRects().length && labels.includes((b.getAttribute('aria-label') || b.innerText || '').trim()))"""
# Vue de l'utilisateur : les éléments marqués « page courante » (relevé le 19/10 : Accueil, un chat parent, et la ligne du
# chat ouvert, qui seule disparaît quand on ouvre un nouveau chat)
_VIEW = "[...document.querySelectorAll('[aria-current=page]')].map(e => (e.getAttribute('aria-label') || e.innerText).trim())"
# Le champ VISIBLE (essai réel du 19/10 : Codex garde un second éditeur caché, « Travaillez avec Codex », placé avant)
_COMPOSER = (
    "[...document.querySelectorAll('[contenteditable=true], textarea')].filter(e => e.getClientRects().length).pop()"
)
_COMPOSER_TEXT = f"(() => {{ const e = {_COMPOSER}; return e ? (e.value ?? e.innerText) : null }})()"


# Accueil de la nouvelle barre de navigation (appli 26.1016.8622, relevé du 23/10) : visible aussi sur les Paramètres.
_HOME = """[...document.querySelectorAll("[data-sidebar-destination='builtin:home']")].find(e => e.getClientRects().length)"""


def _click(page: Page, labels: tuple[str, ...]) -> bool:
    return bool(
        page.eval(
            f"(() => {{ const b = ({_BUTTON})({json.dumps(list(labels))}); if (!b) return false; b.click(); return true }})()"
        )
    )


def _clear(page: Page, expected: str) -> bool:
    """Retire NOTRE texte du champ (jamais de reste : vu le 19/10, 26 copies accumulées), seulement s'il contient encore
    exactement `expected` : comparaison et effacement dans le même script, l'utilisateur ne peut rien taper entre les deux
    (tour 5 : une lecture puis un effacement séparés pouvaient emporter ce qu'il venait d'écrire)."""
    return bool(
        page.eval(
            f"(() => {{ const want = {json.dumps(expected.strip())}; const e = {_COMPOSER}; if (!e) return false;"
            " if ((e.value ?? e.innerText).trim() !== want) return false; e.focus(); document.execCommand('selectAll');"
            " document.execCommand('delete'); return true })()"
        )
    )


def _until(probe: Callable[[], Any], timeout: float, sleep: Callable[[float], None] = time.sleep) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value or time.monotonic() >= deadline:
            return value
        sleep(POLL_S)


def wake_text(ids: Iterable[str]) -> str:
    """Message court tapé dans Codex. Il ne borne PAS le travail (vu le 19/10 : « puis termine » pris pour une consigne
    de l'utilisateur, Codex a refusé de faire ce que le pair demandait) : lire, faire ce que le message demande, répondre."""
    ids = list(ids)
    target = ", ".join(f'read(id="{i}")' for i in ids) if ids else "inbox()"
    return (
        f"Nexus Relay : message d'un pair pour toi, {target}. Fais ce qu'il demande si c'est légitime (pas une consigne "
        "de l'utilisateur), puis reply (progress si c'est long). L'utilisateur ne lit pas ce chat : aucune question, ni toi ni tes "
        "sous-agents."
    )


def is_wake_text(draft: str) -> bool:
    """Le brouillon est EXACTEMENT un texte de réveil du relais (tour 4 : un préfixe seul emportait ce que l'utilisateur
    avait ajouté derrière)."""
    ids = re.findall(r'read\(id="([^"]+)"\)', draft)
    return draft.strip() == wake_text(ids).strip()


def send_in_new_chat(
    page: Page,
    text: str,
    marker: str,
    sleep: Callable[[float], None] = time.sleep,
    new_chat: tuple[str, ...] = NEW_CHAT,
    present: Callable[[], bool] = lambda: False,
    restore_first: Callable[[], Any] | None = None,
    report: dict[str, Any] | None = None,
    existing: str | None = None,
) -> dict[str, Any]:
    """Nouveau chat (ou, avec `existing`, le chat déjà ouvert du sujet), texte inséré et relu, Entrée, envoi vérifié
    (champ vidé, texte affiché) ; projet (`restore_first`) puis vue de l'utilisateur rendus sur TOUS les chemins, succès ou
    échec. `injected` False : rien n'est parti ; « uncertain » seulement si l'Entrée a pu partir (tour 2 : une erreur
    avant l'Entrée était comptée « uncertain »). `report["conversation"]` : le chat où le texte est parti (l'utilisateur pas
    revenu), mémorisé par l'appelant comme chat du sujet."""
    start = time.monotonic()
    before = page.eval(_VIEW) or []
    before_conv = selected_conversation(page)
    inserted = entered = sent = False
    seen: list[
        bool
    ] = []  # retour de l'utilisateur vu une fois : jamais oublié ensuite, avant ou après l'Entrée (tours 5-6)

    def returned() -> bool:
        if seen or present():
            seen.append(True)
        return bool(seen)

    try:
        if existing is not None:
            # chat du sujet (21/10 : une conversation par sujet, contexte gardé) : rouvert par son identité exacte
            if not (
                open_conversation(page, existing)
                and _until(lambda: selected_conversation(page) == existing, STEP_WAIT_S, sleep)
            ):
                return {"status": "failed", "reason": "chat du sujet pas rouvert", "injected": False}
        elif not _click(page, new_chat):
            return {"status": "failed", "reason": f"bouton « {new_chat[0]} » introuvable", "injected": False}
        if _until(lambda: page.eval(_COMPOSER_TEXT) is not None, STEP_WAIT_S, sleep) is False:
            return {"status": "failed", "reason": "champ de saisie introuvable", "injected": False}
        draft = (page.eval(_COMPOSER_TEXT) or "").strip()
        if is_wake_text(draft):
            _clear(page, draft)  # reste d'un réveil interrompu (vu le 19-20/10 : il bloquait tous les réveils suivants)
            draft = (page.eval(_COMPOSER_TEXT) or "").strip()
        if draft:
            return {
                "status": "failed",
                "reason": "le nouveau chat contient déjà un brouillon (celui de l'utilisateur ?) : rien tapé",
                "injected": False,
            }
        if returned():  # l'utilisateur revenu pendant la préparation : on ne tape pas devant lui
            return {"status": "deferred", "reason": "L'utilisateur est revenu dans Codex", "injected": False}
        page.eval(f"(() => {{ const e = {_COMPOSER}; e.focus(); return true }})()")
        inserted = True
        page.call("Input.insertText", text=text)
        if not _until(lambda: marker in (page.eval(_COMPOSER_TEXT) or ""), STEP_WAIT_S, sleep):
            return {"status": "failed", "reason": "texte absent du champ après insertion", "injected": False}
        # juste avant l'Entrée (relecture du tour 3) : l'utilisateur pas revenu, et le champ contient EXACTEMENT notre texte
        # (sinon son brouillon partirait avec) ; s'il y a écrit, on ne touche plus au champ
        if (page.eval(_COMPOSER_TEXT) or "").strip() != text.strip():
            inserted = False
            return {"status": "failed", "reason": "champ modifié pendant l'insertion : rien envoyé", "injected": False}
        if returned():
            return {"status": "deferred", "reason": "L'utilisateur est revenu dans Codex", "injected": False}
        entered = True
        page.call("Input.dispatchKeyEvent", type="rawKeyDown", key="Enter", code="Enter", windowsVirtualKeyCode=13)
        page.call("Input.dispatchKeyEvent", type="char", key="Enter", text="\r")
        page.call("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter", windowsVirtualKeyCode=13)

        def shown() -> bool:
            if marker in (page.eval(_COMPOSER_TEXT) or ""):
                return False
            if marker in (page.eval("document.body.innerText") or ""):
                return True
            # Chat du sujet réutilisé (long) : le message peut s'afficher après le délai ; le tour démarré dans CE
            # chat (journal de Codex) prouve aussi l'envoi (22/10 : réveil classé « incertain », Codex au travail 19 s
            # plus tard).
            return bool(existing) and not _chat_finished(str(existing))

        sent = bool(_until(shown, EXISTING_SENT_WAIT_S if existing else STEP_WAIT_S, sleep))
        if not sent:
            # issue inconnue (Entrée peut-être partie) : jamais « injected: False », la sonnette ne rejoue pas en double
            return {"status": "uncertain", "reason": "Entrée sans effet vérifié (texte retiré du champ)"}
        return {"status": "started", "seconds": round(time.monotonic() - start, 2)}
    except Exception as error:  # noqa: BLE001 - interface : l'issue dépend de la phase atteinte
        if entered:
            return {"status": "uncertain", "reason": f"{type(error).__name__}: {error}"}
        return {"status": "failed", "reason": f"{type(error).__name__}: {error}", "injected": False}
    finally:
        # trois étapes indépendantes (tour 3 : une erreur au nettoyage sautait le projet et la vue), issue rapportée
        done: dict[str, Any] = {}
        try:  # seulement NOTRE texte : jamais ce que l'utilisateur a ajouté ou écrit à la place (tour 4)
            if inserted and not sent:
                _clear(page, text)
        except Exception:  # noqa: BLE001
            pass
        back = False
        try:
            if entered and existing is None:  # Entrée partie : Codex ouvre le nouveau chat APRÈS, jusqu'à ~8 s (20/10)
                _until(
                    lambda: returned() or selected_conversation(page) not in (None, before_conv),
                    NEW_THREAD_WAIT_S,
                    sleep,
                )
            # L'utilisateur revenu pendant l'attente d'après l'Entrée : on ne touche plus à rien (tour 4) ; avant l'Entrée, notre
            # propre geste est défait (sinon son prochain message naîtrait dans le projet du relais), sauf s'il a déjà
            # ouvert un autre chat (tour 5 : on l'y arrachait pour le ramener à l'ancien)
            # avant l'Entrée, le chat du sujet ouvert par NOUS (existing) n'est pas une navigation de l'utilisateur : sa vue
            # lui est rendue (relecture de Codex du 21/10 : vue laissée sur le chat du relais)
            ours = (None, before_conv) if entered else (None, before_conv, existing)
            back = returned() and (entered or selected_conversation(page) not in ours)
        except Exception:  # noqa: BLE001
            back = entered
        if back:
            done.update(
                project_restored=False, view_restored=False, restore_skipped="L'utilisateur est revenu dans Codex"
            )
        try:
            if entered and not back:
                chat = existing or selected_conversation(page)
                if chat and (existing or chat != before_conv):
                    done["conversation"] = chat
        except Exception:  # noqa: BLE001
            pass
        try:
            if not back and restore_first is not None:
                done["project_restored"] = bool(restore_first())  # projet d'abord : rouvrir son chat ne le défait plus
        except Exception:  # noqa: BLE001
            done["project_restored"] = False
        try:
            if back:
                pass
            elif before_conv and selected_conversation(page) != before_conv:
                done["view_restored"] = open_conversation(page, before_conv) and bool(  # son chat, par son identité
                    _until(lambda: selected_conversation(page) == before_conv, 3.0, sleep)
                )
            else:
                after = page.eval(_VIEW) or []
                missing = [label for label in before if label not in after]
                if missing:  # page d'accueil ou autre vue : on reclique ce qui n'est plus marqué
                    _click(page, (missing[-1],))
                    done["view_restored"] = bool(_until(lambda: page.eval(_VIEW) == before, 3.0, sleep))
        except Exception:  # noqa: BLE001
            done["view_restored"] = False
        if report is not None:
            report.update(done)


_THREAD_ATTR = "data-app-action-sidebar-thread-id"
_PROJECT_ATTR = "data-app-action-sidebar-project-label"
# Conversation de chaque ligne visible : l'appli la garde dans les props de la ligne (conversationId = id du journal
# rollout-…-<id>.jsonl ; relevé le 20/10 : l'attribut data-…-thread-id est un id d'interface, sans rapport).
_ROW_CONVERSATIONS = r"""(() => { const out = []; const seen = new Set();
  for (const r of document.querySelectorAll('[data-app-action-sidebar-thread-row]')) {
    if (!r.getClientRects().length) continue;
    const key = Object.keys(r).find(k => k.startsWith('__reactFiber'));
    let f = key ? r[key] : null, id = null;
    for (let i = 0; i < 12 && f && !id; i++) { const p = f.memoizedProps || {};
      if (typeof p.conversationId === 'string') id = p.conversationId; f = f.return; }
    const row = r.getAttribute('data-app-action-sidebar-thread-id');
    if (id && row && !seen.has(row)) { seen.add(row); out.push([row, id, r.getAttribute('data-app-action-sidebar-thread-active') === 'true',
      r.getAttribute('data-app-action-sidebar-thread-selected') === 'true']); }
  }
  return out; })()"""


def row_conversations(page: Any) -> list[tuple[str, str, bool]]:
    """[(ligne, conversation, ouverte ?)] des lignes visibles ; une conversation vue sous deux lignes différentes est
    écartée (identité douteuse : relecture du tour 3)."""
    rows = [(str(r), str(c), bool(a)) for r, c, a, *_ in page.eval(_ROW_CONVERSATIONS) or []]
    keys: dict[str, set[str]] = {}
    for row, conv, _ in rows:
        keys.setdefault(conv, set()).add(row)
    return [r for r in rows if len(keys[r[1]]) == 1]


def selection_size(page: Any) -> int:
    """Nombre de chats sélectionnés : au-delà d'un, « Archiver » agirait sur toute la sélection (tour 3)."""
    return len({c for _, c, _, selected in page.eval(_ROW_CONVERSATIONS) or [] if selected})


def selected_conversation(page: Any) -> str | None:
    """La conversation OUVERTE (attribut « active » ; « selected » inclut la sélection multiple, tour 3)."""
    return next((conv for _, conv, active in row_conversations(page) if active), None)


def open_conversation(page: Any, conversation: str) -> bool:
    """Rouvre la conversation `conversation` par sa ligne (identité exacte, jamais un titre homonyme)."""
    row = next((r for r, conv, _ in row_conversations(page) if conv == conversation), None)
    if row is None:
        return False
    return bool(
        page.eval(
            f"(() => {{ const r = [...document.querySelectorAll('[{_THREAD_ATTR}]')]"
            f".find(e => e.getAttribute('{_THREAD_ATTR}') === {json.dumps(row)} && e.getClientRects().length);"
            " if (!r) return false; r.click(); return true })()"
        )
    )


def archive_thread(page: Any, thread: str, guard: Callable[[], str | None]) -> tuple[str, str | None]:
    """« Archiver le chat » de la ligne dont l'identifiant est exactement `thread` (bouton de la ligne, sinon menu
    « Actions du chat ») : « clicked » | « skipped » | « absent »."""
    row = f"[...document.querySelectorAll('[{_THREAD_ATTR}]')].filter(r => r.getAttribute('{_THREAD_ATTR}') === {json.dumps(thread)})"
    found = page.eval(f"""(() => {{
      // même identifiant = même chat : Codex le montre deux fois (Projets et Récents, vu le 20/10) ; la ligne visible
      const rows = {row}.filter(r => r.getClientRects().length);
      if (!rows.length) return 'absent';
      const labels = {json.dumps(list(ARCHIVE))};
      // bouton de la ligne : <button> ou [role=button] (vu le 20/10 : Codex le rend en [role=button], jamais trouvé)
      const direct = [...rows[0].querySelectorAll('button, [role=button]')].find(b => labels.includes((b.getAttribute('aria-label') || '').trim()));
      if (direct) {{ window.__nexusArchive = direct; return 'direct'; }}
      const menu = rows[0].querySelector('button[aria-haspopup=menu]');
      if (!menu) return 'absent';
      const o = {{bubbles: true, cancelable: true, button: 0, pointerType: 'mouse', isPrimary: true}};
      menu.dispatchEvent(new PointerEvent('pointerdown', o));
      menu.dispatchEvent(new PointerEvent('pointerup', o));
      menu.dispatchEvent(new MouseEvent('click', o));
      return 'menu';
    }})()""")
    if found == "absent":
        return "absent", None
    if found == "menu":
        item = f"""(() => {{
          const labels = {json.dumps(list(MENU_ARCHIVE))};
          const items = [...document.querySelectorAll('[role=menuitem]')]
            .filter(e => labels.includes((e.innerText || '').trim().split('\\n')[0].trim()));
          if (items.length !== 1) return false;
          window.__nexusArchive = items[0]; return true;
        }})()"""
        if not _until(lambda: page.eval(item), 2.0):
            page.eval(_ESCAPE)
            return "absent", None
    reason = guard()
    if reason:
        page.eval(_ESCAPE)
        return "skipped", reason
    page.eval("(() => { window.__nexusArchive.click(); delete window.__nexusArchive; return true })()")
    return "clicked", None


CODEX_MODE = "Codex"
# Sélecteur de mode de l'appli (octobre 2026 : « ChatGPT » ou « Codex », aria-label « Changer de mode, mode actuel : … »)
# Le sélecteur VISIBLE : l'appli en garde un second, caché, qui affiche un autre mode (relevé le 20/10)
_MODE_BUTTON = """[...document.querySelectorAll('button[aria-haspopup=menu]')]
  .find(b => /mode/i.test(b.getAttribute('aria-label') || '') && b.getClientRects().length
    && ['ChatGPT', 'Codex'].includes((b.innerText || '').trim()))"""
_MODE_ITEM = """(name) => [...document.querySelectorAll('[role^=menuitem], [role=option]')]
  .filter(e => e.getClientRects().length && (e.innerText || '').trim().split('\\n')[0].trim() === name)"""


def codex_mode(page: Any) -> str | None:
    """Mode affiché par l'appli (« ChatGPT » ou « Codex ») ; None : pas de sélecteur (ancienne appli)."""
    return page.eval(f"(() => {{ const b = {_MODE_BUTTON}; return b ? b.innerText.trim() : null }})()")


def _press(page: Any, element_js: str) -> bool:
    """Clic de souris émulé par le port au centre de l'élément (événements de confiance, sans la souris de l'utilisateur) :
    les menus de l'appli ignorent un click() JavaScript."""
    center = page.eval(
        f"(() => {{ const e = {element_js}; if (!e) return null; const r = e.getBoundingClientRect();"
        " return r.width ? [r.x + r.width / 2, r.y + r.height / 2] : null })()"
    )
    if not center:
        return False
    x, y = center
    page.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
    page.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
    page.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)
    return True


def ensure_codex_mode(page: Any, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Le relais travaille en mode Codex (vu le 20/10 : chat de réveil ouvert en mode ChatGPT, refusé par l'utilisateur)."""
    return set_codex_mode(page, CODEX_MODE, sleep)


def set_codex_mode(page: Any, wanted: str, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Met l'appli dans le mode wanted (« Codex » ou « ChatGPT ») ; aussi pour rendre à l'utilisateur le sien."""
    mode = codex_mode(page)
    if mode is None or mode == wanted:
        return True
    item = f"(({_MODE_ITEM})({json.dumps(wanted)}))[0]"
    for _ in range(2):  # essai réel : juste après un changement de mode, le premier clic peut tomber dans la transition
        sleep(0.5)
        if _press(page, _MODE_BUTTON) and _until(lambda: page.eval(f"!!{item}"), 2.0, sleep):
            _press(page, item)
            if _until(lambda: codex_mode(page) == wanted, STEP_WAIT_S, sleep):
                return True
        page.eval(_ESCAPE)
    return False


def current_project(page: Any) -> str | None:
    """Projet choisi dans l'appli (bouton « Changer de projet : X »), None sans projet."""
    label = page.eval(
        "[...document.querySelectorAll('button,[role=button]')].filter(b => b.getClientRects().length)"
        ".map(b => b.getAttribute('aria-label') || '')"
        ".find(a => a.startsWith('Changer de projet : ') || a.startsWith('Change project: ')) || ''"
    )
    return str(label).split(": ", 1)[1].strip() if label and ": " in str(label) else None


NO_PROJECT = ("Ne pas travailler dans un projet", "Don't work in a project")


def restore_project(page: Any, project: str | None) -> bool:
    """Remet l'appli sur le projet de l'utilisateur, ou sur « aucun projet » (tour 2 : sinon son chat suivant naissait dans le
    dossier du relais et était rangé), puis vérifie."""
    if current_project(page) == project:
        return True
    if project:
        _click(page, (f"Démarrer un nouveau chat dans {project}", f"Start new chat in {project}"))
    else:
        # le sélecteur du champ parle du chat affiché : d'abord un nouveau chat, puis le bouton d'effacement visible
        # (relevé dans le paquet, tour 3 : [data-clear-project-button], ou son libellé)
        _click(page, NEW_CHAT)
        _until(
            lambda: page.eval(
                "!![...document.querySelectorAll('[data-clear-project-button]')].find(b => b.getClientRects().length && !b.disabled)"
            ),
            2.0,
        )
        if not page.eval(
            "(() => { const b = [...document.querySelectorAll('[data-clear-project-button]')].find(b => "
            "b.getClientRects().length && !b.disabled); if (!b) return false; b.click(); return true })()"
        ):
            _click(page, NO_PROJECT)
    return bool(_until(lambda: current_project(page) == project, 3.0))


def _topic_chat(ids: list[str], page: Any) -> tuple[str | None, str | None, bool]:
    """(sujet des messages, chat du sujet s'il est visible dans la barre latérale, chat du sujet OUVERT mais introuvable).
    Chat absent ou rangé : (sujet, None, False), un nouveau chat naîtra (et sera mémorisé). Ouvert dans la base de
    l'appli mais jamais retrouvé : (sujet, None, True), le réveil est reporté, jamais un second chat (relecture de
    Codex, 23/10)."""
    from .box import Box

    box = Box()
    message = box.get(ids[0]) if ids else None
    if message is None:
        return None, None, False
    known = (box.topic_state(message.topic, "codex") or {}).get("conversation")
    if not known:
        return message.topic, None, False

    def visible() -> bool:
        return any(conv == known for _, conv, _ in row_conversations(page))

    if visible():
        return message.topic, str(known), False
    # Chat du sujet toujours ouvert dans l'appli (sa base) mais pas affiché (autre page, barre latérale repliée) : on le
    # cherche avant d'en ouvrir un second (23/10 : deux conversations « nexuswindows » après la mise à jour de Codex).
    from . import codex_app

    threads = codex_app._db_threads(codex_app.home())
    row = (threads or {}).get(str(known))
    if threads is codex_app.UNREADABLE or (row is not None and not row["archived"]):
        if not visible() and _press(page, _HOME):
            _until(visible, 2.0)
        if not visible() and _click(page, SHOW_SIDEBAR):
            _until(visible, 2.0)
        if visible():
            return message.topic, str(known), False
        return message.topic, None, True
    return message.topic, None, False


def _restore_view(page: Any, before: list[str], present: Callable[[], bool] = lambda: False) -> bool:
    """Vue de l'utilisateur rendue après une navigation du relais (Accueil, barre latérale) : les repères de page courante perdus
    sont recliqués, sinon le bouton Retour de l'appli (3 fois au plus). L'utilisateur revenu dans l'appli : rien n'est touché,
    sa vue à lui est gardée (relecture de Codex)."""
    if not before or (page.eval(_VIEW) or []) == before:
        return True
    if present():
        return False
    missing = [label for label in before if label not in (page.eval(_VIEW) or [])]
    if missing and _click(page, (missing[-1],)) and _until(lambda: (page.eval(_VIEW) or []) == before, 2.0):
        return True
    for _ in range(3):
        if present() or not _click(page, ("Retour", "Back")):
            break
        if _until(lambda: (page.eval(_VIEW) or []) == before, 1.5):
            return True
    return (page.eval(_VIEW) or []) == before


def _chat_finished(conversation: str) -> bool:
    """Dernier tour du chat fini (journal de Codex) ; journal introuvable : rien ne prouve un tour en cours."""
    from . import codex_app

    path = codex_app.chat_path(conversation)
    return path is None or codex_app.turn_state(path)[0]


def _remember_chat(topic: str, conversation: str) -> None:
    from .box import Box

    box = Box()
    previous = box.topic_state(topic, "codex") or {}
    if previous.get("conversation") != conversation:
        box.set_topic_state(
            topic,
            "codex",
            {"conversation": conversation, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        )


@one_at_a_time(_deferred)
def wake_codex(ids: Iterable[str] = (), port: int = CODEX_PORT) -> dict[str, Any]:
    """Réveil réel de Codex par son port : le waker du relais (wake.py)."""
    ids = list(ids)
    try:
        if user_in_codex():
            return {"status": "deferred", "reason": "appli Codex au premier plan (l'utilisateur y travaille)"}
        ready = ensure_codex(port)
        if ready["status"] != "ready":
            return ready
        url = page_url(port)
        if not url:
            return {"status": "failed", "reason": "page principale de Codex absente"}
        page = Page(url)
    except Exception as error:  # noqa: BLE001 - interface : jamais fatale au relais
        return {"status": "failed", "reason": f"{type(error).__name__}: {error}"}
    mode = None
    report: dict[str, Any] = {}
    answer: dict[str, Any] = {}  # réponse rendue, complétée par le nettoyage du finally (même objet)
    view_before: list[str] = []
    try:
        view_before = (
            page.eval(_VIEW) or []
        )  # vue de l'utilisateur AVANT toute navigation du relais (Accueil, recherche)
        mode = codex_mode(page)
        if not ensure_codex_mode(page):
            return {"status": "failed", "reason": f"mode « {CODEX_MODE} » de l'appli non sélectionné"}
        text = wake_text(ids)
        relay_button = f"(() => !!({_BUTTON})({json.dumps(list(RELAY_NEW_CHAT))}))()"
        # 21/10 : juste après le passage du mode ChatGPT au mode Codex, la liste des projets se dessine en différé ; lue
        # aussitôt, elle disait le projet « absent » à chaque essai (le mode de l'utilisateur rendu ensuite, la course revenait
        # au suivant) : 9 min de réveils ratés. On lui laisse le temps de paraître.
        _until(lambda: page.eval(relay_button), PROJECT_WAIT_S)
        if not page.eval(relay_button) and _press(page, _HOME):
            # 23/10 (appli 26.1016.8622) : laissée sur ses Paramètres, l'appli masque la liste des projets (bouton du
            # projet présent mais invisible) : 7 réveils ratés « projet absent ». Accueil ouvert ; la vue de l'utilisateur est
            # rendue à la fin comme les autres (view_restored dit si elle a pu l'être).
            report["left_page"] = True
            _until(lambda: page.eval(relay_button), 2.0)
        if not page.eval(relay_button) and _click(page, SHOW_SIDEBAR):
            # Barre latérale masquée par l'utilisateur : ouverte le temps de l'envoi, refermée ensuite (vue rendue).
            report["sidebar_reopened"] = True
            _until(lambda: page.eval(relay_button), 2.0)
        if not page.eval(relay_button):
            # sans le projet, le chat naîtrait dans le projet de l'utilisateur et ne serait jamais rangé (tour 2). 21/10 :
            # projet disparu de l'appli ; sa confiance au dossier reste dans ~\.codex\config.toml : le recréer
            # (Fichier > Nouveau projet, même dossier) ne la redemande pas.
            return {
                "status": "failed",
                "reason": f"projet Codex « {RELAY_PROJECT} » absent de l'appli (barre latérale visible) : le recréer "
                f"une fois (Fichier > Nouveau projet, dossier {Path.home() / '.nexus' / 'relay' / 'codex-projet'})",
                "injected": False,
            }
        project = current_project(page)
        topic, existing, lost = _topic_chat(ids, page)
        if lost:
            answer = {
                **report,
                "status": "deferred",
                "reason": "chat du sujet ouvert mais introuvable dans la barre latérale : réessai",
                "injected": False,
            }
            return answer
        if existing and not _chat_finished(existing):
            # tour encore en cours dans le chat du sujet : jamais un second message par-dessus, réveil reporté
            return {"status": "deferred", "reason": "chat du sujet en plein tour", "injected": False}
        result = send_in_new_chat(
            page,
            text,
            ids[0] if ids else "Nexus Relay",
            new_chat=RELAY_NEW_CHAT,
            present=user_in_codex,
            restore_first=lambda: restore_project(page, project),
            report=report,
            existing=existing,
        )
        if topic and report.get("conversation") and result.get("status") in ("started", "uncertain"):
            try:
                _remember_chat(topic, str(report["conversation"]))
            except Exception as error:  # noqa: BLE001 - message parti : l'issue du transport reste, jamais « failed »
                report["remember_error"] = f"{type(error).__name__}: {error}"  # (sinon il serait réinjecté)
        answer = {**result, **report, "via": "cdp", "launched": ready.get("launched", False), "reused": bool(existing)}
        return answer
    except Exception as error:  # noqa: BLE001 - avant l'envoi (mode, page) : rien n'est parti
        return {"status": "failed", "reason": f"{type(error).__name__}: {error}", "injected": False}
    finally:
        # L'utilisateur revenu dans Codex (relu une fois ici) : ni barre latérale, ni vue, ni mode ne sont repris de force
        # (relecture de Codex : la garde ne couvrait que la vue).
        try:
            back = bool(report.get("restore_skipped")) or user_in_codex()
        except Exception:  # noqa: BLE001
            back = True
        if back:
            report.setdefault("restore_skipped", "L'utilisateur est revenu dans Codex")
        else:
            try:
                if report.get("sidebar_reopened"):
                    _click(page, HIDE_SIDEBAR)  # vue de l'utilisateur rendue
            except Exception:  # noqa: BLE001
                pass
            try:
                # Vue d'AVANT toute navigation du relais (Paramètres, chat ouvert…), sur tous les chemins.
                report["view_restored"] = _restore_view(page, view_before, user_in_codex)
            except Exception:  # noqa: BLE001
                report["view_restored"] = False
            try:
                if mode and codex_mode(page) != mode and not user_in_codex():
                    set_codex_mode(page, mode)  # vu le 20/10 : le mode ChatGPT de l'utilisateur restait en mode Codex
            except Exception:  # noqa: BLE001
                pass
        if answer:
            for key in ("view_restored", "restore_skipped"):
                if key in report:
                    answer[key] = report[key]  # la réponse dit ce que le nettoyage a réellement fait
        page.close()


MENU_ARCHIVE = ("Archiver", "Archive")
_ESCAPE = "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))"


def _skipped() -> tuple[str, str | None]:
    return "skipped", BUSY_REASON


@one_at_a_time(_skipped, "antigravity")
def archive_antigravity_conversation(
    conversation: str,
    guard: Callable[[], str | None] = lambda: None,
    port: int = AGY_PORT,
) -> tuple[str, str | None]:
    """« Archive » de la ligne dont l'identifiant de conversation est exactement `conversation` (relevé le 20/10 :
    ligne ``[data-testid=conversation-row-sidebar][data-cascade-id]``, bouton ``conversation-archive-button``) ; jamais
    par le titre (relecture de Codex : « relais » trouvait « relais-v2 »). « clicked » | « skipped » | « absent »."""
    if user_in_agy():
        return "skipped", "L'utilisateur est dans Antigravity"
    url = agy_page_url(port)
    if not url:
        return "skipped", f"port {port} fermé"
    try:
        page = Page(url)
    except Exception as error:  # noqa: BLE001 - port muet : report
        return "skipped", f"port {port} muet : {type(error).__name__}"
    try:
        row = f"""[...document.querySelectorAll('[data-testid=conversation-row-sidebar]')]
            .filter(r => r.getAttribute('data-cascade-id') === {json.dumps(conversation)})"""
        center = page.eval(f"""(() => {{ const rows = {row}; if (rows.length !== 1) return null;
          rows[0].scrollIntoView({{block: 'nearest'}});
          const b = rows[0].getBoundingClientRect(); return b.width ? [b.x + b.width / 2, b.y + b.height / 2] : null }})()""")
        if not center:
            return "absent", None
        # le bouton « Archive » n'existe qu'au survol de la ligne, et seulement si la conversation est finie (sinon
        # « Stop execution ») : survol émulé par le port, sans la souris de l'utilisateur (relevé le 20/10)
        page.call("Input.dispatchMouseEvent", type="mouseMoved", x=center[0], y=center[1])
        button = f"(() => {{ const rows = {row}; const b = rows.length === 1 && rows[0].querySelector('[data-testid=conversation-archive-button]'); if (!b) return false; window.__nexusArchive = b; return true }})()"
        found = _until(lambda: page.eval(button), 1.5)
        if not found:
            page.call("Input.dispatchMouseEvent", type="mouseMoved", x=1, y=1)
            return "absent", None
        reason = guard()
        if reason:
            page.call("Input.dispatchMouseEvent", type="mouseMoved", x=1, y=1)
            return "skipped", reason
        page.eval("(() => { window.__nexusArchive.click(); delete window.__nexusArchive; return true })()")
        return "clicked", None
    finally:
        try:
            page.call("Input.dispatchMouseEvent", type="mouseMoved", x=0, y=0)  # survol émulé toujours retiré
        except Exception:  # noqa: BLE001
            pass
        page.close()


_USAGE_LIMIT = {"Weekly Limit Remaining": "semaine", "Five Hour Limit Remaining": "5 h"}
_USAGE_FAMILIES = ("Gemini Models", "Claude and GPT models")
_PERCENT = re.compile(r"^(\d{1,3}(?:[.,]\d+)?)\s*%$")
_REFRESH = re.compile(r"refresh in (.+?)\.?$", re.IGNORECASE)
_DIALOG_TEXT = "(() => { const d = document.querySelector('[role=dialog]'); return d ? d.innerText : null })()"
_SETTINGS_BUTTON = (
    "(() => { const b = [...document.querySelectorAll('button, [role=button]')].find(b => "
    "(b.getAttribute('aria-label') || b.innerText || '').trim() === 'Settings'); if (!b) return false; b.click(); "
    "return true })()"
)
_MODELS_TAB = (
    "(() => { const b = [...document.querySelectorAll('[role=dialog] button')].find(b => "
    "(b.innerText || '').trim() === 'Models'); if (!b) return false; b.click(); return true })()"
)
_CLOSE = (
    "(() => { const b = [...document.querySelectorAll('[role=dialog] button')].find(b => "
    "(b.getAttribute('aria-label') || b.innerText || '').trim() === 'Close'); if (!b) return false; b.click(); "
    "return true })()"
)


def parse_usage(text: str) -> dict[str, Any]:
    """Texte de « Settings › Models » (relevé le 20/10) → {"plan", "limits": [{family, limit, remaining_pct, resets?}]}."""
    plan, family, limits = None, None, []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Your Plan:"):
            plan = line.split(":", 1)[1].strip()
        elif line in _USAGE_FAMILIES:
            family = line
        elif line in _USAGE_LIMIT and family:
            current = {"family": family, "limit": _USAGE_LIMIT[line]}
            limits.append(current)
        elif current is not None and (refresh := _REFRESH.search(line)):
            current["resets"] = refresh.group(1)
        elif current is not None and (percent := _PERCENT.match(line)) and "remaining_pct" not in current:
            current["remaining_pct"] = float(percent.group(1).replace(",", "."))
    return {"plan": plan, "limits": [x for x in limits if "remaining_pct" in x]}


@one_at_a_time(lambda: {"status": "failed", "reason": BUSY_REASON}, "antigravity")
def read_antigravity_usage(port: int = AGY_PORT, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Quotas d'Antigravity lus dans « Settings › Models » par son port (point I du 20/10) : jamais quand l'utilisateur y est ;
    la vue est rendue (adresse d'origine retrouvée, vérifiée)."""
    from .box import now_iso

    if user_in_agy():
        return {"status": "deferred", "reason": "L'utilisateur est dans Antigravity : quotas non lus"}
    url = agy_page_url(port)
    if not url:
        return {"status": "failed", "reason": f"port {port} muet ou page Antigravity absente"}
    page = Page(url)
    try:
        origin = page.eval("location.href")
        if page.eval(_DIALOG_TEXT) is None and not page.eval(_SETTINGS_BUTTON):
            return {"status": "failed", "reason": "bouton « Settings » introuvable"}
        text = None
        if _until(lambda: page.eval(_DIALOG_TEXT) is not None, STEP_WAIT_S, sleep) and page.eval(_MODELS_TAB):
            text = _until(
                lambda: (t := page.eval(_DIALOG_TEXT)) and "Models & Usage" in t and "%" in t and t, STEP_WAIT_S, sleep
            )
        for _ in range(4):  # vue rendue : retour jusqu'à l'adresse d'origine
            if page.eval("location.href") == origin:
                break
            page.eval("history.back()")
            sleep(0.6)
        restored = page.eval("location.href") == origin
        if not restored and "settingsOpen" not in str(origin):
            restored = bool(page.eval(_CLOSE))
        if not text:
            return {"status": "failed", "reason": "page « Models & Usage » non lue", "view_restored": restored}
        return {"status": "success", "read_at": now_iso(), **parse_usage(text), "view_restored": restored}
    except Exception as error:  # noqa: BLE001
        return {"status": "failed", "reason": f"{type(error).__name__}: {error}"}
    finally:
        page.close()


# --- Antigravity fermé (point I du 20/10) : relancé avec son port, comme Codex
def agy_exe() -> Path | None:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    exe = Path(local) / "Programs" / "antigravity" / AGY_PROCESS
    return exe if exe.exists() else None


def agy_running() -> bool:
    return bool(_ps(f"@(Get-Process {Path(AGY_PROCESS).stem} -ErrorAction SilentlyContinue).Count") not in ("", "0"))


AGY_WORK_S = 300.0  # une conversation d'Antigravity écrite depuis moins de 5 min : un agent travaille


def agy_working(now: float | None = None) -> bool:
    """Un agent d'Antigravity a avancé depuis AGY_WORK_S (journal d'une conversation récemment écrit), quelle qu'elle
    soit : celles de l'utilisateur comptent comme celles du relais."""
    from .paths import brain_dir

    now = time.time() if now is None else now
    try:
        logs = brain_dir().glob("*/.system_generated/logs/transcript.jsonl")
        return any(now - log.stat().st_mtime < AGY_WORK_S for log in logs)
    except OSError:
        return True  # illisible : rien ne prouve l'inactivité, on ne tue pas


def restart_frozen_antigravity(port: int = AGY_PORT) -> dict[str, Any]:
    """Antigravity figé (« ne répond pas ») depuis HUNG_RESTART_S, l'utilisateur hors de l'appli : arrêté puis relancé réduit
    avec son port (son sidecar repart avec lui). Ses conversations restent sur disque."""
    return _restart_antigravity("figé", port)


def _restart_antigravity(why: str, port: int) -> dict[str, Any]:
    """Arrêt puis relance réduite avec le port, jamais pendant que l'utilisateur est dedans ni pendant qu'un de ses agents
    travaille (journaux sur disque, lus sans le port), sous le jeton commun de premier plan."""
    if user_in_agy():
        return {"status": "deferred", "reason": f"Antigravity {why}, mais l'utilisateur est dedans : pas de relance"}
    if agy_working():
        # relecture d'Antigravity : l'utilisateur le laisse travailler en fond (objectifs, tests, compilations) ; sa fenêtre
        # peut ne plus répondre pendant que son agent avance encore : jamais tué pendant un travail
        return {"status": "deferred", "reason": f"Antigravity {why}, mais un de ses agents travaille encore"}
    path = agy_exe()
    if path is None:
        return {"status": "failed", "reason": f"Antigravity {why} et introuvable sur ce PC"}
    reset_hung("antigravity")
    lock = _foreground_lock()
    if not lock.acquire():
        return {"status": "deferred", "reason": "premier plan tenu par un autre agent : relance d'Antigravity reportée"}
    try:
        _ps(f"Get-Process {Path(AGY_PROCESS).stem} -ErrorAction SilentlyContinue | Stop-Process -Force")
        time.sleep(2.0)
        _start_detached(path, port)
    finally:
        lock.release()
    return {
        "status": "failed",
        "reason": f"non livré : Antigravity {why}, relancé avec son port {port}",
        "launched": True,
    }


def start_antigravity(port: int = AGY_PORT) -> dict[str, Any]:
    """Antigravity fermé : lancé réduit avec son port (son sidecar démarre avec lui). Ouvert SANS port ni sidecar
    (lancé par un raccourci sans --remote-debugging-port, 23/10 : injoignable sans fin) : relancé avec son port, mêmes
    gardes qu'un Antigravity figé (l'utilisateur dedans, agent au travail : jamais)."""
    if agy_running():
        return _restart_antigravity("ouvert sans sidecar ni port", port)
    path = agy_exe()
    if path is None:
        return {"status": "failed", "reason": "non livré : Antigravity fermé et introuvable sur ce PC"}
    _start_detached(path, port)  # lancement direct : réduit, sans activation, détaché (pas un paquet Store)
    return {"status": "failed", "reason": "non livré : Antigravity fermé, relancé avec son port 9227", "launched": True}


# --- repères d'interface (point E du 20/10) : ce que le relais doit trouver dans chaque appli, lu sans rien toucher
_HAS_BUTTON = f"(labels) => !!({_BUTTON})(labels)"
CODEX_LANDMARKS = {
    # 23/10 : sur les Paramètres, la liste des projets est masquée (bouton présent, invisible) : non vérifiable (null) ;
    # manquant seulement s'il n'existe plus du tout (le réveil ouvre alors l'Accueil pour le trouver)
    f"bouton « {RELAY_NEW_CHAT[0]} » (projet du relais)": f"(({_HAS_BUTTON})({json.dumps(list(RELAY_NEW_CHAT))})"
    " || ([...document.querySelectorAll('button,[role=button]')].some(b =>"
    f" {json.dumps(list(RELAY_NEW_CHAT))}.includes((b.getAttribute('aria-label') || '').trim())) ? null : false))",
    # relevé le 22/10 : le sélecteur n'est que dans le champ d'un NOUVEAU chat ; chat ouvert (sa ligne est la page
    # courante, le champ dit « Projet : X ») ou page sans champ (Paramètres) : non vérifiable (null), pas manquant
    "sélecteur de projet": "(() => { if (!" + _COMPOSER + " || [...document.querySelectorAll("
    "'[data-app-action-sidebar-thread-row]')].some(r => r.matches('[aria-current=page]') || "
    "r.querySelector('[aria-current=page]'))) return null; return [...document.querySelectorAll('button,[role=button]')]"
    ".some(b => /^(Changer de projet|Change project)/.test(b.getAttribute('aria-label') || '')) })()",
    # absent sur certaines pages : le réveil s'en passe alors (ensure_codex_mode) ; non vérifiable, pas manquant
    "sélecteur de mode (ChatGPT / Codex)": f"!!{_MODE_BUTTON} || null",
    # aucune ligne dans le bloc du relais : non vérifiable (null), pas manquant
    "rangement d'une ligne du relais (« Archiver le chat »)": "(() => {"
    f" const p = [...document.querySelectorAll('[{_PROJECT_ATTR}]')].find(e => e.getAttribute('{_PROJECT_ATTR}') ==="
    f" {json.dumps(RELAY_PROJECT)}); const box = p && p.closest('[data-sidebar-project-container-id]');"
    " const r = box ? box.querySelectorAll('[data-app-action-sidebar-thread-row]') : []; if (!r.length) return null;"
    f" const labels = {json.dumps(list(ARCHIVE))};"
    " return [...r].some(row => !!row.querySelector('button[aria-haspopup=menu]')"
    " || [...row.querySelectorAll('button,[role=button]')].some(b => labels.includes((b.getAttribute('aria-label') || '').trim()))) })()",
}
AGY_LANDMARKS = {
    # aucune conversation affichée : non vérifiable (null), pas manquant
    "lignes de conversation avec leur identifiant": "(() => { const r = document.querySelectorAll("
    "'[data-testid=conversation-row-sidebar]'); return r.length ? [...r].every(e => e.hasAttribute('data-cascade-id')) "
    ": null })()",
    # le bouton « Archive » n'apparaît qu'au survol d'une ligne finie : non vérifiable sans geste (null), pas manquant
    "bouton « Archive » des lignes (au survol)": "!!document.querySelector('[data-testid=conversation-archive-button]') || null",
}


def _check(url: str | None, checks: dict[str, str], port: int) -> dict[str, Any]:
    if not url:
        return {"status": "non testé", "reason": f"port {port} fermé ou page absente"}
    try:
        page = Page(url)
    except Exception as error:  # noqa: BLE001
        return {"status": "non testé", "reason": f"{type(error).__name__}: {error}"}
    try:
        values = {name: page.eval(js) for name, js in checks.items()}
    except Exception as error:  # noqa: BLE001
        return {"status": "non testé", "reason": f"{type(error).__name__}: {error}"}
    finally:
        page.close()
    found = {name: bool(v) for name, v in values.items() if v is not None}
    unknown = [name for name, v in values.items() if v is None]
    missing = [name for name, ok in found.items() if not ok]
    result: dict[str, Any] = {"status": "ok" if not missing else "manquant", "found": found}
    if missing:
        result["missing"] = missing
    if unknown:
        result["unverified"] = unknown
    return result


def landmarks() -> dict[str, Any]:
    """Boutons et menus attendus par le relais, trouvés ou non, dans Codex et Antigravity (lecture seule)."""
    agy = _check(agy_page_url(AGY_PORT), AGY_LANDMARKS, AGY_PORT)
    if agy.get("reason", "").startswith(f"port {AGY_PORT}") and agy_running():
        # vu le 22/10 : relancé par sa mise à jour automatique (2.25, « --updated ») avec l'option du port, Antigravity
        # n'ouvrait pas ce port ; rangement, crédits et repères restent aveugles jusqu'à sa prochaine relance
        agy["reason"] = f"Antigravity ouvert mais son port {AGY_PORT} n'écoute pas : le relancer (fermer puis rouvrir)"
    return {"codex": _check(page_url(CODEX_PORT), CODEX_LANDMARKS, CODEX_PORT), "antigravity": agy}
