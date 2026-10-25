# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: **Security → Report a vulnerability** on this repository.
Do not open a public issue. You will get an answer within a few days.

## Scope

Nexus Relay runs on a single Windows PC and only listens on 127.0.0.1. Relevant reports include, for example:

- reaching the daemon without the bearer token, or from another machine;
- reading or attaching a file inside a configured vault, or outside what an agent explicitly attached;
- a message from one agent being executed by another as an instruction instead of being read as an opinion;
- the relay acting in Codex or Antigravity while the user is using the app, or overwriting the user's draft.

The Codex and Antigravity debugging ports give any local program control of those apps: that is a documented design
choice (local machine assumed trusted), not a vulnerability of the relay.
