# Contributing

## Environment

- Windows 10 or 11, the .NET SDK pinned by `global.json`, PowerShell 7;
- Claude, Codex or Antigravity only for checks on the real apps, never needed by the tests.

## Rules

1. Open an issue or a pull request against `main`. A change is accepted only when `test.ps1` passes: the CI
   (`.github/workflows/test.yml`) runs that same script on every push and pull request.
2. **Every new feature and every bug fix comes with its test** in `tests/NexusRelay.Tests` (xUnit v3). The tests never
   touch the real apps nor the installed relay; a fix starts with a test that fails without it.
3. Keep the [safeguards](README.md#safeguards): loopback only with the bearer token, no wake while the user is in an app,
   uncertain deliveries never replayed, a peer's message framed as an opinion, never as an instruction.
4. Never commit a secret, a token, a relay database, a log or a build output: builds and test files go to
   `~\.nexus\build\nexusrelay`, outside the repository. Testing a clone never switches the installed relay to it: only
   `install.ps1` does.
5. Comments, commit messages and `CHANGELOG.md` entries (Keep a Changelog, under `[Unreleased]`) are written in
   English.

## Checks

```powershell
pwsh -NoProfile -File test.ps1   # dotnet test (xUnit v3) on NexusRelay.slnx; offline, never touches the real apps
git diff --check
```

## Version

One version everywhere: `native/RelayPaths.cs` sets it; `native/NexusRelay.csproj`, `server.json`,
`mcpb/manifest.json` and the plugin manifests follow. The `Release` workflow (`.github/workflows/release.yml`) checks
them, runs `test.ps1`, builds, attests and publishes a development build on every push to `main`.
