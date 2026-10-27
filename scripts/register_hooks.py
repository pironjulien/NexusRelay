"""Declares the relay's edit guard (python -m nexus_relay.edit_guard <agent>, a PreToolUse hook) to Codex and
Antigravity; Claude Code gets it from the nexus-relay plugin of the nexus catalogue (hooks/hooks.json).

- Codex: ~/.codex/hooks.json (CODEX_HOME respected), matcher apply_patch, and its trust in config.toml
  ([hooks.state."<file>:pre_tool_use:<group>:0"] trusted_hash). Codex runs a non-managed hook only once trusted, the
  trust being the hash of the hook's normalized definition: sha256 of its sorted compact JSON
  (codex-rs/hooks/src/engine/discovery.rs hook_hash, codex-rs/config/src/fingerprint.rs version_for_toml). Written
  here, as /hooks would after a review: the definition is this installer's own.
- Antigravity: ~/.gemini/config/hooks.json, hook "nexus-relay-edit-guard", matcher of its file-writing tools.

Our entries are recognized by "nexus_relay.edit_guard" in their command and replaced in place; nothing else in these
files is touched; each file is backed up, then written atomically and read back. NEXUS_PACK_TARGETS honored.
Usage: python scripts/register_hooks.py --backups <folder> [--python <python.exe>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from register_clients import backup, pack_targets, write_atomic  # noqa: E402

MARK = "nexus_relay.edit_guard"
TIMEOUT_S = 15
CODEX_MATCHER = "apply_patch"
# edit_file et write_file : noms réels vus dans le journal d'Antigravity le 27/10 ; les trois autres : sa doc
AGY_MATCHER = "edit_file|write_file|write_to_file|replace_file_content|multi_replace_file_content"
AGY_NAME = "nexus-relay-edit-guard"


def command(python: str, agent: str) -> str:
    exe = f'"{python}"' if " " in python else python  # sans guillemets : lu pareil par cmd et PowerShell
    return f"{exe} -I -B -m {MARK} {agent}"


def ours(group: object) -> bool:
    return isinstance(group, dict) and any(MARK in str(h.get("command", "")) for h in group.get("hooks") or [])


def codex_hash(matcher: str, handler: dict) -> str:
    """Codex's trust hash of one normalized command handler (None fields absent, as in its TOML value)."""
    identity = {"event_name": "pre_tool_use", "matcher": matcher, "hooks": [handler]}
    text = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def toml_literal(text: str) -> str:
    if "'" in text or "\n" in text:
        raise ValueError(f"path not writable as a TOML literal key: {text}")
    return f"'{text}'"


def codex(home: Path, python: str, backups: Path) -> str | None:
    folder = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    if not folder.is_dir():
        return None
    hooks_path = folder / "hooks.json"
    data = json.loads(hooks_path.read_text(encoding="utf-8-sig")) if hooks_path.exists() else {}
    events = data.setdefault("hooks", {})
    groups = events.setdefault("PreToolUse", [])
    handler = {"type": "command", "command": command(python, "codex"), "timeout": TIMEOUT_S, "async": False}
    group = {"matcher": CODEX_MATCHER, "hooks": [handler]}
    index = next((i for i, g in enumerate(groups) if ours(g)), len(groups))
    changed_hooks = index == len(groups) or groups[index] != group
    if index == len(groups):
        groups.append(group)
    else:
        groups[index] = group
    key = f"{hooks_path}:pre_tool_use:{index}:0"
    trusted = codex_hash(CODEX_MATCHER, handler)
    config = folder / "config.toml"
    text = config.read_text(encoding="utf-8-sig") if config.exists() else ""
    state = tomllib.loads(text).get("hooks", {}).get("state", {})
    if not changed_hooks and (state.get(key) or {}).get("trusted_hash") == trusted:
        return "already declared and trusted"
    if changed_hooks:
        backup(hooks_path, backups)
        write_atomic(hooks_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    header = f"[hooks.state.{toml_literal(key)}]"
    # notre table (son en-tête exact) retirée jusqu'à la table suivante, puis réécrite en fin de fichier
    kept = re.sub(rf"(?ms)^{re.escape(header)}\s*$.*?(?=^\[|\Z)", "", text).rstrip("\n")
    new = kept + ("\n\n" if kept.strip() else "") + f'{header}\ntrusted_hash = "{trusted}"\n'
    if (tomllib.loads(new).get("hooks", {}).get("state", {}).get(key) or {}).get("trusted_hash") != trusted:
        return "failed: the edited config.toml would not read back"
    backup(config, backups)
    write_atomic(config, new)
    return f"declared in {hooks_path}, trusted in {config}"


def antigravity(home: Path, python: str, backups: Path) -> str | None:
    folder = home / ".gemini" / "config"
    if not folder.is_dir():
        return None
    path = folder / "hooks.json"
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    entry = {
        "PreToolUse": [
            {
                "matcher": AGY_MATCHER,
                "hooks": [{"type": "command", "command": command(python, "antigravity"), "timeout": TIMEOUT_S}],
            }
        ]
    }
    if data.get(AGY_NAME) == entry:
        return "already declared"
    data[AGY_NAME] = entry
    backup(path, backups)
    write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return f"declared in {path}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backups", required=True)
    parser.add_argument("--python", default=sys.executable, help="Python of the relay (its venv)")
    args = parser.parse_args()
    home, backups, targets = Path.home(), Path(args.backups), pack_targets()
    failed = False
    for client, pack, declare in (("Codex", "codex", codex), ("Antigravity", "antigravity", antigravity)):
        if targets is not None and pack not in targets:
            continue
        try:
            result = declare(home, args.python, backups)
        except (OSError, ValueError) as error:
            result = f"failed: {error}"
        if result is not None:
            print(f"{client} (edit guard): {result}")
            failed = failed or result.startswith("failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
