"""Shared Keypad contract: selected model family, real resets, never maximum across unrelated families."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta

from test_native_csharp import NO_WINDOW, _native_exe

from nexus_relay import credits
from nexus_relay.box import Box


def test_selected_gemini_does_not_inherit_exhausted_argon(monkeypatch, tmp_path):
    path = tmp_path / "quota.json"
    monkeypatch.setenv("NEXUS_RELAY_KEYPAD_QUOTA", str(path))
    observed = datetime.now(UTC)
    data = {
        "lu": observed.isoformat(),
        "groupeSelectionne": "gemini",
        "cinq": 0.8,
        "semaine": 1,
        "groupes": [
            {"groupe": "gemini", "cinq": True, "utilise": 0.8, "fin": (observed + timedelta(hours=1)).isoformat()},
            {"groupe": "gemini", "cinq": False, "utilise": 0.3, "fin": None},
            {"groupe": "argon", "cinq": False, "utilise": 1, "fin": None},
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    usage = credits.antigravity_usage(Box(), read=lambda: (_ for _ in ()).throw(AssertionError("unneeded UI read")))
    assert usage and usage["selected_family"] == "gemini" and usage["selection_proven"]
    assert credits.exhausted("antigravity", usage) is None
    data["groupeSelectionne"] = "argon"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert "argon" in (credits.exhausted("antigravity", credits.cached_antigravity_usage(Box())) or "")
    data["groupeSelectionne"] = None
    path.write_text(json.dumps(data), encoding="utf-8")
    assert credits.exhausted("antigravity", credits.cached_antigravity_usage(Box())) is None
    data["groupeSelectionne"] = "gemini"
    data["groupes"][0]["utilise"] = 1
    data["groupes"][0]["fin"] = (observed - timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")
    assert credits.exhausted("antigravity", credits.cached_antigravity_usage(Box())) is None


def test_stale_shared_selection_is_unknown_not_a_fresh_legacy_gemini_quota(monkeypatch, tmp_path):
    path = tmp_path / "quota.json"
    monkeypatch.setenv("NEXUS_RELAY_KEYPAD_QUOTA", str(path))
    path.write_text(
        json.dumps({"lu": "2000-01-01T00:00:00Z", "groupeSelectionne": "argon", "groupes": []}), encoding="utf-8"
    )
    usage = credits.cached_antigravity_usage(Box())
    assert usage and usage["status"] == "unknown"
    assert credits.exhausted("antigravity", usage) is None


def test_native_and_python_read_the_same_selected_family_and_observation_time(monkeypatch, tmp_path):
    path = tmp_path / "quota.json"
    monkeypatch.setenv("NEXUS_RELAY_KEYPAD_QUOTA", str(path))
    observed = datetime.now(UTC)
    data = {
        "lu": observed.isoformat(),
        "groupeSelectionne": "gemini",
        "groupes": [
            {"groupe": "gemini", "cinq": True, "utilise": 0.8, "fin": (observed + timedelta(hours=1)).isoformat()},
            {"groupe": "gemini", "cinq": False, "utilise": 0.3, "fin": None},
            {"groupe": "argon", "cinq": False, "utilise": 1.0, "fin": None},
        ],
    }

    def read_native():
        path.write_text(json.dumps(data), encoding="utf-8")
        return json.loads(
            subprocess.check_output(
                [str(_native_exe()), "--quota-status"], env=os.environ.copy(), creationflags=NO_WINDOW
            )
        )

    result = read_native()
    assert result["blocked_reason"] is None and result["usage"]["selected_family"] == "gemini"
    assert result["usage"]["read_at"] == data["lu"]
    data["groupeSelectionne"] = "argon"
    result = read_native()
    assert "argon" in result["blocked_reason"] and result["usage"]["read_at"] == data["lu"]
    data["groupeSelectionne"] = None
    assert read_native()["blocked_reason"] is None
    data["groupeSelectionne"] = "gemini"
    data["groupes"][0]["utilise"] = 1
    data["groupes"][0]["fin"] = (observed - timedelta(seconds=1)).isoformat()
    assert read_native()["blocked_reason"] is None
    data["lu"] = (observed - timedelta(hours=7)).isoformat()
    data["groupeSelectionne"] = "argon"
    result = read_native()
    assert result["usage"]["status"] == "unknown" and result["blocked_reason"] is None
