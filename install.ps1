# Installe Nexus Relay sur ce PC (idempotent : à relancer sans risque après une mise à jour du dépôt).
#  - environnement Python hors du dépôt : ~\.nexus\nexus-relay\venv (caches dans ~\.nexus\nexus-relay\cache)
#  - boîte aux lettres : ~\.nexus\relay (hors de tout dossier synchronisé)
#  - réglages du poste : ~\.nexus\nexus-relay\settings.json (facultatif, hors dépôt), voir README :
#      token_file          fichier du jeton du démon (32 caractères au moins) ; sinon -TokenFile ou NEXUS_MCP_TOKEN_FILE
#      vaults              dossiers de secrets jamais joints à un message (NEXUS_RELAY_VAULTS)
#      codex_router        adresse de santé d'un routeur local devant Codex (NEXUS_RELAY_CODEX_ROUTER)
#      codex_router_start  commandes qui le lancent, essayées dans l'ordre (NEXUS_RELAY_CODEX_ROUTER_START)
#  - démon partagé (127.0.0.1:16181, jeton du poste) déclaré au superviseur des démons (tâche « Nexus MCP Daemons ») ;
#    chaque client le joint par URL avec son agent (?agent=claude|antigravity|codex)
#  - Claude Code : serveur MCP et skill par le plugin nexus-relay (dossier claude\), rien ici
#  - Antigravity : plugin ~\.gemini\config\plugins\nexus-relay (serveur MCP par URL, skill,
#    sidecar permanent « nexus-relay/relay ») et son activation dans ~\.gemini\config\config.json, dans le projet
#    Antigravity « Nexus Relay » (créé ici, dossier ~\.nexus\relay\agy-projet)
#  - Codex : dossier du projet « Nexus Relay » (~\.nexus\relay\codex-projet) ; le projet lui-même s'ajoute une fois
#    dans l'appli Codex (« Ajouter un projet »), voir README
#  - tests hors ligne (sauf -SkipTests)
#  - -Register (installation publique, get.ps1) : jeton créé s'il n'y en a aucun (~\.nexus\nexus-relay\token.txt, noté
#    dans settings.json), variable utilisateur NEXUS_MCP_TOKEN posée, serveur déclaré à Claude Code et Codex
#    (scripts\register_clients.py) ; NEXUS_PACK=1 (pack Nexus) : aucune question ni fenêtre, ligne NEXUS_PACK_RESULT
# Windows PowerShell 5.1 ou PowerShell 7.
# Usage : powershell -NoProfile -File install.ps1 [-SkipTests] [-TokenFile <chemin>] [-Register]
param([switch]$SkipTests, [string]$TokenFile, [switch]$Register)
$ErrorActionPreference = 'Stop'

$project = $PSScriptRoot
$state = Join-Path $HOME '.nexus\nexus-relay'
$venv = Join-Path $state 'venv'
$cache = Join-Path $state 'cache'
$pycache = Join-Path $cache 'pycache'
$backups = Join-Path $state 'backups'
$mailbox = Join-Path $HOME '.nexus\relay'
New-Item -ItemType Directory -Force $state, $cache, $backups, $mailbox | Out-Null

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { throw "uv introuvable : l'installer (https://docs.astral.sh/uv/) puis relancer." }
$env:UV_PROJECT_ENVIRONMENT = $venv
$env:UV_LINK_MODE = 'copy'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPYCACHEPREFIX = $pycache
$env:PYTHONUTF8 = '1'
$env:PYRIGHT_PYTHON_CACHE_DIR = Join-Path $cache 'pyright'

$systemPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    if (Test-Path $systemPython) { & $uv venv --python $systemPython $venv } else { & $uv venv --python 3.14 $venv }
}
& $uv sync --locked --project $project
if ($LASTEXITCODE) { throw 'uv sync a échoué.' }

