using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Xml.Linq;
using Microsoft.Win32.SafeHandles;

namespace NexusRelay;

/// <summary>Messages of the installer: English, and French when Windows is displayed in French (rule of the public
/// Nexus products). The `lang` setting only chooses the language of the instructions deployed for the agents.</summary>
public static class InstallText
{
    public static bool French { get; set; } = CultureInfo.CurrentUICulture.TwoLetterISOLanguageName == "fr";
    public static string Tr(string en, string fr) => French ? fr : en;
}

/// <summary>Files the installer writes: UTF-8 without BOM, JSON indented with paths unescaped.</summary>
public static class InstallFiles
{
    public static readonly UTF8Encoding Utf8 = new(false);
    public static readonly JsonSerializerOptions Json = new() { WriteIndented = true, Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    public static string ToJson(JsonNode node) => node.ToJsonString(Json);

    public static JsonObject? ReadObject(string path)
    {
        try { return File.Exists(path) ? JsonNode.Parse(File.ReadAllText(path, Encoding.UTF8)) as JsonObject : null; }
        catch (JsonException) { return null; }
    }

    /// <summary>Writes through a temporary file then one rename, so a reader never sees half a file.</summary>
    public static void WriteAtomic(string path, string text)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        string temporary = $"{path}.{Environment.ProcessId.ToString(CultureInfo.InvariantCulture)}.tmp";
        File.WriteAllText(temporary, text, Utf8);
        File.Move(temporary, path, overwrite: true);
    }

    /// <summary>SHA-256 of a file, upper-case hexadecimal (as Get-FileHash prints it).</summary>
    public static string Sha256(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream));
    }

    /// <summary>Removes the "downloaded from the Internet" mark of every file of a tree, as Unblock-File does.</summary>
    public static void Unblock(string folder)
    {
        foreach (string file in Directory.EnumerateFiles(folder, "*", SearchOption.AllDirectories))
        {
            try { File.Delete(file + ":Zone.Identifier"); }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException or NotSupportedException) { /* no mark */ }
        }
    }

    /// <summary>Deletes a folder tree, read-only files included; false when something is still there.</summary>
    public static bool TryDeleteTree(string folder)
    {
        for (int attempt = 0; attempt < 10 && Directory.Exists(folder); attempt++)
        {
            try
            {
                foreach (string file in Directory.EnumerateFiles(folder, "*", SearchOption.AllDirectories)) File.SetAttributes(file, FileAttributes.Normal);
                Directory.Delete(folder, recursive: true);
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException) { Thread.Sleep(200); }
        }
        return !Directory.Exists(folder);
    }

    /// <summary>Renames a folder, retrying for up to five seconds: a scanner may hold a file just written for a moment,
    /// and Windows refuses the rename meanwhile. The last refusal is thrown.</summary>
    public static void MoveFolder(string from, string to)
    {
        for (int attempt = 1; ; attempt++)
        {
            try { Directory.Move(from, to); return; }
            catch (Exception e) when (attempt < 20 && e is IOException or UnauthorizedAccessException) { Thread.Sleep(250); }
        }
    }

    /// <summary>Copies a folder tree (files overwritten); the folders named in skip are left out at its top level.</summary>
    public static void CopyTree(string from, string to, ICollection<string>? skip = null)
    {
        Directory.CreateDirectory(to);
        foreach (string file in Directory.EnumerateFiles(from)) File.Copy(file, Path.Join(to, Path.GetFileName(file)), overwrite: true);
        foreach (string folder in Directory.EnumerateDirectories(from))
        {
            string name = Path.GetFileName(folder);
            if (skip is not null && skip.Contains(name)) continue;
            CopyTree(folder, Path.Join(to, name));
        }
    }

    /// <summary>
    /// Moves each entry of <paramref name="from"/> into <paramref name="to"/> (created if needed), a subfolder entry by
    /// entry when it cannot move whole. Windows never renames a folder holding an open or mapped file, but does move
    /// the files of a running program one by one; a file opened without delete sharing stays. Returns the relative
    /// paths left behind.
    /// </summary>
    public static List<string> MoveContent(string from, string to)
    {
        Directory.CreateDirectory(to);
        var left = new List<string>();
        foreach (var item in new DirectoryInfo(from).EnumerateFileSystemInfos())
        {
            string target = Path.Join(to, item.Name);
            try
            {
                if (item is DirectoryInfo) Directory.Move(item.FullName, target); else File.Move(item.FullName, target);
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                if (item is DirectoryInfo) left.AddRange(MoveContent(item.FullName, target).Select(p => Path.Join(item.Name, p)));
                else left.Add(item.Name);
            }
        }
        return left;
    }
}

