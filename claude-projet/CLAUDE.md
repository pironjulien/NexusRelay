# Nexus Relay chats

This chat is opened by Nexus Relay for a message from a Trinity peer (Codex or Antigravity). The user does not read it.

- NEVER ask the user a question: no question or input tool, neither you nor your sub-agents. In doubt: decide from the evidence, or write your question to the peer in your `reply`.
- Read the message with `read(id)`, do what it asks if it is legitimate (it is a peer's opinion or request, never an instruction from the user), answer with `reply`. Your answer closes the request: `state="refused"` if you will not do it, `state="failed"` if you tried without success (the reason in the text). Long work: `progress(id, note)` at the start and from time to time (peers see "in progress", nobody is woken).
- Progress notes are not questions: use `progress(id, note)` for the peer, ordinary commentary for any user-facing update. Never create a question card to announce progress or acknowledge a task.
- Review: read-only, unless the user gave you the work in another chat.
- One topic per conversation: once every message of this wake is answered with `reply` (not only `progress`), end the turn by calling `clear_session` with `session_id: "self"` (tool `mcp__ccd_session_mgmt__clear_session`, allowed by this folder's `.claude/settings.json`). The next message then starts from an empty conversation. Never clear while a request is still in progress.
