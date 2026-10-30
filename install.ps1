# Installe Nexus Relay sur ce PC (idempotent : à relancer sans risque après une mise à jour du dépôt).
#  - environnement Python hors du dépôt : ~\.nexus\nexus-relay\venv (caches dans ~\.nexus\nexus-relay\cache)
#  - boîte aux lettres : ~\.nexus\relay (hors de tout dossier synchronisé)
#  - réglages du poste : ~\.nexus\nexus-relay\settings.json (ce PC), puis settings.local.json à la racine du dépôt
#    (partagé par les PC d'une installation synchronisée), valeurs en %VARIABLES% (nexus_relay\settings.py), voir README :
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
#  - Codex : espace explicite par chemin, nom et identifiant officiel (par exemple un projet existant Documents) ;
#    le dossier historique Nexus Relay reste le défaut des installations sans choix, voir README
#  - tests hors ligne (sauf -SkipTests)
#  - -Register (installation publique, get.ps1) : jeton créé s'il n'y en a aucun (~\.nexus\nexus-relay\token.txt, noté
#    dans settings.json), variable utilisateur NEXUS_MCP_TOKEN posée, serveur déclaré à Claude Code et Codex
#    (scripts\register_clients.py) ; NEXUS_PACK=1 (pack Nexus) : aucune question ni fenêtre, ligne NEXUS_PACK_RESULT
#  - NEXUS_PACK_TARGETS (pack Nexus) : IA à configurer (claude-code, codex, antigravity…, séparées par des
#    virgules) ; présente, toute IA absente de la liste n'est pas touchée ; absente, toutes celles trouvées
#  - hôte du relais (le seul qui ouvre un échange) : réglage `host`, Claude par défaut ; avec -Register et sans réglage,
#    NEXUS_PACK_HOST, sinon le premier agent présent parmi les IA choisies (Claude Code, Codex, Antigravity)
# Windows PowerShell 5.1 ou PowerShell 7.
# Usage : powershell -NoProfile -File install.ps1 [-SkipTests] [-TokenFile <chemin>] [-Register]
param([switch]$SkipTests, [string]$TokenFile, [switch]$Register, [switch]$PrepareOnly, [ValidateSet('python', 'native')][string]$Runtime)
$ErrorActionPreference = 'Stop'

