# Branche la garde de commit Nexus Relay (scripts/pre-commit) dans chaque dépôt Git des pôles de Documents.
# Un pre-commit étranger déjà présent est laissé tel quel et signalé ; relancer après la création d'un dépôt.
# Usage : pwsh -NoProfile -File scripts/install_commit_guard.ps1 [-Root <dossier>…] [-Remove]
param([string[]]$Root, [switch]$Remove)
$ErrorActionPreference = 'Stop'

$marker = 'Garde de commit Nexus Relay'
# fins de ligne LF : avec core.autocrlf, la copie du dépôt peut être en CRLF, que sh lit mal (« /bin/sh\r »)
$hook = (Get-Content -Raw -Encoding utf8 (Join-Path $PSScriptRoot 'pre-commit')) -replace "`r`n", "`n"
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
    $target = Join-Path $folder 'pre-commit'
    $ours = (Test-Path $target) -and ((Get-Content -Raw $target) -match $marker)
    if ((Test-Path $target) -and -not $ours) { Write-Warning "$repo : pre-commit étranger laissé en place"; continue }
    if ($Remove) {
        if ($ours) { Remove-Item $target; "retirée : $repo" }
        continue
    }
    New-Item -ItemType Directory -Force $folder | Out-Null
    [IO.File]::WriteAllText($target, $hook, [Text.UTF8Encoding]::new($false))
    "branchée : $repo"
}