/// <summary>Processes, paths and images of this PC, through the Windows API (no PowerShell, no WMI).</summary>
public static partial class InstallNative
{
    private const uint ProcessQueryLimitedInformation = 0x1000;
    private const int ErrorInvalidParameter = 87;
    private const uint StillActive = 259;

    [LibraryImport("kernel32.dll", SetLastError = true)]
    private static partial nint OpenProcess(uint access, [MarshalAs(UnmanagedType.Bool)] bool inherit, int pid);

    [LibraryImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool CloseHandle(nint handle);

    [LibraryImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool GetExitCodeProcess(nint handle, out uint code);

    [LibraryImport("kernel32.dll", EntryPoint = "QueryFullProcessImageNameW", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static unsafe partial bool QueryFullProcessImageName(nint process, uint flags, char* name, ref uint size);

    [LibraryImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool GetProcessTimes(nint process, out long creation, out long exit, out long kernel, out long user);

    [LibraryImport("kernel32.dll", EntryPoint = "CreateFileW", StringMarshalling = StringMarshalling.Utf16, SetLastError = true)]
    private static partial SafeFileHandle CreateFile(string path, uint access, uint share, nint security, uint mode, uint flags, nint template);

    [LibraryImport("kernel32.dll", EntryPoint = "GetFinalPathNameByHandleW", SetLastError = true)]
    private static unsafe partial uint GetFinalPathNameByHandle(SafeFileHandle file, char* path, uint size, uint flags);

    [LibraryImport("iphlpapi.dll")]
    private static partial uint GetExtendedTcpTable(nint table, ref int size, [MarshalAs(UnmanagedType.Bool)] bool order, int family, int tableClass, uint reserved);

    /// <summary>A running process as the installer identifies it: image path (null when it runs but cannot be read)
    /// and creation time; Exited when there is no such process any more.</summary>
    public sealed record ProcessInfo(bool Exited, string? Image, DateTime? Birth);

    public static unsafe ProcessInfo Read(int pid)
    {
        nint handle = OpenProcess(ProcessQueryLimitedInformation, false, pid);
        if (handle == 0) return Marshal.GetLastPInvokeError() == ErrorInvalidParameter ? new(true, null, null) : new(false, null, null);
        try
        {
            if (GetExitCodeProcess(handle, out uint code) && code != StillActive) return new(true, null, null);
            DateTime? birth = GetProcessTimes(handle, out long creation, out _, out _, out _) ? DateTime.FromFileTimeUtc(creation) : null;
            char* buffer = stackalloc char[32768];
            uint size = 32768;
            string? image = QueryFullProcessImageName(handle, 0, buffer, ref size) ? new string(buffer, 0, (int)size) : null;
            return new(false, image, birth);
        }
        finally { CloseHandle(handle); }
    }

    /// <summary>Image paths of the running processes of that name (this one excepted); Unknown when one of them runs
    /// but cannot be read at all, and callers then keep every folder.</summary>
    public sealed record Images(IReadOnlyList<string> Paths, bool Unknown)
    {
        public static Images Of(string name = "NexusRelay")
        {
            var paths = new List<string>();
            bool unknown = false;
            foreach (var process in Process.GetProcessesByName(name))
            {
                int pid = process.Id;
                process.Dispose();
                if (pid == Environment.ProcessId) continue;
                var read = Read(pid);
                if (read.Exited) continue;
                if (read.Image is null) unknown = true; else paths.Add(read.Image);
            }
            return new Images(paths, unknown);
        }
    }

    /// <summary>
    /// The path Windows really opens (GetFinalPathNameByHandleW): a packaged app (Codex, an MSIX) redirects new files
    /// of AppData into its own cache, and external hosts need the path actually written, not the logical one.
    /// </summary>
    public static unsafe string PhysicalPath(string path)
    {
        using var file = CreateFile(Path.GetFullPath(path), 0, 7, 0, 3 /* OPEN_EXISTING */, 0x02000000 /* BACKUP_SEMANTICS */, 0);
        if (file.IsInvalid) throw new IOException($"{path}: {Marshal.GetPInvokeErrorMessage(Marshal.GetLastPInvokeError())}");
        char* buffer = stackalloc char[32768];
        uint count = GetFinalPathNameByHandle(file, buffer, 32768, 0);
        if (count == 0 || count >= 32768) throw new IOException($"{path}: real path unreadable.");
        string value = new(buffer, 0, (int)count);
        if (value.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) return @"\\" + value[8..];
        return value.StartsWith(@"\\?\", StringComparison.OrdinalIgnoreCase) ? value[4..] : value;
    }

    /// <summary>The PID listening on 127.0.0.1 or any address at that TCP port (IPv4 table), else null.</summary>
    public static unsafe int? TcpListener(int port)
    {
        int size = 0;
        _ = GetExtendedTcpTable(0, ref size, false, 2 /* AF_INET */, 3 /* TCP_TABLE_OWNER_PID_LISTENER */, 0);
        for (int attempt = 0; attempt < 4; attempt++)
        {
            nint table = Marshal.AllocHGlobal(size);
            try
            {
                uint result = GetExtendedTcpTable(table, ref size, false, 2, 3, 0);
                if (result == 122 /* ERROR_INSUFFICIENT_BUFFER */) continue;
                if (result != 0) return null;
                int count = *(int*)table;
                var rows = (uint*)(table + 4);
                for (int i = 0; i < count; i++)
                {
                    uint* row = rows + i * 6;  // state, local address, local port (network order), remote address, remote port, owner
                    int local = (int)(((row[2] & 0xFF) << 8) | ((row[2] >> 8) & 0xFF));
                    if (local == port && row[5] > 0) return (int)row[5];  // a listener table: every row listens
                }
                return null;
            }
            finally { Marshal.FreeHGlobal(table); }
        }
        return null;
    }

    /// <summary>True when something accepts a TCP connection on 127.0.0.1 at that port within one second.</summary>
    public static bool PortOpen(int port)
    {
        using var client = new System.Net.Sockets.TcpClient();
        try { return client.ConnectAsync(IPAddress.Loopback, port).Wait(1000) && client.Connected; }
        catch (AggregateException) { return false; }
    }

    /// <summary>A Windows program image (MZ header pointing to a PE signature). A file that is not one is never
    /// started: Windows would show its "16-bit application not supported" box and block until clicked.</summary>
    public static bool IsPeImage(string path)
    {
        try
        {
            byte[] bytes = File.ReadAllBytes(path);
            if (bytes.Length < 64 || bytes[0] != 0x4D || bytes[1] != 0x5A) return false;
            int pe = BitConverter.ToInt32(bytes, 0x3C);
            return pe > 0 && pe + 4 <= bytes.Length && bytes[pe] == 0x50 && bytes[pe + 1] == 0x45 && bytes[pe + 2] == 0 && bytes[pe + 3] == 0;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { return false; }
    }

    /// <summary>Runs a program without a window, its input closed and its output collected; kills it after the timeout.</summary>
    public static (int Code, string Output) Run(string exe, IEnumerable<string> args, int timeoutSeconds = 120, IDictionary<string, string?>? env = null, Action<string>? line = null)
    {
        var start = new ProcessStartInfo(exe)
        {
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = InstallFiles.Utf8, StandardErrorEncoding = InstallFiles.Utf8,
        };
        foreach (string arg in args) start.ArgumentList.Add(arg);
        if (env is not null) foreach (var (key, value) in env) { if (value is null) start.Environment.Remove(key); else start.Environment[key] = value; }
        using var process = Process.Start(start) ?? throw new IOException($"{exe}: not started.");
        process.StandardInput.Close();
        var output = new StringBuilder();
        var gate = new Lock();
        void Collect(object? sender, DataReceivedEventArgs e)
        {
            if (e.Data is null) return;
            lock (gate) output.AppendLine(e.Data);
            line?.Invoke(e.Data);
        }
        process.OutputDataReceived += Collect;
        process.ErrorDataReceived += Collect;
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        if (!process.WaitForExit(timeoutSeconds * 1000))
        {
            try { process.Kill(entireProcessTree: true); } catch (InvalidOperationException) { /* already gone */ }
            throw new TimeoutException($"{exe} {string.Join(' ', args)}: no answer in {timeoutSeconds} s.");
        }
        process.WaitForExit();
        lock (gate) return (process.ExitCode, output.ToString());
    }

    /// <summary>The engine answers its health check (--schemas: the tools list) without a window.</summary>
    public static bool Healthy(string exe)
    {
        if (!File.Exists(exe) || !IsPeImage(exe)) return false;
        try { return Run(exe, ["--schemas"], 60).Code == 0; }
        catch (Exception e) when (e is System.ComponentModel.Win32Exception or IOException or TimeoutException) { return false; }
    }

    /// <summary>A command found on the PATH (as Get-Command would find it), else null.</summary>
    public static string? OnPath(string name)
    {
        string[] extensions = [".exe", ".cmd", ".bat", ".ps1", ""];
        foreach (string folder in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            foreach (string extension in extensions)
            {
                try
                {
                    string candidate = Path.Join(folder, name + extension);
                    if (File.Exists(candidate)) return candidate;
                }
                catch (ArgumentException) { /* invalid PATH entry */ }
            }
        }
        return null;
    }
}

/// <summary>
/// Engine folders: one immutable folder per build, %LOCALAPPDATA%\NexusRelay\versions\&lt;fingerprint&gt;, never
/// written again; old folders (and ~\.nexus\nexus-relay\bin.old-*) removed only once no process uses them. A running
/// .NET engine loads its DLLs from its folder on demand (NexusFree 1.3.3): deleting the files it has not loaded yet
/// leaves it answering every call with an empty error. Three checks, any of which keeps a folder whole:
/// - its engine.lock is held: every engine run from a versioned folder opens it for its whole life without delete
///   sharing (EngineLock.cs), whatever spelling of the path started it (junction, MSIX redirection, elevated view);
/// - a running NexusRelay.exe has its image in it (QueryFullProcessImageNameW); when one running NexusRelay.exe cannot
///   be read at all, no folder is removed;
/// - Windows refuses to rename the folder aside (a file in it is open or mapped).
/// Only a folder renamed aside, which no pointer names any more, is deleted.
/// </summary>
public static class EngineFolders
{
    /// <summary>Fingerprint of a prebuilt engine folder: SHA-256 of the files' SHA-256 (upper-case hexadecimal, in
    /// the order of their paths), first 16 hexadecimal characters.</summary>
    public static string Digest(string folder)
    {
        string root = Path.GetFullPath(folder).TrimEnd('\\');
        var comparer = StringComparer.Create(CultureInfo.InvariantCulture, ignoreCase: true);
        var hashes = Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories).Order(comparer).Select(InstallFiles.Sha256);
        return Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(string.Concat(hashes))))[..16];
    }

    public static bool InUse(string folder, InstallNative.Images images)
    {
        if (images.Unknown) return true;
        string prefix = folder.TrimEnd('\\') + "\\";
        if (images.Paths.Any(p => p.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))) return true;
        string lockPath = Path.Join(folder, EngineLock.FileName);
        if (!File.Exists(lockPath)) return false;
        // Exclusive open: refused while an engine holds the lock. Closed before the rename (Windows never renames a
        // folder with an open file in it); an engine starting meanwhile holds it again, which refuses the rename.
        try { new FileStream(lockPath, FileMode.Open, FileAccess.ReadWrite, FileShare.None).Dispose(); return false; }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { return true; }
    }

    /// <summary>Removes one old engine folder unless it is in use; true when it is gone.</summary>
    public static bool RemoveIfUnused(string folder, InstallNative.Images images)
    {
        if (InUse(folder, images)) return false;
        string aside = folder.EndsWith(".removing", StringComparison.OrdinalIgnoreCase) ? folder
            : $"{folder.TrimEnd('\\')}.{Environment.ProcessId.ToString(CultureInfo.InvariantCulture)}.removing";
        if (aside != folder)
        {
            try { Directory.Move(folder, aside); }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException) { return false; }  // in use: kept whole
        }
        return InstallFiles.TryDeleteTree(aside);  // anything left goes next time
    }

    /// <summary>Every version folder but the one just installed (compared by fingerprint, never by path), when unused.</summary>
    public static void RemoveStale(string versions, string keep, InstallNative.Images images)
    {
        if (images.Unknown || !Directory.Exists(versions)) return;  // a running engine whose folder cannot be known: everything stays
        foreach (string folder in Directory.EnumerateDirectories(versions))
        {
            if (Path.GetFileName(folder).Equals(keep, StringComparison.OrdinalIgnoreCase)) continue;
            RemoveIfUnused(folder, images);
        }
    }
}

/// <summary>
/// Shared MCP token of the Nexus daemons (common rule of 30/10/2026, the same for Relay, Android and Browser): the
/// explicit file (-TokenFile, NEXUS_MCP_TOKEN_FILE, then the token_file setting), else %USERPROFILE%\.nexus\mcp-token.txt.
/// A stable sibling .lock file serializes the installers (32 random bytes in hexadecimal when the file is empty); the
/// token itself stays readable by its consumers, and an existing non-empty token is never regenerated. Relay 1.9.0 to
/// 1.10.1 created its own ~\.nexus\nexus-relay\token.txt: when the shared file is still empty, it takes that token.
/// </summary>
public static class SharedToken
{
    public static string ResolveFile(IEnumerable<string?> explicitFiles, string home, string? legacy)
    {
        bool IsLegacy(string file) => legacy is not null && string.Equals(Path.GetFullPath(file), Path.GetFullPath(legacy), StringComparison.OrdinalIgnoreCase);
        string? chosen = explicitFiles.FirstOrDefault(f => !string.IsNullOrWhiteSpace(f) && !IsLegacy(f!));
        string path = Path.GetFullPath(chosen ?? Path.Join(home, ".nexus", "mcp-token.txt"));
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        FileStream? held = null;
        for (int attempt = 1; held is null; attempt++)
        {
            // Keep this lock file: deleting it could split concurrent installers between different file identities.
            try { held = new FileStream(path + ".lock", FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None); }
            catch (IOException) when (attempt < 50) { Thread.Sleep(100); }
        }
        using (held)
        {
            string existing = File.Exists(path) ? File.ReadAllText(path).Trim() : "";
            if (existing.Length == 0)
            {
                string old = legacy is not null && File.Exists(legacy) ? File.ReadAllText(legacy).Trim() : "";
                File.WriteAllText(path, old.Length >= 32 ? old : Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(32)), InstallFiles.Utf8);
            }
        }
        return path;
    }
}

/// <summary>The Task Scheduler as the installer uses it: task definitions as XML.</summary>
public interface ITaskStore
{
    /// <summary>The task's definition, or null when there is no such task.</summary>
    string? Xml(string name);

