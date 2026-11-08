# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.15.1] - 2026-11-08

### Fixed
- An uncertain Codex wake is now settled by Codex's own journal instead of staying stuck: when the user came back to
  Codex during the send check, the wake text stayed in the topic chat's draft, the request was never delivered, never
  replayed, and every later wake of the topic waited behind that draft. Two minutes after Enter, a wake found as a user
  turn in Codex's journal counts as delivered (never sent twice); one absent from every journal counts as not delivered
  and the bounded retry may wake again.
- A draft that is exactly Relay's own wake text is recognized as Relay's leftover and cleared in the verified topic
  chat before the new wake; any other draft is still the user's and is never touched.

## [1.15.0] - 2026-11-08

### Changed
- The engine is compiled ahead of time with Native AOT: the release packages and the `.mcpb` bundles carry one
  `NexusRelay.exe` per architecture (win-x64, win-arm64), with no .NET runtime and no JIT. Measured on the same PC
  against 1.14.1, with the same method (isolated `--http --protocol-fixture` daemon, then the real `--http 16181`
  daemon after 5 minutes):
  - engine on disk: 1 file of 11.4 MB instead of 195 files and 77.9 MB;
  - start-up until the first `tools/list` answer: 37 ms instead of 93 ms (median of 15); `--version` 11 ms instead of
    46 ms;
  - isolated daemon at rest after 5 minutes: working set 26.4 MB instead of 36.8 MB (private bytes 17.6 MB instead
    of 11.1 MB: the AOT garbage collector reserves its heap up front);
  - real daemon (Claude, Codex and Antigravity connected) after 5 minutes: working set 72.9 MB instead of 134.6 MB,
    private bytes 74.7 MB instead of 95.7 MB, although two end-to-end wake self-tests ran during the AOT window.
- Windows interop without the runtime binder or the built-in COM wrappers: the desktop launch broker (Explorer's
  `ShellWindows` and `Shell.Application`) goes through a source-generated `IDispatch`, the Task Scheduler and the
  Store app activation through `[GeneratedComInterface]` interfaces, window enumeration through an
  `UnmanagedCallersOnly` callback and the process snapshot through a blittable `PROCESSENTRY32W`; JSON strings are
  written with a source-generated context. The trimming and AOT analyzers run in every build, warnings as errors.
- The AOT compiler and trimmer packages are pinned in `packages.lock.json`; building from a clone (`install.ps1`) needs
  the Visual Studio C++ build tools.

## [1.14.1] - 2026-11-08

### Fixed
- The common journal of the Nexus tools is written again (`%USERPROFILE%\.nexus\journal\relay\<agent>\<YYYY-MM>.jsonl`,
  format v1): the move to the native engine (30/10) had dropped it with the Python engine, so NexusSystem and `NexusJournal revue`
  saw no relay activity since 30/10. One line per `send`, `read`, `reply`, `wait_reply`, `cancel`, `close`, wake
  (`wake`, `✗` with its short cause for a failed one), Antigravity delivery (`livraison`) and peer turn stopped on an
  error (`tour`); `outil` is the engine version, `ms` the duration, `erreur` a short cause, never the text of a message.
  A test checks that each action writes its line in the common format.

## [1.14.0] - 2026-11-08

