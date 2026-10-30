# Shared hook entry point. Runtime selection follows the installed product, never a second server.
param(
    [ValidateSet('claude', 'codex', 'antigravity')][string]$Agent = 'codex',
    [ValidateSet('begin', 'finish-tool', 'finish-session', 'notice', 'staged', 'release-committed')][string]$Phase = 'begin'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Resolve-RelayPath.ps1')
$OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$stateDirectory = Join-Path $env:USERPROFILE '.nexus\nexus-relay'
$settingsPath = Join-Path $stateDirectory 'settings.json'
$settings = if (Test-Path -LiteralPath $settingsPath) { Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json } else { $null }
$selectedRuntime = if ($settings -and $settings.PSObject.Properties['runtime']) { [string]$settings.runtime } else { 'python' }
$payload = if ($Phase -in @('staged', 'release-committed')) { $null } else { [Console]::In.ReadToEnd() }
[string[]]$hookArguments = @()
if ($selectedRuntime -eq 'native') {
    $specPath = Join-Path $env:USERPROFILE '.nexus\daemons\specs\nexus-relay.json'
    $spec = Get-Content -LiteralPath $specPath -Raw | ConvertFrom-Json
    $hookExecutable = [IO.Path]::GetFullPath([string]$spec.python)
    $declaredRoot = if ($settings.PSObject.Properties['native_root']) { [string]$settings.native_root } else { Join-Path $env:LOCALAPPDATA 'NexusRelay' }
    $localRoot = [IO.Path]::GetFullPath($env:LOCALAPPDATA).TrimEnd('\') + '\'
    $allowedRoot = [IO.Path]::GetFullPath($declaredRoot).TrimEnd('\') + '\'
    if (-not $allowedRoot.StartsWith($localRoot, [StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($declaredRoot.TrimEnd('\')) -ine 'NexusRelay') { throw 'Native Relay product root is outside the user local application data.' }
    if (-not $hookExecutable.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($hookExecutable) -ine 'NexusRelay.exe' -or -not (Test-Path -LiteralPath $hookExecutable -PathType Leaf)) {
        throw 'Native Relay hook executable is not an installed NexusRelay.exe.'
    }
    $hookArguments = switch ($Phase) {
        'notice' { @('--notice') }
        'staged' { @('--staged') }
        'release-committed' { @('--release-committed') }
        'begin' { @('--edit-guard', $Agent) }
        default { @('--edit-guard', $Agent, "--$Phase") }
    }
} else {
    if ($selectedRuntime -ne 'python') { throw 'Unknown installed Relay runtime.' }
    $hookExecutable = Join-Path $stateDirectory 'venv\Scripts\python.exe'
    $hookArguments = switch ($Phase) {
        'notice' { @('-I', '-B', '-m', 'nexus_relay.hook') }
        'staged' { @('-I', '-B', '-m', 'nexus_relay.reservations', '--staged') }
        'release-committed' { @('-I', '-B', '-m', 'nexus_relay.reservations', '--release-committed') }
        'begin' { @('-I', '-B', '-m', 'nexus_relay.edit_guard', $Agent) }
        default { @('-I', '-B', '-m', 'nexus_relay.edit_guard', $Agent, "--$Phase") }
    }
}
if ($null -eq $payload) { & $hookExecutable @hookArguments } else { $payload | & $hookExecutable @hookArguments }
exit $LASTEXITCODE
