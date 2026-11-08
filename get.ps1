# NexusRelay installer for Windows 10/11 (Windows PowerShell 5.1 or PowerShell 7), no admin rights, no Python:
#   download get.ps1 and SHA256SUMS.txt from the latest release, check the hash, then run it as a file:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\get.ps1
#   (never run downloaded code in memory; the NexusSystem app of the Microsoft Store does the same with -File)
# A minimal starter: it downloads NexusRelay-win-x64.zip or NexusRelay-win-arm64.zip (this PC's architecture) from the
# latest GitHub release, checks it against the release's SHA256SUMS.txt and, when the GitHub CLI is signed in, its
# Sigstore build provenance, unpacks it, and starts the package's own engine: NexusRelay.exe --install does everything
# else (%LOCALAPPDATA%\NexusRelay\app and an immutable engine copy, token, background daemon with its scheduled task
# "NexusRelay", Antigravity plugin and sidecar, edit hooks, MCP server declared to Claude Code and Codex), and puts the
# previous installation back if it fails. Run it again to update; the daemon also updates itself (NexusRelay.exe --update).
# NEXUS_RELEASE_TAG: install that release (vX.Y.Z, or a development build vX.Y.Z-dev.N) instead of the latest.
# NEXUS_PACK=1 (the Nexus pack): no question, no wait, no window; exit code 0 on success; a last
# "NEXUS_PACK_RESULT ..." line (one word per AI client).
# NEXUS_RELAY_SOURCE: a local folder instead of a download, an unpacked release (bin\NexusRelay.exe) or a checkout
# (built by its install.ps1 with the .NET 10 SDK). NEXUS_RELAY_ROOT: another folder than %LOCALAPPDATA%\NexusRelay for
# the app copy and the download.
$ErrorActionPreference = 'Stop'
# A Windows PowerShell child of PowerShell 7 inherits Core-only module folders ahead of its own modules.
# Restore this host's standard modules first (Get-FileHash, Unblock-File), retaining user modules.
if ($PSVersionTable.PSEdition -eq 'Desktop') {
    $desktopModules = Join-Path $PSHOME 'Modules'
    $env:PSModulePath = (@($desktopModules) + @($env:PSModulePath -split ';' | Where-Object {
        $_ -and $_.TrimEnd('\') -ine $desktopModules.TrimEnd('\')
    })) -join ';'
}
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is many times slower with its progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$root = if ($env:NEXUS_RELAY_ROOT) { $env:NEXUS_RELAY_ROOT } else { Join-Path $env:LOCALAPPDATA 'NexusRelay' }
$repository = 'pironjulien/NexusRelay'
$firstNative = [version]'1.14.0'  # first release whose engine installs itself (--install)
New-Item -ItemType Directory -Force $root | Out-Null

# Runs the engine (a Windows program: PowerShell would not wait for it) without a window, shows its output as it
# comes, returns its exit code. Its standard input is closed at once.
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

# A checkout: its own install.ps1 builds the engine, then the engine installs (same steps as a release).
if ($env:NEXUS_RELAY_SOURCE -and (Test-Path -LiteralPath (Join-Path $env:NEXUS_RELAY_SOURCE 'native\NexusRelay.csproj'))) {
    $source = (Resolve-Path -LiteralPath $env:NEXUS_RELAY_SOURCE).Path
    Write-Host "Installing NexusRelay from $source..."
    & (Get-Process -Id $PID).Path -NoProfile -ExecutionPolicy Bypass -File (Join-Path $source 'install.ps1') -SkipTests -Register -Mirror
    exit $LASTEXITCODE
}

# Expected SHA-256 of one file in a SHA256SUMS.txt ("<hash>  <name>" lines).
function Get-ExpectedHash([string]$Sums, [string]$Name) {
    $expected = $null
    foreach ($line in ($Sums -split '\r?\n' | Where-Object { $_.Trim() })) {
        if ($line -notmatch '^([0-9a-fA-F]{64})\s+\*?(\S+)\s*$') { throw 'SHA256SUMS.txt has an invalid line: installation refused.' }
        if ($Matches[2] -ceq $Name) {
            if ($expected) { throw "SHA256SUMS.txt lists $Name twice: installation refused." }
            $expected = $Matches[1].ToLowerInvariant()
        }
    }
    if (-not $expected) { throw "$Name is absent from SHA256SUMS.txt: installation refused." }
    return $expected
}

# Build provenance when the GitHub CLI is installed and signed in; a failed check refuses the package. gh writes to
# stderr when it is not signed in: under Windows PowerShell 5.1 with ErrorActionPreference Stop, a redirected stderr
# line of a native command is a terminating error, so gh runs with Continue here.
function Confirm-Provenance([string]$File) {
    $gh = Get-Command gh.exe -ErrorAction SilentlyContinue
    if (-not $gh) { return }
    $preference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $gh.Source auth status *> $null
        if ($LASTEXITCODE -ne 0) { Write-Host 'Build provenance not checked (GitHub CLI not signed in); SHA-256 checked.'; return }
        & $gh.Source attestation verify $File --repo $repository --signer-repo pironjulien/NexusSystem *> $null
        $verified = $LASTEXITCODE -eq 0
    } finally { $ErrorActionPreference = $preference }
    if (-not $verified) { throw "Build provenance of $(Split-Path -Leaf $File) not verified (gh attestation verify): installation refused." }
    Write-Host 'Build provenance verified.'
}

