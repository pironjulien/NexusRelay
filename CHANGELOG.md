# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.9.0] - 2026-10-30

### Added
- Native C# (.NET 10) engine in `native/`, built outside OneDrive and installed as an immutable version under
  `%LOCALAPPDATA%\NexusRelay\versions\<hash>`: wake and tidy workers, unread-message recovery, per-topic backoff,
  bounded retries, protection of uncertain deliveries, `/health` (service, PID, runtime) and a `status` with real
  heartbeats, errors and sidecar state. The 20 MCP schemas of the Python server are frozen in a native resource and
  compared in full. A C# Antigravity sidecar uses the official `agentapi`. Qualification status: `native/QUALIFICATION.md`.
- Native self-tests (`NexusRelay.exe --selftest`) and black-box pytest tests (`tests/test_native_csharp.py`): full
  room cycle, exclusive claims, `cancel`, crash recovery, vault protection, edit guard, stdio and loopback HTTP MCP.
- `release(id, force=true)`: on the user's request the host takes back an absent agent's reservation, by id only,
  traced by `released_by`; files are left as they are.
- Single hook entry point `scripts/invoke-hook.ps1` for edit, end-of-tool/session, Git guards and host
  notification; it follows the installed runtime and keeps UTF-8.

### Changed
- The installed runtime is chosen explicitly (`install.ps1 -Runtime python|native`, then the local `runtime`
  setting); a build no longer switches the service. Installs without a choice keep the Python engine until the
  native end-to-end qualification is complete.
- User apps are launched through the Windows shell, outside the service's job object. The installer no longer
  kills processes by a global pattern: it checks executable, module, port, PID and start time, then asks that one
  instance to exit cleanly.

### Fixed
- Codex wake on app 26.10: the relay goes back to Home (where projects are listed) before checking the project
  identity; the new-chat button is activated by keyboard on the exact element; a topic chat shown both under its
  project and in "Chats" is reopened; the empty draft of a freshly started Codex is no longer protected as a user draft.
- Antigravity edit hook: the `invoke-hook.ps1` path is no longer quoted (Antigravity runs it without a shell).
- Codex Git guard identity aligned with its hooks (`NEXUS_RELAY_SESSION`, then `CODEX_SESSION_ID`, then
  `CODEX_THREAD_ID`): a sub-agent no longer blocks the files of its own parent conversation.
- Automatic reservations: end-of-tool and end-of-turn hooks release attempts without writes as soon as Git confirms
  nothing changed; failed tools after a write, staged/modified/untracked files, manual reservations and other
  conversations stay protected. Post-commit requires the exact conversation and a finished write.
- `wait_reply` claims every id before reading, uses one shared deadline and keeps answers until the whole group
  returns; an exclusive lease protects each request against a second concurrent wait, even from another process.
- A Codex delivery intent is persisted before Enter; an interruption there becomes uncertain and is never replayed.
- NexusKeypad shared quota uses the model family actually selected; an exhausted unused family no longer blocks Gemini.
- ISO date strings in settings stay strings under PowerShell 7.5.
- The installer no longer drops the `host` and `token_file` settings it has just written when it records the runtime.
- From an MSIX-installed Codex, AppData artifacts may be redirected into its package cache: the installer now
  declares their real physical path.

## [1.8.7] - 2026-10-28

### Fixed
- Codex reuses the edit guard carried by an installed Relay plugin from any catalog, including local-version
  caches. Migration removes only the duplicate global guard, preserves other hooks and keeps their trust records
  attached to the correct handler. Disabled plugins and orphaned caches are not treated as active plugins.

### Changed
- Codex guide and Relay project instructions, in English and French: progress uses `progress` or ordinary
  commentary, never question cards. Peer-only chats still ask no questions of the user; these instructions ship
  with the product and do not depend on a private user profile.

## [1.8.6] - 2026-10-28

### Fixed
- Codex: a nexus-relay plugin from any catalog (including `nexus-plugins`, the NexusSystem catalog that offers it
  since 28/10) is recognised, so the install no longer adds a second server to config.toml.

## [1.8.5] - 2026-10-28

### Fixed
- Reviews: a message that asks for a verdict ("Expected: green light on …", or its French form) is a review request
  wherever that phrase is written, while a mere mention ("I already gave my green light on…") is not; a "second review" sent to one peer was not widened to both.

