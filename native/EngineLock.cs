namespace NexusRelay;

/// <summary>
/// Marks this engine's versioned folder (%LOCALAPPDATA%\NexusRelay\versions\&lt;fingerprint&gt;) as in use for the whole
/// life of the process. The installer writes an empty engine.lock in each versioned folder; every engine opens the one
/// beside it without delete sharing and never closes it, so the cleanup of old versions (EngineFolders, InstallSupport.cs)
/// sees the folder in use whatever spelling of the path started the process (junction, MSIX redirection, elevated view).
/// Never the stable copy (~\.nexus\nexus-relay\bin, and its bin.new-* / bin.old-* siblings): every update replaces it,
/// so nothing may hold a file in it (1.13.10 copied the lock there, and a waiting Stop hook blocked the next update).
/// A folder without the file (development build) or a lock taken by the cleanup at that instant: nothing held.
/// </summary>
internal static class EngineLock
{
    public const string FileName = "engine.lock";
    private static FileStream? held;

    public static void Hold(string? folder = null)
    {
        if (held is not null) return;
        string directory = folder ?? AppContext.BaseDirectory;
        if (IsStableCopy(directory)) return;
        try { held = new FileStream(Path.Join(directory, FileName), FileMode.Open, FileAccess.Read, FileShare.ReadWrite); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { /* no lock file, or a cleanup holds it */ }
    }

    /// <summary>The installer's stable copy: a folder named bin or bin.* inside a folder named nexus-relay.</summary>
    public static bool IsStableCopy(string directory)
    {
        var info = new DirectoryInfo(Path.TrimEndingDirectorySeparator(Path.GetFullPath(directory)));
        return info.Parent is { } parent && parent.Name.Equals("nexus-relay", StringComparison.OrdinalIgnoreCase)
            && (info.Name.Equals("bin", StringComparison.OrdinalIgnoreCase) || info.Name.StartsWith("bin.", StringComparison.OrdinalIgnoreCase));
    }
}