$work = Join-Path $root ('download\' + [Guid]::NewGuid().ToString('N').Substring(0, 12))
$code = 1
try {
    if ($env:NEXUS_RELAY_SOURCE) {
        $package = (Resolve-Path -LiteralPath $env:NEXUS_RELAY_SOURCE).Path
        Write-Host "Installing NexusRelay from $package..."
    } else {
        # This PC's architecture (an x64 PowerShell emulated on ARM still gets the ARM64 build).
        $arch = $env:PROCESSOR_ARCHITEW6432
        if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
        $rid = switch ($arch) { 'ARM64' { 'win-arm64' } 'AMD64' { 'win-x64' } default { throw "Unsupported processor architecture: $arch (x64 or ARM64 needed)." } }
        if ($env:NEXUS_RELEASE_TAG) {
            if ($env:NEXUS_RELEASE_TAG -notmatch '^v\d+\.\d+\.\d+(-dev\.\d+)?$') { throw "NEXUS_RELEASE_TAG is not a release tag: $env:NEXUS_RELEASE_TAG" }
            $tag = $env:NEXUS_RELEASE_TAG
        } else {
            # Latest tag from the releases/latest redirect (no GitHub API, so no rate limit).
            $request = [Net.WebRequest]::Create("https://github.com/$repository/releases/latest")
            $request.Method = 'HEAD'
            $request.AllowAutoRedirect = $false
            try { $response = $request.GetResponse() } catch [Net.WebException] { $response = $_.Exception.Response }
            $location = if ($response) { [string]$response.Headers['Location'] } else { '' }
            if ($response) { $response.Close() }
            $tag = ($location -split '/tag/')[-1]
            if ($tag -notmatch '^v\d+\.\d+\.\d+$') { throw "Latest NexusRelay release not found (redirect: '$location')." }
        }
        if ([version]($tag.TrimStart('v') -replace '-dev\.\d+$') -lt $firstNative) { throw "NexusRelay $tag predates this installer: use the get.ps1 of that release." }
        $name = "NexusRelay-$rid.zip"
        Write-Host "Downloading NexusRelay $tag ($rid)..."
        New-Item -ItemType Directory -Force $work | Out-Null
        $base = "https://github.com/$repository/releases/download/$tag"
        $zip = Join-Path $work $name
        Invoke-WebRequest -UseBasicParsing "$base/$name" -OutFile $zip
        $sums = [Text.Encoding]::UTF8.GetString((Invoke-WebRequest -UseBasicParsing "$base/SHA256SUMS.txt").RawContentStream.ToArray())
        $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne (Get-ExpectedHash $sums $name)) { throw "$name does not match its SHA-256 in SHA256SUMS.txt: the download is not the published package." }
        Write-Host 'Package checked against SHA256SUMS.txt.'
        Confirm-Provenance $zip
        $package = Join-Path $work 'package'
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, $package)
        Get-ChildItem -LiteralPath $package -Recurse -File | Unblock-File
    }
    $engine = Join-Path $package 'bin\NexusRelay.exe'
    if (-not (Test-Path -LiteralPath $engine)) { throw 'Not a NexusRelay release package (bin\NexusRelay.exe missing).' }
    $code = Invoke-Engine $engine @('--install', '--source', $package, '--mirror', '--register')
} finally {
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
    $downloads = Join-Path $root 'download'
    if ((Test-Path -LiteralPath $downloads) -and -not (Get-ChildItem -LiteralPath $downloads -Force)) { Remove-Item -LiteralPath $downloads -Force }
}
if ($code) { Write-Host "NexusRelay installation failed (exit code $code)." }
exit $code
