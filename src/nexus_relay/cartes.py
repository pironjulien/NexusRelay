"""Cartes des interfaces de Codex et d'Antigravity (demande de l'utilisateur du 20/10, suite du point E).

Le 20/10, deux changements d'interface de Codex (« Archiver » passé dans le menu « Actions du chat », sélecteur de mode
ChatGPT/Codex avec un double caché) ont cassé le relais sans prévenir. Une carte faite à la main se périme ; ici :

- relevé par le port de débogage, sans effet de bord : chaque page de navigation et chaque menu atteignables sont
  ouverts, lus et refermés par Échap ; jamais un clic sur une action (envoyer, archiver, supprimer, compte, réglage) ;
  jamais pendant que l'utilisateur est dans l'appli ; sa vue est rendue à la fin ;
- index sans naviguer : les libellés de l'appli lus dans son paquet (Codex : fichiers de traduction de app.asar ;
  Antigravity : son bundle main.js, servi par son serveur local), comme l'index de recherche des Paramètres Android ;
- cartes datées et versionnées hors OneDrive (~\\.nexus\\relay\\v1\\cartes\\<appli>-<version>.json), nouveau relevé
  quand la version change, différence lisible avec la précédente, repères du relais signalés s'ils disparaissent ;
- where(appli, quoi) : où est un élément, quel repère, comment l'atteindre par le port.
"""

from __future__ import annotations

import json
import re
import struct
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from . import cdp, wake
from .box import now_iso
from .paths import relay_home

APPS = ("codex", "antigravity")
MENU_WAIT_S = 0.8
PAGE_WAIT_S = 1.2
CHECK_EVERY_S = 1800.0  # version de chaque appli relue toutes les 30 min au plus
MAX_PAGES = 40
MAX_MENUS = 60
UI_WAIT_S = 90.0  # attente d'un réveil en cours avant un relevé
# Jamais ouvert ni cliqué, même si c'est une page ou un menu (filet en plus de la règle : on ne clique que des liens de
# navigation et des déclencheurs de menu)
DANGER = re.compile(
    r"envoy|send|archiv|supprim|delete|remove|retir|déconnect|deconnect|log ?out|sign ?out|quitter|quit|exit|"
    r"acheter|buy|upgrade|abonn|install|réinitial|reset|feedback|commentaire|signaler|report|stop|arrêt|"
    r"changer de compte|switch account|ajouter un compte|add account|nouvel|nouveau|nouvelle|new |fermer|close|"
    r"dupliquer|duplicate|clear|effac|vider|purg|trash|corbeille|run\b|lancer|exécut|execut|start|play|revoke|révoqu|"
    r"disconnect|import|export|download|télécharg",
    re.I,
)

# --- relevé dans la page (JavaScript fixe, lecture seule)
# Jamais de contenu : ni valeur de champ, ni texte d'un élément qui n'est pas un bouton, un lien ou une entrée de menu ;
# les libellés inconnus du paquet de l'appli sont ensuite masqués (Known.mask), avant tout stockage.
_SNAPSHOT = r"""(scope) => {
  const vis = e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const VOLATILE = /^data-(state|selected|tooltip-id|radix|base-ui|disabled|highlighted|orientation|side|align|open)/;
  const RANDOM = /^:r|[0-9a-f]{8}-[0-9a-f]{4}|^[0-9a-f]{16,}$/i;
  const root = scope ? [...document.querySelectorAll(scope)].filter(vis) : [document];
  const IMPLICIT = {BUTTON: 'button', A: 'link', INPUT: 'textbox', TEXTAREA: 'textbox', SELECT: 'combobox'};
  const TEXT_ROLES = ['button', 'link', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'tab', 'option', 'treeitem',
    'checkbox', 'radio', 'switch'];
  // routes internes seulement (ids de conversation masqués) ; tout le reste : « externe »
  const route = h => /^\/(?!\/)/.test(h) ? h.split(/[?#]/)[0].replace(/\/c\/[^/]+/, '/c/*').slice(0, 60)
    : (h.startsWith('#') ? '#' : 'externe');
  const region = e => {
    for (let p = e.parentElement; p; p = p.parentElement) {
      const r = p.getAttribute('role'), t = p.tagName;
      if (['menu', 'listbox', 'dialog', 'navigation', 'tablist', 'menubar'].includes(r) || ['NAV', 'ASIDE', 'HEADER', 'MAIN', 'FOOTER'].includes(t))
        return (r || t.toLowerCase()) + (p.getAttribute('aria-label') ? ':' + p.getAttribute('aria-label') : '');
    }
    return '';
  };
  const out = [], seen = new Set();
  for (const base of root) for (const e of base.querySelectorAll(
      'button, a[href], input, textarea, select, [role], [aria-label], [data-testid], [contenteditable=true], [aria-haspopup]')) {
    if (seen.has(e) || !vis(e) || e.closest('[data-ending-style], [data-closed]')) continue;
    seen.add(e);
    const role = e.getAttribute('role') || IMPLICIT[e.tagName] || (e.isContentEditable ? 'textbox' : '');
    const aria = (e.getAttribute('aria-label') || e.getAttribute('title') || e.getAttribute('placeholder') || '').trim();
    const field = ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName) || e.isContentEditable;
    const text = TEXT_ROLES.includes(role) && !field ? (e.innerText || '').trim().split('\n')[0].trim().slice(0, 80) : '';
    if (!role && !aria && !e.getAttribute('data-testid')) continue;
    if (['presentation', 'none', 'group', 'generic', 'img', 'separator'].includes(role) && !aria) continue;
    const data = {};
    for (const a of e.attributes) if (a.name.startsWith('data-') && !VOLATILE.test(a.name))
      data[a.name] = RANDOM.test(a.value) || /title|name|label/.test(a.name) ? '*' : a.value.slice(0, 60);  // titres de l'utilisateur : jamais stockés
    const item = {role, aria, text: aria && text === aria ? '' : text, data, region: region(e)};
    if (e.getAttribute('aria-haspopup')) item.popup = e.getAttribute('aria-haspopup');
    if (e.getAttribute('aria-current') || e.getAttribute('aria-selected') === 'true') item.current = true;
    if (e.getAttribute('href')) item.href = route(e.getAttribute('href'));
    out.push(item);
    if (out.length >= 1500) break;
  }
  return out;
}"""
# menus ouverts (pas ceux qui finissent de se fermer : fenêtre cachée, leur animation de sortie attend qu'elle se montre)
_OPEN_LAYERS = (
    "[...document.querySelectorAll('[role=menu], [role=listbox], [role=dialog]')]"
    ".filter(e => e.getClientRects().length && !e.hasAttribute('data-ending-style') && !e.hasAttribute('data-closed')).length"
)
_JS_ESCAPE = (
    "(() => { const t = document.activeElement || document.body; t.dispatchEvent(new KeyboardEvent('keydown',"
    " {key: 'Escape', code: 'Escape', bubbles: true, cancelable: true})); return 1 })()"
)


