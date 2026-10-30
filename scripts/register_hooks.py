"""Declares the relay's edit guard (python -m nexus_relay.edit_guard <agent>, a PreToolUse hook) to Codex and
Antigravity; Claude Code gets it from the nexus-relay plugin of the nexus catalogue (hooks/hooks.json).

- Codex: ~/.codex/hooks.json (CODEX_HOME respected), PreToolUse/PostToolUse on apply_patch, then Stop. New handlers
  require Codex's own review; this installer never writes a trusted_hash. Existing unrelated trust is preserved.
  When an installed nexus-relay plugin carries an event, its global duplicate is removed instead.
  Disabled plugins stay disabled. An older plugin carrying only PreToolUse still gets the missing finalizers.
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
EVENTS = {
    "PreToolUse": ("pre_tool_use", ""),
    "PostToolUse": ("post_tool_use", "--finish-tool"),
    "Stop": ("stop", "--finish-session"),
}


def command(python: str, agent: str, phase: str = "") -> str:
    exe = f'"{python}"' if " " in python else python  # sans guillemets : lu pareil par cmd et PowerShell
    if Path(python).name.lower() == "invoke-hook.ps1":
        selected_phase = phase.removeprefix("--") or "begin"
        # Antigravity lance la commande sans shell : des guillemets inutiles arrivent tels quels à pwsh -File (30/10)
        return f"pwsh -NoProfile -NonInteractive -File {exe} -Agent {agent} -Phase {selected_phase}"
    if Path(python).name.lower() == "nexusrelay.exe":
        return f"{exe} --edit-guard {agent}" + (f" {phase}" if phase else "")
    return f"{exe} -I -B -m {MARK} {agent}" + (f" {phase}" if phase else "")


def ours(group: object) -> bool:
    return isinstance(group, dict) and any(our_handler(h) for h in group.get("hooks") or [])


def our_handler(handler: object) -> bool:
    if not isinstance(handler, dict):
        return False
    text = str(handler.get("command", ""))
    return (
        MARK in text
        or ("nexusrelay.exe" in text.lower() and "--edit-guard" in text)
        or ("nexus-relay" in text.lower() and "invoke-hook.ps1" in text.lower())
    )


def codex_hash(matcher: str, handler: dict, event: str = "pre_tool_use") -> str:
    """Codex's trust hash of one normalized command handler (None fields absent, as in its TOML value)."""
    identity = {"event_name": event, "matcher": matcher, "hooks": [handler]}
    text = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def toml_literal(text: str) -> str:
    if "'" in text or "\n" in text:
        return json.dumps(text, ensure_ascii=False)
    return f"'{text}'"


def manifest_carries_guard(manifest: Path, event: str = "PreToolUse") -> bool:
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or data.get("name") != "nexus-relay":
            return False
        hooks = data.get("hooks") or {}
        events = hooks.get("hooks") if isinstance(hooks, dict) else None
        return isinstance(events, dict) and any(
            ours(group)
            and (event == "Stop" or group.get("matcher") == CODEX_MATCHER)
            and any(
                our_handler(h)
                and (
                    EVENTS[event][1] in h.get("command", "")
                    or f"-Phase {EVENTS[event][1].removeprefix('--')}" in h.get("command", "")
                )
                for h in group.get("hooks") or []
            )
            for group in events.get(event) or []
        )
    except (OSError, ValueError):
        return False


def relay_plugins(config: dict) -> dict[str, dict]:
    return {
        name: settings
        for name, settings in (config.get("plugins") or {}).items()
        if name.split("@", 1)[0] == "nexus-relay" and isinstance(settings, dict)
    }