### Changed
- Installation and updates are in the engine: `NexusRelay.exe --install` (C#) does every step the PowerShell
  installer did, with the same result on the PC: app folder, immutable engine copy in `versions`, stable copy `bin`
  checked before and after its switch, settings, shared token, host, daemon spec, cooperative restart requests of the
  daemon and the sidecar (ownership checked through the Windows TCP table and the process image), scheduled task
  "NexusRelay" through the Task Scheduler API, Antigravity plugin, sidecar and project, edit hooks, user variable
  `NEXUS_MCP_TOKEN` and the clients declared. Its messages are in English, and in French when Windows is displayed in
  French.
- `get.ps1` is a minimal starter: it downloads the release package, checks it against `SHA256SUMS.txt` and, when the
  GitHub CLI is signed in, its build provenance, then starts the package's own engine. `install.ps1` only builds the
  engine of a clone (and runs `test.ps1` before anything is installed), or starts the engine of an unpacked release
  (what the `get.ps1` of an earlier version runs).
- The daemon's update (`POST /api/update`) starts the engine itself (`NexusRelay.exe --update`: download, checks, the
  new package's `--install`), no longer Windows PowerShell with `-ExecutionPolicy Bypass` on a script.
- A failed installation or update puts the previous one back: app folder, stable copy, spec, settings, engine
  pointer, Antigravity plugin, sidecar and configuration, scheduled task; the daemon and the sidecar are asked to
  restart on the restored engine. Old engine folders are removed only once the run has succeeded, and never while an
  engine runs from them.
- Uninstalling the task: `NexusRelay.exe --remove-task` (was `scripts\RelayTask.ps1 -Remove`).
- Restarting the Codex app finds its package natively (the user's package folder, confirmed by the Windows package
  API) instead of starting a hidden PowerShell child.
- The commit guard installer (`scripts/install_commit_guard.ps1`) speaks English, and French when Windows is
  displayed in French; it was French only. The `lang` setting still chooses the language of the instructions deployed
  for the agents, which now includes the description of the Antigravity sidecar.

### Removed
- `scripts\update-native.ps1`, `RelayTask.ps1`, `Restart-Relay.ps1`, `Remove-RelayVersions.ps1`,
  `Resolve-NexusToken.ps1`, `Resolve-RelayPath.ps1` and `Configure-AntigravityRelay.ps1`, and their test fixtures:
  their logic is in the engine, covered by xUnit tests (fresh install, update while an engine runs, failure then
  restore at each step, scheduled task migration, restart ownership, token, cleanup of old versions).
- `NEXUS_RELAY_DOWNLOAD_ONLY`, a test variable of `get.ps1` that nothing used.
- `native/QUALIFICATION.md`, the October qualification report of the switch from Python, which no longer described
  the product (the Git history keeps it).
- The daemon supervisor no longer reads the retired `python` key of its spec: every native install writes `exe`.

### Fixed
- The installer no longer runs a stable engine that is not a Windows program image (a corrupted or truncated file):
  it is treated as unhealthy and replaced without being started, so Windows never shows its "16-bit application not
  supported" box, which blocked the update until clicked.
- Tests: no test can show a Windows error box. The test process sets the error mode (no critical-error, crash or
  missing-file box) when it loads, and every process it starts inherits it; a test checks it in a child process.
- Tests: the user-application lifetime test is deterministic under load and when two test runs overlap (another
  session's test gate). Its fixture was published to, and kept its state files in, one shared folder
  (`~\.nexus\build\nexusrelay\lifetime`, with a shared `obj`), so concurrent runs deleted and overwrote each other's
  files; it is now built and run in the test's own folder. It read the child's heartbeat without sharing the file with
  its writer, and judged survival after a fixed 1.6 s sleep: the beat is now read shared, and survival is a beat
  written after the caller's job was closed, each wait bounded by a 60 s deadline instead of a fixed time.

## [1.13.11] - 2026-11-07

### Fixed
- An update no longer fails while a Claude conversation waits for relay answers. 1.13.10 copied `engine.lock` into
  the stable folder `~\.nexus\nexus-relay\bin`; the `Stop` hook (`--await-replies`, up to 55 minutes) ran from that
  folder and held it, so the next update could not rename `bin` aside and stopped before the engine pointer, the task
  and the clients. The lock now protects only the versioned copies (`%LOCALAPPDATA%\NexusRelay\versions`): it is no
  longer copied into `bin`, and an engine started from `bin` never holds one. The waiting hook now runs the versioned
  copy named in `engine.txt`, as the Antigravity bridge does, so no long-running process uses `bin`. A process still
  running from `bin` (a hook of an earlier version) no longer blocks an update: Windows refuses to rename a folder
  with mapped files, so its content is moved aside file by file and the new files take their place; the old lock is
  removed once released.
- Tests: an update succeeds while a 1.13.10-like waiter runs from `bin` and holds its lock; the lock check of the
  cleanup is tested where Windows would accept the rename (lock held through a hard link); the unreadable-engine case
  uses a real running engine its owner cannot query (empty DACL); an engine run from `bin` holds nothing there; the
  `Stop` hook command runs the versioned copy through Git's bash.

## [1.13.10] - 2026-11-07

### Fixed
- The cleanup of old engine folders no longer relies on Windows refusing to rename the folder of a running program:
  Windows does rename it when the program keeps no file open in it (a single-file engine is only mapped).
  Each engine now holds the `engine.lock` the installer writes in its version folder for its whole life (path
  independent: junction, MSIX redirection, elevated view); a running `NexusRelay.exe` is located by
  `QueryFullProcessImageNameW`, which also reads an elevated process; when one cannot be read at all, no folder is
  removed. The rename aside stays as an extra check. The same rules now protect the `bin.old-*` copies of the stable
  folder, which were deleted outright. Tests: a held lock keeps a folder Windows would rename, an unreadable engine
  keeps everything, a running engine holds its lock and is found by its image path.

## [1.13.9] - 2026-11-07

### Fixed
- An update no longer deletes the files of an engine version a running process still uses. The installer removed old
  versions (`%LOCALAPPDATA%\NexusRelay\versions\<fingerprint>`) unless the process list named a running
  `NexusRelay.exe` inside them; an engine whose path it could not match (elevated, MSIX-redirected or reached through
  another spelling of the path) lost every DLL it had not loaded yet and answered each call with an empty error, as
  NexusFree did before 1.3.3. Now the file system decides: an old version is first renamed aside, which Windows refuses while
  an engine holds files in it, and only a folder renamed aside is deleted; one in use stays whole until a later
  install (`scripts/Remove-RelayVersions.ps1`, with a test that runs an engine from an old version during the cleanup).
- Installing or updating no longer leaves the retired `scripts/bridge.ps1` in Antigravity's `nexus-relay` plugin
  folder: the installer only adds files, so the 1.13.8 update kept the old PowerShell bridge next to `start.cmd`.

## [1.13.8] - 2026-11-07

### Changed
- Antigravity Marketplace plugin: its stdio bridge is now the engine itself (`NexusRelay.exe --bridge <url>`)
  instead of `scripts/bridge.ps1`, which compiled C# with Windows PowerShell at every start. Same relay: one line per
  JSON-RPC message, event streams split per event, `NEXUS_MCP_TOKEN` or `~\.nexus\mcp-token.txt`, the MCP session
  kept and closed at the end, a JSON-RPC error saying how to get NexusRelay when the daemon does not answer.
- The plugin starts it like NexusWindows, NexusBrowser and NexusAndroid: `cmd.exe /d /c
  ${PLUGIN_ROOT}\scripts\start.cmd <url>`. The launcher reads the engine every install records in
  `~\.nexus\nexus-relay\engine.txt` (`NexusRelay.exe --engine-pointer`: version and immutable versioned copy, never
  the stable `bin` folder, so a running Antigravity session no longer makes the next install fail with "bin in use"),
  and fails at once, with how to install or update, when no engine is recorded or it is older than 1.13.8 (an older
  engine ignored `--bridge` and started an unrelated server). It no longer assumes a global install path.
- Build outputs under `~\.nexus\build\nexusrelay` take the user profile from .NET: Dependabot restores on Linux, where
  `USERPROFILE` is unset and its NuGet updates silently failed.
- CodeQL runs from `.github/workflows/codeql.yml` (actions and C#, default queries, actions pinned by SHA) instead of
  the default setup, so OpenSSF Scorecard credits SAST on commits pushed without pull requests.
- Strict build: recommended .NET analyzers, code style enforced, warnings as errors (`.editorconfig` added). Number
  and date formats written for files and arguments use the invariant culture; SQLite clean-up results are discarded
  explicitly; fixed name lists are allocated once.
- Tests run on Microsoft.Testing.Platform only (xUnit v3), without the old VSTest packages.
- Tomlyn 2.10.2 and JsonSchema.Net 9.6.0 (tests).
- Release workflow on NexusSystem's shared workflows v1.6.54, pinned by commit SHA.
- CHANGELOG compare links for every released version.

## [1.13.7] - 2026-11-06

### Fixed
- Claude's relay session stays reachable between turns: the Claude app stops an idle Code session's process after
  its turn, which closed the inbox the daemon wakes Claude through (`relay_session.open: false`, peer requests left
  waiting). Turning Remote Control on for the relay session keeps its process running; checked on a real wake (a
  request posted as Codex was picked up and answered in 5 s). `status()` now gives that step as a `hint` while the
  session is closed, and the README and skill document it.

## [1.13.6] - 2026-11-06

### Fixed
- The Antigravity sidecar no longer keeps a CPU core busy while idle (about a third of a core, growing with the
  relay's history): its error watch re-read every past delivery (events, replies, topic messages) every second
  before checking their age. Finished deliveries (answered, stopped on error, older than 6 h) are now remembered
  and their claim files are no longer read; the loop health state is written every 30 s instead of every second.
  Measured on a copy of a real mailbox: 16.6 % of a core before, 0.6 % after.

### Changed
- The sidecar waits on a watcher of the messages folder instead of polling every second: a new message is
  delivered at once (about 50 ms instead of up to 1 s), and a 5 s step still runs retries, the error watch and
  restart checks.

## [1.13.5] - 2026-11-06

### Fixed
- A Codex chat whose last turn is longer than the 16 MB read window (images inlined as base64 by `view_image`) no
  longer stays "still working" forever: the capped window is judged like any other, so a final `task_complete` or
  `turn_aborted` ends the turn and the topic is woken again (it was deferred on every wake, even after a Codex
  restart, and never archived). The `since` check of the stall watchdog is kept.
- Antigravity's turn state reads the transcript's last step whole, whatever its size, instead of an 8 MB tail that
  cut a bigger step and left the turn unfinished.

## [1.13.4] - 2026-11-04

### Fixed
- `scripts/install_commit_guard.ps1` without `-Root` installs the guard in the Nexus workstation repositories under
  `C:\Nexus` (moved out of OneDrive) instead of the former OneDrive folders, and says to pass `-Root` when there is none.

## [1.13.3] - 2026-11-04

### Changed
- The nexus-relay skill names the NexusWindows tools without a space, like every other product name.

## [1.13.2] - 2026-11-04

### Changed
- The remaining user-visible labels are written NexusRelay: the Antigravity sidecar name, the scheduled task, wake
  and hook messages, the sender name of Claude's inbox, conversation titles, instructions, skills and README.
- The scheduled task "Nexus Relay" becomes "NexusRelay". The installer stops and removes the old task before it
  registers and starts the new one, so two supervisors never run; it also removes the old task when the shared
  "Nexus MCP Daemons" supervisor is present. `scripts\RelayTask.ps1 -Remove` removes both names (uninstall).
- New Antigravity projects are named NexusRelay; an existing "Nexus Relay" project is reused as is.

### Fixed
- Names kept for compatibility: the Codex project the user adds by hand is found as "NexusRelay" or "Nexus Relay";
  older Codex chats still prove their ownership with the wake text written before this version; commit guards
  installed with the old marker are still recognized and updated.

## [1.13.1] - 2026-11-04

### Changed
- Display name written NexusRelay, without a space, like the repository and the NexusSystem catalog.

## [1.13.0] - 2026-11-04

### Changed
- Automatic edit reservations are now short advisory leases (compare-and-swap concurrency). The edit hook no longer
  refuses an edit because another agent or conversation edited the same file earlier: the lease is held only while
  the edit tool runs, released when it returns (success or failure) whatever the Git state of the file, or at the end
  of the turn, with a 10-minute safety expiry instead of one hour. Lost updates stay impossible: Claude's `Edit`,
  Codex's `apply_patch` and Antigravity's replace all refuse a change whose expected text moved. While another edit
  tool runs on the same file, Claude gets a note naming it. On 2026-11-04 a single Edit of a file outside Git had
  locked it for the other Claude conversations for the rest of the turn (never "clean", so never released), and 29
  automatic reservations were active at once.
- Explicit `reserve()` / `release()` are kept for long multi-file work: an explicit exclusive reservation still
  refuses another holder's edit and commit, and is never refused by a running automatic lease. The commit guard
  ignores automatic leases. Skills and server instructions say single edits need no reservation.

### Removed
- The Git "clean file" checks that kept automatic reservations after the tool (dirty, untracked, ignored or non-Git
  files stayed reserved until the end of the turn).

## [1.12.12] - 2026-11-04

### Changed
- Product icon now shows the julienpiron.fr medallion on a transparent background at every size (no dark square), with the knot badge at the bottom of the ring kept whole.
- Same `.gitattributes` as every Nexus repository (2026-11-04): LF in Git and on disk, CRLF only for `.cmd`/`.bat`; files stored with CRLF were renormalized once.

## [1.12.11] - 2026-11-03

### Fixed
- Claude is woken again after a first topic: 1.12.10's self-clear (`clear_session`) stopped the relay session's
  process, so its inbox disappeared and every later wake waited with "relay session is not open". The clear
  instruction and its `.claude/settings.json` are removed (the installer deletes the deployed file); the relay session
  keeps every topic in turn and must never be cleared.

## [1.12.10] - 2026-11-03

### Changed
- One topic per Claude conversation: Claude's relay session clears itself (`clear_session` on itself) once every
  message of a wake is answered, so the next topic starts from an empty conversation instead of piling every topic
  into one growing context. The installer deploys `.claude/settings.json` to `~\.nexus\relay\NexusRelay`, allowing
  only that tool; the Claude app offers no way for an outside program to open a conversation per topic (no CDP, a
  conversation started from a peer message is refused). The README asks to pin the relay session.

## [1.12.9] - 2026-11-03

### Added
- Antigravity Marketplace bundle: `plugin/antigravity` passes `agy plugin validate --strict` with the Marketplace
  fields (English and French manifests), a README for the detail page and `scripts/bridge.ps1`. A Marketplace copy
  cannot name this PC's token (Antigravity expands only `${PLUGIN_ROOT}`), so the bridge relays Antigravity's stdio to
  the local daemon on `127.0.0.1:16181` with the token of `%USERPROFILE%\.nexus\mcp-token.txt` and answers with how
  to install NexusRelay when the daemon is missing. `install.ps1` keeps writing its own declaration and the sidecar.
- The Antigravity skill says how to install NexusRelay from the latest release (`get.ps1` checked against
  `SHA256SUMS.txt`) when its tools do not answer.

## [1.12.8] - 2026-11-03

### Fixed
- Maps of a packaged app are keyed by its package version: Codex's executable reports Chromium's file version
  (156.0.8078.79), so a Codex update that kept the same Chromium never triggered a new survey.

## [1.12.7] - 2026-11-03

### Fixed
- An interrupted map survey no longer leaves Codex or Antigravity on the page it opened (Settings): it closes its own
  menus and returns to the starting view, as long as the view is still one the survey itself reached. A view the user
  changed is left alone, and an emergency stop still sends no further gesture.

## [1.12.6] - 2026-11-03

### Fixed
- Codex is no longer seen as busy forever, which deferred its map survey and app restarts: subagent threads (also
  those without a spawn edge) and headless `codex exec` runs, whose journals Codex deletes while their rows stay, no
  longer count as open app chats. An open app chat without a readable journal still reads as unknown.

## [1.12.5] - 2026-11-03

### Fixed
- A map survey of an idle Antigravity is no longer deferred forever: empty zero-size listboxes that Antigravity 2.28
  keeps in its page (typeahead menus) no longer count as an open menu.

## [1.12.4] - 2026-11-03

### Fixed
- Align the Claude, Codex and Antigravity skills, shared MCP instructions, tool descriptions and hosting documentation
  with the default equal-peer `host: all` mode. Initiation is restricted only for an explicitly configured single host;
  received requests and review results address the requesting peer rather than assuming Claude initiated them.

## [1.12.3] - 2026-11-03

### Fixed
- A map surveyed with no conversation open reports conversation landmarks (Antigravity archive, options, composer,
  send; Codex archive) as unverifiable instead of gone; Antigravity's open conversation is detected from its
  conversation view or sidebar rows.

## [1.12.2] - 2026-11-03

### Changed
- The three AI apps are equal by default: the engine's default host is `all`, and the installer picks `all` when it finds
  two AI apps or more (a single AI app keeps itself as host). Before, an install without a `host` setting made the
  first AI app found the only one allowed to open exchanges, so reinstalling from a release silently replaced a
  three-way setup with `claude`. An explicit `host` stays as set.

## [1.12.1] - 2026-11-03

### Fixed
- Release archives now ship the Claude relay session template (`claude-projet/CLAUDE.md`, English and French): installing 1.11.4 to 1.12.0 from a release stopped on the missing file.

## [1.12.0] - 2026-11-03

### Added
- Claude is woken under host `all`: a peer request to Claude is written to the local inbox of Claude's relay session (a Claude desktop Code session in `~\.nexus\relay\NexusRelay`, in Auto mode) through Claude Code's documented per-session named pipe, with the peer key the session publishes in `~\.claude\sessions`. `status()` shows whether that session is open; `track(id)` follows the wake like Codex's and Antigravity's.

### Changed
- A reply addressed to Claude still goes back to the Claude conversation that asked (its Stop hook), never to the relay session.

## [1.11.4] - 2026-11-02

### Added
- Dedicated Claude project folder (`claude-projet/CLAUDE.md`, English and French) deployed during installation alongside `codex-projet`.
- Symmetrical Trinity hosting: any AI (Claude, Codex, or Antigravity) can initiate an exchange when host is set to "all" (active in workstation settings; engine defaults to claude).

### Fixed
- Claude automatic rewake hook (`HostRewake.cs`) was exiting immediately when `host` was set to `"all"`.
- Installer concurrency: prevented cleaning up `bin.new-*` temporary folders of concurrently running installer processes.
- Installer recovery: protected health check against execution exceptions on corrupted binaries, and validated health before skipping promotion or purging backups.

## [1.11.3] - 2026-11-02

### Fixed
- Installer: resilient directory swap with pre-flight binary health check, automatic rollback on promotion failure, and recovery from interrupted previous installations.


## [1.11.2] - 2026-11-02

### Fixed
- Codex wakes were deferred as "Codex quota 97% used" while Codex LB still had half of its pooled quota: behind a
  local router the Codex journal only shows the upstream account that served the last turn. With a router configured,
  the relay now reads the router's pooled usage (`/api/usage/summary`) and falls back to the journal when it does not
  answer.

## [1.11.1] - 2026-11-02

### Fixed
- Edit guard: an automatic reservation ends with its holder's turn (Claude's `Stop`, the end of a Codex or
  Antigravity answer), even when the file is modified and not committed yet. It used to wait for a commit or its
  60-minute expiry: on 02/11 a finished Antigravity turn kept a script reserved and refused Claude's commit until
  Antigravity was asked through the relay to release it. During the turn the file stays protected as before.

## [1.11.0] - 2026-10-31

### Added
- Claude, the host, is woken instead of blocking on `wait_reply`: a Claude Code `Stop` hook run with `asyncRewake`
  (`NexusRelay.exe --await-replies claude`) reads the requests the conversation sent from its transcript, waits in the
  background for their answers and exits 2, so the desktop app wakes the conversation with them once its turn is over
  (checked in the Claude desktop app: woken 11 minutes after the turn ended). It wakes once everything is answered or
  closed, 10 minutes after a first answer when another is late, and after 55 minutes at most; a newer turn end of the
  same conversation takes the wait over. An answer the conversation already saw (its `wait_reply`, `inbox`, `read`
  or an earlier wake) wakes nobody; one listed by another conversation's inbox still wakes the one that asked. A
  request closed without an answer, or still unanswered at the deadline, is reported once.
- Reviews are checked against their files. `send` notes HEAD and the content hash of the attached and cited files
  (`repo`, otherwise found from the absolute paths of the request); `track`, `wait_reply` and the answer report
  `stale` when they changed before the answer. The file:line citations of a review answer are checked: a missing
  file or a line past the end gives `quality: "citation introuvable"` with `bad_citations`.
- Review rounds: `send(round=N)`, or « tour N » / « round N » at the head of a review; `summary(topic, round=N)`
  lists the points of round N's answers, each new or already raised in an earlier round.

## [1.10.12] - 2026-10-31

### Fixed
- `GET /mcp` (and any non-POST request) answers 405 Method Not Allowed, as Streamable HTTP expects from a server
  without a standalone SSE stream, instead of dropping the connection on an empty body.
- The installer removes older engine builds from `%LOCALAPPDATA%\NexusRelay\versions` once no process runs them
  (12 builds, about 400 MB, had piled up); the current one is kept.

## [1.10.11] - 2026-10-31

### Fixed
- Edit guard: an automatic reservation whose edits are over and whose files are all identical to HEAD (committed or
  reverted) no longer blocks another conversation; it is released on the spot. The release at commit relied on the
  repository's post-commit hook alone, and the product repositories cloned again on 30/10 had lost it, so committed
  files stayed reserved until the holder's turn ended (25 minutes on NexusBrowser).

## [1.10.10] - 2026-10-31

### Fixed
- Edit guard: a file ignored by its Git work tree is released at the end of the turn too, like a file outside Git.
  Claude's memory folder is a junction into an ignored folder of a repository, so 1.10.9 still kept its notes
  reserved for 60 minutes. A reservation mixing such files with tracked ones keeps only the tracked files.

## [1.10.9] - 2026-10-31

### Fixed
- Edit guard: a file outside any Git work tree (memory notes, scratch files) is released at the end of the
  conversation's turn. Its automatic reservation waited for a commit that can never come, so it blocked every other
  conversation for the full 60 minutes.

## [1.10.8] - 2026-10-31

### Fixed
- Codex: the global edit-guard hooks in `~/.codex/hooks.json` are removed again once the nexus-relay plugin carries
  them. The installer looked for the cached plugin only under `cache/<catalog>/nexus-relay/local`, while Codex caches
  a catalog plugin under its version folder, so the guard ran twice (global and plugin).

### Changed
- Release: a push whose version is not released yet becomes the stable release right after its development build
  (same files); "Run workflow" still promotes by hand.

### Security
- CodeQL (GitHub default setup) and the OpenSSF Scorecard workflow run again on every push to main.

## [1.10.7] - 2026-10-31

### Changed
- Code quality pass on the CodeQL findings, behaviour unchanged. Unbraced one-line `if`/`foreach` bodies now carry
  braces, redundant null checks are gone, and disposable objects (HTTP requests, the TCP listener, process lists, test
  streams) are released by `using`.
- Catch blocks name the exceptions they expect (file, JSON, process, socket errors); the deliberate boundaries that keep
  the daemon, the sidecar, the hooks and the tool calls alive still catch everything and say why in a comment.
- `Path.Join` replaces `Path.Combine` wherever no later segment can be rooted; `Path.Combine` stays where a rooted
  segment must win (user paths, `PATH` lookup, plugin cache keys, unvalidated message or conversation IDs).
- Win32 and SQLite calls use source-generated `LibraryImport`; `EnumWindows` (managed callback) and
  `Process32FirstW`/`Process32NextW` (`ByValTStr` struct) stay `DllImport`.
- Loops filter and project with LINQ `Where`/`Select` instead of `if`/`continue` inside the body.

## [1.10.6] - 2026-10-31

### Added
- Development builds on every push to `main`: the release workflow checks one version everywhere, runs `test.ps1`,
  builds both archives and both MCP bundles, and the shared NexusSystem workflow `product-dev-release` attests them and
  publishes the pre-release `vX.Y.Z-dev.N` (NexusSystem's development channel; `releases/latest` never points to it).
  The one-line install is then run from that build on a clean runner.
- `get.ps1` honours `NEXUS_RELEASE_TAG`: it installs that release (`vX.Y.Z` or a development build `vX.Y.Z-dev.N`)
  instead of the latest, checked against its own `SHA256SUMS.txt` like any other.

### Changed
- A stable release is now the promotion of a tested development build: "Run workflow" runs the shared NexusSystem
  workflow `product-promote`, which publishes the same files, unchanged, as `vX.Y.Z` with this changelog's section.
  Pushing a `vX.Y.Z` tag no longer releases anything.
- The MCP registry step stays in this repository, after the promotion, because `server.json` lists two bundles (x64
  and Arm64); both are checked against the release's attested `SHA256SUMS.txt`. The `registry` input re-sends a stable
  release's `server.json` alone.

## [1.10.5] - 2026-10-31

### Changed
- `plugin/` is now the single source of the nexus-relay plugin for all three AI apps: Claude Code
  (`.claude-plugin`, `.mcp.json`, `hooks/hooks.json`, `skills/`), Codex (`.codex-plugin` with its edit hooks,
  `.codex-mcp.json`, `codex/skills/`) and Antigravity (`antigravity/`, deployed by `install.ps1`, French copy in
  `fr/plugin/antigravity`). The skill stays specific to each app because each one is woken differently. The former
  `claude/`, `codex/` and `plugins/` copies and the unused French Claude/Codex copies are removed; the NexusSystem
  catalog points to `plugin/` at the release tag. Release archives ship only the Antigravity part.

### Fixed
- Claude Code now receives the host skill (open exchanges, review rounds) instead of Antigravity's answering skill,
  which the NexusSystem catalog had been publishing for Claude.

## [1.10.4] - 2026-10-30

### Fixed
- The bootstrap gives Windows PowerShell 5.1's own standard modules priority when launched by PowerShell 7. Inherited
  Core-only modules could prevent SHA-256 verification, archive extraction and unblocking downloaded files. User module
  paths are preserved, and a child-process regression checks the real bootstrap against a .NET-computed checksum.

## [1.10.3] - 2026-10-30

### Fixed
- Shared-token initialization uses a stable sibling `.lock` file. Concurrent installers still create one token, while
  daemons and clients can read an existing token throughout another installation. The lock file is kept, never deleted.
- A regression test holds an existing token open for reading while an installer resolves it; this failed when the
  installer locked the token itself.

## [1.10.2] - 2026-10-30

### Fixed
- Card survey tests wait for the fixture document and its event handlers. Chrome can expose its page target while the
  initial empty document still reports `readyState=complete`, which intermittently failed before the draft was filled.
- One token for every Nexus daemon (common rule): the installer uses `NEXUS_MCP_TOKEN_FILE` (or the `token_file`
  setting), otherwise `%USERPROFILE%\.nexus\mcp-token.txt`; an existing token is reused, never regenerated, and a missing
  one is initialized under an exclusive file handle, so another Nexus installer run before or at the same time ends
  with the same token. The user variable `NEXUS_MCP_TOKEN` is aligned on that file and the daemon reads it. Before, a fresh install
  created a relay-only `~\.nexus\nexus-relay\token.txt`, and the daemons of other products answered 401 with the
  token of the variable; that older token is carried over into the shared file when it does not exist yet.
- Concurrent token initialization keeps one exclusive handle through reading and writing. A second installer can no
  longer delete a token that another installer has just created after the first existence check.

### Removed
- The obsolete `python` alias in the daemon supervisor spec; native supervisors read `exe` directly.

## [1.10.1] - 2026-10-30

### Changed
- Install instructions follow the common Nexus method: the NexusSystem app of the Microsoft Store first, otherwise the
  release files (`get.ps1`, checked against `SHA256SUMS.txt` and its attestation) run with `-File`. No more
  `irm | iex`: running downloaded code in memory is flagged by Microsoft Defender. A test forbids these forms in the
  repository.

## [1.10.0] - 2026-10-30

### Changed
- The native C# (.NET 10) engine is now the only engine: daemon, Antigravity sidecar, edit guard, host notice and
  commit guard. `install.ps1` no longer needs uv nor Python: it builds `native/` with `dotnet publish` (build outputs
  under `~\.nexus\build\nexusrelay`) or takes the self-contained engine of a release (`bin\`), installs it as an
  immutable version, and declares the daemon with the `exe` key of its supervisor spec (`python` repeats it for workstation supervisors that predate `exe`). A stable path
  `~\.nexus\nexus-relay\bin\NexusRelay.exe` (a copy of the version in service, swapped by rename so a running guard is never broken) is what hooks call.
- Settings, client and hook registration are engine commands: `NexusRelay.exe --settings`, `--register-clients`,
  `--register-hooks` (same files, same backups, same Codex trust preservation as before). A Codex plugin that still
  carries a retired Python guard no longer counts as carrying it: the native global guard is declared instead.
- Commit guard hooks (`scripts/pre-commit`, `post-commit`) call the engine by its stable path, without PowerShell.
- The one-line installer (`get.ps1`) downloads `NexusRelay-win-x64.zip` or `NexusRelay-win-arm64.zip` from the latest
  release, checks it against `SHA256SUMS.txt`, and installs it: no uv, no Python, no .NET to install.
- Tests are xUnit v3 (`dotnet test`, Microsoft.Testing.Platform): the former native self-tests, the black-box tests of
  the engine, and the Python-oracle contracts (tool schemas, UI maps, card semantics) replayed from recorded goldens.
  `test.ps1` and the CI run the same `dotnet test`. CodeQL analyses C#; Dependabot follows NuGet.

### Added
- Release workflow (`release.yml`, on a `vX.Y.Z` tag): self-contained win-x64 and win-arm64 archives, MCP bundles
  (`.mcpb`), `get.ps1`, `SHA256SUMS.txt` and their Sigstore build provenance; `server.json` (MCPB packages) published to
  the official MCP registry by GitHub OIDC; the one-line install is then run from the published release.

### Fixed
- `GET /api/update` answers the update state again (the native engine only served `/api/status`).
- Commit guard: staged file names are read from Git as UTF-8, so a reserved file with an accented name is refused.

### Removed
- The Python package (`src/nexus_relay`, `pyproject.toml`, `uv.lock`), its scripts (`register_clients.py`,
  `register_hooks.py`, `invoke-hook.ps1`), its Atheris fuzzing and benchmark, the Linux startup job, and the
  `--selftest` / `--runtime-selftest` options (now xUnit tests). PyPI and Glama publishing stay retired; earlier PyPI
  packages stay available.

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

[Unreleased]: https://github.com/pironjulien/NexusRelay/compare/v1.15.1...HEAD
[1.15.1]: https://github.com/pironjulien/NexusRelay/compare/v1.15.0...v1.15.1
[1.15.0]: https://github.com/pironjulien/NexusRelay/compare/v1.14.1...v1.15.0
[1.14.1]: https://github.com/pironjulien/NexusRelay/compare/v1.14.0...v1.14.1
[1.14.0]: https://github.com/pironjulien/NexusRelay/compare/v1.13.11...v1.14.0
[1.13.11]: https://github.com/pironjulien/NexusRelay/compare/v1.13.10...v1.13.11
[1.13.10]: https://github.com/pironjulien/NexusRelay/compare/v1.13.9...v1.13.10
[1.13.9]: https://github.com/pironjulien/NexusRelay/compare/v1.13.8...v1.13.9
[1.13.8]: https://github.com/pironjulien/NexusRelay/compare/v1.13.7...v1.13.8
[1.13.7]: https://github.com/pironjulien/NexusRelay/compare/v1.13.6...v1.13.7
[1.13.6]: https://github.com/pironjulien/NexusRelay/compare/v1.13.5...v1.13.6
[1.13.5]: https://github.com/pironjulien/NexusRelay/compare/v1.13.4...v1.13.5
[1.13.4]: https://github.com/pironjulien/NexusRelay/compare/v1.13.3...v1.13.4
[1.13.3]: https://github.com/pironjulien/NexusRelay/compare/v1.13.2...v1.13.3
[1.13.2]: https://github.com/pironjulien/NexusRelay/compare/v1.13.1...v1.13.2
[1.13.1]: https://github.com/pironjulien/NexusRelay/compare/v1.13.0...v1.13.1
[1.13.0]: https://github.com/pironjulien/NexusRelay/compare/v1.12.12...v1.13.0
[1.12.12]: https://github.com/pironjulien/NexusRelay/compare/v1.12.11...v1.12.12
[1.12.11]: https://github.com/pironjulien/NexusRelay/compare/v1.12.10...v1.12.11
[1.12.10]: https://github.com/pironjulien/NexusRelay/compare/v1.12.9...v1.12.10
[1.12.9]: https://github.com/pironjulien/NexusRelay/compare/v1.12.8...v1.12.9
[1.12.8]: https://github.com/pironjulien/NexusRelay/compare/v1.12.7...v1.12.8
[1.12.7]: https://github.com/pironjulien/NexusRelay/compare/v1.12.6...v1.12.7
[1.12.6]: https://github.com/pironjulien/NexusRelay/compare/v1.12.5...v1.12.6
[1.12.5]: https://github.com/pironjulien/NexusRelay/compare/v1.12.4...v1.12.5
[1.12.4]: https://github.com/pironjulien/NexusRelay/compare/v1.12.3...v1.12.4
[1.12.3]: https://github.com/pironjulien/NexusRelay/compare/v1.12.2...v1.12.3
[1.12.2]: https://github.com/pironjulien/NexusRelay/compare/v1.12.1...v1.12.2
[1.12.1]: https://github.com/pironjulien/NexusRelay/compare/v1.12.0...v1.12.1
[1.12.0]: https://github.com/pironjulien/NexusRelay/compare/v1.11.4...v1.12.0
[1.11.4]: https://github.com/pironjulien/NexusRelay/compare/v1.11.3...v1.11.4
[1.11.3]: https://github.com/pironjulien/NexusRelay/compare/v1.11.1...v1.11.3
[1.11.1]: https://github.com/pironjulien/NexusRelay/compare/v1.11.0...v1.11.1
[1.11.0]: https://github.com/pironjulien/NexusRelay/compare/v1.10.12...v1.11.0
[1.10.12]: https://github.com/pironjulien/NexusRelay/compare/v1.10.11...v1.10.12
[1.10.11]: https://github.com/pironjulien/NexusRelay/compare/v1.10.10...v1.10.11
[1.10.10]: https://github.com/pironjulien/NexusRelay/compare/v1.10.9...v1.10.10
[1.10.9]: https://github.com/pironjulien/NexusRelay/compare/v1.10.8...v1.10.9
[1.10.8]: https://github.com/pironjulien/NexusRelay/compare/v1.10.7...v1.10.8
[1.10.7]: https://github.com/pironjulien/NexusRelay/compare/v1.10.6...v1.10.7
[1.10.6]: https://github.com/pironjulien/NexusRelay/compare/v1.10.5...v1.10.6
[1.10.5]: https://github.com/pironjulien/NexusRelay/compare/v1.10.4...v1.10.5
[1.10.4]: https://github.com/pironjulien/NexusRelay/compare/v1.10.3...v1.10.4
[1.10.3]: https://github.com/pironjulien/NexusRelay/compare/v1.10.2...v1.10.3
[1.10.2]: https://github.com/pironjulien/NexusRelay/compare/v1.10.1...v1.10.2
[1.10.1]: https://github.com/pironjulien/NexusRelay/compare/v1.10.0...v1.10.1
[1.10.0]: https://github.com/pironjulien/NexusRelay/compare/v1.9.0...v1.10.0
[1.9.0]: https://github.com/pironjulien/NexusRelay/compare/v1.8.7...v1.9.0
[1.8.7]: https://github.com/pironjulien/NexusRelay/compare/v1.8.6...v1.8.7
[1.8.6]: https://github.com/pironjulien/NexusRelay/compare/v1.8.5...v1.8.6
[1.8.5]: https://github.com/pironjulien/NexusRelay/compare/v1.8.4...v1.8.5
[1.8.4]: https://github.com/pironjulien/NexusRelay/compare/v1.8.3...v1.8.4
[1.8.3]: https://github.com/pironjulien/NexusRelay/compare/v1.8.2...v1.8.3
[1.8.2]: https://github.com/pironjulien/NexusRelay/compare/v1.8.1...v1.8.2
[1.8.1]: https://github.com/pironjulien/NexusRelay/compare/v1.8.0...v1.8.1
[1.8.0]: https://github.com/pironjulien/NexusRelay/compare/v1.7.0...v1.8.0
[1.7.0]: https://github.com/pironjulien/NexusRelay/compare/v1.6.2...v1.7.0
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
