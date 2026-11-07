# Full offline check of NexusRelay (Nexus convention: test.ps1 at the root, exit code 0 when everything passes).
# xUnit v3 on Microsoft.Testing.Platform (global.json); builds and test files under ~\.nexus\build\nexusrelay.
# Nexus pre-push gate: on a Nexus workstation the run goes through ~\.nexus\git\Invoke-NexusTest.ps1 (NexusInfra), which
# records the tested tree when it passes; the pre-push hook refuses a pushed tree that has not passed this script.
if (-not $env:NEXUS_TEST_EMPREINTE -and (Test-Path -LiteralPath "$HOME\.nexus\git\Invoke-NexusTest.ps1")) { & "$HOME\.nexus\git\Invoke-NexusTest.ps1" -Script $PSCommandPath -Parametres $PSBoundParameters -Arguments $args; exit $LASTEXITCODE }
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try { dotnet test --solution NexusRelay.slnx -c Release; exit $LASTEXITCODE } finally { Pop-Location }