$python = Join-Path $venv 'Scripts\python.exe'
# Sidecar sans aucune fenêtre : pythonw.exe (aucune console à créer, même si Antigravity n'en fournit pas ; Windows
# Terminal, terminal par défaut de ce PC, ignorerait -WindowStyle Hidden). Sa sortie reste capturée par Antigravity
# (vérifié : pythonw écrit dans les tubes hérités). Le démon MCP garde python.exe.
$pythonw = Join-Path $venv 'Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "pythonw.exe absent de $venv : environnement à recréer." }
$utf8 = [Text.UTF8Encoding]::new($false)
New-Item -ItemType Directory -Force (Join-Path $HOME '.nexus\relay\codex-projet') | Out-Null  # dossier du projet Codex du relais
# Consignes des chats du relais, lues par Codex dans ce dossier : jamais de question à l'utilisateur, qui ne les lit pas.
Copy-Item -Force (Join-Path $PSScriptRoot 'codex-projet\AGENTS.md') (Join-Path $HOME '.nexus\relay\codex-projet\AGENTS.md')
$stampTime = Get-Date -Format 'yyyyMMdd-HHmmss'

function Save-Backup([string]$path, [string]$name) {
    $backup = Join-Path $backups "$name-$stampTime$([IO.Path]::GetExtension($path))"
    Copy-Item -LiteralPath $path -Destination $backup
    Write-Host "  copie de sauvegarde : $backup"
}
function Write-IfChanged([string]$path, [string]$content) {
    if ((Test-Path $path) -and [IO.File]::ReadAllText($path) -ceq $content) { return $false }
    New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null
    [IO.File]::WriteAllText($path, $content, $utf8)
    return $true
}
function Copy-Tree([string]$source, [string]$target) {
    $copied = 0
    foreach ($file in Get-ChildItem $source -File -Recurse) {
        $dest = Join-Path $target $file.FullName.Substring($source.Length + 1)
        $same = (Test-Path $dest) -and ((Get-FileHash $dest).Hash -eq (Get-FileHash $file.FullName).Hash)
        if (-not $same) {
            New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
            Copy-Item -LiteralPath $file.FullName -Destination $dest -Force
            $copied++
        }
    }
    return $copied
}
# -B et PYTHONDONTWRITEBYTECODE : aucun __pycache__ dans les sources, quel que soit le lanceur.
function Get-ServerEnv([string]$agent) {
    [ordered]@{ NEXUS_RELAY_AGENT = $agent; PYTHONUTF8 = '1'; PYTHONDONTWRITEBYTECODE = '1'; PYTHONPYCACHEPREFIX = $pycache }
}
$serverArgs = @('-B', '-m', 'nexus_relay.server')