    /// <summary>Absent, Disabled, Queued, Ready, Running or Unknown.</summary>
    string State(string name);

    /// <summary>Creates or replaces the task.</summary>
    void Register(string name, string xml);

    void Stop(string name);

    void Delete(string name);

    void Run(string name);
}

/// <summary>Task XML (Task Scheduler schema 1.2) of the relay supervisor task, and the edits made to an existing one.</summary>
public static class TaskXml
{
    public static readonly XNamespace Ns = "http://schemas.microsoft.com/windows/2004/02/mit/task";

    /// <summary>At logon of this user, hidden, on battery too, no time limit, one instance.</summary>
    public static string Logon(string description, string execute, string arguments, string workingDirectory, string userId) =>
        new XElement(Ns + "Task", new XAttribute("version", "1.2"),
            new XElement(Ns + "RegistrationInfo", new XElement(Ns + "Description", description)),
            new XElement(Ns + "Triggers", new XElement(Ns + "LogonTrigger", new XElement(Ns + "Enabled", "true"), new XElement(Ns + "UserId", userId))),
            new XElement(Ns + "Principals", new XElement(Ns + "Principal", new XAttribute("id", "Author"),
                new XElement(Ns + "UserId", userId), new XElement(Ns + "LogonType", "InteractiveToken"), new XElement(Ns + "RunLevel", "LeastPrivilege"))),
            new XElement(Ns + "Settings",
                new XElement(Ns + "MultipleInstancesPolicy", "IgnoreNew"),
                new XElement(Ns + "DisallowStartIfOnBatteries", "false"),
                new XElement(Ns + "StopIfGoingOnBatteries", "false"),
                new XElement(Ns + "AllowHardTerminate", "true"),
                new XElement(Ns + "StartWhenAvailable", "false"),
                new XElement(Ns + "RunOnlyIfNetworkAvailable", "false"),
                new XElement(Ns + "IdleSettings", new XElement(Ns + "StopOnIdleEnd", "true"), new XElement(Ns + "RestartOnIdle", "false")),
                new XElement(Ns + "AllowStartOnDemand", "true"),
                new XElement(Ns + "Enabled", "true"),
                new XElement(Ns + "Hidden", "true"),
                new XElement(Ns + "RunOnlyIfIdle", "false"),
                new XElement(Ns + "WakeToRun", "false"),
                new XElement(Ns + "ExecutionTimeLimit", "PT0S"),
                new XElement(Ns + "Priority", "7")),
            new XElement(Ns + "Actions", new XAttribute("Context", "Author"), Exec(execute, arguments, workingDirectory))).ToString();

