"""Hook d'édition déclaré à Codex (hooks.json + confiance dans config.toml) et Antigravity, sans toucher au reste."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import register_hooks  # noqa: E402

PY = r"C:\relay\venv\Scripts\python.exe"


def test_hash_de_confiance_de_codex():
    """Même identité que codex-rs (event_name, matcher, handler normalisé ; JSON trié et compact, SHA-256)."""
    handler = {"type": "command", "command": "echo hello", "timeout": 600, "async": False}
    expected = '{"event_name":"pre_tool_use","hooks":[{"async":false,"command":"echo hello","timeout":600,'
    expected += '"type":"command"}],"matcher":"^Bash$"}'
    import hashlib

    assert register_hooks.codex_hash("^Bash$", handler) == "sha256:" + hashlib.sha256(expected.encode()).hexdigest()


def test_codex_declare_et_fait_confiance_sans_toucher_au_reste(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path
    codex = home / ".codex"
    codex.mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    foreign = {"matcher": "^Bash$", "hooks": [{"type": "command", "command": "echo moi"}]}
    (codex / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [foreign]}}), encoding="utf-8")
    (codex / "config.toml").write_text('model = "x"\n\n[features]\nhooks = true\n', encoding="utf-8")
    backups = tmp_path / "backups"
    assert str(register_hooks.codex(home, PY, backups)).startswith("declared")
    groups = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert groups[0] == foreign and groups[1]["matcher"] == "apply_patch"
    config = tomllib.loads((codex / "config.toml").read_text(encoding="utf-8"))
    key = f"{codex / 'hooks.json'}:pre_tool_use:1:0"
    assert config["model"] == "x" and config["features"] == {"hooks": True}
    trusted = register_hooks.codex_hash("apply_patch", groups[1]["hooks"][0])
    assert config["hooks"]["state"][key]["trusted_hash"] == trusted
    assert register_hooks.codex(home, PY, backups) == "already declared and trusted"
    assert (codex / "config.toml").read_text(encoding="utf-8").count("[hooks.state.") == 1


def test_antigravity_ajoute_son_hook_a_cote_des_autres(tmp_path: Path):
    config = tmp_path / ".gemini" / "config"
    config.mkdir(parents=True)
    (config / "hooks.json").write_text(json.dumps({"autre": {"Stop": []}}), encoding="utf-8")
    assert str(register_hooks.antigravity(tmp_path, PY, tmp_path / "b")).startswith("declared")
    data = json.loads((config / "hooks.json").read_text(encoding="utf-8"))
    assert data["autre"] == {"Stop": []}
    hook = data[register_hooks.AGY_NAME]["PreToolUse"][0]["hooks"][0]
    assert hook["command"] == f"{PY} -I -B -m nexus_relay.edit_guard antigravity"
    assert register_hooks.antigravity(tmp_path, PY, tmp_path / "b") == "already declared"
