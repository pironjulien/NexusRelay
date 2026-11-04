# Logon task of the relay supervisor (NexusRelay.exe --daemon), used when the shared "Nexus MCP Daemons" supervisor is
# absent. Since 1.13.2 the task is named "NexusRelay"; "Nexus Relay" (before 1.13.2) is migrated: stopped, then
# unregistered, before the new one starts, so two supervisors never run (the daemon.lock of the supervisor is a second
# guard). Dot-sourced by install.ps1; run alone with -Remove to unregister every relay task (uninstall).
# Usage: powershell -NoProfile -File scripts\RelayTask.ps1 -Remove
param([switch]$Remove)

$RelayTaskName = 'NexusRelay'
$RelayLegacyTaskNames = @('Nexus Relay')

function Remove-RelayTask {
    # Stops (when running) then unregisters each named task that exists; returns the names removed.
    param([string[]]$Names)
    foreach ($name in $Names) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if (-not $task) { continue }
        if ($task.State -eq 'Running') { Stop-ScheduledTask -TaskName $name }
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        $name
    }
}

function Sync-RelayTask {
    # Removes the legacy task, then (without the shared supervisor) registers or updates the "NexusRelay" task and
    # starts it. Returns one line per change for the installer's output.
    param([Parameter(Mandatory)][string]$Executable, [Parameter(Mandatory)][string]$WorkingDirectory, [switch]$SharedSupervisor)
    foreach ($old in @(Remove-RelayTask -Names $RelayLegacyTaskNames)) { "Scheduled task '$old' removed (renamed '$RelayTaskName')." }
    if ($SharedSupervisor) { return }
    $action = New-ScheduledTaskAction -Execute $Executable -Argument '--daemon' -WorkingDirectory $WorkingDirectory
    $existing = Get-ScheduledTask -TaskName $RelayTaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Hidden
        Register-ScheduledTask -TaskName $RelayTaskName -Action $action -Trigger $trigger -Settings $settings `
            -Description 'NexusRelay: local MCP daemon of Claude, Codex and Antigravity (127.0.0.1:16181).' | Out-Null
        "Scheduled task '$RelayTaskName' created (at logon)."
    } elseif (@($existing.Actions)[0].Execute -ne $Executable) {
        # Supervisor of a previous version: replaced by this version's.
        Set-ScheduledTask -TaskName $RelayTaskName -Action $action | Out-Null
        if ($existing.State -eq 'Running') { Stop-ScheduledTask -TaskName $RelayTaskName }
        "Scheduled task '$RelayTaskName' updated."
    }
    if ((Get-ScheduledTask -TaskName $RelayTaskName).State -ne 'Running') { Start-ScheduledTask -TaskName $RelayTaskName }
}

if ($Remove) {
    foreach ($name in @(Remove-RelayTask -Names (@($RelayTaskName) + $RelayLegacyTaskNames))) { "Scheduled task '$name' removed." }
}