    private static XElement Exec(string execute, string arguments, string workingDirectory)
    {
        var exec = new XElement(Ns + "Exec", new XElement(Ns + "Command", execute));
        if (arguments.Length > 0) exec.Add(new XElement(Ns + "Arguments", arguments));
        if (workingDirectory.Length > 0) exec.Add(new XElement(Ns + "WorkingDirectory", workingDirectory));
        return exec;
    }

    /// <summary>The definition with its first action replaced; settings, triggers, principal and state kept.</summary>
    public static string WithAction(string xml, string execute, string arguments, string workingDirectory)
    {
        var doc = XDocument.Parse(xml);
        var actions = doc.Root!.Element(Ns + "Actions") ?? throw new InvalidDataException("Task without actions.");
        var first = actions.Elements(Ns + "Exec").FirstOrDefault();
        var replacement = Exec(execute, arguments, workingDirectory);
        if (first is null) actions.AddFirst(replacement); else first.ReplaceWith(replacement);
        doc.Root.Element(Ns + "RegistrationInfo")?.Element(Ns + "URI")?.Remove();
        return doc.Root.ToString();
    }

    public static string? Command(string? xml) =>
        xml is null ? null : XDocument.Parse(xml).Root?.Element(Ns + "Actions")?.Element(Ns + "Exec")?.Element(Ns + "Command")?.Value;
}

/// <summary>The Task Scheduler's own COM API (Schedule.Service), root folder: no schtasks.exe, no PowerShell.</summary>
public sealed class WindowsTaskStore : ITaskStore
{
    private const int TaskCreateOrUpdate = 6, LogonInteractiveToken = 3;

