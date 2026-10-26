"""Declares the nexus-relay MCP server to the AI clients found on this PC (install.ps1 -Register, one-line install).

The relay is one local daemon (http://127.0.0.1:16181/mcp) shared by every client, each one naming itself with
?agent=: Claude Code (`claude mcp add --transport http`, or ~/.claude.json) and Codex (~/.codex/config.toml) get an
HTTP entry whose bearer token is read from the user environment variable NEXUS_MCP_TOKEN, never written in their
files. Antigravity is handled by install.ps1 itself (nexus-relay plugin and sidecar). Claude Desktop's chat is not a
relay client (only Claude Code, the host, opens exchanges).

NEXUS_PACK_TARGETS (the Nexus pack: AI apps to configure, pack names comma separated): when set, a client not in
the list is never touched nor reported.

An entry of ours is replaced in place; an entry the user wrote differently, or an enabled nexus-relay Claude Code
plugin (which already provides the server), is left alone. Nothing else in these files is touched; each file is
written atomically after a backup. Prints one line per client, and with NEXUS_PACK=1 a last
"NEXUS_PACK_RESULT Claude Code=...; Codex=..." line for the Nexus pack.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path

NAME = "nexus-relay"
PACK_NAMES = {"Claude Code": "claude-code", "Codex": "codex", "Antigravity": "antigravity"}
TOKEN_VAR = "NEXUS_MCP_TOKEN"


def url(port: int, agent: str) -> str:
    return f"http://127.0.0.1:{port}/mcp?agent={agent}"


def claude_entry(port: int) -> dict:
    return {"type": "http", "url": url(port, "claude"), "headers": {"Authorization": f"Bearer ${{{TOKEN_VAR}}}"}}


def codex_entry(port: int) -> dict:
    return {"url": url(port, "codex"), "bearer_token_env_var": TOKEN_VAR}


def ours(existing: object) -> bool:
    """An entry written by this script (the local relay daemon), or none at all."""
    if existing is None:
        return True
    return isinstance(existing, dict) and str(existing.get("url", "")).startswith("http://127.0.0.1:")


def backup(path: Path, backups: Path) -> None:
    if path.exists():
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, backups / f"{path.parent.name}-{path.name}-{stamp}")


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".nexus-tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


PLUGIN = "provided by the nexus-relay plugin"


def claude_plugin_enabled(home: Path) -> bool:
    settings = home / ".claude" / "settings.json"
    try:
        enabled = json.loads(settings.read_text(encoding="utf-8-sig")).get("enabledPlugins", {})
    except (OSError, ValueError):
        return False
    return any(key.split("@")[0] == NAME and on for key, on in enabled.items())


def claude_code(home: Path, port: int, backups: Path) -> str | None:
    if claude_plugin_enabled(home):
        return PLUGIN
    cli = shutil.which("claude")
    config = home / ".claude.json"
    data = json.loads(config.read_text(encoding="utf-8-sig")) if config.exists() and config.stat().st_size else {}
    current = data.get("mcpServers", {}).get(NAME)
    if not ours(current):
        return "kept (an entry of yours already exists)"
    if current == claude_entry(port):
        return "already declared"
    if cli:
        subprocess.run([cli, "mcp", "remove", NAME, "--scope", "user"], capture_output=True, check=False)
        added = subprocess.run(
            [cli, "mcp", "add-json", NAME, json.dumps(claude_entry(port)), "--scope", "user"],
            capture_output=True,
            text=True,
            check=False,
        )
        if added.returncode:
            return f"failed: {(added.stderr or added.stdout).strip()}"
        return "declared (claude mcp add-json, user scope)"
    if not (home / ".claude").is_dir() and not config.exists():
        return None
    backup(config, backups)
    data.setdefault("mcpServers", {})[NAME] = claude_entry(port)
    write_atomic(config, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return f"declared in {config}"


TOML_TABLE = re.compile(r"^\[(?P<name>[^\]]+)\]\s*$", re.M)


def toml_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def codex(home: Path, port: int, backups: Path) -> str | None:
    folder = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    if not folder.is_dir() and not shutil.which("codex"):
        return None
    config = folder / "config.toml"
    text = config.read_text(encoding="utf-8-sig") if config.exists() else ""
    data = tomllib.loads(text)
    if (data.get("plugins") or {}).get(f"{NAME}@nexus", {}).get("enabled"):
        return PLUGIN
    current = data.get("mcp_servers", {}).get(NAME)
    if not ours(current):
        return "kept (an entry of yours already exists)"
    if current == codex_entry(port):
        return "already declared"
    if current is not None:
        # Our own tables ([mcp_servers.nexus-relay] and its subtables) are cut out, each up to the next table.
        parts, dropping, position = [], False, 0
        for match in TOML_TABLE.finditer(text):
            if not dropping:
                parts.append(text[position : match.start()])
            name = match.group("name").strip()
            dropping = name == f"mcp_servers.{NAME}" or name.startswith(f"mcp_servers.{NAME}.")
            position = match.start()
        if not dropping:
            parts.append(text[position:])
        text = "".join(parts)
        if tomllib.loads(text).get("mcp_servers", {}).get(NAME) is not None:
            return "kept (entry written in a form this installer does not edit)"
    block = (
        f"[mcp_servers.{NAME}]\n"
        f"url = {toml_string(url(port, 'codex'))}\n"
        f"bearer_token_env_var = {toml_string(TOKEN_VAR)}\n"
    )
    new = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block
    if tomllib.loads(new).get("mcp_servers", {}).get(NAME) != codex_entry(port):
        return "failed: the edited config.toml would not read back"  # never write a file Codex could not read
    backup(config, backups)
    write_atomic(config, new)
    return f"declared in {config}"


def pack_targets() -> set[str] | None:
    """AI apps chosen in the Nexus pack (NEXUS_PACK_TARGETS), or None: every client found."""
    raw = os.environ.get("NEXUS_PACK_TARGETS")
    return None if raw is None else {name.strip() for name in raw.split(",") if name.strip()}


def short(result: str | None) -> str:
    """One word per client for the Nexus pack's summary line."""
    if result is None:
        return "absent"
    for prefix, word in (("failed", "failed"), ("kept", "kept"), ("provided by", "plugin"), ("plugin", "plugin")):
        if result.startswith(prefix):
            return word
    return "declared"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=16181)
    parser.add_argument("--backups", required=True)
    parser.add_argument("--antigravity", default=None, help="result of install.ps1 for Antigravity (summary line)")
    args = parser.parse_args()
    home = Path.home()
    backups = Path(args.backups)
    results: dict[str, str | None] = {}
    targets = pack_targets()
    for client, declare in (("Claude Code", claude_code), ("Codex", codex)):
        if targets is not None and PACK_NAMES[client] not in targets:
            continue  # outside the user's choice in the Nexus pack: never touched
        try:
            results[client] = declare(home, args.port, backups)
        except (OSError, ValueError) as error:  # unreadable file (JSON or TOML): left as it is
            results[client] = f"failed: {error}"
    if args.antigravity and (targets is None or "antigravity" in targets):
        results["Antigravity"] = args.antigravity
    found = False
    for client, result in results.items():
        if result is not None:
            found = True
            print(f"{client}: {result}")
    if not found:
        print("No AI client found (Claude Code, Codex, Antigravity): install one, then run the installer again.")
    if os.environ.get("NEXUS_PACK") == "1":  # summary line read by the Nexus pack (get-nexus.ps1)
        print("NEXUS_PACK_RESULT " + "; ".join(f"{client}={short(result)}" for client, result in results.items()))
    return 1 if any(r and r.startswith("failed") for r in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
