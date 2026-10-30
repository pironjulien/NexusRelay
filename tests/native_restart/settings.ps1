param([string]$InputPath)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '..\..\scripts\Restart-Relay.ps1')
$settings=Get-Content -LiteralPath $InputPath -Raw | ConvertFrom-RelayJson
@{ cutoff=[string]$settings.wake_since; label=[string]$settings.codex_project; nested=[string]$settings.env.timestamp } | ConvertTo-Json -Compress
