"""Langue des textes rendus aux agents (réponses des outils, erreurs, texte de réveil) : réglage `lang`
(`NEXUS_RELAY_LANG`), anglais par défaut, `fr` pour le français. Les journaux du démon, les clés et les valeurs
enregistrées dans le salon ne changent pas de langue."""

from __future__ import annotations

import os
from functools import cache


@cache
def _configured() -> str:
    from nexus_relay import settings  # import tardif : settings n'importe rien du relais

    return str(settings.load().get("lang", ""))


def code() -> str:
    """`fr` ou `en` : la variable d'environnement, sinon les réglages du poste (sidecar lancé par Antigravity)."""
    value = os.environ.get("NEXUS_RELAY_LANG") or _configured()
    return "fr" if value.strip().lower().startswith("fr") else "en"


def tr(en: str, fr: str) -> str:
    """Le texte dans la langue réglée."""
    return fr if code() == "fr" else en
