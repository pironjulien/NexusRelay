# Contrôle complet du projet hors ligne (convention Nexus : test.ps1 à la racine, code 0 si tout va bien).
# Environnement et caches hors du dépôt (~\.nexus\nexus-relay) préparés par install.ps1 ; rien n'est écrit dans le
# dépôt. Aucun appel à Antigravity : les tests utilisent une boîte temporaire, un faux agentapi et de faux journaux.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$state = Join-Path $HOME '.nexus\nexus-relay'
$cache = Join-Path $state 'cache'
$env:UV_PROJECT_ENVIRONMENT = Join-Path $state 'venv'
# Le venv du relais EN SERVICE (installation éditable vers ce dossier) ne sert qu'au dépôt installé. Une copie
# (worktree, clone, relecture d'un pair) a son propre venv : sinon `uv run` repointerait le relais en service vers
# elle (vu le 18/10 : une relecture a basculé le relais en service sur une branche de travail).
# Dépôt installé : celui vers lequel pointe l'installation éditable du venv en service (nexus_relay.pth → <dépôt>\src).
# CI GitHub (0.12.0) : aucun relais installé ; tout dépôt y est une copie.
$pth = Join-Path $state 'venv\Lib\site-packages\nexus_relay.pth'
$installed = if (Test-Path $pth) { Split-Path ((Get-Content $pth -TotalCount 1).Trim()) } else { '' }
$copy = (Resolve-Path $root).Path.TrimEnd('\') -ine $installed.TrimEnd('\')
if ($copy) {
    $bytes = [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant()))
    $key = -join ($bytes[0..3] | ForEach-Object { $_.ToString('x2') })
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $state "copies\$key\venv"
    $cache = Join-Path $state "copies\$key\cache"
}
$env:UV_LINK_MODE = 'copy'
if (-not $env:CI) { $env:UV_OFFLINE = '1' }  # hors ligne sur le poste ; la CI télécharge les paquets du uv.lock
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPYCACHEPREFIX = Join-Path $cache 'pycache'
$env:PYTHONUTF8 = '1'
$env:RUFF_CACHE_DIR = Join-Path $cache 'ruff'
$env:PYRIGHT_PYTHON_CACHE_DIR = Join-Path $cache 'pyright'
$env:PYRIGHT_PYTHON_IGNORE_WARNINGS = '1'  # pas de recherche de nouvelle version (réseau)
if ($copy) {
    uv sync --locked --project $root -q
    if ($LASTEXITCODE) { Write-Error 'uv sync de la copie : échec.' -ErrorAction Continue; exit 1 }
}
$pythonExe = Join-Path $env:UV_PROJECT_ENVIRONMENT 'Scripts\python.exe'
if (-not (Test-Path $pythonExe)) { throw 'Environnement absent : lancer install.ps1.' }
New-Item -ItemType Directory -Force $cache | Out-Null

function Invoke-Step([string]$name, [scriptblock]$step) {
    & $step
    if ($LASTEXITCODE) { Write-Error "$name : échec (code $LASTEXITCODE)." -ErrorAction Continue; exit $LASTEXITCODE }
}

Invoke-Step 'ruff check' { uv run --frozen --project $root ruff check $root }
Invoke-Step 'ruff format' { uv run --frozen --project $root ruff format --check $root }
Invoke-Step 'pyright' { uv run --frozen --project $root pyright --project $root --pythonpath $pythonExe }
# Dossier temporaire propre à CETTE exécution : deux contrôles simultanés ne se nettoient plus l'un l'autre.
$basetemp = Join-Path $cache "pytest-$PID"
Invoke-Step 'pytest' { uv run --frozen --project $root pytest $root -q -p no:cacheprovider --basetemp $basetemp }
Remove-Item -Recurse -Force $basetemp -ErrorAction SilentlyContinue

$stray = Get-ChildItem $root -Recurse -Force -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue
if ($stray) { Write-Error "__pycache__ dans le dépôt : $($stray.FullName -join ', ')" -ErrorAction Continue; exit 1 }
Write-Host 'NexusRelay : contrôle complet vert.'
exit 0
