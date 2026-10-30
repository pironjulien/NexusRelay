param([string]$InputPath)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '..\..\scripts\Configure-AntigravityRelay.ps1')
$config=Get-Content -LiteralPath $InputPath -Raw | ConvertFrom-Json
$result=Set-AntigravityRelayActivation -Config $config -ProjectId 'fixture-project'
$result | ConvertTo-Json -Depth 100 -Compress
