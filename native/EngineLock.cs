namespace NexusRelay;

/// <summary>
/// Marks this engine's folder as in use for the whole life of the process. install.ps1 writes an empty engine.lock in
/// each versioned folder; every engine opens the one beside it without delete sharing and never closes it, so the
/// cleanup of old versions (scripts\Remove-RelayVersions.ps1) sees the folder in use whatever spelling of the path
/// started the process (junction, MSIX redirection, elevated view), and Windows refuses to rename or delete it.
/// A folder without the file (development build) or a lock taken by the cleanup at that instant: nothing held.
/// </summary>
internal static class EngineLock
{
    public const string FileName = "engine.lock";
    private static FileStream? held;

    public static void Hold(string? folder = null)
    {
        if (held is not null) return;
        try { held = new FileStream(Path.Join(folder ?? AppContext.BaseDirectory, FileName), FileMode.Open, FileAccess.Read, FileShare.ReadWrite); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { /* no lock file, or a cleanup holds it */ }
    }
}
