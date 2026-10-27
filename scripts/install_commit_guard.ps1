# Branche la garde de commit Nexus Relay dans chaque dépôt Git des pôles de Documents : scripts/pre-commit (refus d'un
# fichier réservé par un autre agent) et scripts/post-commit (libération des réservations automatiques commitées).
# Un hook étranger déjà présent est laissé tel quel et signalé ; relancer après la création d'un dépôt.
# Usage : pwsh -NoProfile -File scripts/install_commit_guard.ps1 [-Root <dossier>…] [-Remove]
param([string[]]$Root, [switch]$Remove)
$ErrorActionPreference = 'Stop'

$marker = 'Garde de commit Nexus Relay'
# fins de ligne LF : avec core.autocrlf, la copie du dépôt peut être en CRLF, que sh lit mal (« /bin/sh\r »)
$hooks = [ordered]@{}
foreach ($name in 'pre-commit', 'post-commit') {
    $hooks[$name] = (Get-Content -Raw -Encoding utf8 (Join-Path $PSScriptRoot $name)) -replace "`r`n", "`n"
}
if (-not $Root) {
    $documents = Join-Path $env:OneDrive 'Documents'
    $Root = 'NexusAscent', 'NexusHarness', 'NexusInfra' | ForEach-Object { Join-Path $documents $_ } | Where-Object { Test-Path $_ }
}
$repos = foreach ($folder in $Root) {
    Get-ChildItem $folder -Directory -Recurse -Depth 3 -Filter '.git' -Force -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName 'HEAD') } | ForEach-Object { $_.Parent.FullName }
    if (Test-Path (Join-Path $folder '.git\HEAD')) { $folder }
}
foreach ($repo in $repos | Sort-Object -Unique) {
    $custom = git -C $repo config --local core.hooksPath
    $folder = if ($custom) { Join-Path $repo $custom } else { Join-Path $repo '.git\hooks' }
    $done = foreach ($name in $hooks.Keys) {
        $target = Join-Path $folder $name
        $ours = (Test-Path $target) -and ((Get-Content -Raw $target) -match $marker)
        if ((Test-Path $target) -and -not $ours) { Write-Warning "$repo : $name étranger laissé en place"; continue }
        if ($Remove) {
            if ($ours) { Remove-Item $target; $name }
            continue
        }
        New-Item -ItemType Directory -Force $folder | Out-Null
        [IO.File]::WriteAllText($target, $hooks[$name], [Text.UTF8Encoding]::new($false))
        $name
    }
    if ($done) { "$(if ($Remove) { 'retirée' } else { 'branchée' }) ($($done -join ', ')) : $repo" }
}