### Added
- Signed releases: the files PyPI serves (wheel, sdist) are attached to each release with their Sigstore attestation
  and SHA256SUMS.txt (`release-assets.yml`; verify with `gh attestation verify <file> --repo pironjulien/NexusRelay`).
  1.8.4 was signed afterwards.

## [1.8.4] - 2026-10-28

### Changed
- Reviews: a review request always goes to BOTH peers (independent opinions). Sent to one of them, it is also sent to
  the other; the answer gives `ids` (one per peer, for `wait_reply(id=ids)`) and `widened`.

## [1.8.3] - 2026-10-27

### Fixed
- Reviews: the `file:line` reference finder no longer takes exponential time on a crafted peer reply (ReDoS found
  by CodeQL: 1,000 repetitions of `-\` blocked it for more than a minute); a few more path forms are recognized.

### Changed
- CodeQL findings addressed: reasons written in silent `except` blocks, tests tidied; the others dismissed on GitHub
  with their reason.

## [1.8.2] - 2026-10-27

### Added
- CodeQL code scanning of the relay and the workflows, on every push and weekly.
- Coverage-guided fuzzing (Atheris, `fuzz/`, `.github/workflows/fuzz.yml`) of file reservations (a pattern drawn
  from a path always covers it: never a false "free"), stored paths and topics.

### Changed
- `SECURITY.md`: disclosure timeline (first answer within 3 days, fix and advisory usually within 30 days); private reporting link.
- Self-contained daemon spec (`~\.nexus\daemons\specs\nexus-relay.json`): `install.ps1` declares the restart
  request the daemon follows (`restart_request`) and pins its mailbox (`NEXUS_RELAY_HOME`, also in the environment
  of the Antigravity sidecar). A general supervisor takes that request before each launch without knowing the relay;
  `nexus_relay.daemon` also takes the declared request, then reads the spec again. A `NEXUS_RELAY_HOME` inherited
  from the launcher no longer sends the daemon to another mailbox than the one holding its requests.

### Fixed
- File reservations: paths that designate no file are refused (network path without server and share such as
  `\\?\UNC\` or `\\server`, doubled prefix such as `\\?\\\?\`, a device such as `\\.\PhysicalDrive0`, a control
  character, a path without a real drive letter such as `\:\`, a name made only of dots or spaces outside the long
  form); their stored form did not read back the same (found by the new fuzzing). `\\.\C:\…` is stored as `C:\…`,
  the same file; a trailing Unicode space (no-break…) is kept as part of the name, like Windows does.

## [1.8.1] - 2026-10-27

### Fixed
- Daemon supervisor (`nexus_relay.daemon`): a restart request left by `install.ps1` before the task starts is
  satisfied by that launch (taken before the spec is read, given back if nothing is launched; a newer request
  stays). A brand-new daemon no longer restarts two seconds after answering, which left the port closed for the
  next step of the Windows CI on 1.8.0.

## [1.8.0] - 2026-10-27

### Added
- Shared edit hook for the three agents (`python -m nexus_relay.edit_guard <agent>`, PreToolUse): before an agent
  writes a file, the write is refused if another agent or conversation holds it; otherwise the file is reserved for
  an hour, renewed on each write and released by a `post-commit` hook once committed. Installed for Claude Code
  (plugin), Codex (`~/.codex/hooks.json`) and Antigravity (`~/.gemini/config/hooks.json`) by `install.ps1`.
- `cancel(id)`: the sender withdraws a request the recipient has not taken yet. It leaves the recipient's inbox, the
  doorbell and the sidecar; `track` says "cancelled", `wait_reply` returns `cancelled`. If it was already rung or
  delivered, the peer that opens it sees `cancelled` and cannot `reply` or `progress`. Once read, in progress or
  answered, cancelling is refused.
- Asking both peers at once: `send(to=["codex", "antigravity"], …)` posts one message per recipient (only the host
  opens an exchange, as before) and returns `ids`; `wait_reply(id=ids)` waits for all the answers (`success`, or
  `partial` with each one's result).

### Changed
- Structured output: `inbox`, `read`, `track`, `search`, `summary`, `reservations` and `status` declare a real output
  schema and return their data as `structuredContent` (the same JSON stays in the text block); errors come back with
  `isError`. The other tools no longer advertise a placeholder `{result: string}` schema.

## [1.7.0] - 2026-10-27

### Added
- The MCP server sends its icon and website to clients when they connect, and announces how long its tool list stays
  fresh (one minute, private cache: MCP protocol 2026-07-28).

### Changed
- The MCP SDK is capped below 3 (version 3 will remove what 2026-07-28 deprecates).

## [1.6.2] - 2026-10-27

### Fixed

- Search index: a catch-up could remove from the index a message that still existed (file inventory and index read
  taken before `BEGIN IMMEDIATE`, while the in-process lock does not guard against other processes). New messages
  are read outside the lock; the inventory that decides removals and the index read are redone inside the immediate
  transaction, exclusive across processes.
- Search index: several simultaneous openings of a new database failed with "database is locked" (unguarded
  `PRAGMA journal_mode=WAL`). WAL is set only when missing, and a busy initialization is retried within 30 s, across
  threads and processes.
- `inbox()`: an error in the middle of the listing discarded the previews already marked read (read, never shown).
  Those already delivered are returned along with the error; the rest stays unread.
- `search`: a term containing the NUL character (U+0000) crashed FTS5 ("unterminated string"); it is matched with
  `instr()`.
- CHANGELOG: [1.6.1] comparison link added, [Unreleased] compares from the latest release.

## [1.6.1] - 2026-10-27

### Added

- `server.json`: `icons` (Nexus family medallion, PNG and SVG), so the MCP registry and its clients show the product
  icon.

### Fixed

- Messages for the host were never delivered when nobody waited for them (a follow-up sent after the answer was
  returned, the answer to a request never awaited, a peer's for-information message): they stayed unread. A new
  Claude Code hook (`python -m nexus_relay.hook`, SessionStart and UserPromptSubmit) points out, once per message,
  the host's unread messages by topic and sender, without marking anything read. `inbox()` marks as read the answers
  and for-information messages it lists (previews; `read(id)` for the full text); a request stays unread until
  `read` or `reply`.

### Changed

- Wake-up texts aligned between Codex and Antigravity: "the user does not answer here" and "decide, or ask
  Claude"; same words in the skills.

## [1.6.0] - 2026-10-27

### Added

- On PyPI and in the official MCP registry: `uvx --python 3.14 nexus-relay` starts the server over stdio for one
  agent (`NEXUS_RELAY_AGENT`), with no install step; `get.ps1` stays the complete setup. Releases are published by
  Trusted Publishing (`publish.yml`, OIDC, no token), then `server.json` goes to the registry
  (`io.github.pironjulien/nexus-relay`).

### Changed

- `search` uses a SQLite FTS5 index (trigram, accent and case insensitive) in `~/.nexus/relay/v1/index` instead of
  reading every message on each call. Existing history is indexed when the daemon starts (or at the first search);
  new messages are added at each search; a missing index is rebuilt. Results are the same as before.
- pyright 1.1.414 (development).

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

[Unreleased]: https://github.com/pironjulien/NexusRelay/compare/v1.9.0...HEAD
[1.8.3]: https://github.com/pironjulien/NexusRelay/releases/tag/v1.8.3
[1.8.2]: https://github.com/pironjulien/NexusRelay/releases/tag/v1.8.2
[1.6.2]: https://github.com/pironjulien/NexusRelay/compare/v1.6.1...v1.6.2
[1.6.1]: https://github.com/pironjulien/NexusRelay/compare/v1.6.0...v1.6.1
[1.6.0]: https://github.com/pironjulien/NexusRelay/compare/v1.5.3...v1.6.0
[1.5.3]: https://github.com/pironjulien/NexusRelay/compare/v1.5.2...v1.5.3
[1.5.2]: https://github.com/pironjulien/NexusRelay/compare/v1.5.1...v1.5.2
[1.5.1]: https://github.com/pironjulien/NexusRelay/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/pironjulien/NexusRelay/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/pironjulien/NexusRelay/compare/v1.3.2...v1.4.0
[1.3.2]: https://github.com/pironjulien/NexusRelay/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/pironjulien/NexusRelay/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/pironjulien/NexusRelay/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/pironjulien/NexusRelay/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/pironjulien/NexusRelay/releases/tag/v1.1.0
