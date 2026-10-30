# Installe Nexus Relay sur ce PC (idempotent : à relancer sans risque après une mise à jour du dépôt). Aucun Python.
#  - moteur natif (.NET 10) : compilé depuis native\ (dotnet publish, sorties de compilation dans ~\.nexus\build\nexusrelay)
#    ou pris tel quel dans bin\ (release publique, autonome) ; installé immuable par empreinte dans
#    %LOCALAPPDATA%\NexusRelay\versions\<empreinte> ; chemin stable ~\.nexus\nexus-relay\bin (copie de la version
#    en service), lu par les hooks d'édition et la garde de commit
#  - boîte aux lettres : ~\.nexus\relay (hors de tout dossier synchronisé)
#  - réglages du poste : ~\.nexus\nexus-relay\settings.json (ce PC), puis settings.local.json à la racine du dépôt
#    (partagé par les PC d'une installation synchronisée), valeurs en %VARIABLES% (NexusRelay.exe --settings), voir README :
#      token_file          fichier du jeton du démon (32 caractères au moins) ; sinon -TokenFile ou NEXUS_MCP_TOKEN_FILE
#      vaults              dossiers de secrets jamais joints à un message (NEXUS_RELAY_VAULTS)
#      codex_router        adresse de santé d'un routeur local devant Codex (NEXUS_RELAY_CODEX_ROUTER)
#      codex_router_start  commandes qui le lancent, essayées dans l'ordre (NEXUS_RELAY_CODEX_ROUTER_START)
#  - démon partagé (127.0.0.1:16181, jeton du poste) déclaré au superviseur des démons (tâche « Nexus MCP Daemons ») par
#    ~\.nexus\daemons\specs\nexus-relay.json (clé exe) ; chaque client le joint par URL avec son agent (?agent=…)
#  - Claude Code : serveur MCP et skill par le plugin nexus-relay (dossier claude\), rien ici
#  - Antigravity : plugin ~\.gemini\config\plugins\nexus-relay (serveur MCP par URL, skill, sidecar permanent
#    « nexus-relay/relay ») et son activation dans ~\.gemini\config\config.json, dans le projet Antigravity « Nexus Relay »
#    (créé ici, dossier ~\.nexus\relay\agy-projet)
#  - hooks d'édition de Codex et d'Antigravity : NexusRelay.exe --register-hooks
#  - tests hors ligne (sauf -SkipTests ; jamais sur une release publique, qui n'a pas les sources)
#  - -Register (installation publique, get.ps1) : jeton partagé des démons Nexus (NEXUS_MCP_TOKEN_FILE, sinon
#    ~\.nexus\mcp-token.txt, créé s'il manque, jamais régénéré), variable utilisateur NEXUS_MCP_TOKEN alignée dessus,
#    serveur déclaré à Claude Code et Codex
#    (NexusRelay.exe --register-clients) ; NEXUS_PACK=1 (pack Nexus) : aucune question ni fenêtre, ligne NEXUS_PACK_RESULT
#  - NEXUS_PACK_TARGETS (pack Nexus) : IA à configurer (claude-code, codex, antigravity…, séparées par des virgules) ;
#    présente, toute IA absente de la liste n'est pas touchée ; absente, toutes celles trouvées
#  - hôte du relais (le seul qui ouvre un échange) : réglage `host`, Claude par défaut ; avec -Register et sans réglage,
#    NEXUS_PACK_HOST, sinon le premier agent présent parmi les IA choisies (Claude Code, Codex, Antigravity)
# Windows PowerShell 5.1 ou PowerShell 7.
# Usage : powershell -NoProfile -File install.ps1 [-SkipTests] [-TokenFile <chemin>] [-Register] [-PrepareOnly]
param([switch]$SkipTests, [string]$TokenFile, [switch]$Register, [switch]$PrepareOnly)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)  # sorties JSON et chemins accentués du moteur

