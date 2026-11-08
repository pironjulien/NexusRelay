# Installe NexusRelay sur ce PC depuis ce dossier (idempotent : à relancer sans risque après une mise à jour du dépôt).
# Un amorceur : toute l'installation est dans le moteur (NexusRelay.exe --install, native\Installer.cs).
#  - clone (native\NexusRelay.csproj) : moteur compilé par dotnet publish (.NET 10 SDK, sorties de compilation dans
#    ~\.nexus\build\nexusrelay) dans sa copie immuable %LOCALAPPDATA%\NexusRelay\versions\<empreinte des sources>, puis
#    tests hors ligne (test.ps1, sauf -SkipTests) avant toute installation ;
#  - release décompressée (bin\NexusRelay.exe, autonome) : le moteur de bin\ (une installation lancée par le get.ps1
#    d'une version antérieure arrive ici) ;
#  puis NexusRelay.exe --install --source <ce dossier> : moteur, copie stable ~\.nexus\nexus-relay\bin, réglages, jeton,
#  démon et tâche planifiée, plugin et sidecar d'Antigravity, hooks d'édition ; -Register (installation publique) :
#  variable NEXUS_MCP_TOKEN et serveur déclaré à Claude Code et Codex ; -Mirror : ce dossier devient
#  %LOCALAPPDATA%\NexusRelay\app. En cas d'échec, l'installation précédente est remise en place. Voir README.
# Windows PowerShell 5.1 ou PowerShell 7.
# Usage : powershell -NoProfile -File install.ps1 [-SkipTests] [-TokenFile <chemin>] [-Register] [-Mirror] [-PrepareOnly] [-PromoteOnly]
param([switch]$SkipTests, [string]$TokenFile, [switch]$Register, [switch]$PrepareOnly, [switch]$PromoteOnly, [switch]$Mirror)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)  # sorties du moteur et chemins accentués
# Messages de cet amorceur : anglais par défaut, français quand Windows est affiché en français (règle des produits Nexus).
$frenchUi = [Globalization.CultureInfo]::CurrentUICulture.TwoLetterISOLanguageName -eq 'fr'
function Tr([string]$en, [string]$fr) { if ($frenchUi) { $fr } else { $en } }

# Lance le moteur (programme Windows : PowerShell ne l'attendrait pas) sans fenêtre, affiche sa sortie au fil de l'eau,
# rend son code de sortie ; son entrée standard est fermée aussitôt.
function Invoke-Engine([string]$Exe, [string[]]$Arguments) {
    $info = New-Object Diagnostics.ProcessStartInfo $Exe
    $info.Arguments = (($Arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join ' ')
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [Text.Encoding]::UTF8
    $process = [Diagnostics.Process]::Start($info)
    $process.StandardInput.Close()
    $errors = $process.StandardError.ReadToEndAsync()
    while ($null -ne ($line = $process.StandardOutput.ReadLine())) { Write-Host $line }
    $process.WaitForExit()
    if ($errors.Result.Trim()) { Write-Host $errors.Result.Trim() }
    return $process.ExitCode
}

$project = $PSScriptRoot
$nativeCsproj = Join-Path $project 'native\NexusRelay.csproj'
if (Test-Path -LiteralPath $nativeCsproj) {
    if (-not (Get-Command dotnet.exe -ErrorAction SilentlyContinue)) { throw (Tr '.NET 10 SDK required to build the native engine (native\).' '.NET 10 SDK requis pour compiler le moteur natif (native\).') }
    # Copie immuable par empreinte des sources : une publication n'écrase jamais une DLL en service.
    $sources = @(Get-ChildItem (Join-Path $project 'native') -File | Where-Object Extension -in @('.cs', '.csproj', '.json')) + @(Get-Item (Join-Path $project 'Directory.Build.props'))
    $hashes = $sources.FullName | Sort-Object | ForEach-Object { (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash }
    $digest = [Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes(($hashes -join '')))
    $nativeKey = (-join ($digest | ForEach-Object { $_.ToString('x2') })).Substring(0, 16)
    $nativeOut = Join-Path $env:LOCALAPPDATA "NexusRelay\versions\$nativeKey"
    if (-not (Test-Path -LiteralPath (Join-Path $nativeOut 'publish.complete'))) {
        # obj et bin intermédiaires : ~\.nexus\build\nexusrelay (Directory.Build.props), jamais dans OneDrive.
        & dotnet.exe publish $nativeCsproj -c Release -o $nativeOut --nologo -v q
        if ($LASTEXITCODE) { throw (Tr 'dotnet publish (native engine) failed.' 'dotnet publish (moteur natif) a échoué.') }
        [IO.File]::WriteAllText((Join-Path $nativeOut 'engine.lock'), '', [Text.UTF8Encoding]::new($false))
        [IO.File]::WriteAllText((Join-Path $nativeOut 'publish.complete'), $nativeKey, [Text.UTF8Encoding]::new($false))
    }
    $engine = Join-Path $nativeOut 'NexusRelay.exe'
    # Tests hors ligne avant toute installation : un échec ne change rien au poste.
    if (-not ($SkipTests -or $PrepareOnly -or $PromoteOnly) -and (Test-Path (Join-Path $project 'test.ps1'))) {
        & (Get-Process -Id $PID).Path -NoProfile -File (Join-Path $project 'test.ps1')
        if ($LASTEXITCODE) { throw (Tr 'test.ps1 failed: nothing installed.' 'test.ps1 échoue : rien n''est installé.') }
    }
} elseif (Test-Path -LiteralPath (Join-Path $project 'bin\NexusRelay.exe')) {
    $engine = Join-Path $project 'bin\NexusRelay.exe'
} else { throw (Tr "Native engine missing: neither native\NexusRelay.csproj nor bin\NexusRelay.exe in $project." "Moteur natif absent : ni native\NexusRelay.csproj ni bin\NexusRelay.exe dans $project.") }

$arguments = @('--install', '--source', $project)
if ($Register) { $arguments += '--register' }
if ($Mirror) { $arguments += '--mirror' }
if ($PrepareOnly) { $arguments += '--prepare-only' }
if ($PromoteOnly) { $arguments += '--promote-only' }
if ($TokenFile) { $arguments += @('--token-file', $TokenFile) }
exit (Invoke-Engine $engine $arguments)