    private static dynamic Folder()
    {
        var type = Type.GetTypeFromProgID("Schedule.Service", throwOnError: true)!;
        dynamic service = Activator.CreateInstance(type)!;
        service.Connect();
        return service.GetFolder("\\");
    }

    /// <summary>No such task (or folder): the COM binder raises it as a FileNotFoundException, others as COMException.</summary>
    private static bool Missing(Exception e) => e.HResult is unchecked((int)0x80070002) or unchecked((int)0x80070003);

    private static dynamic? Find(string name)
    {
        try { return Folder().GetTask(name); }
        catch (Exception e) when (Missing(e)) { return null; }
    }

    public string? Xml(string name) => Find(name) is { } task ? (string)task.Xml : null;

    public string State(string name)
    {
        if (Find(name) is not { } task) return "Absent";
        return (int)task.State switch { 1 => "Disabled", 2 => "Queued", 3 => "Ready", 4 => "Running", _ => "Unknown" };
    }

    public void Register(string name, string xml) => Folder().RegisterTask(name, xml, TaskCreateOrUpdate, null, null, LogonInteractiveToken, null);

    public void Stop(string name)
    {
        if (Find(name) is { } task) task.Stop(0);
    }

    public void Delete(string name)
    {
        try { Folder().DeleteTask(name, 0); }
        catch (Exception e) when (Missing(e)) { /* already gone */ }
    }