$project = $PSScriptRoot
$state = Join-Path $HOME '.nexus\nexus-relay'
$backups = Join-Path $state 'backups'
$mailbox = Join-Path $HOME '.nexus\relay'
New-Item -ItemType Directory -Force $state, $backups, $mailbox | Out-Null
. (Join-Path $project 'scripts\Restart-Relay.ps1')
. (Join-Path $project 'scripts\Configure-AntigravityRelay.ps1')
. (Join-Path $project 'scripts\Resolve-RelayPath.ps1')
$specPath = Join-Path $HOME '.nexus\daemons\specs\nexus-relay.json'
$previousSpec = if (Test-Path -LiteralPath $specPath) { Get-Content -LiteralPath $specPath -Raw | ConvertFrom-RelayJson } else { $null }
$previousExe = if ($previousSpec) { @([string]$previousSpec.exe, [string]$previousSpec.python) | Where-Object { $_ } | Select-Object -First 1 } else { $null }
$daemonOwner = if (-not $PrepareOnly -and $previousExe) { Get-RelayRestartOwner -Mailbox $mailbox -Name daemon -ExpectedExecutable $previousExe -Port ([int]$previousSpec.port) } else { $null }
$previousSidecarPath = Join-Path $HOME '.gemini\config\plugins\nexus-relay\sidecars\relay\sidecar.json'
$previousSidecar = if (Test-Path -LiteralPath $previousSidecarPath) { Get-Content -LiteralPath $previousSidecarPath -Raw | ConvertFrom-RelayJson } else { $null }
$sidecarOwner = if (-not $PrepareOnly -and $previousSidecar) { Get-RelayRestartOwner -Mailbox $mailbox -Name sidecar -ExpectedExecutable ([string]$previousSidecar.command) } else { $null }

# --- Moteur natif : versions immuables par empreinte (une publication n'écrase jamais une DLL en service).
function Get-Digest([string[]]$files) {
    $hashes = $files | Sort-Object | ForEach-Object { (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash }
    $digest = [Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes(($hashes -join '')))
    (-join ($digest | ForEach-Object { $_.ToString('x2') })).Substring(0, 16)
}
$versions = Join-Path $env:LOCALAPPDATA 'NexusRelay\versions'
$nativeCsproj = Join-Path $project 'native\NexusRelay.csproj'
$prebuilt = Join-Path $project 'bin'
if (Test-Path -LiteralPath $nativeCsproj) {
    if (-not (Get-Command dotnet.exe -ErrorAction SilentlyContinue)) { throw '.NET 10 SDK requis pour compiler le moteur natif (native\).' }
    $sources = @(Get-ChildItem (Join-Path $project 'native') -File | Where-Object Extension -in @('.cs', '.csproj', '.json')) + @(Get-Item (Join-Path $project 'Directory.Build.props'))
    $nativeKey = Get-Digest $sources.FullName
    $nativeOut = Join-Path $versions $nativeKey
    if (-not (Test-Path -LiteralPath (Join-Path $nativeOut 'publish.complete'))) {
        # obj et bin intermédiaires : ~\.nexus\build\nexusrelay (Directory.Build.props), jamais dans OneDrive.
        & dotnet.exe publish $nativeCsproj -c Release -o $nativeOut --nologo -v q
        if ($LASTEXITCODE) { throw 'dotnet publish (moteur natif) a échoué.' }
        [IO.File]::WriteAllText((Join-Path $nativeOut 'publish.complete'), $nativeKey, [Text.UTF8Encoding]::new($false))
    }
} elseif (Test-Path -LiteralPath (Join-Path $prebuilt 'NexusRelay.exe')) {
    $files = @(Get-ChildItem -LiteralPath $prebuilt -File -Recurse)
    $nativeKey = Get-Digest $files.FullName
    $nativeOut = Join-Path $versions $nativeKey
    if (-not (Test-Path -LiteralPath (Join-Path $nativeOut 'publish.complete'))) {
        $partial = "$nativeOut.partial"
        if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Recurse -Force }
        New-Item -ItemType Directory -Force $partial | Out-Null
        Copy-Item -Path (Join-Path $prebuilt '*') -Destination $partial -Recurse -Force
        Rename-Item -LiteralPath $partial -NewName $nativeKey
        [IO.File]::WriteAllText((Join-Path $nativeOut 'publish.complete'), $nativeKey, [Text.UTF8Encoding]::new($false))
    }
} else { throw "Moteur natif absent : ni native\NexusRelay.csproj ni bin\NexusRelay.exe dans $project." }
# Codex empaqueté redirige les nouveaux fichiers d'AppData dans son cache MSIX : les hôtes externes ont besoin du
# chemin réellement ouvert, pas du chemin logique vu seulement par l'installeur.
$nativeExe = Resolve-RelayPath -Path (Join-Path $nativeOut 'NexusRelay.exe')
if ($PrepareOnly) {
    & $nativeExe --schemas | Out-Null
    if ($LASTEXITCODE) { throw 'Candidat natif illisible.' }
    Write-Output "NEXUS_RELAY_NATIVE_CANDIDATE $nativeExe"
    Write-Output 'Candidat préparé ; spec, plugins et processus en service inchangés.'
    exit 0
}