def carried_by_plugin(folder: Path, config: dict, event: str = "PreToolUse") -> bool:
    """Personal source or installed repo-marketplace copy; an orphaned cache is not an installed plugin.

    Codex installs local marketplace entries into cache/<marketplace>/<name>/local (OpenAI's plugin packaging docs).
    The personal generator writes its source and installed copy together, retaining its historical source lookup.
    """
    plugins = relay_plugins(config)
    active = [name for name, settings in plugins.items() if settings.get("enabled", True)]
    if not plugins or "nexus-relay@personal" in active:
        if manifest_carries_guard(folder / "plugins" / "nexus-relay" / ".codex-plugin" / "plugin.json", event):
            return True
    for name in active:
        parts = name.split("@", 1)
        if len(parts) != 2 or parts[1] in {"", ".", ".."} or any(c in parts[1] for c in "/\\"):
            continue
        manifest = folder / "plugins" / "cache" / parts[1] / "nexus-relay" / "local" / ".codex-plugin" / "plugin.json"
        if manifest_carries_guard(manifest, event):
            return True
    return False


TABLE_HEADER = re.compile(r"(?m)^[ \t]*\[[^\r\n]*(?:\r?\n|$)")


def move_trust(text: str, remap: dict[str, str | None]) -> str:
    """Move only affected trust sections when deleting handlers shifts their global group/handler indexes."""
    expected = tomllib.loads(text)
    state = expected.get("hooks", {}).get("state", {})
    moved = {key: value for key, value in state.items() if key not in remap}
    moved.update({target: state[key] for key, target in remap.items() if target is not None and key in state})
    if state:
        expected["hooks"]["state"] = moved
    matches = list(TABLE_HEADER.finditer(text))
    parts = [text[: matches[0].start()] if matches else text]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        header = match.group()
        try:
            keys = tomllib.loads(header).get("hooks", {}).get("state", {})
        except ValueError:
            keys = {}
        key = next(iter(keys)) if isinstance(keys, dict) and len(keys) == 1 else None
        if key is not None and key in remap:
            target = remap[key]
            if target is not None:
                parts.append(f"[hooks.state.{toml_literal(target)}]\n" + text[match.end() : end])
        else:
            parts.append(text[match.start() : end])
    result = "".join(parts)
    # Empty state tables may disappear after removing our final entry.
    actual = tomllib.loads(result)
    for parsed in (actual, expected):
        if parsed.get("hooks", {}).get("state") == {}:
            parsed["hooks"].pop("state")
        if parsed.get("hooks") == {}:
            parsed.pop("hooks")
    if actual != expected:
        raise ValueError("could not preserve the unrelated Codex hook trust sections")
    return result