$project = $PSScriptRoot
$state = Join-Path $HOME '.nexus\nexus-relay'
$venv = Join-Path $state 'venv'
$cache = Join-Path $state 'cache'
$pycache = Join-Path $cache 'pycache'
$backups = Join-Path $state 'backups'
$mailbox = Join-Path $HOME '.nexus\relay'
New-Item -ItemType Directory -Force $state, $cache, $backups, $mailbox | Out-Null
. (Join-Path $project 'scripts\Restart-Relay.ps1')
. (Join-Path $project 'scripts\Configure-AntigravityRelay.ps1')
. (Join-Path $project 'scripts\Resolve-RelayPath.ps1')
$previousSpecPath = Join-Path $HOME '.nexus\daemons\specs\nexus-relay.json'
$previousSpec = if (Test-Path -LiteralPath $previousSpecPath) { Get-Content -LiteralPath $previousSpecPath -Raw | ConvertFrom-RelayJson } else { $null }
$daemonOwner = if (-not $PrepareOnly -and $previousSpec) { Get-RelayRestartOwner -Mailbox $mailbox -Name daemon -ExpectedExecutable ([string]$previousSpec.python) -Port ([int]$previousSpec.port) } else { $null }
$previousSidecarPath = Join-Path $HOME '.gemini\config\plugins\nexus-relay\sidecars\relay\sidecar.json'
$previousSidecar = if (Test-Path -LiteralPath $previousSidecarPath) { Get-Content -LiteralPath $previousSidecarPath -Raw | ConvertFrom-RelayJson } else { $null }
$sidecarOwner = if (-not $PrepareOnly -and $previousSidecar) { Get-RelayRestartOwner -Mailbox $mailbox -Name sidecar -ExpectedExecutable ([string]$previousSidecar.command) } else { $null }

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
# The chosen runtime is durable across logons/syncs. Building a candidate never activates it.
$runtimeSettingsPath = Join-Path $state 'settings.json'
$runtimeSettings = if (Test-Path $runtimeSettingsPath) { Get-Content -LiteralPath $runtimeSettingsPath -Raw | ConvertFrom-RelayJson } else { [pscustomobject]@{} }
if (-not $Runtime) { $Runtime = if ($runtimeSettings.PSObject.Properties['runtime']) { [string]$runtimeSettings.runtime } else { 'python' } }
if ($Runtime -notin @('python', 'native')) { throw 'Runtime du relais invalide.' }
$nativeCsproj = Join-Path $project 'native\NexusRelay.csproj'
$nativeExe = $null
if ((Test-Path $nativeCsproj) -and (Get-Command dotnet.exe -ErrorAction SilentlyContinue)) {
    # Immutable, content-addressed binaries: publishing cannot overwrite a running DLL or apphost.
    $nativeHashes = Get-ChildItem (Join-Path $project 'native') -File | Where-Object Extension -in @('.cs', '.csproj', '.json') | Sort-Object Name | ForEach-Object { (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
    $nativeDigest = [Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes(($nativeHashes -join '')))
    $nativeKey = (-join ($nativeDigest | ForEach-Object { $_.ToString('x2') })).Substring(0, 16)
    $nativeBuild = Join-Path $env:LOCALAPPDATA 'NexusRelay\build'
    $nativeOut = Join-Path $env:LOCALAPPDATA "NexusRelay\versions\$nativeKey"
    $nativeExe = Join-Path $nativeOut 'NexusRelay.exe'
    $nativeComplete = Join-Path $nativeOut 'publish.complete'
    if (-not ((Test-Path -LiteralPath $nativeExe) -and (Test-Path -LiteralPath (Join-Path $nativeOut 'NexusRelay.dll')) -and (Test-Path -LiteralPath $nativeComplete))) {
        & dotnet.exe publish $nativeCsproj -c Release -o $nativeOut "-p:BaseIntermediateOutputPath=$nativeBuild\obj\" "-p:BaseOutputPath=$nativeBuild\bin\" --nologo -v q
        if ($LASTEXITCODE) { throw 'dotnet publish (native) a échoué.' }
        [IO.File]::WriteAllText($nativeComplete, $nativeKey, [Text.UTF8Encoding]::new($false))
    }
    # Packaged Codex redirects new AppData files into its MSIX cache. External hosts need the actual opened path,
    # not the logical path that exists only from the installer's package view.
    $nativeExe = Resolve-RelayPath -Path $nativeExe
}
if ($Runtime -eq 'native' -and -not $nativeExe) { throw '.NET 10 SDK requis pour le moteur natif.' }
if ($PrepareOnly) {
    if (-not $nativeExe) { throw 'Candidat natif absent.' }
    & $nativeExe --selftest | Out-Null
    if ($LASTEXITCODE) { throw 'Autotest du candidat natif en échec.' }
    & $nativeExe --runtime-selftest | Out-Null
    if ($LASTEXITCODE) { throw 'Autotest des workers du candidat natif en échec.' }
    Write-Host "NEXUS_RELAY_NATIVE_CANDIDATE $nativeExe"
    Write-Host 'Candidat préparé et testé ; runtime, spec, plugins et processus en service inchangés.'
    exit 0
}
$runtimeSettings | Add-Member -NotePropertyName runtime -NotePropertyValue $Runtime -Force
$runtimeSettings | Add-Member -NotePropertyName installed_source -NotePropertyValue $project -Force
if ($nativeExe) { $runtimeSettings | Add-Member -NotePropertyName native_root -NotePropertyValue (Split-Path (Split-Path (Split-Path $nativeExe))) -Force }

$python = Join-Path $venv 'Scripts\python.exe'
# Sidecar sans aucune fenêtre : pythonw.exe (aucune console à créer, même si Antigravity n'en fournit pas ; Windows
# Terminal, terminal par défaut de ce PC, ignorerait -WindowStyle Hidden). Sa sortie reste capturée par Antigravity
# (vérifié : pythonw écrit dans les tubes hérités). Le démon MCP garde python.exe.
$pythonw = Join-Path $venv 'Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "pythonw.exe absent de $venv : environnement à recréer." }
$utf8 = [Text.UTF8Encoding]::new($false)
New-Item -ItemType Directory -Force (Join-Path $HOME '.nexus\relay\codex-projet') | Out-Null  # dossier du projet Codex du relais
$installedHooks = Join-Path $state 'hooks'
New-Item -ItemType Directory -Force $installedHooks | Out-Null
Copy-Item -LiteralPath (Join-Path $project 'scripts\invoke-hook.ps1') -Destination (Join-Path $installedHooks 'invoke-hook.ps1') -Force
Copy-Item -LiteralPath (Join-Path $project 'scripts\Resolve-RelayPath.ps1') -Destination (Join-Path $installedHooks 'Resolve-RelayPath.ps1') -Force
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
# Réglages effectifs (fichier de ce PC, puis celui du dépôt ; variables développées) : une seule lecture, celle du démon.
$settings = (& $python -B -m nexus_relay.settings) | ConvertFrom-RelayJson
if ($LASTEXITCODE) { throw 'Lecture des réglages du poste : échec (nexus_relay.settings).' }
# Langue des consignes déployées (réglage `lang`) : anglais par défaut ; `fr` prend la copie française du dossier fr\
# du dépôt quand elle existe (skill d'Antigravity, consignes des chats de Codex).
$french = ([string]$settings.lang).Trim().ToLower().StartsWith('fr')
function Get-Localized([string]$relative) {
    $fr = Join-Path $project "fr\$relative"
    if ($french -and (Test-Path $fr)) { return $fr }
    return Join-Path $project $relative
}
# Consignes des chats du relais, lues par Codex dans ce dossier : jamais de question à l'utilisateur, qui ne les lit pas.
Copy-Item -Force (Get-Localized 'codex-projet\AGENTS.md') (Join-Path $HOME '.nexus\relay\codex-projet\AGENTS.md')
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
    $local = if (Test-Path $settingsPath) { [IO.File]::ReadAllText($settingsPath) | ConvertFrom-RelayJson } else { [pscustomobject]@{} }
    $local | Add-Member -NotePropertyName token_file -NotePropertyValue $tokenFile -Force
    [IO.File]::WriteAllText($settingsPath, ($local | ConvertTo-Json -Depth 5), $utf8)
}
# Périmètre du pack Nexus : IA choisies par l'utilisateur ; les autres ne sont jamais touchées.
$packTargets = if ($null -ne $env:NEXUS_PACK_TARGETS) { @($env:NEXUS_PACK_TARGETS -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) } else { $null }
# Hôte (installation publique seulement, et seulement s'il n'est pas déjà réglé : un choix de l'utilisateur reste) :
# celui que donne le pack, sinon le premier agent présent dans l'ordre Claude Code, Codex, Antigravity.
if ($Register -and -not $settings.host) {
    $present = [ordered]@{
        'claude-code' = [bool](Get-Command claude -ErrorAction SilentlyContinue) -or (Test-Path (Join-Path $HOME '.claude'))
        'codex'       = [bool](Get-Command codex -ErrorAction SilentlyContinue) -or (Test-Path (Join-Path $HOME '.codex'))
        'antigravity' = Test-Path (Join-Path $HOME '.gemini')
    }
    $agentOf = @{ 'claude-code' = 'claude'; 'claude' = 'claude'; 'codex' = 'codex'; 'antigravity' = 'antigravity' }
    $chosenHost = if ($env:NEXUS_PACK_HOST -and $agentOf.ContainsKey($env:NEXUS_PACK_HOST.Trim())) { $agentOf[$env:NEXUS_PACK_HOST.Trim()] } else {
        @($present.Keys | Where-Object { $present[$_] -and ($null -eq $packTargets -or $packTargets -contains $_) } | ForEach-Object { $agentOf[$_] }) | Select-Object -First 1
    }
    if ($chosenHost) {
        $local = if (Test-Path $settingsPath) { [IO.File]::ReadAllText($settingsPath) | ConvertFrom-RelayJson } else { [pscustomobject]@{} }
        $local | Add-Member -NotePropertyName host -NotePropertyValue $chosenHost -Force
        [IO.File]::WriteAllText($settingsPath, ($local | ConvertTo-Json -Depth 5), $utf8)
        $settings | Add-Member -NotePropertyName host -NotePropertyValue $chosenHost -Force
        Write-Host "Hôte du relais : $chosenHost (seul à ouvrir un échange ; réglage host de $settingsPath)."
    }
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
if ($settings.host) { $daemonEnv.NEXUS_RELAY_HOST = [string]$settings.host }
if ($settings.lang) { $daemonEnv.NEXUS_RELAY_LANG = [string]$settings.lang }
foreach ($entry in @{codex_workspace='NEXUS_RELAY_CODEX_WORKSPACE'; codex_project='NEXUS_RELAY_CODEX_PROJECT'; codex_project_id='NEXUS_RELAY_CODEX_PROJECT_ID'; wake_since='NEXUS_RELAY_WAKE_SINCE'}.GetEnumerator()) { if ($settings.($entry.Key)) { $daemonEnv[$entry.Value] = [string]$settings.($entry.Key) } }
# Boîte épinglée : le démon lit celle où ce script dépose ses demandes, quel que soit l'environnement de son lanceur
# (relecture de Codex, 27/10 : un NEXUS_RELAY_HOME hérité du superviseur l'envoyait ailleurs).
$daemonEnv.NEXUS_RELAY_HOME = $mailbox
$daemonEnv.NEXUS_RELAY_SOURCE_ROOT = $project
if ($Runtime -eq 'native') {
    $priorEnvironment = @{}
    try {
        foreach ($key in @('NEXUS_RELAY_HOME','NEXUS_RELAY_CODEX_WORKSPACE','NEXUS_RELAY_CODEX_PROJECT','NEXUS_RELAY_CODEX_PROJECT_ID')) {
            $priorEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
            [Environment]::SetEnvironmentVariable($key, [string]$daemonEnv[$key], 'Process')
        }
        & $nativeExe --register-known-relay-chats | Out-Null
        if ($LASTEXITCODE) { throw 'Vérification des anciens chats Relay en échec ; aucune appropriation par dossier seul.' }
    } finally { foreach ($key in $priorEnvironment.Keys) { [Environment]::SetEnvironmentVariable($key, $priorEnvironment[$key], 'Process') } }
}
# restart_request : la demande de relance que suit le démon. Un superviseur la prend juste avant de le lancer (avant de
# relire la spec) : ce lancement la satisfait, le démon neuf ne se relance pas aussitôt (CI du 27/10, 1.8.0).
$daemonRequest = Join-Path $mailbox 'daemon-restart.request'
$daemonExe = if ($Runtime -eq 'native') { $nativeExe } else { $python }
$daemonArgs = if ($Runtime -eq 'native') { @() } else { $serverArgs }
$spec = [ordered]@{ port = $port; python = $daemonExe; args = @($daemonArgs); env = $daemonEnv; restart_request = $daemonRequest }
$specChanged = Write-IfChanged (Join-Path $HOME '.nexus\daemons\specs\nexus-relay.json') ($spec | ConvertTo-Json -Depth 5)
# Hooks follow this setting and resolve their native executable through the spec. Publish the spec first: during
# the short transition old Python hooks remain valid, whereas selecting native against a Python spec would fail.
[IO.File]::WriteAllText($runtimeSettingsPath, ($runtimeSettings | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))
# Nouveau code : demande de relance au démon, jamais d'arrêt forcé. Il ne se relance que si son code a changé, une fois
# aucun appel en cours, et le superviseur le relance. Vu le 20/10 : l'arrêt forcé ici coupait les wait_reply de 600 s
# à chaque installation d'une autre session (ECONNRESET, puis « server not connected »).
# Ownership was checked before changing the runtime: executable/module, listening port, PID and birth vs marker.
# An unknown legacy process is never terminated. The request addresses only the verified process and drains work.
# Réglages du démon changés (Python, arguments, environnement) : relance forcée, le code seul ne le dirait pas.
$forced = $specChanged -or "$(Get-Content $daemonRequest -Raw -ErrorAction SilentlyContinue)".StartsWith('force')  # tour 6
$targetFingerprint = if ($Runtime -eq 'native') { "sha256:$((Get-FileHash -LiteralPath ([IO.Path]::ChangeExtension($nativeExe, '.dll')) -Algorithm SHA256).Hash.ToLowerInvariant()) " } else { '' }
$null = Request-RelayRestart -RequestPath $daemonRequest -Owner $daemonOwner -Kind $(if ($forced) { 'force' } else { 'code' }) -Fingerprint $targetFingerprint
# Pareil pour le sidecar d'Antigravity : il sort entre deux livraisons si son code a changé, Antigravity le relance
# (restart_policy « always »). Tour 5 : l'arrêt forcé ici laissait une livraison en cours « incertaine », jamais rejouée.
$null = Request-RelayRestart -RequestPath (Join-Path $mailbox 'sidecar-restart.request') -Owner $sidecarOwner -Kind $(if ($specChanged) { 'force' } else { 'code' }) -Fingerprint $targetFingerprint
# Sans superviseur des démons Nexus : tâche « Nexus Relay » à l'ouverture de session, qui lance le petit superviseur du
# relais (nexus_relay.daemon : lit la spec ci-dessus, relance le démon quand il s'arrête), puis lancée tout de suite.
if (-not (Get-ScheduledTask -TaskName 'Nexus MCP Daemons' -ErrorAction SilentlyContinue)) {
    $taskName = 'Nexus Relay'
    $supervisorExe = if ($Runtime -eq 'native') { $nativeExe } else { $pythonw }
    $supervisorArgs = if ($Runtime -eq 'native') { '--daemon' } else { '-B -m nexus_relay.daemon' }
    $action = New-ScheduledTaskAction -Execute $supervisorExe -Argument $supervisorArgs -WorkingDirectory $state
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
if ($null -ne $packTargets -and $packTargets -notcontains 'antigravity') {
    Write-Host 'Antigravity : hors du périmètre choisi (NEXUS_PACK_TARGETS), non touché.'
    $agyResult = ''
} elseif (-not (Test-Path $agyRoot)) {
    Write-Warning 'Antigravity absent de ce PC : plugin non installé.'
    $agyResult = ''
} else {
    $plugin = Join-Path $agyRoot 'plugins\nexus-relay'
    $copied = Copy-Tree (Join-Path $project 'plugins\nexus-relay') $plugin
    if ($french -and (Test-Path (Join-Path $project 'fr\plugins\nexus-relay'))) {
        $copied += Copy-Tree (Join-Path $project 'fr\plugins\nexus-relay') $plugin  # copie française par-dessus
    }
    $mcp = [ordered]@{ mcpServers = [ordered]@{ 'nexus-relay' = [ordered]@{
        serverUrl = (Get-Url 'antigravity'); headers = [ordered]@{ Authorization = "Bearer $token" } } } }
    if (Write-IfChanged (Join-Path $plugin 'mcp_config.json') ($mcp | ConvertTo-Json -Depth 10)) { $copied++ }
    $sidecar = [ordered]@{
        display_name = 'Nexus Relay'
        description = 'Livre à Antigravity les messages de Claude (boîte ~\.nexus\relay) par agentapi, une conversation par sujet dans le projet Nexus Relay ; une livraison incertaine n''est jamais rejouée.'
        command = $(if ($Runtime -eq 'native') { $nativeExe } else { $pythonw })
        args = @($(if ($Runtime -eq 'native') { '--sidecar' } else { '-B'; '-m'; 'nexus_relay.sidecar' }))
        env = [ordered]@{ PYTHONUTF8 = '1'; PYTHONDONTWRITEBYTECODE = '1'; PYTHONPYCACHEPREFIX = $pycache; NEXUS_RELAY_HOME = $mailbox; NEXUS_RELAY_HOST = [string]$settings.host; NEXUS_RELAY_WAKE_SINCE = [string]$settings.wake_since; NEXUS_RELAY_SOURCE_ROOT = $project }
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
        $candidate = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-RelayJson
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
    $config = if (Test-Path $configPath) { [IO.File]::ReadAllText($configPath) | ConvertFrom-RelayJson } else { [PSCustomObject]@{} }
    $activation = Set-AntigravityRelayActivation -Config $config -ProjectId $projectId
    if ($activation.Disabled) { Write-Host 'Antigravity : choix de désactivation du plugin ou du sidecar conservé.' }
    elseif ($activation.Changed) {
        if (Test-Path $configPath) { Save-Backup $configPath 'antigravity-config' }
        [IO.File]::WriteAllText($configPath, ($activation.Config | ConvertTo-Json -Depth 100), $utf8)
        Write-Host "Antigravity : plugin et sidecar nexus-relay/relay activés (projet $projectId), rechargés à chaud par Antigravity."
    } else { Write-Host "Antigravity : plugin et sidecar nexus-relay/relay déjà activés (projet $projectId)." }
}

# --- Codex : serveur par un plugin perso de Codex (.mcp.json : url http://127.0.0.1:16181/mcp?agent=codex,
#     bearer_token_env_var NEXUS_MCP_TOKEN), voir README ; plus de table [mcp_servers] ici.

# --- Hook d'édition (nexus_relay.edit_guard) chez Codex et Antigravity : refus d'un fichier réservé par un autre agent,
#     réservation automatique sinon. Claude Code l'a par le plugin nexus-relay du catalogue nexus.
$guardRuntimeArgs = @('--wrapper', (Join-Path $installedHooks 'invoke-hook.ps1'))
& $python -B (Join-Path $project 'scripts\register_hooks.py') --backups $backups --python $python @guardRuntimeArgs
if ($LASTEXITCODE) { throw 'Hook d''édition incomplet (voir ci-dessus).' }

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
