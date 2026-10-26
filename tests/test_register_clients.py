"""Déclaration du serveur aux clients trouvés (install.ps1 -Register) : Claude Code et Codex, jeton jamais écrit."""

from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "register_clients", Path(__file__).resolve().parents[1] / "scripts" / "register_clients.py"
)
assert _SPEC and _SPEC.loader
rc: Any = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rc)


@pytest.fixture(autouse=True)
def no_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc.shutil, "which", lambda name: None)  # ni `claude` ni `codex` : jamais les vrais
    monkeypatch.delenv("CODEX_HOME", raising=False)


def test_claude_code_par_son_fichier_sans_jeton_ecrit(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"mcpServers": {"autre": {"command": "x"}}, "theme": "dark"}), encoding="utf-8")
    assert rc.claude_code(tmp_path, 16181, tmp_path / "b").startswith("declared in")
    data = json.loads(config.read_text(encoding="utf-8"))
    entry = data["mcpServers"]["nexus-relay"]
    assert entry["url"] == "http://127.0.0.1:16181/mcp?agent=claude"
    assert entry["headers"]["Authorization"] == "Bearer ${NEXUS_MCP_TOKEN}"  # variable, jamais la valeur
    assert data["mcpServers"]["autre"] == {"command": "x"} and data["theme"] == "dark"
    assert rc.claude_code(tmp_path, 16181, tmp_path / "b") == "already declared"


def test_claude_code_entree_de_l_utilisateur_gardee(tmp_path: Path):
    config = tmp_path / ".claude.json"
    mine = {"mcpServers": {"nexus-relay": {"type": "http", "url": "https://ailleurs.example/mcp"}}}
    config.write_text(json.dumps(mine), encoding="utf-8")
    assert rc.claude_code(tmp_path, 16181, tmp_path / "b").startswith("kept")
    assert json.loads(config.read_text(encoding="utf-8")) == mine


def test_claude_code_plugin_deja_actif(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    settings = {"enabledPlugins": {"nexus-relay@nexus-plugins": True}}
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    assert rc.claude_code(tmp_path, 16181, tmp_path / "b") == rc.PLUGIN
    assert not (tmp_path / ".claude.json").exists()


def test_claude_code_absent(tmp_path: Path):
    assert rc.claude_code(tmp_path, 16181, tmp_path / "b") is None


def test_codex_table_ajoutee_relue_et_remplacee(tmp_path: Path):
    folder = tmp_path / ".codex"
    folder.mkdir()
    config = folder / "config.toml"
    config.write_text('model = "x"\n\n[mcp_servers.autre]\ncommand = "y"\n', encoding="utf-8")
    assert rc.codex(tmp_path, 16181, tmp_path / "b").startswith("declared in")
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["mcp_servers"]["nexus-relay"] == {
        "url": "http://127.0.0.1:16181/mcp?agent=codex",
        "bearer_token_env_var": "NEXUS_MCP_TOKEN",
    }
    assert data["model"] == "x" and data["mcp_servers"]["autre"] == {"command": "y"}
    assert rc.codex(tmp_path, 16181, tmp_path / "b") == "already declared"
    assert rc.codex(tmp_path, 16999, tmp_path / "b").startswith("declared in")  # port changé : remplacée sur place
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["mcp_servers"]["nexus-relay"]["url"].startswith("http://127.0.0.1:16999/")
    assert config.read_text(encoding="utf-8").count("[mcp_servers.nexus-relay]") == 1
    assert list((tmp_path / "b").iterdir())  # sauvegarde avant écriture


def test_codex_absent(tmp_path: Path):
    assert rc.codex(tmp_path, 16181, tmp_path / "b") is None


def test_ligne_du_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(rc.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("NEXUS_PACK", "1")
    (tmp_path / ".codex").mkdir()
    monkeypatch.setattr("sys.argv", ["x", "--backups", str(tmp_path / "b"), "--antigravity", "plugin installed in X"])
    assert rc.main() == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last == "NEXUS_PACK_RESULT Claude Code=absent; Codex=declared; Antigravity=plugin"


def test_perimetre_du_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """NEXUS_PACK_TARGETS : seules les IA choisies sont touchées et citées ; les autres restent intactes."""
    monkeypatch.setattr(rc.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("NEXUS_PACK", "1")
    monkeypatch.setenv("NEXUS_PACK_TARGETS", "codex, claude-desktop")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".claude").mkdir()
    claude = tmp_path / ".claude.json"
    claude.write_text('{"theme": "dark"}', encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["x", "--backups", str(tmp_path / "b"), "--antigravity", "plugin installed in X"])
    assert rc.main() == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last == "NEXUS_PACK_RESULT Codex=declared"
    assert claude.read_text(encoding="utf-8") == '{"theme": "dark"}'  # Claude Code exclu : fichier intact
    assert "nexus-relay" in (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")


def test_claude_code_par_sa_commande(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Commande `claude` présente : remove puis add-json en portée utilisateur, entrée HTTP sans jeton écrit."""
    calls: list[list[str]] = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def run(args: list[str], **kwargs: Any) -> Done:
        calls.append(args)
        return Done()

    monkeypatch.setattr(rc.shutil, "which", lambda name: "C:/bin/claude.exe" if name == "claude" else None)
    monkeypatch.setattr(rc.subprocess, "run", run)
    assert rc.claude_code(tmp_path, 16181, tmp_path / "b") == "declared (claude mcp add-json, user scope)"
    assert calls[0] == ["C:/bin/claude.exe", "mcp", "remove", "nexus-relay", "--scope", "user"]
    add = calls[1]
    assert add[:4] == ["C:/bin/claude.exe", "mcp", "add-json", "nexus-relay"] and add[-2:] == ["--scope", "user"]
    entry = json.loads(add[4])
    assert entry["url"] == "http://127.0.0.1:16181/mcp?agent=claude"
    assert entry["headers"]["Authorization"] == "Bearer ${NEXUS_MCP_TOKEN}"


def test_claude_code_commande_en_echec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class Failed:
        returncode, stdout, stderr = 1, "", "boom"

    monkeypatch.setattr(rc.shutil, "which", lambda name: "claude" if name == "claude" else None)
    monkeypatch.setattr(rc.subprocess, "run", lambda args, **kw: Failed())
    assert rc.claude_code(tmp_path, 16181, tmp_path / "b") == "failed: boom"
