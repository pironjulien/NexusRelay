# Targeted, cooperative restart only. Never enumerate similar command lines for termination.
. (Join-Path $PSScriptRoot 'Resolve-RelayPath.ps1')
function ConvertFrom-RelayJson {
    param([Parameter(ValueFromPipeline=$true)][string]$Json)
    process {
        # PowerShell 7.5 otherwise turns ISO strings into DateTime, and a later [string] cast localizes the cutoff.
        # Preserve JSON strings verbatim; older PowerShell versions already behave this way.
        if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) { ConvertFrom-Json -InputObject $Json -DateKind String }
        else { ConvertFrom-Json -InputObject $Json }
    }
}
function Get-RelayListener {
    param([int]$Port)
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Where-Object OwningProcess -gt 0)
    if ($listeners.Count) { return $listeners }
    # NetTCPIP's CIM provider can omit loopback listeners in a process compartment. The Windows TCP table remains
    # observable via netstat; only its numeric local endpoint, remote port zero and owning PID are accepted.
    foreach ($line in @(& (Join-Path $env:WINDIR 'System32\netstat.exe') -ano -p tcp)) {
        $columns = @($line.Trim() -split '\s+')
        $owner = 0
        if ($columns.Count -eq 5 -and $columns[0] -eq 'TCP' -and $columns[1] -match ":$Port$" -and $columns[2] -match ':0$' -and [int]::TryParse($columns[4], [ref]$owner) -and $owner -gt 0) { [pscustomobject]@{ OwningProcess=$owner } }
    }
}
function Test-RelayPort {
    param([int]$Port)
    $client = [Net.Sockets.TcpClient]::new()
    try { $pending = $client.ConnectAsync('127.0.0.1', $Port); return $pending.Wait(1000) -and $client.Connected } catch { return $false } finally { $client.Dispose() }
}
function Get-RelayConnectedOwner {
    param([int]$Port)
    # Some Windows TCP providers omit LISTEN rows for this process compartment. Keep a real connection open and
    # identify its server half by the exact pair of ports; neither a stale TIME_WAIT nor a peer on another port fits.
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.ConnectAsync('127.0.0.1', $Port)
        if (-not $pending.Wait(1000) -or -not $client.Connected) { return }
        $peerPort = $client.Client.LocalEndPoint.Port
        $rows = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -RemoteAddress '127.0.0.1' -RemotePort $peerPort -ErrorAction SilentlyContinue | Where-Object OwningProcess -gt 0)
        if ($rows.Count) { return $rows }
        foreach ($line in @(& (Join-Path $env:WINDIR 'System32\netstat.exe') -ano -p tcp)) {
            $columns = @($line.Trim() -split '\s+'); $owner = 0
            if ($columns.Count -eq 5 -and $columns[0] -eq 'TCP' -and $columns[1] -eq "127.0.0.1:$Port" -and $columns[2] -eq "127.0.0.1:$peerPort" -and [int]::TryParse($columns[4], [ref]$owner) -and $owner -gt 0) { [pscustomobject]@{ OwningProcess=$owner } }
        }
    } catch { } finally { $client.Dispose() }
}
function Get-RelayRestartOwner {
    param([string]$Mailbox, [ValidateSet('daemon','sidecar')][string]$Name, [string]$ExpectedExecutable, [int]$Port = 0)
    $marker = Join-Path $Mailbox "$Name-restart.pid"
    $knownPid = 0
    if (-not (Test-Path -LiteralPath $marker) -or -not [int]::TryParse(([IO.File]::ReadAllText($marker)).Trim(), [ref]$knownPid)) {
        if ($Port -gt 0 -and (@(Get-RelayListener -Port $Port).Count -or (Test-RelayPort -Port $Port))) { throw "Relay ${Name}: port $Port exists without its ownership marker; no process was stopped." }
        return $null
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$knownPid" -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    $birth = ([datetime]$process.CreationDate).ToUniversalTime()
    if ((Get-Item -LiteralPath $marker).LastWriteTimeUtc -lt $birth) { throw "Relay ${Name}: PID marker predates this process; no restart requested." }
    $expected = Resolve-RelayPath -Path $ExpectedExecutable
    $same = (Resolve-RelayPath -Path ([string]$process.ExecutablePath)) -ieq $expected
    if (-not $same -and [IO.Path]::GetFileName($expected) -match '^pythonw?\.exe$') {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ParentProcessId)" -ErrorAction SilentlyContinue
        $same = $parent -and (Resolve-RelayPath -Path ([string]$parent.ExecutablePath)) -ieq $expected -and [IO.Path]::GetFileName([string]$process.ExecutablePath) -match '^pythonw?\.exe$' -and ([datetime]$parent.CreationDate).ToUniversalTime() -le $birth
    }
    if (-not $same) { throw "Relay ${Name}: executable identity does not match the installed runtime; no restart requested." }
    $line = [string]$process.CommandLine
    if ([IO.Path]::GetFileName($expected) -match '^pythonw?\.exe$' -and $line -notmatch "(?:^|\s)-m\s+nexus_relay\.$(if($Name -eq 'daemon'){'server'}else{'sidecar'})(?:\s|$)") { throw "Relay ${Name}: Python module identity does not match; no restart requested." }
    if ($Port -gt 0) {
        if ($line -notmatch "(?:^|\s)--http\s+$Port(?:\s|$)") { throw "Relay ${Name}: port argument does not match; no restart requested." }
        $listeners = @(Get-RelayListener -Port $Port)
        if (-not $listeners) { $listeners = @(Get-RelayConnectedOwner -Port $Port) }
        if (-not $listeners) {
            try {
                $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
                if ($health.service -eq 'nexus-relay' -and $health.runtime -eq 'native' -and [int]$health.pid -eq $knownPid) { $listeners = @([pscustomobject]@{ OwningProcess=$knownPid }) }
            } catch { }
        }
        if (-not $listeners -or @($listeners | Where-Object OwningProcess -ne $knownPid).Count) { throw "Relay ${Name}: listener ownership does not match (expected $knownPid, observed $(@($listeners | ForEach-Object OwningProcess) -join ',')); no restart requested." }
    }
    [pscustomobject]@{ Pid = $knownPid; Birth = $birth; Executable = [string]$process.ExecutablePath }
}

function Request-RelayRestart {
    param([string]$RequestPath, $Owner, [string]$Kind = 'code', [string]$Fingerprint = '')
    if (-not $Owner) { return $false }
    $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($Owner.Pid)" -ErrorAction SilentlyContinue
    if (-not $current) { return $false }
    if (([datetime]$current.CreationDate).ToUniversalTime() -ne $Owner.Birth -or [string]$current.ExecutablePath -ine $Owner.Executable) { throw 'Relay restart owner changed; no request written.' }
    [IO.File]::WriteAllText($RequestPath, "$Kind pid:$($Owner.Pid) $Fingerprint$([DateTime]::UtcNow.ToString('o'))", [Text.UTF8Encoding]::new($false))
    return $true
}
