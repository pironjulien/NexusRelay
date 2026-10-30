"""Lifecycle hooks declared without granting Codex trust or touching unrelated/disabled hooks."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import register_hooks  # noqa: E402

PY = r"C:\relay\venv\Scripts\python.exe"


def test_native_and_dispatcher_hooks_are_recognized_without_duplicate_or_trust(tmp_path):
    dispatcher = r"C:\Users\test\.nexus\nexus-relay\hooks\invoke-hook.ps1"
    hooks = {}
    for event, (_, phase) in register_hooks.EVENTS.items():
        command = register_hooks.command(dispatcher, "codex", phase)
        assert "invoke-hook.ps1" in command
        assert '"' not in command  # Antigravity sans shell : guillemets passés tels quels à pwsh -File
        hooks[event] = [{"matcher": "apply_patch" if event != "Stop" else "", "hooks": [{"command": command}]}]
    manifest = tmp_path / "plugin.json"
    manifest.write_text(json.dumps({"name": "nexus-relay", "hooks": {"hooks": hooks}}), encoding="utf-8")
    assert all(register_hooks.manifest_carries_guard(manifest, event) for event in register_hooks.EVENTS)
    assert "--edit-guard codex --finish-tool" in register_hooks.command(
        r"C:\NexusRelay\NexusRelay.exe", "codex", "--finish-tool"
    )


def cached_plugin(codex: Path, *, carries: bool = True) -> Path:
    manifest = codex / "plugins" / "cache" / "nexus-plugins" / "nexus-relay" / "local" / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    data: dict = {"name": "nexus-relay", "version": "1.8.6"}
    if carries:
        data["hooks"] = {
            "hooks": {
                event: [
                    {
                        "matcher": "apply_patch" if event != "Stop" else "",
                        "hooks": [{"command": register_hooks.command(PY, "codex", phase)}],
                    }
                ]
                for event, (_, phase) in register_hooks.EVENTS.items()
            }
        }
    manifest.write_text(json.dumps(data), encoding="utf-8")
    return manifest


def test_hash_de_confiance_de_codex():
    """Même identité que codex-rs (event_name, matcher, handler normalisé ; JSON trié et compact, SHA-256)."""
    handler = {"type": "command", "command": "echo hello", "timeout": 600, "async": False}
    expected = '{"event_name":"pre_tool_use","hooks":[{"async":false,"command":"echo hello","timeout":600,'
    expected += '"type":"command"}],"matcher":"^Bash$"}'
    import hashlib

    assert register_hooks.codex_hash("^Bash$", handler) == "sha256:" + hashlib.sha256(expected.encode()).hexdigest()


def test_codex_declare_sans_inventer_la_confiance_ni_toucher_au_reste(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path
    codex = home / ".codex"
    codex.mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    foreign = {"matcher": "^Bash$", "hooks": [{"type": "command", "command": "echo moi"}]}
    (codex / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [foreign]}}), encoding="utf-8")
    original = 'model = "x"\n\n[features]\nhooks = true\n'
    (codex / "config.toml").write_text(original, encoding="utf-8")
    backups = tmp_path / "backups"
    assert str(register_hooks.codex(home, PY, backups)).startswith("declared")
    groups = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert groups[0] == foreign and groups[1]["matcher"] == "apply_patch"
    config = tomllib.loads((codex / "config.toml").read_text(encoding="utf-8"))
    assert config["model"] == "x" and config["features"] == {"hooks": True}
    assert "hooks" not in config
    assert "already declared" in str(register_hooks.codex(home, PY, backups))
    assert (codex / "config.toml").read_text(encoding="utf-8") == original
    events = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert "--finish-tool" in events["PostToolUse"][0]["hooks"][0]["command"]
    assert "--finish-session" in events["Stop"][0]["hooks"][0]["command"]


def test_codex_plugin_porteur_retire_l_entree_globale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Le plugin perso nexus-relay déclare le garde : l'entrée globale et sa confiance partent, le reste est gardé."""
    home = tmp_path
    codex = home / ".codex"
    codex.mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    foreign = {"matcher": "^Bash$", "hooks": [{"type": "command", "command": "echo moi"}]}
    (codex / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [foreign]}}), encoding="utf-8")
    (codex / "config.toml").write_text('model = "x"\n', encoding="utf-8")
    backups = tmp_path / "backups"
    assert str(register_hooks.codex(home, PY, backups)).startswith("declared")
    manifest = codex / "plugins" / "nexus-relay" / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    hooks = {
        "hooks": {
            event: [
                {
                    "matcher": "apply_patch" if event != "Stop" else "",
                    "hooks": [{"command": register_hooks.command(PY, "codex", phase)}],
                }
            ]
            for event, (_, phase) in register_hooks.EVENTS.items()
        }
    }
    manifest.write_text(json.dumps({"name": "nexus-relay", "hooks": hooks}), encoding="utf-8")
    assert "global entry removed" in str(register_hooks.codex(home, PY, backups))
    assert json.loads((codex / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"] == [foreign]
    config = tomllib.loads((codex / "config.toml").read_text(encoding="utf-8"))
    assert config["model"] == "x" and not config.get("hooks", {}).get("state")
    assert register_hooks.codex(home, PY, backups) == "carried by the nexus-relay plugin"


def test_codex_cache_installe_retire_tous_les_doublons_et_recale_les_confiances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Le garde public est dans le cache réellement installé ; des handlers étrangers partagent nos groupes."""
    codex = tmp_path / "profil d'IA"
    codex.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex))
    cached_plugin(codex)
    own = {"type": "command", "command": register_hooks.command(PY, "codex")}
    foreign = [{"type": "command", "command": f"echo étranger {i}"} for i in range(3)]
    groups = [
        {"matcher": "apply_patch", "hooks": [own]},
        {"matcher": "^Bash$", "hooks": [foreign[0], own, foreign[1]]},
        {"matcher": "*", "hooks": [foreign[2]]},
    ]
    stop = [{"hooks": [{"type": "command", "command": "echo stop"}]}]
    (codex / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": groups, "Stop": stop}}), encoding="utf-8")
    prefix = f"{codex / 'hooks.json'}:pre_tool_use:"
    state = {
        prefix + key: {"trusted_hash": f"sha256:{key}", "enabled": key != "1:2"}
        for key in ("0:0", "1:0", "1:1", "1:2", "2:0")
    }
    state["another-hook.json:stop:0:0"] = {"trusted_hash": "sha256:unrelated"}
    text = '# garder ce commentaire\nmodel = "x"\n[plugins."nexus-relay@nexus-plugins"]\nenabled = true\n'
    for key, settings in state.items():
        text += f"\n[hooks.state.{json.dumps(key)}]\n"  # clés TOML entre guillemets, pas seulement littérales
        text += "\n".join(f"{key} = {json.dumps(value)}" for key, value in settings.items()) + "\n"
    (codex / "config.toml").write_text(text, encoding="utf-8")
    assert "global entry removed" in str(register_hooks.codex(tmp_path, PY, tmp_path / "backups"))
    hooks = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert hooks == {"PreToolUse": [{**groups[1], "hooks": foreign[:2]}, groups[2]], "Stop": stop}
    config = tomllib.loads((codex / "config.toml").read_text(encoding="utf-8"))
    assert config["hooks"]["state"] == {
        prefix + "0:0": state[prefix + "1:0"],
        prefix + "0:1": state[prefix + "1:2"],
        prefix + "1:0": state[prefix + "2:0"],
        "another-hook.json:stop:0:0": state["another-hook.json:stop:0:0"],
    }
    assert config["plugins"]["nexus-relay@nexus-plugins"]["enabled"] is True and config["model"] == "x"
    assert "# garder ce commentaire" in (codex / "config.toml").read_text(encoding="utf-8")
    before = (codex / "hooks.json").read_bytes(), (codex / "config.toml").read_bytes()
    assert register_hooks.codex(tmp_path, PY, tmp_path / "backups") == "carried by the nexus-relay plugin"
    assert before == ((codex / "hooks.json").read_bytes(), (codex / "config.toml").read_bytes())


@pytest.mark.parametrize("installed", [True, False])
def test_codex_plugin_desactive_ne_recoit_pas_de_garde_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed: bool
):
    codex = tmp_path / ".codex"
    codex.mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    backups = tmp_path / "backups"
    assert str(register_hooks.codex(tmp_path, PY, backups)).startswith("declared")
    config = codex / "config.toml"
    with config.open("a", encoding="utf-8") as stream:
        stream.write('\n[plugins."nexus-relay@nexus-plugins"]\nenabled = false\n')
    if installed:
        cached_plugin(codex)
    assert "kept disabled" in str(register_hooks.codex(tmp_path, PY, backups))
    assert not json.loads((codex / "hooks.json").read_text(encoding="utf-8"))["hooks"].get("PreToolUse")
    parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    assert parsed["plugins"]["nexus-relay@nexus-plugins"]["enabled"] is False
    assert not parsed.get("hooks", {}).get("state")
    assert register_hooks.codex(tmp_path, PY, backups) == "kept disabled (nexus-relay plugin)"


@pytest.mark.parametrize("registered,carries", [(False, True), (True, False)])
def test_codex_cache_orphelin_ou_ancien_ne_supprime_pas_la_garde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registered: bool, carries: bool
):
    codex = tmp_path / ".codex"
    codex.mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    cached_plugin(codex, carries=carries)
    if registered:
        (codex / "config.toml").write_text('[plugins."nexus-relay@nexus-plugins"]\nenabled = true\n', encoding="utf-8")
    assert str(register_hooks.codex(tmp_path, PY, tmp_path / "b")).startswith("declared")
    hooks = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert len(hooks) == 1 and register_hooks.ours(hooks[0])


@pytest.mark.parametrize("matcher", ["^Bash$", "Stop"])
def test_codex_garde_du_cache_sur_un_autre_outil_ne_remplace_pas_apply_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matcher: str
):
    codex = tmp_path / ".codex"
    codex.mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    manifest = cached_plugin(codex)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["hooks"]["hooks"]["PreToolUse"][0]["matcher"] = matcher
    manifest.write_text(json.dumps(data), encoding="utf-8")
    (codex / "config.toml").write_text('[plugins."nexus-relay@nexus-plugins"]\nenabled = true\n', encoding="utf-8")
    assert str(register_hooks.codex(tmp_path, PY, tmp_path / "b")).startswith("declared")
    hooks = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert len(hooks) == 1 and hooks[0]["matcher"] == "apply_patch" and register_hooks.ours(hooks[0])


def test_antigravity_ajoute_son_hook_a_cote_des_autres(tmp_path: Path):
    config = tmp_path / ".gemini" / "config"
    config.mkdir(parents=True)
    (config / "hooks.json").write_text(json.dumps({"autre": {"Stop": []}}), encoding="utf-8")
    assert str(register_hooks.antigravity(tmp_path, PY, tmp_path / "b")).startswith("declared")
    data = json.loads((config / "hooks.json").read_text(encoding="utf-8"))
    assert data["autre"] == {"Stop": []}
    hook = data[register_hooks.AGY_NAME]["PreToolUse"][0]["hooks"][0]
    assert hook["command"] == f"{PY} -I -B -m nexus_relay.edit_guard antigravity"
    assert "--finish-tool" in data[register_hooks.AGY_NAME]["PostToolUse"][0]["hooks"][0]["command"]
    assert "--finish-session" in data[register_hooks.AGY_NAME]["Stop"][0]["command"]
    assert register_hooks.antigravity(tmp_path, PY, tmp_path / "b") == "already declared"


def test_ancien_plugin_ne_masque_pas_les_finalisateurs_manquants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    codex = tmp_path / ".codex"
    codex.mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    manifest = cached_plugin(codex)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    del data["hooks"]["hooks"]["PostToolUse"], data["hooks"]["hooks"]["Stop"]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    original = '[plugins."nexus-relay@nexus-plugins"]\nenabled = true\n'
    (codex / "config.toml").write_text(original, encoding="utf-8")
    assert "Codex review required" in str(register_hooks.codex(tmp_path, PY, tmp_path / "b"))
    events = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(events) == {"PostToolUse", "Stop"}
    assert (codex / "config.toml").read_text(encoding="utf-8") == original


def test_antigravity_garde_le_choix_desactive(tmp_path: Path):
    config = tmp_path / ".gemini" / "config"
    config.mkdir(parents=True)
    path = config / "hooks.json"
    path.write_text(json.dumps({register_hooks.AGY_NAME: {"enabled": False, "PreToolUse": []}}), encoding="utf-8")
    register_hooks.antigravity(tmp_path, PY, tmp_path / "b")
    assert json.loads(path.read_text(encoding="utf-8"))[register_hooks.AGY_NAME]["enabled"] is False


def test_mise_a_jour_preserve_les_handlers_etrangers_et_leurt_confiance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    codex = tmp_path / ".codex"
    codex.mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    path = codex / "hooks.json"
    own = {"type": "command", "command": register_hooks.command(PY, "codex", "--finish-tool")}
    foreign = {"type": "command", "command": "echo preserve-me"}
    path.write_text(
        json.dumps({"hooks": {"PostToolUse": [{"matcher": "other-tool", "hooks": [own, foreign]}]}}), encoding="utf-8"
    )
    prefix = f"{path}:post_tool_use:"
    original = f'[hooks.state.{json.dumps(prefix + "0:1")}]\ntrusted_hash = "sha256:foreign"\nenabled = false\n'
    (codex / "config.toml").write_text(original, encoding="utf-8")
    register_hooks.codex(tmp_path, PY, tmp_path / "b")
    events = json.loads(path.read_text(encoding="utf-8"))["hooks"]
    assert events["PostToolUse"][0] == {"matcher": "other-tool", "hooks": [foreign]}
    assert events["PostToolUse"][1]["matcher"] == "apply_patch"
    state = tomllib.loads((codex / "config.toml").read_text(encoding="utf-8"))["hooks"]["state"]
    assert state == {prefix + "0:0": {"trusted_hash": "sha256:foreign", "enabled": False}}