def codex_drop(
    folder: Path,
    backups: Path,
    reason: str = "carried by the nexus-relay plugin",
    events: set[str] | None = None,
) -> str:
    """Remove all our global handlers, preserving other handlers and their trust even when their indexes move."""
    hooks_path = folder / "hooks.json"
    data = json.loads(hooks_path.read_text(encoding="utf-8-sig")) if hooks_path.exists() else {}
    remap = {}
    for event in EVENTS if events is None else events:
        groups = (data.get("hooks") or {}).get(event) or []
        if not any(ours(group) for group in groups):
            continue
        kept = []
        name = EVENTS[event][0]
        for index, group in enumerate(groups):
            handlers = group.get("hooks") or []
            remaining = [handler for handler in handlers if not our_handler(handler)]
            for handler_index, handler in enumerate(handlers):
                key = f"{hooks_path}:{name}:{index}:{handler_index}"
                if our_handler(handler):
                    remap[key] = None
                elif remaining:
                    new_index = sum(not our_handler(h) for h in handlers[:handler_index])
                    remap[key] = f"{hooks_path}:{name}:{len(kept)}:{new_index}"
            if remaining or not ours(group):
                kept.append({**group, "hooks": remaining} if ours(group) else group)
        if kept:
            data["hooks"][event] = kept
        else:
            del data["hooks"][event]
    if not remap:
        return reason
    config = folder / "config.toml"
    text, updated = "", ""
    if config.exists():
        text = config.read_text(encoding="utf-8-sig")
        updated = move_trust(text, remap)
    backup(hooks_path, backups)
    write_atomic(hooks_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    if updated != text:
        backup(config, backups)
        write_atomic(config, updated)
    return f"{reason}, global entry removed from {hooks_path}"


def codex(home: Path, python: str, backups: Path) -> str | None:
    folder = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    if not folder.is_dir():
        return None
    config = folder / "config.toml"
    text = config.read_text(encoding="utf-8-sig") if config.exists() else ""
    settings = tomllib.loads(text)
    plugins = relay_plugins(settings)
    if plugins and not any(plugin.get("enabled", True) for plugin in plugins.values()):
        return codex_drop(folder, backups, "kept disabled (nexus-relay plugin)")
    carried = {event for event in EVENTS if carried_by_plugin(folder, settings, event)}
    migration = codex_drop(folder, backups, events=carried)
    if carried == set(EVENTS):
        return migration
    hooks_path = folder / "hooks.json"
    data = json.loads(hooks_path.read_text(encoding="utf-8-sig")) if hooks_path.exists() else {}
    # An older owned handler can share a group with foreign handlers. Never replace that whole group or change
    # its matcher: remove only ours with the existing trust remapping, then append a separate canonical group.
    mixed = set()
    for event in set(EVENTS) - carried:
        groups = (data.get("hooks") or {}).get(event) or []
        owned = [group for group in groups if ours(group)]
        matcher = CODEX_MATCHER if event != "Stop" else ""
        if owned and not (
            len(owned) == 1 and len(owned[0].get("hooks") or []) == 1 and owned[0].get("matcher", "") == matcher
        ):
            mixed.add(event)
    if mixed:
        codex_drop(folder, backups, "updating mixed relay handlers", events=mixed)
        data = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
    events = data.setdefault("hooks", {})
    changed_hooks = False
    for event, (_, phase) in EVENTS.items():
        if event in carried:
            continue
        groups = events.setdefault(event, [])
        handler = {"type": "command", "command": command(python, "codex", phase), "timeout": TIMEOUT_S, "async": False}
        group = {"matcher": CODEX_MATCHER if event != "Stop" else "", "hooks": [handler]}
        index = next((i for i, g in enumerate(groups) if ours(g)), len(groups))
        changed_hooks = changed_hooks or index == len(groups) or groups[index] != group
        if index == len(groups):
            groups.append(group)
        else:
            groups[index] = group
    if changed_hooks:
        backup(hooks_path, backups)
        write_atomic(hooks_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return f"{'declared' if changed_hooks else 'already declared'} in {hooks_path}; Codex review required for new hooks"


def antigravity(home: Path, python: str, backups: Path) -> str | None:
    folder = home / ".gemini" / "config"
    if not folder.is_dir():
        return None
    path = folder / "hooks.json"
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    entry: dict[str, object] = {
        "PreToolUse": [
            {
                "matcher": AGY_MATCHER,
                "hooks": [{"type": "command", "command": command(python, "antigravity"), "timeout": TIMEOUT_S}],
            }
        ],
        "PostToolUse": [
            {
                "matcher": AGY_MATCHER,
                "hooks": [
                    {
                        "type": "command",
                        "command": command(python, "antigravity", "--finish-tool"),
                        "timeout": TIMEOUT_S,
                    }
                ],
            }
        ],
        "Stop": [
            {"type": "command", "command": command(python, "antigravity", "--finish-session"), "timeout": TIMEOUT_S}
        ],
    }
    if data.get(AGY_NAME, {}).get("enabled") is False:
        entry["enabled"] = False
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
    parser.add_argument("--native", help="Explicit native NexusRelay.exe for installed edit guards")
    parser.add_argument("--wrapper", help="Stable invoke-hook.ps1 runtime dispatcher")
    args = parser.parse_args()
    home, backups, targets = Path.home(), Path(args.backups), pack_targets()
    failed = False
    for client, pack, declare in (("Codex", "codex", codex), ("Antigravity", "antigravity", antigravity)):
        if targets is not None and pack not in targets:
            continue
        try:
            result = declare(home, args.wrapper or args.native or args.python, backups)
        except (OSError, ValueError) as error:
            result = f"failed: {error}"
        if result is not None:
            print(f"{client} (edit guard): {result}")
            failed = failed or result.startswith("failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
