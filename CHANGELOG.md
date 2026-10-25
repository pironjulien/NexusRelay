# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/pironjulien/NexusRelay/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/pironjulien/NexusRelay/releases/tag/v1.1.0
