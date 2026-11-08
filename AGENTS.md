# Development instructions

## Working with peers

These rules apply to Claude, Codex and Antigravity alike.

- Reach a peer through the relay only (`send`/`reply`). Never its CLI (`codex exec`…) or app-server, not even in the
  background, and never a "message to paste" for the user: both put the user's accounts at risk or make the user the
  relay.
- Each wake starts from an empty context: state the task (or "review only"), the repository path, the commit SHAs and
  the expected result. A woken peer never asks the user a question (no question box, no sub-agent): the user does not
  read relay chats. If a peer shows the user a question, fix the relay rather than working around it.
- A silent peer is restarted before giving up: `status()` / `track(id)`, then check its process and window; start the
  app if it is closed; if it is frozen (no redraw, input refused), close it cleanly (never kill the whole process tree)
  and start it again, then resend through the relay. Trigger: a first failed wake, or a ring not taken within 5 min.
  Hand back only after that attempt, saying what was tried.
- An agreement between agents fixes a plan, never the user's go-ahead. A trial that drives the user's apps (sending,
  picking a model, even a "mini-run") waits for the user's explicit approval and never starts during a pause the user
  decided. Close a debate with "plan only, nothing runs without the user's go-ahead".
- Cross review only when the user asks for one (or in the cases of the nexus-relay skill), never after every change.
  Both peers, independent opinions. Never trust a report ("100 % done"): read the diff, rerun the tests, prove each
  defect with a test that fails on the old code. Never ask a model to grade the other agents (it caused an
  oscillation loop).
- The three agents may edit each other's configuration (CLAUDE.md, GEMINI.md, AGENTS.md, scripts) when useful, and
  say so; flag a risky behaviour without turning it into a restrictive rule.

## Pitfalls

- Claude is woken through the local per-session inbox of Claude Code (named pipe, `~\.claude\sessions\<pid>.json` and
  peer key). The receiving session must run in Auto mode: in Bypass, Claude Code holds messages from a sender that does
  not declare Bypass, and `crossSessionInbound: accept` is not read from project settings (user, policy or flag only).
- The Claude desktop app stops a session's process after its first turn unless Remote Control is on for it
  (`set_remote_control`); `clear_session` also stops the process, and with it the inbox. Pin the relay session so a
  tidy-up does not archive or delete it.
- The Claude desktop app refuses a CDP port and offers no external way to create a session; a turn woken by a message
  cannot call `start_session` either. A reply to Claude goes back to the requesting conversation (Stop hook), never to
  the relay session.
- `host` defaults to `all` in the engine and the installer as soon as two agents are present; an installer that picked
  the first agent found when `host` was missing reset it three times.
- A topic is the project, never a sub-task: each topic keeps one conversation per peer.
- A bare YAML value containing `: ` in a SKILL.md front matter makes Antigravity reject the skill every 10 s; quote it.
- After a PC restart, Antigravity's sidecar is dead until Antigravity starts again: check with `selftest`.