    public void Run(string name)
    {
        if (Find(name) is { } task) task.Run(null);
    }
}

/// <summary>
/// Logon task of the relay supervisor (NexusRelay.exe --daemon), used when the shared "Nexus MCP Daemons" supervisor
/// is absent. Since 1.13.2 the task is named "NexusRelay"; "Nexus Relay" (before 1.13.2) is migrated: stopped, then
/// unregistered, before the new one starts, so two supervisors never run (the supervisor's daemon.lock is a second guard).
/// </summary>
public static class RelayTask
{
    public const string Name = "NexusRelay";
    public const string SharedSupervisor = "Nexus MCP Daemons";
    public static readonly string[] Legacy = ["Nexus Relay"];

    /// <summary>Stops (when running) then unregisters each named task that exists; returns the names removed.</summary>
    public static List<string> Remove(ITaskStore tasks, IEnumerable<string> names)
    {
        var removed = new List<string>();
        foreach (string name in names)
        {
            string state = tasks.State(name);
            if (state == "Absent") continue;
            if (state == "Running") tasks.Stop(name);
            tasks.Delete(name);
            removed.Add(name);
        }
        return removed;
    }

    /// <summary>Removes the legacy task, then (without the shared supervisor) registers or updates the "NexusRelay"
    /// task and starts it. Returns one line per change.</summary>
    public static List<string> Sync(ITaskStore tasks, string executable, string workingDirectory, bool sharedSupervisor, string userId)
    {
        var lines = Remove(tasks, Legacy).Select(old => InstallText.Tr($"Scheduled task '{old}' removed (renamed '{Name}').", $"Tâche planifiée « {old} » retirée (renommée « {Name} »).")).ToList();
        if (sharedSupervisor) return lines;
        string? existing = tasks.Xml(Name);
        if (existing is null)
        {
            tasks.Register(Name, TaskXml.Logon("NexusRelay: local MCP daemon of Claude, Codex and Antigravity (127.0.0.1:16181).", executable, "--daemon", workingDirectory, userId));
            lines.Add(InstallText.Tr($"Scheduled task '{Name}' created (at logon).", $"Tâche planifiée « {Name} » créée (à l'ouverture de session)."));
        }
        else if (!string.Equals(TaskXml.Command(existing), executable, StringComparison.Ordinal))
        {
            // Supervisor of a previous version: replaced by this version's.
            tasks.Register(Name, TaskXml.WithAction(existing, executable, "--daemon", workingDirectory));
            if (tasks.State(Name) == "Running") tasks.Stop(Name);
            lines.Add(InstallText.Tr($"Scheduled task '{Name}' updated.", $"Tâche planifiée « {Name} » mise à jour."));
        }
        if (tasks.State(Name) != "Running") tasks.Run(Name);
        return lines;
    }
}

/// <summary>
/// Targeted, cooperative restart of the relay daemon and sidecar: the process named by its ownership marker
/// (~\.nexus\relay\&lt;name&gt;-restart.pid), whose image is the installed engine and, for the daemon, which owns the
/// port, is asked to restart through its request file. A process that does not match is never asked, nor stopped.
/// </summary>
public static class RestartOwner
{
    public sealed record Owner(int Pid, DateTime Birth, string Executable);