def _element_js(locator: dict[str, Any]) -> str:
    """Expression JS qui retrouve l'élément VISIBLE décrit par `locator` (css et/ou role + libellé)."""
    return f"""(() => {{
      const L = {json.dumps(locator)};
      const label = e => (e.getAttribute('aria-label') || e.getAttribute('title') || '').trim();
      const first = e => (e.innerText || '').trim().split('\\n')[0].trim();
      let c = [...document.querySelectorAll(L.css || '*')].filter(e => e.getClientRects().length);
      if (L.role) c = c.filter(e => (e.getAttribute('role') || {{BUTTON: 'button', A: 'link'}}[e.tagName] || '') === L.role);
      if (L.aria) c = c.filter(e => label(e) === L.aria);
      if (L.text) c = c.filter(e => first(e).slice(0, 80) === L.text);
      if (L.aria_re) c = c.filter(e => new RegExp(L.aria_re, 's').test(label(e).replace(/\u00a0/g, ' ')));
      if (L.text_re) c = c.filter(e => new RegExp(L.text_re, 's').test(first(e).replace(/\u00a0/g, ' ')));
      return c[L.nth || 0] || null;
    }})()"""


def locator_of(item: dict[str, Any]) -> dict[str, Any]:
    """Le moyen le plus stable de retrouver un élément : attribut data-* de l'appli, sinon rôle + aria-label, sinon
    rôle + texte."""
    data = item.get("data") or {}
    for key in ("data-testid", "data-sidebar-destination", "data-app-action-sidebar-thread-title"):
        if data.get(key) and data[key] != "*":
            return {"css": f"[{key}={json.dumps(data[key])}]"}
    for key, value in data.items():
        if value and value != "*" and key not in ("data-color", "data-variant", "data-size", "data-icon-size"):
            locator: dict[str, Any] = {"css": f"[{key}={json.dumps(value)}]"}
            break
    else:
        locator = {}
    if item.get("role"):
        locator["role"] = item["role"]
    for field in ("aria", "text"):
        value = item.get(field)
        if value and value != "*":
            if TEMPLATE_VAR.search(value):  # modèle stocké : jamais comparé tel quel (relecture de Codex)
                locator[f"{field}_re"] = template_regex(value)
            else:
                locator[field] = value
            break
    return locator


TEMPLATE_VAR = re.compile(r"\$?\{[^{}]*\}")


def template_regex(label: str) -> str:
    """Expression régulière JavaScript (et Python) d'un libellé-modèle : parties fixes échappées, variables « .+ »."""
    return (
        "^"
        + ".+".join(
            re.sub(r"[.*+?^${}()|[\]\\/]", lambda m: "\\" + m.group(), part) for part in TEMPLATE_VAR.split(label)
        )
        + "$"
    )