# --- Démon partagé : un seul serveur nexus-relay pour toutes les sessions de Claude, Codex et Antigravity, déclaré
#     au superviseur des démons (tâche « Nexus MCP Daemons », qui lit ~\.nexus\daemons\specs).
#     Chaque client le joint par URL avec son agent (?agent=…) et le jeton du poste.
$port = 16181
$settingsPath = Join-Path $state 'settings.json'
$settings = if (Test-Path $settingsPath) { [IO.File]::ReadAllText($settingsPath) | ConvertFrom-Json } else { [pscustomobject]@{} }
$tokenFile = @($TokenFile, $settings.token_file, $env:NEXUS_MCP_TOKEN_FILE) | Where-Object { $_ } | Select-Object -First 1
# Jeton partagé entre plusieurs PC (poste réglé) : jamais créé ici, un installeur lancé avant la synchro du fichier en
# créait un autre, qui cassait l'autre PC. Installation publique (-Register) sans aucun réglage : jeton propre à ce PC.
if ($Register -and -not $tokenFile) {
    $tokenFile = Join-Path $state 'token.txt'
    if (-not (Test-Path $tokenFile)) {
        $bytes = New-Object byte[] 32
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        [IO.File]::WriteAllText($tokenFile, (-join ($bytes | ForEach-Object { $_.ToString('x2') })), $utf8)
        Write-Host "Jeton du démon créé : $tokenFile"
    }
    $settings | Add-Member -NotePropertyName token_file -NotePropertyValue $tokenFile -Force
    [IO.File]::WriteAllText($settingsPath, ($settings | ConvertTo-Json -Depth 5), $utf8)
}
if (-not $tokenFile -or -not (Test-Path $tokenFile)) {
    throw "Jeton absent ($tokenFile) : régler token_file dans $settingsPath, -TokenFile ou NEXUS_MCP_TOKEN_FILE."
}
$token = (Get-Content $tokenFile -Raw).Trim()
function Get-Url([string]$agent) { "http://127.0.0.1:$port/mcp?agent=$agent" }
$daemonEnv = Get-ServerEnv ''
$daemonEnv.Remove('NEXUS_RELAY_AGENT')
$daemonEnv.NEXUS_MCP_TOKEN_FILE = (Resolve-Path $tokenFile).Path  # lu par le démon (NEXUS_MCP_TOKEN_FILE)
# Réglages du poste passés au démon (absents : pas de coffre, pas de routeur devant Codex)
if ($settings.vaults) { $daemonEnv.NEXUS_RELAY_VAULTS = @($settings.vaults) -join [IO.Path]::PathSeparator }
if ($settings.codex_router) { $daemonEnv.NEXUS_RELAY_CODEX_ROUTER = [string]$settings.codex_router }
if ($settings.codex_router_start) { $daemonEnv.NEXUS_RELAY_CODEX_ROUTER_START = @($settings.codex_router_start) -join ';' }
$spec = [ordered]@{ port = $port; python = $python; args = $serverArgs; env = $daemonEnv }
$specChanged = Write-IfChanged (Join-Path $HOME '.nexus\daemons\specs\nexus-relay.json') ($spec | ConvertTo-Json -Depth 5)
# Nouveau code : demande de relance au démon, jamais d'arrêt forcé. Il ne se relance que si son code a changé, une fois
# aucun appel en cours, et le superviseur le relance. Vu le 20/10 : l'arrêt forcé ici coupait les wait_reply de 600 s
# à chaque installation d'une autre session (ECONNRESET, puis « server not connected »).
# Processus d'avant la relance par demande (pas de marqueur à leur PID, ni à celui de leur lanceur) : arrêtés de force
# une dernière fois, leur relanceur les remet sur le nouveau code (tour 5 : sinon l'ancien code restait pour toujours).
function Stop-Legacy([string]$pattern, [string]$name) {
    $procs = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $pattern })
    $known = 0
    $null = [int]::TryParse("$(Get-Content (Join-Path $mailbox "$name-restart.pid") -Raw -ErrorAction SilentlyContinue)".Trim(), [ref]$known)
    $spare = @($known) + @($procs | Where-Object { $_.ProcessId -eq $known } | ForEach-Object { $_.ParentProcessId })
    $procs | Where-Object { $_.ProcessId -notin $spare } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
