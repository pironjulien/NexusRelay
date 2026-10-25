# NexusRelay one-line installer for Windows 10/11 (Windows PowerShell 5.1 or PowerShell 7), no admin rights:
#   irm https://raw.githubusercontent.com/pironjulien/NexusRelay/main/get.ps1 | iex
# Installs uv if missing (https://docs.astral.sh/uv/), downloads the latest NexusRelay release into
# %LOCALAPPDATA%\NexusRelay\app (the same folder on every update), then runs install.ps1 -Register: Python environment,
# token, background daemon (scheduled task "Nexus Relay"), Antigravity plugin and sidecar, MCP server declared to
# Claude Code and Codex. Run it again to update. NEXUS_RELAY_SOURCE (a local folder) installs that copy instead.
# NEXUS_PACK=1 (the Nexus pack, get-nexus.ps1): no question, no wait, no window; exit code 0 on success; a last
# "NEXUS_PACK_RESULT ..." line (one word per AI client).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is many times slower with its progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$root = Join-Path $env:LOCALAPPDATA 'NexusRelay'
$app = Join-Path $root 'app'
New-Item -ItemType Directory -Force $root | Out-Null

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host 'Installing uv (Python package manager, https://docs.astral.sh/uv/)...'
    $uvInstaller = Join-Path $root 'uv-install.ps1'
    Invoke-WebRequest -UseBasicParsing 'https://astral.sh/uv/install.ps1' -OutFile $uvInstaller
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $uvInstaller
    if ($LASTEXITCODE) { throw 'uv installation failed.' }
    Remove-Item -LiteralPath $uvInstaller
    $env:Path = (Join-Path $HOME '.local\bin') + ';' + $env:Path
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'uv is installed but not found: open a new terminal and run this command again.' }
}

if ($env:NEXUS_RELAY_SOURCE) {
    $source = (Resolve-Path -LiteralPath $env:NEXUS_RELAY_SOURCE).Path
    Write-Host "Installing NexusRelay from $source..."
} else {
    # Latest tag from the releases/latest redirect (no GitHub API, so no rate limit).
    $request = [Net.WebRequest]::Create('https://github.com/pironjulien/NexusRelay/releases/latest')
    $request.Method = 'HEAD'
    $request.AllowAutoRedirect = $false
    try { $response = $request.GetResponse() } catch [Net.WebException] { $response = $_.Exception.Response }
    $location = if ($response) { [string]$response.Headers['Location'] } else { '' }
    if ($response) { $response.Close() }
    $tag = ($location -split '/tag/')[-1]
    if ($tag -notmatch '^v\d+\.\d+\.\d+$') { throw "Latest NexusRelay release not found (redirect: '$location')." }
    Write-Host "Downloading NexusRelay $tag..."
    $download = Join-Path $root 'download'
    if (Test-Path -LiteralPath $download) { Remove-Item -LiteralPath $download -Recurse -Force }
    New-Item -ItemType Directory -Force $download | Out-Null
    $zip = Join-Path $download 'NexusRelay.zip'
    Invoke-WebRequest -UseBasicParsing "https://github.com/pironjulien/NexusRelay/archive/refs/tags/$tag.zip" -OutFile $zip
    Expand-Archive -LiteralPath $zip -DestinationPath $download
    $source = (Get-ChildItem -LiteralPath $download -Directory | Select-Object -First 1).FullName
}

# Mirror into the stable folder (files of an older version that no longer exist are removed).
& robocopy.exe $source $app /MIR /NFL /NDL /NJH /NJS /NP /XD .git | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copy to $app failed (robocopy $LASTEXITCODE)." }
Get-ChildItem -LiteralPath $app -Recurse -File | Unblock-File
if (-not $env:NEXUS_RELAY_SOURCE) { Remove-Item -LiteralPath (Join-Path $root 'download') -Recurse -Force }

$shell = (Get-Process -Id $PID).Path  # the same PowerShell that runs this script
& $shell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $app 'install.ps1') -SkipTests -Register
if ($LASTEXITCODE) { throw 'NexusRelay installation failed (see above).' }
if ($env:NEXUS_PACK -ne '1') {
    Write-Host ''
    Write-Host 'Last steps:'
    Write-Host '  1. Restart Claude Code and Codex (quit them completely) so they see NEXUS_MCP_TOKEN.'
    Write-Host '  2. Codex app, once: Add a project, name "Nexus Relay", folder %USERPROFILE%\.nexus\relay\codex-projet.'
    Write-Host '  3. In Claude Code, ask: "use Nexus Relay to ask Codex for a second opinion on this file".'
}
