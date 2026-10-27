"""
Cible de fuzzing de NexusRelay : les réservations de fichiers, qui empêchent deux agents de modifier le même fichier
en même temps. Un faux « libre » laisse deux éditions se croiser : il ne doit jamais arriver.

- Un motif tiré d'un chemin concret (le chemin lui-même, un dossier parent, un nom remplacé par `*`, un début ou une
  fin de nom suivi ou précédé de `*`, `**`) le couvre toujours (`reservations.overlap`), dans les deux sens.
- Un chemin rangé (`reservations.clean`) se relit à l'identique, et sa forme sans casse (`norm`) aussi.
- Un sujet (`box.check_topic`) accepté respecte le format des noms de conversation.

Lancée sous Atheris par .github/workflows/fuzz.yml (Linux) et, sans Atheris, par tests/test_fuzz_targets.py sur des
octets pseudo-aléatoires (test.ps1, Windows).
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from nexus_relay import box, reservations  # noqa: E402

_NAME = "abcXYZ019 ._-é"


class Bytes:
    """Lecteur d'octets du fuzzer, sans jamais échouer (octets épuisés : zéros)."""

    def __init__(self, data: bytes) -> None:
        self.data, self.at = data, 0

    def byte(self) -> int:
        if self.at >= len(self.data):
            return 0
        self.at += 1
        return self.data[self.at - 1]

    def name(self) -> str:
        size = 1 + self.byte() % 8
        text = "".join(_NAME[self.byte() % len(_NAME)] for _ in range(size)).strip(". ")
        return text if text not in ("", ".", "..") else "x"


def _pattern(b: Bytes, parts: list[str]) -> list[str]:
    """Motif qui couvre forcément le chemin `parts`."""
    kind = b.byte() % 4
    if kind == 0:  # dossier parent
        return parts[: 1 + b.byte() % len(parts)]
    if kind == 1:  # tout ce qui suit
        return [*parts[: b.byte() % len(parts)], "**"]
    out = list(parts)
    i = b.byte() % len(out)
    if kind == 2:
        out[i] = "*"
    else:
        cut = b.byte() % (len(out[i]) + 1)
        out[i] = out[i][:cut] + "*" if b.byte() & 1 else "*" + out[i][cut:]
    return out


def overlap(data: bytes) -> None:
    b = Bytes(data)
    parts = [b.name() for _ in range(1 + b.byte() % 5)]
    path = "C:\\" + "\\".join(parts)
    pattern = "C:\\" + "\\".join(_pattern(b, parts))
    if not (reservations.overlap(pattern, path) and reservations.overlap(path, pattern)):
        raise AssertionError(f"{pattern!r} does not cover {path!r}")


def clean(data: bytes) -> None:
    text = data.decode("utf-8", "replace")
    try:
        once = reservations.clean(text)
    except (box.RelayError, ValueError):
        return
    if reservations.clean(once) != once or reservations.norm(once) != reservations.norm(reservations.norm(once)):
        raise AssertionError(f"clean not stable: {text!r} -> {once!r}")


def topic(data: bytes) -> None:
    try:
        out = box.check_topic(data.decode("utf-8", "replace"))
    except box.RelayError:
        return
    if not box.TOPIC.match(out) or "\n" in out:
        raise AssertionError(f"bad topic {out!r}")


def target(data: bytes) -> None:
    (overlap, clean, topic)[data[0] % 3 if data else 0](data[1:])
