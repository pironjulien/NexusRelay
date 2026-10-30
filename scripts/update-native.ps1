param([Parameter(Mandatory)][string]$Root)
$ErrorActionPreference = 'Stop'
$state = Join-Path $HOME '.nexus\nexus-relay'
$marker = Join-Path $state 'update.json'
$data = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
$data | Add-Member -NotePropertyName pid -NotePropertyValue $PID -Force
$data | Add-Member -NotePropertyName launch -NotePropertyValue 'started' -Force
[IO.File]::WriteAllText($marker, ($data | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
$env:NEXUS_PACK = '1'
& (Join-Path $Root 'get.ps1') *> (Join-Path $state 'update.log')
exit $LASTEXITCODE
