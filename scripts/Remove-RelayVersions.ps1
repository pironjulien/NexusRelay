# Old engine folders (%LOCALAPPDATA%\NexusRelay\versions\<fingerprint>, ~\.nexus\nexus-relay\bin.old-*): removed only
# once no process uses them. A running .NET engine loads its DLLs from its folder on demand (NexusFree 1.3.3): deleting
# the files it has not loaded yet leaves it answering every call with an empty error.
# Three checks, any of which keeps a folder whole until a later install:
# - its engine.lock (written by install.ps1 in each version) is held: every engine opens the one beside it for its whole
#   life without delete sharing (native\EngineLock.cs), whatever spelling of the path started it (junction, MSIX
#   redirection, elevated view);
# - a running NexusRelay.exe has its image in it, read by QueryFullProcessImageNameW (also an elevated process of the
#   same user); when one running NexusRelay.exe cannot be read at all, no folder is removed;
# - Windows refuses to rename the folder aside (a file in it is open). This is only an extra signal: Windows does
#   rename the folder of a running program that keeps no file in it open (a single-file executable, mapped only).
# Only a folder renamed aside, which no pointer names any more, is deleted.

if (-not ('NexusRelayProcessImage' -as [type])) {
    Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices; using System.Text;
public static class NexusRelayProcessImage {
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr OpenProcess(uint access, bool inherit, int id);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")] static extern bool GetExitCodeProcess(IntPtr handle, out uint code);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool QueryFullProcessImageNameW(IntPtr process, uint flags, StringBuilder name, ref uint size);
    // Image path of a process; "" once it has exited; null while it runs but cannot be read.
    public static string Read(int id) {
        IntPtr handle = OpenProcess(0x1000, false, id);  // PROCESS_QUERY_LIMITED_INFORMATION
        if (handle == IntPtr.Zero) return Marshal.GetLastWin32Error() == 87 ? "" : null;  // 87: no such process
        try {
            var name = new StringBuilder(32768); uint size = (uint)name.Capacity;
            if (QueryFullProcessImageNameW(handle, 0, name, ref size)) return name.ToString(0, (int)size);
            uint code; return GetExitCodeProcess(handle, out code) && code != 259 ? "" : null;  // 259: STILL_ACTIVE
        } finally { CloseHandle(handle); }
    }
}
'@
}

# Image paths of the running NexusRelay.exe processes; Unknown when one of them runs but cannot be read.
function Get-RelayEngineImages {
    $paths = [Collections.Generic.List[string]]::new(); $unknown = $false
    foreach ($process in @(Get-Process -Name NexusRelay -ErrorAction SilentlyContinue)) {
        $path = [NexusRelayProcessImage]::Read($process.Id)
        if ($null -eq $path) { $unknown = $true } elseif ($path) { $paths.Add($path) }
    }
    [pscustomobject]@{ Paths = $paths.ToArray(); Unknown = $unknown }
}

# Removes one old engine folder unless it is in use; returns $true when it is gone.
function Remove-RelayFolderIfUnused {
    param([Parameter(Mandatory=$true)][string]$Folder, [Parameter(Mandatory=$true)]$Images)
    if ($Images.Unknown) { return $false }
    $prefix = $Folder.TrimEnd('\') + '\'
    if ($Images.Paths | Where-Object { $_.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) }) { return $false }
    $lockPath = Join-Path $Folder 'engine.lock'
    if (Test-Path -LiteralPath $lockPath -PathType Leaf) {
        # Exclusive open: refused while an engine holds the lock. It is closed before the rename (Windows never renames
        # a folder with an open file in it), and an engine starting meanwhile holds it again, which refuses the rename.
        try { [IO.File]::Open($lockPath, 'Open', 'ReadWrite', 'None').Dispose() } catch { return $false }
    }
    $aside = if ($Folder -like '*.removing') { $Folder } else { "$($Folder.TrimEnd('\')).$PID.removing" }
    if ($aside -ne $Folder) {
        try { [IO.Directory]::Move($Folder, $aside) } catch { return $false }  # in use: kept whole
    }
    Remove-Item -LiteralPath $aside -Recurse -Force -ErrorAction SilentlyContinue  # anything left goes next time
    return -not (Test-Path -LiteralPath $aside)
}

function Remove-StaleRelayVersions {
    param([Parameter(Mandatory=$true)][string]$Versions, [Parameter(Mandatory=$true)][string]$Keep, $Images = (Get-RelayEngineImages))
    if ($Images.Unknown) { return }  # a running engine whose folder cannot be known: everything stays
    foreach ($old in Get-ChildItem -LiteralPath $Versions -Directory -ErrorAction SilentlyContinue) {
        if ($old.Name -eq $Keep) { continue }  # the version just installed, compared by fingerprint, never by path
        $null = Remove-RelayFolderIfUnused -Folder $old.FullName -Images $Images
    }
}