Stop-Legacy 'nexus_relay\.server --http' 'daemon'
Stop-Legacy 'nexus_relay\.sidecar' 'sidecar'
# Réglages du démon changés (Python, arguments, environnement) : relance forcée, le code seul ne le dirait pas.
$daemonRequest = Join-Path $mailbox 'daemon-restart.request'
$forced = $specChanged -or "$(Get-Content $daemonRequest -Raw -ErrorAction SilentlyContinue)".StartsWith('force')  # tour 6
[IO.File]::WriteAllText($daemonRequest, "$(if ($forced) { 'force' } else { 'code' }) $(Get-Date -Format o)", $utf8)
# Pareil pour le sidecar d'Antigravity : il sort entre deux livraisons si son code a changé, Antigravity le relance
# (restart_policy « always »). Tour 5 : l'arrêt forcé ici laissait une livraison en cours « incertaine », jamais rejouée.
[IO.File]::WriteAllText((Join-Path $mailbox 'sidecar-restart.request'), "code $(Get-Date -Format o)", $utf8)
# Sans superviseur des démons Nexus : tâche « Nexus Relay » à l'ouverture de session, qui lance le petit superviseur du
# relais (nexus_relay.daemon : lit la spec ci-dessus, relance le démon quand il s'arrête), puis lancée tout de suite.
if (-not (Get-ScheduledTask -TaskName 'Nexus MCP Daemons' -ErrorAction SilentlyContinue)) {
    $taskName = 'Nexus Relay'
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument '-B -m nexus_relay.daemon' -WorkingDirectory $state
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settingsTask = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Hidden
    if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settingsTask `
            -Description 'Nexus Relay : démon MCP local de Claude, Codex et Antigravity (127.0.0.1:16181).' | Out-Null
        Write-Host "Tâche planifiée « $taskName » créée (ouverture de session)."
    }
    if ((Get-ScheduledTask -TaskName $taskName).State -ne 'Running') { Start-ScheduledTask -TaskName $taskName }
}

# --- Claude Code : serveur MCP et skill par le plugin nexus-relay (dossier claude\ ; jeton dans la variable
#     NEXUS_MCP_TOKEN des réglages de Claude Code). Aucune inscription ici (aucune CLI Claude). Claude n'a plus de tâche planifiée de réveil
#     (20/10 : hôte, il attend les réponses par wait_reply).

# --- Antigravity : plugin nexus-relay (serveur MCP, skill, sidecar) et activation du sidecar
$agyRoot = Join-Path $HOME '.gemini\config'
if (-not (Test-Path $agyRoot)) {
    Write-Warning 'Antigravity absent de ce PC : plugin non installé.'
    $agyResult = ''
} else {
    $plugin = Join-Path $agyRoot 'plugins\nexus-relay'
    $copied = Copy-Tree (Join-Path $project 'plugins\nexus-relay') $plugin
    $mcp = [ordered]@{ mcpServers = [ordered]@{ 'nexus-relay' = [ordered]@{
        serverUrl = (Get-Url 'antigravity'); headers = [ordered]@{ Authorization = "Bearer $token" } } } }
    if (Write-IfChanged (Join-Path $plugin 'mcp_config.json') ($mcp | ConvertTo-Json -Depth 10)) { $copied++ }
    $sidecar = [ordered]@{
        display_name = 'Nexus Relay'
        description = 'Livre à Antigravity les messages de Claude (boîte ~\.nexus\relay) par agentapi, une conversation par sujet dans le projet Nexus Relay ; une livraison incertaine n''est jamais rejouée.'
        command = $pythonw
        args = @('-B', '-m', 'nexus_relay.sidecar')
        env = [ordered]@{ PYTHONUTF8 = '1'; PYTHONDONTWRITEBYTECODE = '1'; PYTHONPYCACHEPREFIX = $pycache }
        restart_policy = 'always'
    }
    if (Write-IfChanged (Join-Path $plugin 'sidecars\relay\sidecar.json') ($sidecar | ConvertTo-Json -Depth 10)) { $copied++ }
    Write-Host "Antigravity : plugin nexus-relay $(if ($copied) { "installé ($copied fichier(s))" } else { 'déjà à jour' }) dans $plugin."
    $agyResult = "plugin installed in $plugin"

    # Projet où agentapi crée les conversations : « Nexus Relay » (l'utilisateur, 20/10), à part des conversations de l'utilisateur
    # dans « Documents », sur un dossier vide hors de tout dossier synchronisé ; créé ici s'il manque (fichier de projet d'Antigravity,
    # réglages vides : ceux d'Antigravity s'appliquent). Avant le 20/10 : le projet « Documents ».
    $relayProjectName = 'Nexus Relay'
    $relayFolder = Join-Path $HOME '.nexus\relay\agy-projet'
    New-Item -ItemType Directory -Force $relayFolder | Out-Null
    $projectsDir = Join-Path $agyRoot 'projects'
    New-Item -ItemType Directory -Force $projectsDir | Out-Null
    $projectId = $null
    foreach ($file in Get-ChildItem $projectsDir -Filter *.json -ErrorAction SilentlyContinue) {
        $candidate = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
        if ($candidate.id -and [string]$candidate.name -eq $relayProjectName) { $projectId = [string]$candidate.id }
    }
    if (-not $projectId) {
        $projectId = [guid]::NewGuid().ToString()
        $full = (Resolve-Path $relayFolder).Path
        $uri = 'file:///' + $full.Substring(0, 1).ToLowerInvariant() + '%3A' + ($full.Substring(2) -replace '\\', '/')
        $definition = [ordered]@{
            id = $projectId; name = $relayProjectName
            projectResources = [ordered]@{ resources = @([ordered]@{ folderUri = $uri }) }
            permissionGrants = [ordered]@{ permissionGrants = [ordered]@{} }; settings = [ordered]@{}; isWorkspaceOnly = $false
        }
        [IO.File]::WriteAllText((Join-Path $projectsDir "$projectId.json"), ($definition | ConvertTo-Json -Depth 10), $utf8)
        Write-Host "Antigravity : projet « $relayProjectName » créé ($relayFolder)."
    }
    # Sidecar activé dans config.json (doc officielle : sidecars.<id>.enabled + projectId). Rien d'autre n'est touché ;
    # Un outil qui réécrit ce fichier doit garder la clé sidecars.
    $configPath = Join-Path $agyRoot 'config.json'
    $config = if (Test-Path $configPath) { [IO.File]::ReadAllText($configPath) | ConvertFrom-Json } else { [PSCustomObject]@{} }
    $entry = if ($config.PSObject.Properties['sidecars']) { $config.sidecars.'nexus-relay/relay' } else { $null }
    $knownProject = $entry -and $entry.projectId -eq $projectId
    $ready = $entry -and $entry.enabled -eq $true -and $knownProject
    $pluginOff = $config.PSObject.Properties['plugins'] -and $config.plugins.PSObject.Properties['nexus-relay'] -and
        $config.plugins.'nexus-relay'.enabled -eq $false
    if ($ready -and -not $pluginOff) {
        Write-Host "Antigravity : sidecar nexus-relay/relay déjà activé (projet $($entry.projectId))."
    } else {
        if (Test-Path $configPath) { Save-Backup $configPath 'antigravity-config' }
        if (-not $config.PSObject.Properties['sidecars']) { $config | Add-Member sidecars ([PSCustomObject]@{}) }
        if (-not $entry) {
            $entry = [PSCustomObject]@{}
            $config.sidecars | Add-Member -NotePropertyName 'nexus-relay/relay' -NotePropertyValue $entry
        }
        $entry | Add-Member -NotePropertyName enabled -NotePropertyValue $true -Force
        if (-not $knownProject) { $entry | Add-Member -NotePropertyName projectId -NotePropertyValue $projectId -Force }
        if ($pluginOff) { $config.plugins.'nexus-relay'.enabled = $true }
        [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 100), $utf8)
        Write-Host "Antigravity : sidecar nexus-relay/relay activé (projet $($entry.projectId)), rechargé à chaud par Antigravity."
    }
}

# --- Codex : serveur par un plugin perso de Codex (.mcp.json : url http://127.0.0.1:16181/mcp?agent=codex,
#     bearer_token_env_var NEXUS_MCP_TOKEN), voir README ; plus de table [mcp_servers] ici.
if (-not $SkipTests) {
    & (Get-Process -Id $PID).Path -NoProfile -File (Join-Path $project 'test.ps1')
    if ($LASTEXITCODE) { throw 'test.ps1 échoue : installation incomplète.' }
}

$stamp = (Get-FileHash (Join-Path $project 'uv.lock')).Hash + ' ' + (Get-FileHash (Join-Path $project 'install.ps1')).Hash
[IO.File]::WriteAllText((Join-Path $state 'installed.stamp'), $stamp, $utf8)

# --- Installation publique : jeton dans la variable utilisateur NEXUS_MCP_TOKEN (lue par Claude Code et Codex, jamais
#     écrite dans leurs fichiers), puis serveur déclaré aux clients trouvés.
if ($Register) {
    if ([Environment]::GetEnvironmentVariable('NEXUS_MCP_TOKEN', 'User') -ne $token) {
        [Environment]::SetEnvironmentVariable('NEXUS_MCP_TOKEN', $token, 'User')
        Write-Host 'Variable utilisateur NEXUS_MCP_TOKEN posée (redémarrer Claude Code et Codex pour la voir).'
    }
    $env:NEXUS_MCP_TOKEN = $token
    $agyArgs = if ($agyResult) { @('--antigravity', $agyResult) } else { @() }  # 5.1 : un argument vide disparaît
    & $python -B (Join-Path $project 'scripts\register_clients.py') --port $port --backups $backups @agyArgs
    if ($LASTEXITCODE) { throw 'Déclaration aux clients incomplète (voir ci-dessus).' }
}
# pack Nexus : la ligne NEXUS_PACK_RESULT reste la dernière
if (-not ($Register -and $env:NEXUS_PACK -eq '1')) { Write-Host 'Nexus Relay installé avec succès.' }
