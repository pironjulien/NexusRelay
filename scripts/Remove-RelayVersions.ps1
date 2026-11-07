# Old engine versions (%LOCALAPPDATA%\NexusRelay\versions\<fingerprint>): removed only once no process uses them.
# A running .NET engine loads its DLLs from its folder on demand (NexusFree 1.3.3): deleting the files it has not loaded
# yet leaves it answering every call with an empty error. The process list cannot tell reliably (an elevated process
# hides its path, an MSIX-redirected or aliased path does not match the folder), so the file system decides: a folder is
# first renamed aside, which Windows refuses while a running engine holds files in it; only a folder renamed aside,
# which no pointer names any more, is deleted. A folder in use stays whole until a later install.
function Remove-StaleRelayVersions {
    param([Parameter(Mandatory=$true)][string]$Versions, [Parameter(Mandatory=$true)][string]$Keep)
    foreach ($old in Get-ChildItem -LiteralPath $Versions -Directory -ErrorAction SilentlyContinue) {
        if ($old.Name -eq $Keep) { continue }  # the version just installed, compared by fingerprint, never by path
        $aside = if ($old.Name -like '*.removing') { $old.FullName } else { Join-Path $Versions "$($old.Name).$PID.removing" }
        if ($aside -ne $old.FullName) {
            try { [IO.Directory]::Move($old.FullName, $aside) } catch { continue }  # in use: kept whole
        }
        Remove-Item -LiteralPath $aside -Recurse -Force -ErrorAction SilentlyContinue  # anything left goes next time
    }
}