# Chemin stable du moteur (hooks d'édition, garde de commit des dépôts) : copie de la version en service. Remplacée
# sans casser une garde en cours : copie complète dans un dossier temporaire, puis échange par renommages ; un ancien
# dossier encore utilisé par un hook est laissé de côté et retiré à l'installation suivante.
$stable = Join-Path $state 'bin'
$stableExe = Join-Path $stable 'NexusRelay.exe'
$installedDir = [IO.Path]::GetDirectoryName($nativeExe)
$stamp = Join-Path $stable 'publish.complete'
if (-not ((Test-Path -LiteralPath $stamp) -and [IO.File]::ReadAllText($stamp) -eq $nativeKey)) {
    $fresh = Join-Path $state "bin.new-$PID"
    if (Test-Path -LiteralPath $fresh) { Remove-Item -LiteralPath $fresh -Recurse -Force }
    Copy-Item -LiteralPath $installedDir -Destination $fresh -Recurse
    [IO.File]::WriteAllText((Join-Path $fresh 'publish.complete'), $nativeKey, [Text.UTF8Encoding]::new($false))
    $link = Get-Item -LiteralPath $stable -Force -ErrorAction SilentlyContinue
    if ($link -and ($link.Attributes -band [IO.FileAttributes]::ReparsePoint)) { [IO.Directory]::Delete($stable, $false) }  # ancienne jonction : le lien seul
    elseif ($link) {
        $old = Join-Path $state "bin.old-$(Get-Date -Format 'yyyyMMddHHmmss')"
        $moved = $false
        foreach ($try in 1..20) { try { Rename-Item -LiteralPath $stable -NewName (Split-Path $old -Leaf); $moved = $true; break } catch { Start-Sleep -Milliseconds 250 } }
        if (-not $moved) { throw "$stable est en cours d'utilisation : relancer l'installation." }
    }
    Rename-Item -LiteralPath $fresh -NewName 'bin'
}
foreach ($old in Get-ChildItem -LiteralPath $state -Directory -Filter 'bin.old-*' -ErrorAction SilentlyContinue) { Remove-Item -LiteralPath $old.FullName -Recurse -Force -ErrorAction SilentlyContinue }

$settingsPath = Join-Path $state 'settings.json'
function Read-LocalSettings { if (Test-Path -LiteralPath $settingsPath) { [IO.File]::ReadAllText($settingsPath) | ConvertFrom-RelayJson } else { [pscustomobject]@{} } }
$utf8 = [Text.UTF8Encoding]::new($false)
New-Item -ItemType Directory -Force (Join-Path $HOME '.nexus\relay\codex-projet') | Out-Null  # dossier du projet Codex du relais

