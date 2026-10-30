# Full offline check of NexusRelay (Nexus convention: test.ps1 at the root, exit code 0 when everything passes).
# xUnit v3 on Microsoft.Testing.Platform (global.json); builds and test files under ~\.nexus\build\nexusrelay.
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try { dotnet test --solution NexusRelay.slnx -c Release; exit $LASTEXITCODE } finally { Pop-Location }
