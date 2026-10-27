"""Cibles de fuzzing (fuzz/targets.py) sans Atheris : octets pseudo-aléatoires à graine fixe, sur Windows aussi."""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fuzz"))

import targets  # noqa: E402  # pyright: ignore[reportMissingImports]


def test_targets_hold_on_random_inputs() -> None:
    rng = random.Random(1618)
    for _ in range(20000):
        targets.target(rng.randbytes(rng.randrange(0, 200)))