function Save-Backup([string]$path, [string]$name) {
    $backup = Join-Path $backups "$name-$(Get-Date -Format 'yyyyMMdd-HHmmss')$([IO.Path]::GetExtension($path))"
    Copy-Item -LiteralPath $path -Destination $backup
    Write-Output "  copie de sauvegarde : $backup"
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

# --- Démon partagé : un seul serveur nexus-relay pour toutes les sessions de Claude, Codex et Antigravity.
$port = 16181
# Réglages effectifs (fichier de ce PC, puis celui du dépôt ; variables développées) : une seule lecture, celle du démon.
$settings = (& $nativeExe --settings $project | Out-String) | ConvertFrom-RelayJson
if ($LASTEXITCODE) { throw 'Lecture des réglages du poste : échec (NexusRelay.exe --settings).' }
# Langue des consignes déployées (réglage `lang`) : anglais par défaut ; `fr` prend la copie française du dossier fr\.
$french = ([string]$settings.lang).Trim().ToLower().StartsWith('fr')
function Get-Localized([string]$relative) {
    $fr = Join-Path $project "fr\$relative"
    if ($french -and (Test-Path $fr)) { return $fr }
    return Join-Path $project $relative
}
# Consignes des chats du relais, lues par Codex dans ce dossier : jamais de question à l'utilisateur, qui ne les lit pas.
Copy-Item -Force (Get-Localized 'codex-projet\AGENTS.md') (Join-Path $HOME '.nexus\relay\codex-projet\AGENTS.md')
# Jeton partagé des démons Nexus (scripts\Resolve-NexusToken.ps1, règle commune du 30/10) : fichier explicite (-TokenFile,
# NEXUS_MCP_TOKEN_FILE, réglage token_file), sinon %USERPROFILE%\.nexus\mcp-token.txt ; réutilisé, jamais régénéré.
# Poste réglé sans -Register : jamais créé ici (un installeur lancé avant la synchro du fichier en créerait un autre).
. (Join-Path $project 'scripts\Resolve-NexusToken.ps1')
$legacyToken = Join-Path $state 'token.txt'  # jeton propre au relais (1.9.0 à 1.10.1), repris dans le fichier partagé
$explicitToken = @($TokenFile, $env:NEXUS_MCP_TOKEN_FILE, $settings.token_file) | Where-Object { $_ -and [IO.Path]::GetFullPath($_) -ne [IO.Path]::GetFullPath($legacyToken) }
if ($Register -or @($explicitToken | Where-Object { Test-Path -LiteralPath $_ }).Count) {
    $tokenFile = Resolve-NexusTokenFile -Explicit @($explicitToken) -UserHome $env:USERPROFILE -Legacy $legacyToken
    $local = Read-LocalSettings
    if ($local.PSObject.Properties['token_file'] -and [IO.Path]::GetFullPath([string]$local.token_file) -eq [IO.Path]::GetFullPath($legacyToken)) {
        $local.PSObject.Properties.Remove('token_file')  # plus de jeton propre au relais
        [IO.File]::WriteAllText($settingsPath, ($local | ConvertTo-Json -Depth 10), $utf8)
    }
} else { $tokenFile = $null }
# Périmètre du pack Nexus : IA choisies par l'utilisateur ; les autres ne sont jamais touchées.
$packTargets = if ($null -ne $env:NEXUS_PACK_TARGETS) { @($env:NEXUS_PACK_TARGETS -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) } else { $null }
# Hôte (installation publique seulement, et seulement s'il n'est pas déjà réglé : un choix de l'utilisateur reste).
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
        $local = Read-LocalSettings
        $local | Add-Member -NotePropertyName host -NotePropertyValue $chosenHost -Force
        [IO.File]::WriteAllText($settingsPath, ($local | ConvertTo-Json -Depth 10), $utf8)
        $settings | Add-Member -NotePropertyName host -NotePropertyValue $chosenHost -Force
        Write-Output "Hôte du relais : $chosenHost (seul à ouvrir un échange ; réglage host de $settingsPath)."
    }
}
if (-not $tokenFile -or -not (Test-Path $tokenFile)) {
    throw "Jeton absent : régler token_file dans $settingsPath, -TokenFile ou NEXUS_MCP_TOKEN_FILE (ou installer avec -Register)."
}
$token = (Get-Content $tokenFile -Raw).Trim()
function Get-Url([string]$agent) { "http://127.0.0.1:$port/mcp?agent=$agent" }
$daemonEnv = [ordered]@{ NEXUS_MCP_TOKEN_FILE = (Resolve-Path $tokenFile).Path }  # lu par le démon
# Réglages du poste passés au démon (absents : pas de coffre, pas de routeur devant Codex)
if ($settings.vaults) { $daemonEnv.NEXUS_RELAY_VAULTS = @($settings.vaults) -join [IO.Path]::PathSeparator }
if ($settings.codex_router) { $daemonEnv.NEXUS_RELAY_CODEX_ROUTER = [string]$settings.codex_router }
if ($settings.codex_router_start) { $daemonEnv.NEXUS_RELAY_CODEX_ROUTER_START = @($settings.codex_router_start) -join ';' }
if ($settings.host) { $daemonEnv.NEXUS_RELAY_HOST = [string]$settings.host }
if ($settings.lang) { $daemonEnv.NEXUS_RELAY_LANG = [string]$settings.lang }
foreach ($entry in @{codex_workspace='NEXUS_RELAY_CODEX_WORKSPACE'; codex_project='NEXUS_RELAY_CODEX_PROJECT'; codex_project_id='NEXUS_RELAY_CODEX_PROJECT_ID'; wake_since='NEXUS_RELAY_WAKE_SINCE'}.GetEnumerator()) { if ($settings.($entry.Key)) { $daemonEnv[$entry.Value] = [string]$settings.($entry.Key) } }
# Boîte épinglée : le démon lit celle où ce script dépose ses demandes, quel que soit l'environnement de son lanceur.
$daemonEnv.NEXUS_RELAY_HOME = $mailbox
$daemonEnv.NEXUS_RELAY_SOURCE_ROOT = $project
$priorEnvironment = @{}
try {
    foreach ($key in @('NEXUS_RELAY_HOME','NEXUS_RELAY_CODEX_WORKSPACE','NEXUS_RELAY_CODEX_PROJECT','NEXUS_RELAY_CODEX_PROJECT_ID')) {
        $priorEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, [string]$daemonEnv[$key], 'Process')
    }
    & $nativeExe --register-known-relay-chats | Out-Null
    if ($LASTEXITCODE) { throw 'Vérification des anciens chats Relay en échec ; aucune appropriation par dossier seul.' }
} finally { foreach ($key in $priorEnvironment.Keys) { [Environment]::SetEnvironmentVariable($key, $priorEnvironment[$key], 'Process') } }
# restart_request : la demande de relance que suit le démon. Un superviseur la prend juste avant de le lancer (avant de
# relire la spec) : ce lancement la satisfait, le démon neuf ne se relance pas aussitôt.
$daemonRequest = Join-Path $mailbox 'daemon-restart.request'
# Native supervisors read exe; do not keep the retired Python alias in the spec.
$spec = [ordered]@{ port = $port; exe = $nativeExe; args = @(); env = $daemonEnv; restart_request = $daemonRequest }
$specChanged = Write-IfChanged $specPath ($spec | ConvertTo-Json -Depth 5)
# Réglages relus (host et token_file viennent peut-être d'y être écrits) ; seul le moteur natif existe désormais.
$currentSettings = Read-LocalSettings
foreach ($pair in @{ runtime = 'native'; installed_source = $project; native_root = (Split-Path $versions) }.GetEnumerator()) {
    $currentSettings | Add-Member -NotePropertyName $pair.Key -NotePropertyValue $pair.Value -Force
}
[IO.File]::WriteAllText($settingsPath, ($currentSettings | ConvertTo-Json -Depth 10), $utf8)
# Nouveau code : demande de relance au démon, jamais d'arrêt forcé. Il ne se relance que si son code a changé, une fois
# aucun appel en cours, et le superviseur le relance. L'identité du processus (exécutable, port, PID et naissance) a été
# vérifiée avant tout changement ; un processus inconnu n'est jamais arrêté.
$forced = $specChanged -or "$(Get-Content $daemonRequest -Raw -ErrorAction SilentlyContinue)".StartsWith('force')
$targetFingerprint = "sha256:$((Get-FileHash -LiteralPath ([IO.Path]::ChangeExtension($nativeExe, '.dll')) -Algorithm SHA256).Hash.ToLowerInvariant()) "
$null = Request-RelayRestart -RequestPath $daemonRequest -Owner $daemonOwner -Kind $(if ($forced) { 'force' } else { 'code' }) -Fingerprint $targetFingerprint
# Pareil pour le sidecar d'Antigravity : il sort entre deux livraisons si son code a changé, Antigravity le relance.
$null = Request-RelayRestart -RequestPath (Join-Path $mailbox 'sidecar-restart.request') -Owner $sidecarOwner -Kind $(if ($specChanged) { 'force' } else { 'code' }) -Fingerprint $targetFingerprint
# Sans superviseur des démons Nexus : tâche « Nexus Relay » à l'ouverture de session, qui lance le petit superviseur du
# relais (NexusRelay.exe --daemon : lit la spec ci-dessus, relance le démon quand il s'arrête).
if (-not (Get-ScheduledTask -TaskName 'Nexus MCP Daemons' -ErrorAction SilentlyContinue)) {
    $taskName = 'Nexus Relay'
    $action = New-ScheduledTaskAction -Execute $nativeExe -Argument '--daemon' -WorkingDirectory $state
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settingsTask = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Hidden
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settingsTask `
            -Description 'Nexus Relay : démon MCP local de Claude, Codex et Antigravity (127.0.0.1:16181).' | Out-Null
        Write-Output "Tâche planifiée « $taskName » créée (ouverture de session)."
    } elseif (@($existing.Actions)[0].Execute -ne $nativeExe) {
        # Superviseur d'une version précédente (Python, ou autre version native) : remplacé par celui de cette version.
        Set-ScheduledTask -TaskName $taskName -Action $action | Out-Null
        if ($existing.State -eq 'Running') { Stop-ScheduledTask -TaskName $taskName }
        Write-Output "Tâche planifiée « $taskName » mise à jour."
    }
    if ((Get-ScheduledTask -TaskName $taskName).State -ne 'Running') { Start-ScheduledTask -TaskName $taskName }
}

# --- Antigravity : plugin nexus-relay (serveur MCP, skill, sidecar) et activation du sidecar
$agyRoot = Join-Path $HOME '.gemini\config'
if ($null -ne $packTargets -and $packTargets -notcontains 'antigravity') {
    Write-Output 'Antigravity : hors du périmètre choisi (NEXUS_PACK_TARGETS), non touché.'
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
        command = $nativeExe
        args = @('--sidecar')
        env = [ordered]@{ NEXUS_RELAY_HOME = $mailbox; NEXUS_RELAY_HOST = [string]$settings.host; NEXUS_RELAY_WAKE_SINCE = [string]$settings.wake_since; NEXUS_RELAY_SOURCE_ROOT = $project }
        restart_policy = 'always'
    }
    if (Write-IfChanged (Join-Path $plugin 'sidecars\relay\sidecar.json') ($sidecar | ConvertTo-Json -Depth 10)) { $copied++ }
    Write-Output "Antigravity : plugin nexus-relay $(if ($copied) { "installé ($copied fichier(s))" } else { 'déjà à jour' }) dans $plugin."
    $agyResult = "plugin installed in $plugin"

    # Projet où agentapi crée les conversations : « Nexus Relay », à part des conversations de l'utilisateur, sur un
    # dossier vide hors de tout dossier synchronisé ; créé ici s'il manque (réglages vides : ceux d'Antigravity s'appliquent).
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
        Write-Output "Antigravity : projet « $relayProjectName » créé ($relayFolder)."
    }
    # Sidecar activé dans config.json (doc officielle : sidecars.<id>.enabled + projectId). Rien d'autre n'est touché.
    $configPath = Join-Path $agyRoot 'config.json'
    $config = if (Test-Path $configPath) { [IO.File]::ReadAllText($configPath) | ConvertFrom-RelayJson } else { [PSCustomObject]@{} }
    $activation = Set-AntigravityRelayActivation -Config $config -ProjectId $projectId
    if ($activation.Disabled) { Write-Output 'Antigravity : choix de désactivation du plugin ou du sidecar conservé.' }
    elseif ($activation.Changed) {
        if (Test-Path $configPath) { Save-Backup $configPath 'antigravity-config' }
        [IO.File]::WriteAllText($configPath, ($activation.Config | ConvertTo-Json -Depth 100), $utf8)
        Write-Output "Antigravity : plugin et sidecar nexus-relay/relay activés (projet $projectId), rechargés à chaud par Antigravity."
    } else { Write-Output "Antigravity : plugin et sidecar nexus-relay/relay déjà activés (projet $projectId)." }
}

# --- Hook d'édition chez Codex et Antigravity (moteur par son chemin stable) : refus d'un fichier réservé par un autre
#     agent, réservation automatique sinon. Claude Code l'a par le plugin nexus-relay.
& $nativeExe --register-hooks --backups $backups --command $stableExe | Write-Output
if ($LASTEXITCODE) { throw 'Hook d''édition incomplet (voir ci-dessus).' }
# Anciens relais de hooks (PowerShell vers Python ou natif) : plus aucune déclaration ne les appelle.
Remove-Item -LiteralPath (Join-Path $state 'hooks') -Recurse -Force -ErrorAction SilentlyContinue

if (-not $SkipTests -and (Test-Path (Join-Path $project 'test.ps1')) -and (Test-Path $nativeCsproj)) {
    & (Get-Process -Id $PID).Path -NoProfile -File (Join-Path $project 'test.ps1')
    if ($LASTEXITCODE) { throw 'test.ps1 échoue : installation incomplète.' }
}

[IO.File]::WriteAllText((Join-Path $state 'installed.stamp'), "$nativeKey $((Get-FileHash (Join-Path $project 'install.ps1')).Hash)", $utf8)

# --- Installation publique : jeton dans la variable utilisateur NEXUS_MCP_TOKEN (lue par Claude Code et Codex, jamais
#     écrite dans leurs fichiers), puis serveur déclaré aux clients trouvés.
if ($Register) {
    if (Set-NexusTokenVariable -Token $token) { Write-Output 'Variable utilisateur NEXUS_MCP_TOKEN posée (redémarrer Claude Code et Codex pour la voir).' }
    $env:NEXUS_MCP_TOKEN = $token
    $agyArgs = if ($agyResult) { @('--antigravity', $agyResult) } else { @() }  # 5.1 : un argument vide disparaît
    & $nativeExe --register-clients --port $port --backups $backups @agyArgs | Write-Output
    if ($LASTEXITCODE) { throw 'Déclaration aux clients incomplète (voir ci-dessus).' }
}
# pack Nexus : la ligne NEXUS_PACK_RESULT reste la dernière
if (-not ($Register -and $env:NEXUS_PACK -eq '1')) { Write-Output 'Nexus Relay installé avec succès.' }
