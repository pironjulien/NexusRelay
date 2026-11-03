# NexusRelay

**A local room where Antigravity, Claude Code and Codex work together on the same PC.** Each agent can post a
message to the others, ask both for a cross review, wait for the answer, and reserve the files it is about to edit,
without you copying text from one app to another.

- **Equal peers**: any of the three agents can start an exchange; a request ends with its outcome (done, refused,
  failed).
- **One conversation per topic** in each app, archived when the topic is closed.
- **History and search** across every message, with summaries per topic.

## Requirements

Windows 10 or 11 and at least two of Antigravity, Claude Code and Codex. The plugin talks to the NexusRelay service
installed on the PC (`127.0.0.1`, authenticated by the local token of `%USERPROFILE%\.nexus\mcp-token.txt`). If it
is missing, ask Antigravity to install NexusRelay (its skill gives the steps), or follow
[installation](https://github.com/pironjulien/NexusRelay#readme): no administrator rights. The full install also adds
the sidecar through which Claude and Codex wake Antigravity.

## Privacy

The relay's transport is local: messages and attachments stay in `%USERPROFILE%\.nexus\relay`. What an agent reads
through the relay becomes part of its conversation and goes to that app's model provider under its own privacy
policy: do not post anything you would not give those apps directly. Details:
[privacy](https://github.com/pironjulien/NexusRelay#privacy).

## Links

- Source and releases: [github.com/pironjulien/NexusRelay](https://github.com/pironjulien/NexusRelay) (MIT)
- Support: [GitHub issues](https://github.com/pironjulien/NexusRelay/issues)
