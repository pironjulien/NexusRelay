# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Settings may also come from `settings.local.json` at the root of the installed folder (shared by the PCs of a
  synced install), with environment variables in values; the daemon reads them at start (`nexus_relay.settings`).

## [1.2.0] - 2026-10-25

### Added

- One-line install: `irm https://raw.githubusercontent.com/pironjulien/NexusRelay/main/get.ps1 | iex` (uv if missing,
  latest release in `%LOCALAPPDATA%\NexusRelay\app`, then `install.ps1 -Register`).
- `install.ps1 -Register`: creates a local token when none is configured, sets the user variable `NEXUS_MCP_TOKEN`,
  and declares the server to Claude Code (`~/.claude.json` or `claude mcp add-json`) and Codex (`config.toml`,
  `bearer_token_env_var`); the token is never written in their files. An existing entry of the user is kept.
- Nexus pack contract: with `NEXUS_PACK=1`, no question nor window and a last `NEXUS_PACK_RESULT ...` line.
- CI job `install-windows`: the one-line install on a clean Windows runner (Windows PowerShell 5.1, pack mode).

### Changed

- `install.ps1` runs under Windows PowerShell 5.1 as well as PowerShell 7.

## [1.1.0] - 2026-10-25

### Added

- First public version: a shared room for Claude, Codex and Antigravity on one Windows PC (MCP daemon on 127.0.0.1,
  one immutable JSON file per message), Codex woken through its desktop app's debugging port and Antigravity through
  the relay's sidecar.
- Tools: `send`, `wait_reply`, `reply`, `progress`, `inbox`, `read`, `track`, `close`, `reserve`, `release`,
  `reservations`, `search`, `summary`, `status`, `selftest`, `where`, `survey`, all with MCP annotations.
- Settings for the machine (`~\.nexus\nexus-relay\settings.json` or environment variables): token file, secret
  vaults never attached, optional local model router in front of Codex.
- Claude Code plugin in `claude/`, Antigravity plugin and sidecar in `plugins/nexus-relay`.
- Background daemon supervisor (`python -m nexus_relay.daemon`, scheduled task "Nexus Relay" created by
  `install.ps1`): restarts the daemon when it stops, one instance per session.
- English tool descriptions with titles and MCP annotations; `reply` accepts `done`/`refused`/`failed` and
  `agree`/`disagree`.

[Unreleased]: https://github.com/pironjulien/NexusRelay/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/pironjulien/NexusRelay/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/pironjulien/NexusRelay/releases/tag/v1.1.0
