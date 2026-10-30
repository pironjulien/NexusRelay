# Shared MCP token of the Nexus daemons (common rule of 30/10/2026, the same for Relay, Android and Browser):
#  1. file of truth: the explicit file (-TokenFile, NEXUS_MCP_TOKEN_FILE, then the token_file setting), otherwise
#     %USERPROFILE%\.nexus\mcp-token.txt;
#  2. an existing non-empty file is reused, never regenerated; a missing one is created by exclusive creation (32 random
#     bytes in hex), so two installers run one after the other or at the same time end with the same token;
#  3. the user variable NEXUS_MCP_TOKEN is aligned on it (clients read ${NEXUS_MCP_TOKEN}, never a written token);
#  4. the daemon reads the same file (spec env NEXUS_MCP_TOKEN_FILE).
# Relay 1.9.0 to 1.10.1 created its own ~\.nexus\nexus-relay\token.txt: that one is no longer a file of truth; when the
# shared file does not exist yet, it is created with that token, so those installs keep their token.
function Resolve-NexusTokenFile {
    param([string[]]$Explicit, [string]$UserHome = $env:USERPROFILE, [string]$Legacy)
    $chosen = @($Explicit | Where-Object { $_ -and (-not $Legacy -or [IO.Path]::GetFullPath($_) -ne [IO.Path]::GetFullPath($Legacy)) }) | Select-Object -First 1
    $path = if ($chosen) { $chosen } else { Join-Path $UserHome '.nexus\mcp-token.txt' }
    $existing = if (Test-Path -LiteralPath $path) { ([IO.File]::ReadAllText($path)).Trim() } else { '' }
    if (-not $existing) {
        $token = if ($Legacy -and (Test-Path -LiteralPath $Legacy) -and ([IO.File]::ReadAllText($Legacy)).Trim().Length -ge 32) { ([IO.File]::ReadAllText($Legacy)).Trim() } else {
            $bytes = New-Object byte[] 32
            [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
            -join ($bytes | ForEach-Object { $_.ToString('x2') })
        }
        New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }  # empty file: never a token
        try {
            # Exclusive creation: the first installer wins, the other one reads its token.
            $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try { $data = [Text.UTF8Encoding]::new($false).GetBytes($token); $stream.Write($data, 0, $data.Length) } finally { $stream.Dispose() }
        } catch [IO.IOException] {
            foreach ($try in 1..20) { if (([IO.File]::ReadAllText($path)).Trim()) { break }; Start-Sleep -Milliseconds 100 }
        }
    }
    (Resolve-Path -LiteralPath $path).Path
}

function Set-NexusTokenVariable {
    param([string]$Token)
    if ([Environment]::GetEnvironmentVariable('NEXUS_MCP_TOKEN', 'User') -ne $Token) {
        [Environment]::SetEnvironmentVariable('NEXUS_MCP_TOKEN', $Token, 'User')
        return $true
    }
    return $false
}