def _collapse(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lignes répétées (chats, projets) : une seule gardée, texte remplacé par le nombre (titres de l'utilisateur non stockés)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        key = json.dumps([item.get("role"), item.get("aria"), item.get("data"), item.get("region"), item.get("popup")])
        groups.setdefault(key, []).append(item)
    out: list[dict[str, Any]] = []
    for group in groups.values():
        texts = {g.get("text") for g in group}
        if len(group) > 3 and len(texts) > 3:
            out.append({**group[0], "text": "", "repeated": len(group)})
        else:
            unique: dict[str, dict[str, Any]] = {}
            for g in group:
                unique.setdefault(str(g.get("text")), g)
            out.extend(unique.values())
    return out


# --- configuration par appli
class AppSpec:
    def __init__(
        self,
        name: str,
        port: Callable[[], str | None],
        present: Callable[[], bool],
        pages: str,
        subpages: str,
        menu_pages: tuple[str, ...],
        reveal: str = "",
        triggers: dict[str, str] | None = None,
        js_open: bool = False,
        back: dict[str, Any] | None = None,
    ) -> None:
        self.name, self.port, self.present = name, port, present
        # navigation : seulement les cibles de ces sélecteurs (routes de l'appli), revalidées juste avant le clic
        self.pages, self.subpages, self.menu_pages = pages, subpages, menu_pages
        self.reveal = reveal  # survolé avant la lecture : boutons qui n'apparaissent qu'au survol (lignes de chat)
        self.triggers = triggers or {}  # menus sans aria-haspopup (barre de menus d'Antigravity)
        # Antigravity (base-ui) : fenêtre cachée, ses menus ignorent le clic émulé par le port ; click() JavaScript
        self.js_open = js_open
        self.back = (
            back  # bouton « Retour » de l'appli : dernier recours pour rendre la vue (pages sans barre latérale)
        )


def _codex_url() -> str | None:
    return cdp.page_url(cdp.CODEX_PORT)


def _agy_url() -> str | None:
    return cdp.agy_page_url(cdp.AGY_PORT)


def _foreground(process: str) -> bool:
    """La fenêtre au premier plan est à l'appli, quelle qu'elle soit (ses menus et popups n'ont souvent pas de titre :
    relecture d'Antigravity)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return False
        return buffer.value.rsplit("\\", 1)[-1].lower() == process.lower()
    finally:
        kernel32.CloseHandle(handle)


SPECS = {
    "codex": AppSpec(
        "codex",
        _codex_url,
        lambda: _foreground(cdp.CODEX_PROCESS),
        pages="[data-sidebar-destination^='builtin:']",
        # onglets d'une page (pas ceux des chats) et rubriques des Paramètres (sauf le compte)
        subpages="[role=tablist]:not([aria-label*='chat' i]) [role=tab],"
        " [data-settings-panel-slug]:not([data-settings-panel-slug=account])",
        back={"role": "button", "aria_re": "^(Retour|Back)$"},
        menu_pages=("Paramètres", "Settings"),
        reveal="[data-app-action-sidebar-thread-row]",
    ),
    "antigravity": AppSpec(
        "antigravity",
        _agy_url,
        lambda: _foreground(cdp.AGY_PROCESS),
        # routes de l'appli seulement (relevé du 20/10) ; un nouveau lien de navigation apparaît dans la différence
        pages="[data-testid=history-button], [data-testid=automations-button], a[href='/customizations'],"
        " [data-testid=settings-button]",
        subpages="[data-testid^=settings-nav-item-]:not([data-testid$=Account]):not([data-testid*=Feedback])",
        menu_pages=(),
        reveal="a[href^='/c/']",
        triggers={"data-testid": "title-menu-bar-item"},
        js_open=True,
    ),
}


class Known:
    """Libellés que l'appli porte dans son paquet (traductions, chaînes du bundle). Politique de stockage (relectures
    de Codex) : TOUT champ stocké est un libellé du paquet ou une valeur structurelle admise, sinon « * » ; un libellé
    rendu d'après un modèle (« Supprimer {filename} ») est stocké sous son modèle, jamais avec la valeur substituée."""

    STRUCTURAL = frozenset({"data-testid", "data-sidebar-destination", "data-slot", "data-variant", "data-color"})
    TOKEN = re.compile(r"[A-Za-z0-9:_\- ]{1,60}")

    def __init__(self, labels: Iterable[str]) -> None:
        self.exact: set[str] = set()
        self.templates: dict[str, list[tuple[re.Pattern[str], str]]] = {}
        found: list[tuple[int, str, re.Pattern[str], str]] = []
        for label in labels:
            label = label.replace("\xa0", " ").strip()
            if not label:
                continue
            if "{" not in label:
                self.exact.add(label)
                continue
            # « Changer de mode, mode actuel : {mode} » (Codex) ou « Select model, current: ${x} » (bundle)
            parts = re.split(r"\$?\{[^{}]*\}", label)
            if len(parts) < 2 or sum(len(p.strip()) for p in parts) < 6:  # « {a} · {b} » laisserait tout passer
                continue
            pattern = re.compile(".+".join(re.escape(p) for p in parts), re.S)
            found.append((sum(len(p) for p in parts), label, pattern, parts[0][:4]))
        for _, label, pattern, prefix in sorted(found, key=lambda f: (-f[0], f[1])):
            self.templates.setdefault(prefix, []).append((pattern, label))

    def canon(self, text: str) -> str | None:
        """Le libellé tel que le paquet l'écrit (modèle compris), ou None s'il n'en vient pas."""
        text = text.replace("\xa0", " ").strip()
        if text in self.exact:
            return text
        if "@" in text:  # une adresse n'est jamais un libellé de l'appli
            return None
        hits = [(p, label) for p, label in [*self.templates.get(text[:4], []), *self.templates.get("", [])]]
        matching = [label for p, label in hits if p.fullmatch(text)]
        return max(matching, key=lambda label: (len(re.sub(r"\$?\{[^{}]*\}", "", label)), label), default=None)

    def __contains__(self, text: object) -> bool:
        return isinstance(text, str) and self.canon(text) is not None

    def label(self, value: Any) -> Any:
        return value if not value else (self.canon(str(value)) or "*")

    def route(self, href: str) -> str:
        """Route interne : premier segment gardé s'il vient du paquet, le reste masqué, sans requête ni fragment."""
        if not href.startswith("/"):
            return href  # « externe » ou « # » (décidé dans la page)
        segments = [s for s in re.split(r"[?#]", href)[0].split("/") if s]
        if not segments:
            return "/"
        first = segments[0] if f"/{segments[0]}" in self.exact or segments[0] in self.exact else "*"
        return "/" + "/".join([first, *("*" for _ in segments[1:])])

    def data(self, values: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in values.items():
            value = str(value)
            plain = value in ("", "*", "true", "false")
            structural = key in self.STRUCTURAL and bool(self.TOKEN.fullmatch(value))
            out[key] = value if plain or structural or value in self.exact else "*"
        return out

    def mask(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in items:
            item = dict(item)
            item["aria"], item["text"] = self.label(item.get("aria")), self.label(item.get("text"))
            region, _, name = str(item.get("region") or "").partition(":")
            item["region"] = f"{region}:{self.label(name)}" if name else region
            item["data"] = self.data(item.get("data") or {})
            if item.get("href"):
                item["href"] = self.route(str(item["href"]))
            out.append(item)
        return out


_DRAFT = (
    "(() => [...document.querySelectorAll('[contenteditable=true], textarea')].filter(e => e.getClientRects().length)"
    ".some(e => ((e.value ?? e.innerText) || '').trim().length > 0))()"
)
_AGY_RUNNING = (
    "!![...document.querySelectorAll('button')].find(b => b.getClientRects().length"
    " && /^(Stop execution|Stop generating|Stop)$/i.test((b.getAttribute('aria-label') || '').trim()))"
)


def busy(app: str, page: Any) -> str | None:
    """Raison de ne pas faire le relevé : l'agent répond (naviguer pourrait gêner son tour) ou un brouillon attend dans
    le champ (celui de l'utilisateur : une navigation pourrait le perdre). Lu, jamais montré : seul « oui / non » sort."""
    if app == "codex" and cdp.codex_busy():
        return "Codex répond (tour en cours)"
    if app == "antigravity" and page.eval(_AGY_RUNNING):
        return "Antigravity exécute une conversation"
    if page.eval(_DRAFT):
        return "un brouillon attend dans le champ de saisie"
    return None


class Abort(RuntimeError):
    """L'utilisateur est revenu dans l'appli (ou un menu ne se referme pas) : relevé arrêté, vue rendue."""


class Surveyor:
    """Un relevé d'une appli par son port. Ne clique que des cibles de navigation de l'appli (sélecteurs de AppSpec,
    revalidées juste avant le clic) et des déclencheurs de menu ; lit les sous-menus au survol ; referme chaque menu par
    Échap et vérifie qu'il est fermé. L'utilisateur revient : plus aucun geste, pas même la remise en place de la vue.

    Deux lectures : `raw` (libellés réels, en mémoire, pour retrouver un élément) et `snapshot` (libellés inconnus du
    paquet masqués) : seule la seconde est stockée."""

    def __init__(
        self, page: Any, spec: AppSpec, known: Known | None = None, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self.page, self.spec, self.sleep = page, spec, sleep
        self.known = known or Known(())  # rien de connu : tout libellé masqué
        self.menus_done: set[str] = set()
        self.notes: list[str] = []

    def guard(self) -> None:
        if self.spec.present():
            raise Abort("L'utilisateur est dans l'appli")

    def raw(self, scope: str = "") -> list[dict[str, Any]]:
        """Lecture brute, en mémoire seulement (libellés réels : retrouver un élément) ; jamais stockée."""
        return self.page.eval(f"({_SNAPSHOT})({json.dumps(scope)})") or []

    def snapshot(self, scope: str = "") -> list[dict[str, Any]]:
        """Lecture stockable : libellés inconnus du paquet masqués."""
        return self.known.mask(self.raw(scope))

    def press(self, locator: dict[str, Any]) -> bool:
        self.guard()
        return cdp._press(self.page, _element_js(locator))  # pyright: ignore[reportPrivateUsage]

    def hover(self, locator: dict[str, Any]) -> bool:
        self.guard()
        center = self.page.eval(
            f"(() => {{ const e = {_element_js(locator)}; if (!e) return null; const r = e.getBoundingClientRect();"
            " return r.width ? [r.x + r.width / 2, r.y + r.height / 2] : null })()"
        )
        if not center:
            return False
        self.page.call("Input.dispatchMouseEvent", type="mouseMoved", x=center[0], y=center[1])
        return True

    def escape(self) -> None:
        self.guard()
        if self.spec.js_open:
            self.page.eval(_JS_ESCAPE)
            return
        for kind in ("rawKeyDown", "keyUp"):
            self.page.call("Input.dispatchKeyEvent", type=kind, key="Escape", code="Escape", windowsVirtualKeyCode=27)

    def open(self, locator: dict[str, Any]) -> bool:
        self.guard()
        if not self.spec.js_open:
            return self.press(locator)
        js = f"(() => {{ const e = {_element_js(locator)}; if (!e) return false; e.click(); return true }})()"
        return bool(self.page.eval(js))

    def close_layers(self, before: int, trigger: dict[str, Any] | None = None) -> None:
        """Échap jusqu'à la fermeture vérifiée ; en dernier recours, le même déclencheur re-cliqué (il replie son menu :
        vu le 20/10 sur une liste des réglages d'Antigravity, fenêtre cachée)."""
        for attempt in range(6):
            if int(self.page.eval(_OPEN_LAYERS) or 0) <= before:
                return
            self.guard()  # l'utilisateur revenu : on ne touche plus à rien, même pas Échap
            if attempt == 4 and trigger is not None:
                self.open(locator_of(trigger))
            else:
                self.escape()
            self.sleep(0.5)
        raise Abort("un menu ne se referme pas par Échap")

    def menu(self, trigger: dict[str, Any], name: str, path: list[str]) -> list[dict[str, Any]]:
        """Ouvre le menu de `trigger` (lecture brute), lit ses entrées (et sous-menus au survol), le referme. `name` :
        son libellé stockable."""
        if DANGER.search(_label(trigger)):
            return []
        before_layers = int(self.page.eval(_OPEN_LAYERS) or 0)
        before = {json.dumps(i, sort_keys=True) for i in self.raw("[role=menu], [role=listbox], [role=dialog]")}
        self.guard()
        if not self.open(locator_of(trigger)):
            return []
        self.sleep(MENU_WAIT_S)
        items: list[dict[str, Any]] = []
        try:
            fresh = [
                i
                for i in self.raw("[role=menu], [role=listbox], [role=dialog]")
                if json.dumps(i, sort_keys=True) not in before
            ]
            here = [*path, f"menu « {name} »"]
            items += [{**i, "path": here} for i in self.known.mask(fresh)]
            for item in fresh:
                if (
                    not self.spec.js_open  # sous-menus au survol : seulement avec le pointeur émulé (Codex)
                    and item.get("popup")
                    and item.get("role", "").startswith("menuitem")
                    and not DANGER.search(_label(item))
                ):
                    sub_before = {json.dumps(i, sort_keys=True) for i in self.raw("[role=menu]")}
                    self.guard()
                    if self.hover(locator_of(item)):
                        self.sleep(MENU_WAIT_S)
                        sub = f"sous-menu « {_label(self.known.mask([item])[0])} »"
                        news = [s for s in self.raw("[role=menu]") if json.dumps(s, sort_keys=True) not in sub_before]
                        items += [{**s, "path": [*here, sub]} for s in self.known.mask(news)]
        finally:
            self.close_layers(before_layers, trigger)
        return items

    def page_items(self, path: list[str]) -> list[dict[str, Any]]:
        """Éléments de la vue courante, puis entrées de chacun de ses menus (une fois par déclencheur distinct)."""
        self.guard()
        if self.spec.reveal:
            self.hover({"css": self.spec.reveal})
            self.sleep(0.3)
        raw = self.raw()
        masked = self.known.mask(raw)
        items = [{**i, "path": path} for i in _collapse(masked)]
        for trigger, shown in zip(raw, masked, strict=True):
            if not self.is_trigger(trigger):
                continue
            key = json.dumps([shown.get("aria"), shown.get("data"), shown.get("region")], sort_keys=True)
            if key in self.menus_done or len(self.menus_done) >= MAX_MENUS:
                continue
            self.menus_done.add(key)
            try:
                items.extend(self.menu(trigger, _label(shown) or "?", path))
            except Abort:
                raise
            except Exception as error:  # noqa: BLE001 - un menu illisible n'arrête pas le relevé
                self.notes.append(f"menu « {_label(shown)} » : {type(error).__name__}: {error}")
        return items

    def is_trigger(self, item: dict[str, Any]) -> bool:
        data = item.get("data") or {}
        # listes déroulantes (aria-haspopup=listbox) jamais ouvertes : ce sont des réglages (une valeur à choisir), pas
        # des menus ; vu le 20/10, celle des permissions d'Antigravity ne se refermait pas, fenêtre cachée
        return item.get("popup") in ("menu", "true") or (
            bool(self.spec.triggers) and all(data.get(k) == v for k, v in self.spec.triggers.items())
        )

    def nav_targets(self, selector: str) -> list[tuple[dict[str, Any], str]]:
        """(cible brute, nom stockable) des éléments visibles de `selector`, sans libellé dangereux."""
        raw = self.raw()
        return [
            (r, _label(m) or "?")
            for r, m in zip(raw, self.known.mask(raw), strict=True)
            if not DANGER.search(_label(r)) and self.allowed(r, selector)
        ]

    def allowed(self, item: dict[str, Any], selector: str) -> bool:
        """La cible est toujours un élément de `selector`, et un lien ne mène qu'à une route interne de l'appli."""
        href = item.get("href")
        if href is not None and href != "#" and not str(href).startswith("/"):
            return False
        js = (
            f"(() => {{ const e = {_element_js(locator_of(item))};"
            f" return !!(e && e.matches({json.dumps(selector)})) }})()"
        )
        return bool(self.page.eval(js))

    def view(self) -> dict[str, Any]:
        return self.page.eval(
            "(() => ({href: location.href, current: [...document.querySelectorAll('[aria-current=page]')]"
            ".map(e => { const data = Object.fromEntries([...e.attributes].filter(a => a.name === 'data-sidebar-destination'"
            " || a.name === 'data-app-action-sidebar-thread-id').map(a => [a.name, a.value]));"
            " return Object.keys(data).length ? {data} : {aria: (e.getAttribute('aria-label') || '').trim(),"
            " text: (e.innerText || '').trim().split('\\n')[0].trim().slice(0, 80), data} }),"
            f" layers: {_OPEN_LAYERS}}}))()"
        )

    def restore(self, before: dict[str, Any], steps: int = 3) -> bool:
        """Vue de l'utilisateur rendue : même adresse (Antigravity) et mêmes éléments « page courante » (Codex : destination
        puis chat, ou « Retour » quand ils ne sont plus à l'écran, vu le 20/10 sur ses Paramètres). Jamais s'il est
        dans l'appli : sa vue est alors la sienne."""
        for _ in range(steps):
            if self.view() == before:
                return True
            if self.spec.present():
                self.notes.append("vue laissée telle quelle : l'utilisateur est dans l'appli")
                return False
            now = self.view()
            if int(now.get("layers") or 0) > int(before.get("layers") or 0):
                self.close_layers(int(before.get("layers") or 0))
                continue
            if now.get("href") != before.get("href"):
                self.guard()
                self.page.eval(
                    f"(() => {{ history.pushState(null, '', {json.dumps(before.get('href'))});"
                    " dispatchEvent(new PopStateEvent('popstate', {state: null})); return true })()"
                )
                self.sleep(PAGE_WAIT_S)
                continue
            shown = now.get("current") or []
            pressed = False
            for current in reversed(before.get("current") or []):  # le plus précis d'abord : la ligne du chat ouvert
                if current in shown:
                    continue
                data = current.get("data") or {}
                if data:
                    key, value = next(iter(data.items()))
                    locator: dict[str, Any] = {"css": f"[{key}={json.dumps(value)}]"}
                elif current.get("aria"):
                    locator = {"aria": current["aria"]}
                else:
                    locator = {"text": current.get("text") or "", "css": "[aria-current], button, a, [role=button]"}
                if self.press(locator):
                    pressed = True
                    break
            if not pressed:
                # « Retour » seulement loin de la vue d'avant (aucun de ses éléments à l'écran) ; sinon, le chat d'avant
                # a disparu (rangé entre-temps) : on s'arrête là, c'est dit
                lost = not any(c in shown for c in before.get("current") or [])
                if not (lost and self.spec.back and self.press(self.spec.back)):
                    break
            self.sleep(PAGE_WAIT_S)
        restored = self.view() == before
        if not restored:
            self.notes.append("vue non rendue à l'identique (chat rangé ou renommé entre-temps ?)")
        return restored

    def visit(self, target: dict[str, Any], name: str, selector: str, parent: list[str]) -> list[str] | None:
        """Ouvre une page : cible revalidée juste avant le clic. Rend son chemin, ou None (rien cliqué)."""
        self.guard()
        if "via_menu" in target:
            before_layers = int(self.page.eval(_OPEN_LAYERS) or 0)
            if not self.open(locator_of(target["via_menu"])):
                return None
            self.sleep(MENU_WAIT_S)
            entry = target["entry"]
            if _label(entry) not in self.spec.menu_pages or not self.allowed(entry, "[role^=menuitem]"):
                self.close_layers(before_layers)
                return None
            target = entry
        elif not self.allowed(target, selector):
            self.notes.append(f"page « {name} » : plus une cible de navigation au moment du clic")
            return None
        if not self.press(locator_of(target)):
            self.notes.append(f"page « {name} » : introuvable au moment du clic")
            return None
        self.sleep(PAGE_WAIT_S)
        return [*parent, f"page « {name} »"]

    def _entries(self, trigger: dict[str, Any]) -> list[dict[str, Any]]:
        """Entrées brutes du menu de `trigger` (ouvert puis refermé), pour y trouver une entrée de navigation."""
        before_layers = int(self.page.eval(_OPEN_LAYERS) or 0)
        self.guard()
        if not self.open(locator_of(trigger)):
            return []
        self.sleep(MENU_WAIT_S)
        try:
            return [i for i in self.raw("[role=menu]") if i.get("role", "").startswith("menuitem")]
        finally:
            self.close_layers(before_layers)

    def run(self) -> dict[str, Any]:
        before = self.view()
        start = time.monotonic()
        elements: list[dict[str, Any]] = []
        pages: list[str] = []
        status = "complet"
        try:
            elements += self.page_items(["vue de départ"])
            visited: set[str] = set()
            queue: list[tuple[dict[str, Any], str, str, list[str]]] = [
                (t, n, self.spec.pages, []) for t, n in self.nav_targets(self.spec.pages)
            ]
            # pages ouvertes par une entrée nommée d'un menu (Paramètres de Codex), jamais une action
            if self.spec.menu_pages:
                for trigger in [r for r in self.raw() if self.is_trigger(r) and not DANGER.search(_label(r))]:
                    for entry in self._entries(trigger):
                        if _label(entry) in self.spec.menu_pages:
                            queue.append(({"via_menu": trigger, "entry": entry}, _label(entry), "", []))
            while queue and len(pages) < MAX_PAGES:
                target, name, selector, parent = queue.pop(0)
                key = json.dumps([locator_of(target.get("entry") or target), parent], sort_keys=True)
                if key in visited:
                    continue
                visited.add(key)
                path = self.visit(target, name, selector, parent)
                if path is None:
                    continue
                pages.append(" > ".join(path))
                elements += self.page_items(path)
                if not parent:  # sous-pages (onglets, rubriques des Paramètres) : un niveau
                    queue[0:0] = [(t, n, self.spec.subpages, path) for t, n in self.nav_targets(self.spec.subpages)]
        except Abort as error:
            status = f"interrompu : {error}"
        finally:
            try:
                restored = self.restore(before, steps=len(pages) + 4)
            except Abort:
                restored = False
                self.notes.append("vue laissée telle quelle : l'utilisateur est revenu pendant la remise en place")
            except Exception as error:  # noqa: BLE001 - dit, jamais caché
                restored = False
                self.notes.append(f"vue non rendue : {type(error).__name__}: {error}")
        return {
            "status": status,
            "pages": pages,
            "elements": elements,
            "menus": len(self.menus_done),
            "chat_open": any(
                "data-app-action-sidebar-thread-id" in (c.get("data") or {}) for c in before.get("current") or []
            ),
            "view_restored": restored,
            "seconds": round(time.monotonic() - start, 1),
            "notes": self.notes,
        }


# --- index des libellés, sans naviguer
def _asar_files(path: Path) -> tuple[dict[str, tuple[int, int]], int]:
    with path.open("rb") as handle:
        head = handle.read(16)
        size = struct.unpack("<I", head[4:8])[0]
        length = struct.unpack("<I", head[12:16])[0]
        header = json.loads(handle.read(length))
    files: dict[str, tuple[int, int]] = {}

    def walk(node: dict[str, Any], prefix: str) -> None:
        for name, child in (node.get("files") or {}).items():
            if "files" in child:
                walk(child, f"{prefix}/{name}")
            elif "offset" in child:
                files[f"{prefix}/{name}"] = (int(child["offset"]), int(child["size"]))

    walk(header, "")
    return files, 8 + size


def asar_read(path: Path, inner: str) -> bytes:
    files, base = _asar_files(path)
    offset, size = files[inner]
    with path.open("rb") as handle:
        handle.seek(base + offset)
        return handle.read(size)


_LOCALE_PAIR = re.compile(r'"([A-Za-z][\w.\-]*)":(?:`((?:[^`\\]|\\.)*)`|"((?:[^"\\]|\\.)*)")')


_ESCAPE = re.compile(r"\\(x[0-9A-Fa-f]{2}|u\{[0-9A-Fa-f]+\}|u[0-9A-Fa-f]{4}|.)", re.S)
_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def js_unescape(text: str) -> str:
    """Littéral JavaScript → texte affiché (relecture de Codex : « mode actuel\\xA0: {mode} » du paquet)."""

    def one(m: re.Match[str]) -> str:
        code = m.group(1)
        if code[0] in "xu" and len(code) > 1:
            return chr(int(code[1:].strip("{}"), 16))
        return _SIMPLE.get(code, code)

    return _ESCAPE.sub(one, text)


def parse_locale(source: str) -> dict[str, str]:
    """Fichier de traduction du bundle de Codex : « "clé":`texte` » ou « "clé":"texte" », échappements décodés."""
    return {
        m.group(1): js_unescape(m.group(2) if m.group(2) is not None else m.group(3))
        for m in _LOCALE_PAIR.finditer(source)
    }


def codex_index(lang: str = "fr-FR", asar: Path | None = None) -> dict[str, str]:
    """Libellés de l'interface de Codex (clé stable → texte), lus dans son paquet du Store."""
    if asar is None:
        exe = cdp.codex_exe()
        if exe is None:
            return {}
        asar = exe.parent / "resources" / "app.asar"
    files, _ = _asar_files(asar)
    labels: dict[str, str] = {}
    for inner in sorted(files):
        name = inner.rsplit("/", 1)[-1]
        if inner.startswith("/webview/assets/") and name.startswith(f"{lang}-") and name.endswith(".js"):
            labels.update(parse_locale(asar_read(asar, inner).decode("utf-8", "replace")))
        elif inner == f"/native-menu-locales/{lang}.json":
            labels.update({f"menu:{k}": v for k, v in json.loads(asar_read(asar, inner)).items() if isinstance(v, str)})
    return labels


_UI_STRING = re.compile(
    r'(?:"aria-label"|ariaLabel|title|label|placeholder|tooltip|tooltipContent|children)\s*:\s*"((?:[^"\\\n]|\\.){2,80})"'
)


def parse_bundle(source: str) -> dict[str, str]:
    """Libellés d'un bundle JS (Antigravity, en anglais) : valeurs des propriétés aria-label, title, label…"""
    out: dict[str, str] = {}
    for match in _UI_STRING.finditer(source):
        text = js_unescape(match.group(1)).strip()  # « Search skills… » du bundle : « Search skills… »
        if re.search(r"[A-Za-zÀ-ÿ]", text) and not re.search(r"[{}<>=;]|^\w+[-_]\w+$|^[a-z]+$", text):
            out.setdefault(text, text)
    return out


# séquences échappées admises AVANT décodage (relecture de Codex : « Previous (←) », « Search skills… »)
_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.){2,120})"|`((?:[^`\\]|\\.){2,120})`')


def bundle_strings(source: str) -> set[str]:
    """Toutes les chaînes littérales du bundle : ce que l'appli peut afficher d'elle-même (jamais une donnée de
    l'utilisateur) ; sert à reconnaître un libellé connu."""
    return {js_unescape(m.group(1) or m.group(2)) for m in _LITERAL.finditer(source)}


def agy_index(page: Any) -> tuple[dict[str, str], set[str]]:
    """Libellés d'Antigravity (index publié, chaînes connues), lus dans les scripts que sa page a chargés (même
    origine, lecture seule)."""
    source = page.eval(
        "(async () => { const s = [...document.scripts].map(e => e.src).filter(Boolean);"
        " const t = await Promise.all(s.map(u => fetch(u).then(r => r.text()).catch(() => '')));"
        " return t.join('\\n') })()"
    )
    return parse_bundle(source or ""), bundle_strings(source or "")


# --- version de chaque appli
def codex_version() -> str | None:
    return cdp._ps(f"(Get-AppxPackage {cdp.CODEX_PACKAGE} | Select-Object -First 1).Version") or None  # pyright: ignore[reportPrivateUsage]


def agy_version() -> str | None:
    path = cdp._ps(  # pyright: ignore[reportPrivateUsage]
        f"(Get-Process {Path(cdp.AGY_PROCESS).stem} -ErrorAction SilentlyContinue | Select-Object -First 1).Path"
    ) or str(Path.home() / "AppData/Local/Programs/antigravity" / cdp.AGY_PROCESS)
    version = cdp._ps(  # pyright: ignore[reportPrivateUsage]
        f"if (Test-Path '{path}') {{ (Get-Item '{path}').VersionInfo.ProductVersion }}"
    )
    return version or None


VERSIONS: dict[str, Callable[[], str | None]] = {"codex": codex_version, "antigravity": agy_version}


# --- repères du relais : ce que le relais doit pouvoir trouver ; un repère disparu est signalé
_NO_CHAT = ["Aucun chat", "No chats"]
_NO_CONVERSATION = ["No conversations", "Aucune conversation"]
LANDMARKS: dict[str, dict[str, dict[str, Any]]] = {
    "codex": {
        "NEW_CHAT": {"role": "button", "labels": list(cdp.NEW_CHAT)},
        "MODE": {"popup": "menu", "aria_re": r"^(Changer de mode|Switch mode)"},
        # sans aucun chat affiché (barre latérale vide), ces deux-là ne sont pas visibles : non vérifiables
        # menu de l'en-tête d'un chat OUVERT (les lignes portent « Archiver le chat » directement, relevé du 20/10)
        "CHAT_ACTIONS": {
            "popup": "menu",
            "labels": ["Actions du chat", "Chat actions"],
            "unless": _NO_CHAT,
            "needs": "chat_open",
        },
        "ARCHIVE": {"labels": [*cdp.ARCHIVE, *cdp.MENU_ARCHIVE], "unless": _NO_CHAT},  # ligne (avant le 20/10) ou menu
        "COMPOSER": {"role": "textbox"},
    },
    "antigravity": {
        "NEW_CHAT": {"labels": list(cdp.AGY_NEW_CHAT)},
        "ARCHIVE": {"data": {"data-testid": "conversation-archive-button"}, "unless": _NO_CONVERSATION},
        "MORE_OPTIONS": {"data": {"data-testid": "conversation-kebab"}, "unless": _NO_CONVERSATION},
        "COMPOSER": {"aria_re": "^(Message input|Saisie du message)"},
        "SEND": {"data": {"data-testid": "send-button"}},
    },
}


def _label(item: dict[str, Any]) -> str:
    return item.get("aria") or item.get("text") or ""


def matches(item: dict[str, Any], rule: dict[str, Any]) -> bool:
    if "role" in rule and item.get("role") != rule["role"]:
        return False
    if "role_re" in rule and not re.search(rule["role_re"], item.get("role") or ""):
        return False
    if "popup" in rule and item.get("popup") != rule["popup"]:
        return False
    if "labels" in rule and not ({item.get("aria"), item.get("text")} & set(rule["labels"])):
        return False
    if "aria_re" in rule and not re.search(rule["aria_re"], item.get("aria") or ""):
        return False
    if "in_menu" in rule and not any(rule["in_menu"] in step for step in item.get("path") or []):
        return False
    return all((item.get("data") or {}).get(k) == v for k, v in (rule.get("data") or {}).items())


def landmark_status(app: str, elements: list[dict[str, Any]], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Chaque repère : tous les chemins où il est (le plus court d'abord), {"unverified": …} si ce qui le montre est
    absent (aucun chat affiché, aucun chat ouvert au départ), ou None s'il manque."""
    out: dict[str, Any] = {}
    for name, rule in LANDMARKS[app].items():
        hits = [e for e in elements if matches(e, rule)]
        need = rule.get("needs")
        if not hits and need and context is not None and not context.get(need):
            out[name] = {"unverified": "non vérifiable : aucun chat ouvert au départ du relevé"}
            continue
        empty = next((e for e in elements if {e.get("aria"), e.get("text")} & set(rule.get("unless") or [])), None)
        if not hits and empty is not None:
            out[name] = {"unverified": f"non vérifiable : « {_label(empty)} » ({' > '.join(empty.get('path') or [])})"}
            continue
        paths = sorted(
            {" > ".join(e.get("path") or []) for e in hits},
            key=lambda p: (p.count(">"), not p.startswith("vue de départ"), p),
        )
        out[name] = {"path": paths[0], "paths": paths, "label": _label(hits[0])} if hits else None
    return out


# --- différence entre deux cartes
def _identity(item: dict[str, Any]) -> str:
    data = {k: v for k, v in (item.get("data") or {}).items() if v != "*"}
    return json.dumps([item.get("role"), _label(item), sorted(data)], ensure_ascii=False)


def _where(item: dict[str, Any]) -> str:
    return " > ".join(item.get("path") or []) or "?"


def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Ajouts, retraits et déplacements d'éléments ; repères disparus ou déplacés. Lisible par l'utilisateur."""

    def index(card: dict[str, Any]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for e in card.get("elements") or []:
            out.setdefault(_identity(e), set()).add(_where(e))
        return out

    def describe(identity: str) -> str:
        role, label, _ = json.loads(identity)
        return f"{role or 'élément'} « {label} »"

    a, b = index(old), index(new)
    added = sorted(describe(i) + " (" + ", ".join(sorted(b[i])) + ")" for i in b.keys() - a.keys() if json.loads(i)[1])
    removed = sorted(
        describe(i) + " (" + ", ".join(sorted(a[i])) + ")" for i in a.keys() - b.keys() if json.loads(i)[1]
    )
    moved = sorted(
        f"{describe(i)} : {', '.join(sorted(a[i] - b[i]))} → {', '.join(sorted(b[i] - a[i]))}"
        for i in a.keys() & b.keys()
        if (a[i] - b[i]) and (b[i] - a[i]) and json.loads(i)[1]
    )
    landmarks: list[str] = []
    old_marks, new_marks = old.get("landmarks") or {}, new.get("landmarks") or {}
    for name in sorted(set(old_marks) | set(new_marks)):
        before, after = old_marks.get(name), new_marks.get(name)
        if before and "path" not in before:
            before = None  # non vérifiable avant : rien à comparer
        if after and "path" not in after:
            if before:
                landmarks.append(f"repère {name} {after['unverified']} (était : {before['path']})")
            continue
        if before and not after:
            landmarks.append(f"REPÈRE DISPARU {name} (était : {before['path']})")
        elif after and not before:
            landmarks.append(f"repère {name} trouvé : {after['path']}")
        elif before and after:
            was, now = set(before.get("paths") or [before["path"]]), set(after.get("paths") or [after["path"]])
            if now - was:
                landmarks.append(f"repère {name} maintenant aussi : {', '.join(sorted(now - was))}")
            if was - now:
                landmarks.append(f"repère {name} n'est plus : {', '.join(sorted(was - now))}")
    labels_old, labels_new = old.get("index") or {}, new.get("index") or {}
    changed = sorted(
        f"{k} : « {labels_old[k]} » → « {labels_new[k]} »"
        for k in labels_old.keys() & labels_new.keys()
        if labels_old[k] != labels_new[k]
    )
    return {
        "from": f"{old.get('version')} ({old.get('surveyed_at')})",
        "to": f"{new.get('version')} ({new.get('surveyed_at')})",
        "landmarks": landmarks,
        "moved": moved,
        "added": added,
        "removed": removed,
        "labels": {
            "added": len(labels_new.keys() - labels_old.keys()),
            "removed": sorted(labels_old.keys() - labels_new.keys())[:200],
            "changed": changed[:200],
        },
    }


def summary(change: dict[str, Any]) -> str:
    """Quelques lignes en mots simples."""
    lines = [f"{change['from']} → {change['to']}"]
    lines += change["landmarks"] or ["repères du relais : inchangés"]
    lines += [f"déplacé : {m}" for m in change["moved"][:20]]
    lines.append(f"{len(change['added'])} éléments apparus, {len(change['removed'])} disparus")
    labels = change["labels"]
    lines.append(
        f"libellés : {labels['added']} nouveaux, {len(labels['removed'])} retirés, {len(labels['changed'])} changés"
    )
    return "\n".join(lines)


# --- stockage (hors OneDrive)
def cards_dir() -> Path:
    return relay_home() / "v1" / "cartes"


def _safe(version: str) -> str:
    return re.sub(r"[^\w.\-]", "_", version)


def card_path(app: str, version: str, partial: bool = False) -> Path:
    return cards_dir() / f"{app}-{_safe(version)}{'.partiel' if partial else ''}.json"


def complete(card: dict[str, Any]) -> bool:
    """Relevé allé au bout (ni interrompu, ni sans le port) : seule une carte complète vaut pour sa version."""
    live = card.get("live") or {}
    return bool(card.get("elements")) and live.get("status") == "complet" and live.get("view_restored") is True


def cards(app: str) -> list[Path]:
    """Cartes de `app`, de la plus ancienne à la plus récente (date du relevé)."""
    folder = cards_dir()
    found = (
        [p for p in folder.glob(f"{app}-*.json") if not p.name.endswith((".diff.json", ".partiel.json"))]
        if folder.is_dir()
        else []
    )

    def when(path: Path) -> str:
        try:
            return str(json.loads(path.read_text(encoding="utf-8")).get("surveyed_at") or "")
        except (OSError, ValueError):
            return ""

    return sorted(found, key=when)


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def latest(app: str) -> dict[str, Any] | None:
    found = cards(app)
    return load(found[-1]) if found else None


def save(card: dict[str, Any]) -> dict[str, Any]:
    """Range la carte ; si une carte d'une AUTRE version existe, calcule la différence et la range à côté. Une carte
    partielle (relevé interrompu) est rangée à part : elle ne remplace jamais la carte complète de sa version."""
    app, version = card["app"], card["version"]
    if card.get("live") is not None and not complete(card):
        path = card_path(app, version, partial=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"card": str(path), "partial": True}
    previous = next((p for p in reversed(cards(app)) if p != card_path(app, version)), None)
    path = card_path(app, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"card": str(path)}
    if previous is not None:
        change = diff(load(previous), card)
        card["diff_from"] = previous.name
        diff_file = path.with_name(path.stem + ".diff.json")
        diff_file.write_text(json.dumps(change, ensure_ascii=False, indent=1), encoding="utf-8")
        result.update(
            diff=str(diff_file), summary=summary(change), alerts=[x for x in change["landmarks"] if "DISPARU" in x]
        )
    path.write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
    card_path(app, version, partial=True).unlink(missing_ok=True)  # essai interrompu de la même version : remplacé
    return result


# --- relevé complet d'une appli
_LOCK = threading.Lock()


def survey(app: str, live: bool = True) -> dict[str, Any]:
    """Relevé de `app` : index des libellés (paquet) et, avec `live`, relevé par le port (seulement si l'utilisateur n'est pas
    dans l'appli). Rend le résumé ; la carte est rangée dans ~\\.nexus\\relay\\v1\\cartes."""
    if app not in SPECS:
        raise ValueError(f"appli inconnue : {app!r} ({', '.join(APPS)})")
    if not _LOCK.acquire(blocking=False):
        return {"status": "deferred", "reason": "un relevé est déjà en cours"}
    try:
        spec = SPECS[app]
        version = VERSIONS[app]()
        if not version:
            return {"status": "failed", "reason": f"version de {app} introuvable (appli installée ?)"}
        card: dict[str, Any] = {"app": app, "version": version, "surveyed_at": now_iso(), "elements": []}
        if live and not cdp.UI_LOCKS[app].acquire(timeout=UI_WAIT_S):  # un réveil en cours : on le laisse finir
            return {"status": "deferred", "reason": "un réveil ou un rangement tient le port : relevé reporté"}
        try:
            return _survey(app, spec, version, card, live)
        finally:
            if live:
                cdp.UI_LOCKS[app].release()
    finally:
        _LOCK.release()


def _survey(app: str, spec: AppSpec, version: str, card: dict[str, Any], live: bool) -> dict[str, Any]:
    url = spec.port()
    page = cdp.Page(url) if url else None
    try:
        known: set[str] = set()
        if app == "codex":
            lang = (page.eval("document.documentElement.lang") if page else None) or "fr-FR"
            card["lang"] = lang
            card["index"] = codex_index(lang)
            known = set(card["index"].values())
        elif page is not None:
            card["index"], known = agy_index(page)
            known |= set(card["index"])
        if live:
            if page is None:
                card["live"] = {"status": "non fait : port fermé ou page absente"}
            elif spec.present():
                card["live"] = {"status": "reporté : l'utilisateur est dans l'appli"}
            elif reason := busy(app, page):
                card["live"] = {"status": f"reporté : {reason}"}
            else:
                run = Surveyor(page, spec, Known(known)).run()
                card["elements"] = run.pop("elements")
                card["live"] = run
    finally:
        if page is not None:
            page.close()
    card["landmarks"] = landmark_status(app, card["elements"], card.get("live"))
    saved = save(card)
    return {
        "status": "success",
        "app": app,
        "version": version,
        "elements": len(card["elements"]),
        "labels": len(card.get("index") or {}),
        "live": {k: v for k, v in (card.get("live") or {}).items() if k != "notes"},
        "landmarks": {k: (v.get("path") or v.get("unverified")) if v else None for k, v in card["landmarks"].items()},
        **saved,
    }


_CHECKED: dict[str, float] = {}


def refresh_due(clock: Callable[[], float] = time.time) -> list[dict[str, Any]]:
    """Appelé par la sonnette du démon : nouvelle version d'une appli → nouveau relevé (dans un fil à part, jamais
    pendant que l'utilisateur est dans l'appli ; reporté sinon)."""
    started: list[dict[str, Any]] = []
    for app in APPS:
        now = clock()
        if now - _CHECKED.get(app, -CHECK_EVERY_S) < CHECK_EVERY_S:
            continue
        version = VERSIONS[app]()
        last = latest(app)
        stale = last is None or last.get("version") != version or not complete(last)
        if not version or not stale or SPECS[app].port() is None or SPECS[app].present():
            _CHECKED[app] = now if version and not stale else now - CHECK_EVERY_S + 300  # reporté : revu dans 5 min
            continue
        _CHECKED[app] = now
        # compté jusqu'au bout (tour 6) : une relance ne coupe plus un relevé en plein geste (menu laissé ouvert)
        threading.Thread(
            target=wake._counted, args=(lambda a=app: survey(a),), name=f"nexus-relay-carte-{app}", daemon=True
        ).start()
        started.append({"app": app, "version": version})
    return started


def watch(every_s: float = 300.0) -> None:
    """Fil du démon : refresh_due toutes les 5 min (lui-même ne relit la version qu'au plus toutes les 30 min)."""

    def loop() -> None:
        while True:
            try:
                refresh_due()
            except Exception:  # noqa: BLE001 - un pas raté n'arrête jamais la veille
                pass
            time.sleep(every_s)

    threading.Thread(target=loop, name="nexus-relay-cartes", daemon=True).start()


def overview() -> dict[str, Any]:
    """Dernière carte de chaque appli : version, date, repères manquants (pour status / selftest)."""
    out: dict[str, Any] = {}
    for app in APPS:
        card = latest(app)
        if card is None:
            out[app] = {"status": "pas encore de carte"}
            continue
        marks = card.get("landmarks") or {}
        out[app] = {
            "version": card.get("version"),
            "surveyed_at": card.get("surveyed_at"),
            "elements": len(card.get("elements") or []),
            "missing": sorted(k for k, v in marks.items() if not v),
            "unverified": sorted(k for k, v in marks.items() if v and "path" not in v),
        }
    return out


# --- où est un élément
def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return re.sub(r"\s+", " ", "".join(c for c in folded if not unicodedata.combining(c))).strip()


def how_to_reach(item: dict[str, Any], elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gestes par le port pour atteindre l'élément : chaque page puis chaque menu de son chemin, puis l'élément."""
    steps: list[dict[str, Any]] = []
    for step in item.get("path") or []:
        kind, _, name = step.partition(" « ")
        name = name.rstrip(" »")
        if kind == "vue de départ":
            continue
        source = next(
            (
                e
                for e in elements
                if _label(e) == name
                and (
                    bool(e.get("popup"))
                    if kind != "page"
                    else not e.get("popup") or e.get("role", "").startswith("menuitem")
                )
            ),
            None,
        )
        gesture = "survol (Input.dispatchMouseEvent mouseMoved)" if kind == "sous-menu" else "clic émulé (cdp._press)"
        steps.append({"step": step, "gesture": gesture, "locator": locator_of(source) if source else {"text": name}})
    steps.append({"step": "élément", "locator": locator_of(item)})
    return steps


def _search(app: str, card: dict[str, Any], what: str, limit: int) -> list[dict[str, Any]]:
    query = _norm(what)
    elements: list[dict[str, Any]] = card.get("elements") or []
    rule = LANDMARKS[app].get(what.strip().upper())
    scored: list[tuple[int, dict[str, Any]]] = []
    for e in elements:
        if rule is not None:
            score = 100 if matches(e, rule) else 0
        else:
            fields = [
                _norm(_label(e)),
                _norm(e.get("text") or ""),
                *(_norm(f"{k}={v}") for k, v in (e.get("data") or {}).items()),
            ]
            score = max((100 if f == query else 60 if f.startswith(query) else 30 if query in f else 0) for f in fields)
        if score:
            scored.append((score - len(e.get("path") or []), e))
    scored.sort(key=lambda x: -x[0])
    return [
        {
            "label": _label(e),
            "role": e.get("role"),
            "path": " > ".join(e.get("path") or []),
            "landmark": next((name for name, r in LANDMARKS[app].items() if matches(e, r)), None),
            "data": e.get("data") or {},
            "how": how_to_reach(e, elements),
        }
        for _, e in scored[:limit]
    ]


def where(app: str, what: str, limit: int = 5) -> dict[str, Any]:
    """Cherche `what` (libellé, aria-label, attribut data-*, clé de traduction ou nom de repère) dans la dernière carte ;
    pas vu (aucun chat affiché ce jour-là…) : dans les cartes précédentes, la plus récente d'abord, en le disant."""
    if app not in SPECS:
        raise ValueError(f"appli inconnue : {app!r} ({', '.join(APPS)})")
    found = [load(p) for p in reversed(cards(app))]
    if not found:
        return {"status": "error", "error": f"pas encore de carte pour {app} : lancer un relevé"}
    card = found[0]
    hits = _search(app, card, what, limit)
    result: dict[str, Any] = {
        "status": "success",
        "app": app,
        "version": card.get("version"),
        "surveyed_at": card.get("surveyed_at"),
        "found": hits,
    }
    for older in found[1:] if not hits else []:
        if hits := _search(app, older, what, limit):
            result["found"] = hits
            result["from_older_card"] = (
                f"{older.get('version')} ({older.get('surveyed_at')}) : pas vu au dernier relevé"
            )
            break
    query = _norm(what)
    known = [
        {"key": k, "text": v}
        for k, v in (card.get("index") or {}).items()
        if query and (query in _norm(v) or query in _norm(k))
    ][:limit]
    if known:
        result["labels"] = known  # dans le paquet de l'appli : existe, même si le relevé ne l'a pas vu
    if not result["found"]:
        result["note"] = "pas vu au relevé" + (
            " ; libellé connu de l'appli (index), page ou menu non parcourus" if known else ""
        )
    return result


def main(argv: list[str] | None = None) -> None:
    """python -m nexus_relay.cartes survey <codex|antigravity|all> | where <appli> <quoi> | diff <ancienne> <nouvelle>"""
    import sys

    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["survey"]:
        apps = APPS if args[1:] in ([], ["all"]) else args[1:]
        out: Any = [survey(a) for a in apps]
    elif args[:1] == ["where"] and len(args) >= 3:
        out = where(args[1], " ".join(args[2:]))
    elif args[:1] == ["diff"] and len(args) == 3:
        out = summary(diff(load(Path(args[1])), load(Path(args[2]))))
    else:
        out = main.__doc__
    print(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
