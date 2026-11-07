# Branche la garde de commit NexusRelay dans chaque dépôt Git des pôles de C:\Nexus (ou de -Root) : scripts/pre-commit (refus d'un
# fichier réservé par un autre agent) et scripts/post-commit (libération des réservations automatiques commitées).
# Un hook étranger déjà présent est laissé tel quel et signalé ; relancer après la création d'un dépôt.
# Usage : pwsh -NoProfile -File scripts/install_commit_guard.ps1 [-Root <dossier>…] [-Remove]
param([string[]]$Root, [switch]$Remove)
$ErrorActionPreference = 'Stop'
# Messages : anglais par défaut, français quand Windows est affiché en français (règle des produits Nexus).
$frenchUi = [Globalization.CultureInfo]::CurrentUICulture.TwoLetterISOLanguageName -eq 'fr'
function Tr([string]$en, [string]$fr) { if ($frenchUi) { $fr } else { $en } }

$marker = 'Garde de commit Nexus ?Relay'  # regex : « Nexus Relay » des gardes posées avant 1.13.2
# fins de ligne LF : avec core.autocrlf, la copie du dépôt peut être en CRLF, que sh lit mal (« /bin/sh\r »)
$hooks = [ordered]@{}
foreach ($name in 'pre-commit', 'post-commit') {
    $hooks[$name] = (Get-Content -Raw -Encoding utf8 (Join-Path $PSScriptRoot $name)) -replace "`r`n", "`n"
}
if (-not $Root) {
    # Par défaut, les pôles du poste Nexus sous C:\Nexus (sortis de OneDrive le 04/11/2026) ; ailleurs, -Root.
    $nexus = Join-Path $env:SystemDrive 'Nexus'
    $Root = 'NexusAscent', 'NexusHarness', 'NexusInfra' | ForEach-Object { Join-Path $nexus $_ } | Where-Object { Test-Path $_ }
    if (-not $Root) { throw (Tr "No repository under $($nexus): give the folders with -Root." "Aucun dépôt sous $nexus : indiquer les dossiers avec -Root.") }
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
        if ((Test-Path $target) -and -not $ours) { Write-Warning (Tr "$($repo): foreign $name hook left in place" "$repo : $name étranger laissé en place"); continue }
        if ($Remove) {
            if ($ours) { Remove-Item $target; $name }
            continue
        }
        New-Item -ItemType Directory -Force $folder | Out-Null
        [IO.File]::WriteAllText($target, $hooks[$name], [Text.UTF8Encoding]::new($false))
        $name
    }
    if ($done) {
        $verb = if ($Remove) { Tr 'removed' 'retirée' } else { Tr 'installed' 'branchée' }
        Tr "$verb ($($done -join ', ')): $repo" "$verb ($($done -join ', ')) : $repo"
    }
}
