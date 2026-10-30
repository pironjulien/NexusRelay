# NexusRelay one-line installer for Windows 10/11 (Windows PowerShell 5.1 or PowerShell 7), no admin rights, no Python:
#   irm https://raw.githubusercontent.com/pironjulien/NexusRelay/main/get.ps1 | iex
# Downloads NexusRelay-win-x64.zip or NexusRelay-win-arm64.zip (this PC's architecture) from the latest GitHub release,
# checks it against the release's SHA256SUMS.txt, mirrors it into %LOCALAPPDATA%\NexusRelay\app (the same folder on
# every update), then runs install.ps1 -Register: self-contained native engine (no .NET to install), token, background
# daemon (scheduled task "Nexus Relay"), Antigravity plugin and sidecar, MCP server declared to Claude Code and Codex.
# Run it again to update. Each release file also has a Sigstore build provenance attestation:
#   gh attestation verify NexusRelay-win-x64.zip --repo pironjulien/NexusRelay
# NEXUS_RELAY_SOURCE (a local folder: a checkout, built with the .NET 10 SDK, or an extracted release) installs that
# copy instead. NEXUS_PACK=1 (the Nexus pack, get-nexus.ps1): no question, no wait, no window; exit code 0 on success;
# a last "NEXUS_PACK_RESULT ..." line (one word per AI client).
# Test variables: NEXUS_RELAY_ROOT (install folder instead of %LOCALAPPDATA%\NexusRelay), NEXUS_RELAY_DOWNLOAD_ONLY=1
# (download, check and mirror only; nothing is installed nor declared).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is many times slower with its progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$root = if ($env:NEXUS_RELAY_ROOT) { $env:NEXUS_RELAY_ROOT } else { Join-Path $env:LOCALAPPDATA 'NexusRelay' }
$app = Join-Path $root 'app'
New-Item -ItemType Directory -Force $root | Out-Null

if ($env:NEXUS_RELAY_SOURCE) {
    $source = (Resolve-Path -LiteralPath $env:NEXUS_RELAY_SOURCE).Path
    Write-Output "Installing NexusRelay from $source..."
} else {
    # This PC's architecture (an x64 PowerShell emulated on ARM still gets the ARM64 build).
    $arch = $env:PROCESSOR_ARCHITEW6432
    if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
    $rid = switch ($arch) { 'ARM64' { 'win-arm64' } 'AMD64' { 'win-x64' } default { throw "Unsupported processor architecture: $arch (x64 or ARM64 needed)." } }
    # Latest tag from the releases/latest redirect (no GitHub API, so no rate limit).
    $request = [Net.WebRequest]::Create('https://github.com/pironjulien/NexusRelay/releases/latest')
    $request.Method = 'HEAD'
    $request.AllowAutoRedirect = $false
    try { $response = $request.GetResponse() } catch [Net.WebException] { $response = $_.Exception.Response }
    $location = if ($response) { [string]$response.Headers['Location'] } else { '' }
    if ($response) { $response.Close() }
    $tag = ($location -split '/tag/')[-1]
    if ($tag -notmatch '^v\d+\.\d+\.\d+$') { throw "Latest NexusRelay release not found (redirect: '$location')." }
    $name = "NexusRelay-$rid.zip"
    Write-Output "Downloading NexusRelay $tag ($rid)..."
    $download = Join-Path $root 'download'
    if (Test-Path -LiteralPath $download) { Remove-Item -LiteralPath $download -Recurse -Force }
    New-Item -ItemType Directory -Force $download | Out-Null
    $base = "https://github.com/pironjulien/NexusRelay/releases/download/$tag"
    $zip = Join-Path $download $name
    $sums = Join-Path $download 'SHA256SUMS.txt'
    Invoke-WebRequest -UseBasicParsing "$base/$name" -OutFile $zip
    Invoke-WebRequest -UseBasicParsing "$base/SHA256SUMS.txt" -OutFile $sums
    $expected = @(Get-Content -LiteralPath $sums | Where-Object { $_ -match "^([0-9a-fA-F]{64})\s+\*?$([regex]::Escape($name))$" } | ForEach-Object { $Matches[1].ToLowerInvariant() })
    if ($expected.Count -ne 1) { throw "$name is not listed in SHA256SUMS.txt of $tag." }
    $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected[0]) { throw "$name does not match its SHA-256 in SHA256SUMS.txt (expected $($expected[0]), got $actual)." }
    $source = Join-Path $download 'release'
    Expand-Archive -LiteralPath $zip -DestinationPath $source
}

# Mirror into the stable folder (files of an older version that no longer exist are removed).
& robocopy.exe $source $app /MIR /NFL /NDL /NJH /NJS /NP /XD .git | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copy to $app failed (robocopy $LASTEXITCODE)." }
Get-ChildItem -LiteralPath $app -Recurse -File | Unblock-File
if (-not $env:NEXUS_RELAY_SOURCE) { Remove-Item -LiteralPath (Join-Path $root 'download') -Recurse -Force }
if ($env:NEXUS_RELAY_DOWNLOAD_ONLY -eq '1') { Write-Output "NexusRelay mirrored into $app (download only)."; exit 0 }

$shell = (Get-Process -Id $PID).Path  # the same PowerShell that runs this script
& $shell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $app 'install.ps1') -SkipTests -Register
if ($LASTEXITCODE) { throw 'NexusRelay installation failed (see above).' }
if ($env:NEXUS_PACK -ne '1') {
    Write-Output ''
    Write-Output 'Last steps:'
    Write-Output '  1. Restart Claude Code and Codex (quit them completely) so they see NEXUS_MCP_TOKEN.'
    Write-Output '  2. Codex app, once: Add a project, name "Nexus Relay", folder %USERPROFILE%\.nexus\relay\codex-projet.'
    Write-Output '  3. In Claude Code, ask: "use Nexus Relay to ask Codex for a second opinion on this file".'
}
