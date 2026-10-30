function Set-AntigravityRelayActivation {
    param($Config, [string]$ProjectId)
    $plugin = if ($Config.PSObject.Properties['plugins']) { $Config.plugins.PSObject.Properties['nexus-relay'] } else { $null }
    $sidecar = if ($Config.PSObject.Properties['sidecars']) { $Config.sidecars.PSObject.Properties['nexus-relay/relay'] } else { $null }
    $disabled = ($plugin -and $plugin.Value.enabled -eq $false) -or ($sidecar -and $sidecar.Value.enabled -eq $false)
    if ($disabled) { return [pscustomobject]@{ Config=$Config; Changed=$false; Disabled=$true } }
    $changed = $false
    if (-not $Config.PSObject.Properties['plugins']) { $Config | Add-Member plugins ([pscustomobject]@{}) }
    if (-not $plugin) { $Config.plugins | Add-Member -NotePropertyName 'nexus-relay' -NotePropertyValue ([pscustomobject]@{ enabled=$true }); $changed=$true }
    elseif ($plugin.Value.enabled -ne $true) { $plugin.Value | Add-Member enabled $true -Force; $changed=$true }
    if (-not $Config.PSObject.Properties['sidecars']) { $Config | Add-Member sidecars ([pscustomobject]@{}) }
    if (-not $sidecar) { $Config.sidecars | Add-Member -NotePropertyName 'nexus-relay/relay' -NotePropertyValue ([pscustomobject]@{ enabled=$true; projectId=$ProjectId }); $changed=$true }
    else {
        if ($sidecar.Value.enabled -ne $true) { $sidecar.Value | Add-Member enabled $true -Force; $changed=$true }
        if ($sidecar.Value.projectId -ne $ProjectId) { $sidecar.Value | Add-Member projectId $ProjectId -Force; $changed=$true }
    }
    return [pscustomobject]@{ Config=$Config; Changed=$changed; Disabled=$false }
}
