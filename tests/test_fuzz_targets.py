"""Cibles de fuzzing (fuzz/targets.py) sans Atheris : octets pseudo-aléatoires à graine fixe, sur Windows aussi."""

import base64
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fuzz"))

import targets  # noqa: E402  # pyright: ignore[reportMissingImports]


def test_targets_hold_on_random_inputs() -> None:
    rng = random.Random(1618)
    for _ in range(20000):
        targets.target(rng.randbytes(rng.randrange(0, 200)))


def test_crashes_found_by_the_fuzzer() -> None:
    targets.target(bytes.fromhex("3d5c5c3f5c554e435c"))  # « \?\UNC\ » : chemin rangé instable (CI, 27/10)
    targets.target(bytes.fromhex("0a5c5c3f5c5c5c3f5c554e435c554e305c"))  # préfixe long doublé (CI, 27/10)
    # caractères de contrôle et espaces Unicode dans les noms (CI, 27/10)
    targets.target(
        base64.b64decode(
            "TFxcCgoCXApcCgJcXApMXFwrXFxcAsdSYWNcXAAaGhoGGhoaGhoaGhoaGhoaGhoaGhoaGhoaGhoaGgIaGhoaGhoaGhoaGhoaGhoaGgEAAAAAAAAAGhpMXFwpCgJcClwK"
        )
    )
