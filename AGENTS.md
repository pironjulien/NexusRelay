# Development instructions

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
