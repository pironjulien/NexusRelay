# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.5.3] - 2026-10-27

### Changed

- Sharper Antigravity and Codex plugin logos (256 px): the julienpiron.fr medallion rebuilt at 600 px in the icon kit, sharp up to 512 px instead of a 240 px picture enlarged.

## [1.5.2] - 2026-10-27

### Changed

- Codex `composerIcon` (`codex/assets/icon.png`, 64 px): the julienpiron.fr medallion cut round (transparent corners),
  the Nexus family rule under 44 px, instead of the relay pictogram alone.

## [1.5.1] - 2026-10-26

### Added

- Nexus family icon: the relay pictogram in the julienpiron.fr medallion, the same as on the site. It is the logo of
  the Antigravity plugin (`plugins/nexus-relay/logo.png`, `logo` key, copied with the plugin) and of the Codex plugin
  (`codex/assets/logo.png`, 256 px; `composerIcon` is the pictogram alone, 64 px, for small sizes), and heads the
  README.

## [1.5.0] - 2026-10-26

### Added

- English everywhere for agents: the `lang` setting (`NEXUS_RELAY_LANG`, `en` by default, or `fr`) sets the language
  of every text agents receive (tool results and errors, wake text, selftest) and of the deployed instructions
  (skills, Codex `AGENTS.md`); French copies live in `fr/`.
- File reservations under the mcp_agent_mail names: `file_reservation_paths` and `release_file_reservations`
  (same parameters; `project_key` and `agent_name` are accepted and ignored).
- Antigravity open without its debugging port is restarted with it after 5 minutes, never while you are in it or
  while one of its agents works, at most once an hour.
- Ready-made Codex plugin in `codex/` (MCP server, skill, marketplace file).

## [1.4.0] - 2026-10-26

### Added

- Configurable host: one agent opens the exchanges and is never woken; Claude by default, otherwise the `host`
  setting (`NEXUS_RELAY_HOST`): `claude`, `codex` or `antigravity`. `install.ps1 -Register` picks it when unset:
  `NEXUS_PACK_HOST`, else the first agent found among the chosen ones (Claude Code, Codex, Antigravity). `status()`
  shows `host`; `state/peers.json` carries `host` only when it is not Claude. NexusRelay now works without Claude Code.

### Fixed

- Antigravity quotas read again: the reader targets the Settings window (the one with the Models tab) instead of
  the first `[role=dialog]`, which could be Antigravity's "Conversation Archived" notification.

## [1.3.2] - 2026-10-26

### Added

- Nexus pack scope: with `NEXUS_PACK_TARGETS` (AI apps chosen by the user, comma separated), the installer only
  configures those apps; Claude Code, Codex or Antigravity left out are never touched nor reported. CI job
  `install-windows-scoped`.

### Changed

- README, Privacy: the relay's transport is local, but what a connected AI reads through it (messages, attachments,
  images) goes to that AI's model provider under its own policy; the previous wording said nothing left the machine.

## [1.3.1] - 2026-10-26

### Fixed

- `get.ps1` reads the latest release from the `releases/latest` redirect instead of the GitHub API, whose
  unauthenticated rate limit could stop an install or a self-update.
- Self-update and the Codex router's script launcher start PowerShell in a new process group instead of
  `DETACHED_PROCESS`, under which PowerShell runs nothing and exits with code 0.

## [1.3.0] - 2026-10-26

### Added

- Self-update: `POST /api/update` on the daemon's local API (bearer token) answers `202` and the daemon runs its
  own `get.ps1` (`-File`, no window), then restarts on the new release; `GET /api/update` and
  `~\.nexus\status\relay.json` give `{product, version, latest, state, checked_at, message}`. Latest version
  read from the `releases/latest` redirect at most once a day (`NEXUS_RELAY_UPDATE_CHECK=0` turns it off).
  Only installs made by `get.ps1` update themselves (a git checkout answers `409`).
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

[Unreleased]: https://github.com/pironjulien/NexusRelay/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/pironjulien/NexusRelay/compare/v1.3.2...v1.4.0
[1.3.2]: https://github.com/pironjulien/NexusRelay/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/pironjulien/NexusRelay/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/pironjulien/NexusRelay/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/pironjulien/NexusRelay/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/pironjulien/NexusRelay/releases/tag/v1.1.0