    private static string Physical(string path)
    {
        try { return InstallNative.PhysicalPath(path); }
        catch (IOException) { return Path.GetFullPath(path); }
    }

    public static Owner? Find(string mailbox, string name, string? expectedExecutable, int port = 0)
    {
        if (string.IsNullOrEmpty(expectedExecutable)) return null;
        string marker = Path.Join(mailbox, $"{name}-restart.pid");
        if (!File.Exists(marker) || !int.TryParse(File.ReadAllText(marker).Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out int knownPid))
        {
            if (port > 0 && (InstallNative.TcpListener(port) is not null || InstallNative.PortOpen(port)))
                throw new InvalidOperationException($"Relay {name}: port {port} exists without its ownership marker; no process was stopped.");
            return null;
        }
        var process = InstallNative.Read(knownPid);
        if (process.Exited) return null;
        if (process.Birth is not { } birth || process.Image is null) throw new InvalidOperationException($"Relay {name}: process {knownPid} cannot be read; no restart requested.");
        if (File.GetLastWriteTimeUtc(marker) < birth) throw new InvalidOperationException($"Relay {name}: PID marker predates this process; no restart requested.");
        // A retired Python runtime is not an engine this installer can identify: nothing is asked of it.
        if (Path.GetFileName(expectedExecutable).StartsWith("python", StringComparison.OrdinalIgnoreCase)) return null;
        if (!string.Equals(Physical(process.Image), Physical(expectedExecutable), StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException($"Relay {name}: executable identity does not match the installed runtime; no restart requested.");
        if (port > 0)
        {
            int? listener = InstallNative.TcpListener(port) ?? HealthPid(port);
            if (listener != knownPid)
                throw new InvalidOperationException($"Relay {name}: listener ownership of port {port} does not match (expected {knownPid}, observed {listener?.ToString(CultureInfo.InvariantCulture) ?? "none"}); no restart requested.");
        }
        return new Owner(knownPid, birth, process.Image);
    }

    private static int? HealthPid(int port)
    {
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
            var health = JsonNode.Parse(http.GetStringAsync(new Uri($"http://127.0.0.1:{port.ToString(CultureInfo.InvariantCulture)}/health")).GetAwaiter().GetResult());
            return health?["service"]?.ToString() == "nexus-relay" && health["runtime"]?.ToString() == "native" ? health["pid"]?.GetValue<int>() : null;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or JsonException or InvalidOperationException or FormatException) { return null; }
    }

    /// <summary>Writes the restart request for that owner, after checking it is still the same process.</summary>
    public static bool Request(string requestPath, Owner? owner, string kind, string fingerprint)
    {
        if (owner is null) return false;
        var current = InstallNative.Read(owner.Pid);
        if (current.Exited) return false;
        if (current.Birth != owner.Birth || !string.Equals(current.Image, owner.Executable, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Relay restart owner changed; no request written.");
        File.WriteAllText(requestPath, $"{kind} pid:{owner.Pid.ToString(CultureInfo.InvariantCulture)} {fingerprint}{DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture)}", InstallFiles.Utf8);
        return true;
    }
}

/// <summary>Antigravity's activation of the plugin and its sidecar in ~\.gemini\config\config.json (official
/// documentation: plugins.&lt;id&gt;.enabled, sidecars.&lt;id&gt;.enabled + projectId). A user's choice to disable
/// either is kept; nothing else of the file is touched.</summary>
public static class AntigravityActivation
{
    public sealed record Result(JsonObject Config, bool Changed, bool Disabled);

    public static Result Apply(JsonObject config, string projectId)
    {
        var plugin = (config["plugins"] as JsonObject)?["nexus-relay"] as JsonObject;
        var sidecar = (config["sidecars"] as JsonObject)?["nexus-relay/relay"] as JsonObject;
        static bool IsFalse(JsonNode? node) => node is JsonValue v && v.TryGetValue<bool>(out bool b) && !b;
        static bool IsTrue(JsonNode? node) => node is JsonValue v && v.TryGetValue<bool>(out bool b) && b;
        if (IsFalse(plugin?["enabled"]) || IsFalse(sidecar?["enabled"])) return new(config, false, true);
        bool changed = false;
        if (config["plugins"] is not JsonObject plugins) { plugins = []; config["plugins"] = plugins; }
        if (plugin is null) { plugins["nexus-relay"] = new JsonObject { ["enabled"] = true }; changed = true; }
        else if (!IsTrue(plugin["enabled"])) { plugin["enabled"] = true; changed = true; }
        if (config["sidecars"] is not JsonObject sidecars) { sidecars = []; config["sidecars"] = sidecars; }
        if (sidecar is null) { sidecars["nexus-relay/relay"] = new JsonObject { ["enabled"] = true, ["projectId"] = projectId }; changed = true; }
        else
        {
            if (!IsTrue(sidecar["enabled"])) { sidecar["enabled"] = true; changed = true; }
            if (sidecar["projectId"]?.ToString() != projectId) { sidecar["projectId"] = projectId; changed = true; }
        }
        return new(config, changed, false);
    }
}
