param([string]$Mailbox, [string]$Executable, [int]$Port, [switch]$Request, [switch]$ConnectionOnly)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\..\scripts\Restart-Relay.ps1')
if ($ConnectionOnly) { function Get-RelayListener { param([int]$Port) } }
try {
    $owner = Get-RelayRestartOwner -Mailbox $Mailbox -Name daemon -ExpectedExecutable $Executable -Port $Port
    if ($Request) { $null = Request-RelayRestart -RequestPath (Join-Path $Mailbox 'daemon-restart.request') -Owner $owner -Kind force }
    @{ ok=$true; pid=$owner.Pid } | ConvertTo-Json -Compress
} catch { @{ ok=$false; reason=$_.Exception.Message } | ConvertTo-Json -Compress }
