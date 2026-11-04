# Fixture: scripts\RelayTask.ps1 against an in-memory Task Scheduler (never the real one). Each mock refuses what would
# leave two supervisors: a start while another relay task runs, or an unregistration of a running task.
param([Parameter(Mandatory)][string]$Scenario)
$ErrorActionPreference = 'Stop'
$script:tasks = [ordered]@{}
$script:log = [Collections.Generic.List[string]]::new()
function New-Task([string]$exe, [string]$state) { [pscustomobject]@{ State = $state; Actions = @([pscustomobject]@{ Execute = $exe }) } }
function Get-ScheduledTask { param($TaskName, $ErrorAction) if ($script:tasks.Contains($TaskName)) { $script:tasks[$TaskName] } elseif ($ErrorAction -eq 'SilentlyContinue') { $null } else { throw "no task $TaskName" } }
function Stop-ScheduledTask { param($TaskName) $script:log.Add("stop $TaskName"); $script:tasks[$TaskName].State = 'Ready' }
function Start-ScheduledTask {
    param($TaskName)
    $running = @($script:tasks.Keys | Where-Object { $script:tasks[$_].State -eq 'Running' })
    if ($running.Count) { throw "two supervisors: $TaskName while $($running -join ', ') runs" }
    $script:log.Add("start $TaskName"); $script:tasks[$TaskName].State = 'Running'
}
function Unregister-ScheduledTask {
    param($TaskName, [switch]$Confirm)
    if ($script:tasks[$TaskName].State -eq 'Running') { throw "unregistered while running: $TaskName" }
    $script:log.Add("unregister $TaskName"); $script:tasks.Remove($TaskName)
}
function Register-ScheduledTask { param($TaskName, $Action, $Trigger, $Settings, $Description) $script:log.Add("register $TaskName"); $script:tasks[$TaskName] = New-Task $Action.Execute 'Ready' }
function Set-ScheduledTask { param($TaskName, $Action) $script:log.Add("set $TaskName"); $script:tasks[$TaskName].Actions = @([pscustomobject]@{ Execute = $Action.Execute }) }
function New-ScheduledTaskAction { param($Execute, $Argument, $WorkingDirectory) [pscustomobject]@{ Execute = $Execute } }
function New-ScheduledTaskTrigger { param([switch]$AtLogOn, $User) 'trigger' }
function New-ScheduledTaskSettingsSet { param([switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries, $ExecutionTimeLimit, $MultipleInstances, [switch]$Hidden) 'settings' }

$script = Join-Path $PSScriptRoot '..\..\scripts\RelayTask.ps1'
switch ($Scenario) {
    'fresh' { }
    'legacy' { $script:tasks['Nexus Relay'] = New-Task 'old.exe' 'Running' }
    'both' { $script:tasks['Nexus Relay'] = New-Task 'old.exe' 'Running'; $script:tasks['NexusRelay'] = New-Task 'new.exe' 'Ready' }
    'current' { $script:tasks['NexusRelay'] = New-Task 'new.exe' 'Running' }
    'upgrade' { $script:tasks['NexusRelay'] = New-Task 'old.exe' 'Running' }
    'shared' { $script:tasks['Nexus Relay'] = New-Task 'old.exe' 'Running' }
    'remove' { $script:tasks['Nexus Relay'] = New-Task 'old.exe' 'Ready'; $script:tasks['NexusRelay'] = New-Task 'new.exe' 'Running'; $script:tasks['Other'] = New-Task 'other.exe' 'Ready' }
    default { throw "unknown scenario $Scenario" }
}
$out = if ($Scenario -eq 'remove') { . $script -Remove } else {
    . $script
    Sync-RelayTask -Executable 'new.exe' -WorkingDirectory 'state' -SharedSupervisor:($Scenario -eq 'shared')
}
$states = [ordered]@{}
foreach ($name in $script:tasks.Keys) { $states[$name] = "$($script:tasks[$name].State) $(@($script:tasks[$name].Actions)[0].Execute)" }
[ordered]@{ log = @($script:log); tasks = $states; out = @($out) } | ConvertTo-Json -Depth 5 -Compress
