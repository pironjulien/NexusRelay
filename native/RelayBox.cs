using System.Globalization;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.Win32.SafeHandles;

namespace NexusRelay;

public sealed class RelayError(string message) : Exception(message);

public sealed record RelayMessage(
    string Id,
    string Sender,
    string To,
    string Topic,
    string Created,
    string Text,
    string? ReplyTo = null,
    bool Info = false,
    string? Vote = null,
    JsonObject? Extra = null)
{
    public JsonObject ExtraObj => Extra ?? new JsonObject();

    public JsonObject ToJson()
    {
        var obj = new JsonObject
        {
            ["id"] = Id,
            ["to"] = To,
            ["topic"] = Topic,
            ["created"] = Created,
            ["text"] = Text,
            ["reply_to"] = ReplyTo is not null ? JsonValue.Create(ReplyTo) : null,
            ["info"] = Info,
            ["vote"] = Vote is not null ? JsonValue.Create(Vote) : null,
            ["extra"] = Extra?.DeepClone() ?? new JsonObject(),
            ["from"] = Sender,
        };
        return obj;
    }

    public static RelayMessage FromJson(JsonObject data)
    {
        var extraNode = data["extra"] as JsonObject;
        return new RelayMessage(
            Id: data["id"]?.GetValue<string>() ?? throw new RelayError("missing id"),
            Sender: data["from"]?.GetValue<string>() ?? throw new RelayError("missing from"),
            To: data["to"]?.GetValue<string>() ?? throw new RelayError("missing to"),
            Topic: data["topic"]?.GetValue<string>() ?? throw new RelayError("missing topic"),
            Created: data["created"]?.GetValue<string>() ?? throw new RelayError("missing created"),
            Text: data["text"]?.GetValue<string>() ?? "",
            ReplyTo: data["reply_to"]?.GetValue<string>(),
            Info: data["info"]?.GetValue<bool>() ?? false,
            Vote: data["vote"]?.GetValue<string>(),
            Extra: extraNode is not null ? (JsonObject)extraNode.DeepClone() : new JsonObject()
        );
    }
}

public sealed partial class RelayBox
{
    public const int MaxText = 100_000;
    public const int MaxAttachments = 8;
    public const int MaxAttachment = 10 * 1024 * 1024;
    public const int MaxAttachmentsTotal = 25 * 1024 * 1024;
    public static readonly string[] Votes = ["accord", "désaccord"];

    public static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    private static readonly UTF8Encoding Utf8NoBom = new(false);

    [GeneratedRegex(@"^[a-z0-9][a-z0-9-]{0,47}$", RegexOptions.Compiled)]
    public static partial Regex TopicRegex();

    [GeneratedRegex(@"^\d{8}T\d{9}-[0-9a-f]{6}$", RegexOptions.Compiled)]
    public static partial Regex MessageIdRegex();

    [GeneratedRegex(@"[^\w.\-]+", RegexOptions.Compiled)]
    private static partial Regex SafeFileRegex();

    public string Root { get; }

    public RelayBox(string? root = null)
    {
        Root = Path.GetFullPath(root ?? Path.Join(RelayPaths.RelayHome(), "v1"));
    }

    public static string NowIso()
        => DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture);

    public static string NewId()
    {
        var now = DateTime.UtcNow;
        var hex = Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(3));
        return now.ToString("yyyyMMddTHHmmssfff", CultureInfo.InvariantCulture) + "-" + hex;
    }

    public static string RandomHex(int byteCount)
        => Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(byteCount));

    public static string CheckTopic(string? topic)
    {
        var cleaned = (topic ?? "").Trim().ToLowerInvariant();
        if (!TopicRegex().IsMatch(cleaned))
        {
            throw new RelayError(RelayPaths.Tr(
                "topic required: lowercase letters, digits and hyphens (e.g. “nexuswindows”, “relais”)",
                "sujet obligatoire : minuscules, chiffres et tirets (ex. « nexuswindows », « relais »)"));
        }
        return cleaned;
    }

    public static void WriteNew(string path, JsonNode data)
    {
        var dir = Path.GetDirectoryName(path)!;
        Directory.CreateDirectory(dir);
        if (File.Exists(path))
        {
            var name = Path.GetFileName(path);
            throw new RelayError(RelayPaths.Tr($"{name} already exists", $"{name} existe déjà"));
        }
        var tmp = Path.Join(dir, $".{Path.GetFileName(path)}.{RandomHex(4)}.tmp");
        File.WriteAllText(tmp, data.ToJsonString(JsonOptions), Utf8NoBom);
        try
        {
            File.Move(tmp, path, overwrite: false);
        }
        catch (IOException)
        {
            try { File.Delete(tmp); } catch (Exception cleanup) when (cleanup is IOException or UnauthorizedAccessException) { /* Best-effort removal of the temporary file. */ }
            var name = Path.GetFileName(path);
            throw new RelayError(RelayPaths.Tr($"{name} already exists", $"{name} existe déjà"));
        }
    }

    public static void WriteState(string path, JsonNode data)
    {
        var dir = Path.GetDirectoryName(path)!;
        Directory.CreateDirectory(dir);
        var tmp = Path.Join(dir, $".{Path.GetFileName(path)}.{RandomHex(4)}.tmp");
        using (var stream = new FileStream(tmp, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        {
            stream.Write(Utf8NoBom.GetBytes(data.ToJsonString(JsonOptions)));
            stream.Flush(flushToDisk: true);
        }
        for (int attempt = 0; attempt < 5; attempt++)
        {
            try
            {
                File.Move(tmp, path, overwrite: true);
                return;
            }
            catch (IOException) when (attempt < 4)
            {
                Thread.Sleep(5);
            }
            catch (UnauthorizedAccessException) when (attempt < 4)
            {
                Thread.Sleep(5);
            }
        }
        try { File.Delete(tmp); } catch (Exception cleanup) when (cleanup is IOException or UnauthorizedAccessException) { /* Best-effort removal of the temporary file. */ }
    }

    public static JsonObject? ReadJson(string path)
    {
        if (!File.Exists(path))
            return null;
        for (int attempt = 0; attempt < 3; attempt++)
        {
            try
            {
                using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
                return JsonNode.Parse(fs) as JsonObject;
            }
            catch (IOException) when (attempt < 2)
            {
                Thread.Sleep(2);
            }
            catch
            {
                // Unreadable or corrupt JSON reads as absent: callers treat state files as optional.
                return null;
            }
        }
        return null;
    }

    #region Win32 P/Invoke (PidAlive, GetFinalPathNameByHandleW, HardLink check)

    private const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
    private const uint STILL_ACTIVE = 259;

    [LibraryImport("kernel32.dll", SetLastError = true)]
    private static partial IntPtr OpenProcess(uint dwDesiredAccess, [MarshalAs(UnmanagedType.Bool)] bool bInheritHandle, uint dwProcessId);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool CloseHandle(IntPtr hObject);

    [StructLayout(LayoutKind.Sequential)]
    private struct BY_HANDLE_FILE_INFORMATION
    {
        public uint dwFileAttributes;
        public uint ftCreationTimeLow;
        public uint ftCreationTimeHigh;
        public uint ftLastAccessTimeLow;
        public uint ftLastAccessTimeHigh;
        public uint ftLastWriteTimeLow;
        public uint ftLastWriteTimeHigh;
        public uint dwVolumeSerialNumber;
        public uint nFileSizeHigh;
        public uint nFileSizeLow;
        public uint nNumberOfLinks;
        public uint nFileIndexHigh;
        public uint nFileIndexLow;
    }

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool GetFileInformationByHandle(SafeFileHandle hFile, out BY_HANDLE_FILE_INFORMATION lpFileInformation);

    [LibraryImport("kernel32.dll", StringMarshalling = StringMarshalling.Utf16, SetLastError = true)]
    private static partial uint GetFinalPathNameByHandleW(SafeFileHandle hFile, [Out] char[] lpszFilePath, uint cchFilePath, uint dwFlags);

    public static bool PidAlive(int? pid)
    {
        if (pid is null or <= 0)
            return false;
        if (pid.Value == Environment.ProcessId)
            return true;
        if (OperatingSystem.IsWindows())
        {
            IntPtr handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, (uint)pid.Value);
            if (handle == IntPtr.Zero)
                return false;
            try
            {
                return GetExitCodeProcess(handle, out uint code) && code == STILL_ACTIVE;
            }
            finally
            {
                CloseHandle(handle);
            }
        }
        try
        {
            using var proc = System.Diagnostics.Process.GetProcessById(pid.Value);
            return !proc.HasExited;
        }
        catch (Exception e) when (e is ArgumentException or InvalidOperationException or System.ComponentModel.Win32Exception)
        {
            return false;
        }
    }

    public static string PlainPath(string path)
    {
        if (path.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase))
            path = @"\\" + path[8..];
        else if (path.StartsWith(@"\\?\", StringComparison.OrdinalIgnoreCase))
            path = path[4..];
        return Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar).ToLowerInvariant();
    }

    public static List<string> Vaults()
    {
        var raw = Environment.GetEnvironmentVariable("NEXUS_RELAY_VAULTS") ?? "";
        var roots = raw.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        var set = new SortedSet<string>(StringComparer.Ordinal);
        foreach (var r in roots)
        {
            try
            {
                set.Add(PlainPath(r));
            }
            catch (Exception e) when (e is ArgumentException or NotSupportedException or IOException)
            {
                // Ignore invalid vault entry
            }
        }
        return [.. set];
    }

    private static string OpenedPath(SafeFileHandle handle)
    {
        if (!OperatingSystem.IsWindows())
            throw new PlatformNotSupportedException();
        var buffer = new char[32768];
        uint size = GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Length, 0);
        if (size == 0 || size >= buffer.Length)
        {
            throw new RelayError(RelayPaths.Tr("attachment: real path unreadable", "pièce jointe : chemin réel illisible"));
        }
        return new string(buffer, 0, (int)size);
    }

    public static List<string> CheckFiles(IReadOnlyList<string> files)
    {
        if (files.Count > MaxAttachments)
        {
            throw new RelayError(RelayPaths.Tr(
                $"attachments: {MaxAttachments} at most",
                $"pièces jointes : {MaxAttachments} au plus"));
        }
        var paths = new List<string>(files.Count);
        foreach (var name in files)
        {
            if (!Path.IsPathRooted(name) || (OperatingSystem.IsWindows() && name.Length < 3))
            {
                throw new RelayError(RelayPaths.Tr(
                    $"attachment: absolute path expected ({name})",
                    $"pièce jointe : chemin absolu attendu ({name})"));
            }
            paths.Add(name);
        }
        return paths;
    }

    public static byte[] ReadAttachment(string path, int budget)
    {
        var fileName = Path.GetFileName(path);
        SafeFileHandle handle;
        try
        {
            // Check reparse point on path before/while opening
            var attr = File.GetAttributes(path);
            if ((attr & FileAttributes.ReparsePoint) != 0)
            {
                throw new RelayError(RelayPaths.Tr(
                    $"attachment not found or unreadable: {path} (symlink/reparse)",
                    $"pièce jointe introuvable ou illisible : {path} (symlink/reparse)"));
            }
            if ((attr & FileAttributes.Directory) != 0)
            {
                throw new RelayError(RelayPaths.Tr(
                    $"attachment: not a regular file ({fileName})",
                    $"pièce jointe : pas un fichier ordinaire ({fileName})"));
            }
            handle = File.OpenHandle(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        }
        catch (RelayError)
        {
            throw;
        }
        catch (Exception ex)
        {
            // Boundary: any open failure becomes a RelayError that names the attachment.
            throw new RelayError(RelayPaths.Tr(
                $"attachment not found or unreadable: {path} ({ex.Message})",
                $"pièce jointe introuvable ou illisible : {path} ({ex.Message})"));
        }

        using (handle)
        {
            if (OperatingSystem.IsWindows())
            {
                if (!GetFileInformationByHandle(handle, out var info))
                {
                    throw new RelayError(RelayPaths.Tr(
                        $"attachment: not a regular file ({fileName})",
                        $"pièce jointe : pas un fichier ordinaire ({fileName})"));
                }
                if ((info.dwFileAttributes & (uint)FileAttributes.Directory) != 0)
                {
                    throw new RelayError(RelayPaths.Tr(
                        $"attachment: not a regular file ({fileName})",
                        $"pièce jointe : pas un fichier ordinaire ({fileName})"));
                }
                if (info.nNumberOfLinks > 1)
                {
                    throw new RelayError(RelayPaths.Tr(
                        $"attachment refused: file with several names (hard link) ({fileName})",
                        $"pièce jointe refusée : fichier à plusieurs noms (lien dur) ({fileName})"));
                }
                var opened = OpenedPath(handle);
                if (opened.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase) ||
                    (opened.StartsWith(@"\\", StringComparison.Ordinal) && !opened.StartsWith(@"\\?\", StringComparison.Ordinal)))
                {
                    throw new RelayError(RelayPaths.Tr(
                        $"attachment refused: network share ({fileName})",
                        $"pièce jointe refusée : partage réseau ({fileName})"));
                }
                var real = PlainPath(opened);
                foreach (var vault in Vaults())
                {
                    if (real == vault || real.StartsWith(vault + Path.DirectorySeparatorChar, StringComparison.Ordinal))
                    {
                        throw new RelayError(RelayPaths.Tr(
                            $"attachment refused: secrets vault ({fileName})",
                            $"pièce jointe refusée : coffre de secrets ({fileName})"));
                    }
                }
            }

            int limit = Math.Min(MaxAttachment, budget);
            using var fs = new FileStream(handle, FileAccess.Read);
            using var ms = new MemoryStream();
            var buf = new byte[65536];
            int total = 0;
            int read;
            while ((read = fs.Read(buf, 0, buf.Length)) > 0)
            {
                total += read;
                if (total > limit)
                {
                    if (limit < MaxAttachment)
                    {
                        throw new RelayError(RelayPaths.Tr(
                            $"attachments too large together ({MaxAttachmentsTotal} bytes at most)",
                            $"pièces jointes trop grosses ensemble ({MaxAttachmentsTotal} octets au plus)"));
                    }
                    throw new RelayError(RelayPaths.Tr(
                        $"attachment too large: {fileName} ({MaxAttachment} bytes at most)",
                        $"pièce jointe trop grosse : {fileName} ({MaxAttachment} octets au plus)"));
                }
                ms.Write(buf, 0, read);
            }
            return ms.ToArray();
        }
    }

    #endregion

    public static string ClaimLabel(JsonObject claim)
    {
        var phase = claim["phase"]?.GetValue<string>() ?? "claimed";
        int? pid = null;
        if (claim["pid"] is JsonValue pv && pv.TryGetValue<int>(out int p))
            pid = p;
        if ((phase == "submitting" || phase == "claimed") && !PidAlive(pid))
            return phase == "submitting" ? "uncertain" : "abandoned_dead_pid";
        return phase;
    }

    private string MessagesFolder => Path.Join(Root, "messages");

    private string? FindMessagePath(string messageId)
    {
        if (!MessageIdRegex().IsMatch(messageId))
        {
            throw new RelayError(RelayPaths.Tr(
                $"invalid message id: '{messageId}'",
                $"identifiant de message invalide : '{messageId}'"));
        }
        if (!Directory.Exists(MessagesFolder))
            return null;
        return Directory.EnumerateFiles(MessagesFolder, $"{messageId}.*.json").FirstOrDefault();
    }

    private static List<RelayMessage> LoadMessages(IEnumerable<string> paths)
    {
        var list = new List<RelayMessage>();
        foreach (var data in paths.OrderBy(Path.GetFileName, StringComparer.Ordinal).Select(p => ReadJson(p)).OfType<JsonObject>())
        {
            try { list.Add(RelayMessage.FromJson(data)); } catch { /* A malformed message file must never hide the others. */ }
        }
        return list;
    }

    public RelayMessage Post(
        string sender,
        string to,
        string text,
        string topic,
        string? replyTo = null,
        bool info = false,
        string? vote = null,
        JsonObject? extra = null,
        IReadOnlyList<string>? files = null)
    {
        sender = RelayPaths.NormalizeParty(sender);
        to = RelayPaths.NormalizeParty(to);
        topic = CheckTopic(topic);

        if (sender == to)
            throw new RelayError(RelayPaths.Tr("you cannot write to yourself", "on n'écrit pas à soi-même"));
        if (string.IsNullOrWhiteSpace(text))
            throw new RelayError(RelayPaths.Tr("empty message", "message vide"));
        if (text.Length > MaxText)
        {
            throw new RelayError(RelayPaths.Tr(
                $"message too long ({text.Length} characters, {MaxText} at most): put the details in a file",
                $"message trop long ({text.Length} caractères, {MaxText} au plus) : mets le détail dans un fichier"));
        }
        if (vote is not null && !Votes.Contains(vote, StringComparer.Ordinal))
            throw new RelayError(RelayPaths.Tr($"vote: {string.Join(" or ", Votes)}", $"vote : {string.Join(" ou ", Votes)}"));

        var sources = CheckFiles(files ?? []);
        var contents = new List<(string Path, byte[] Data)>();
        int budget = MaxAttachmentsTotal;
        foreach (var src in sources)
        {
            var bytes = ReadAttachment(src, budget);
            contents.Add((src, bytes));
            budget -= bytes.Length;
        }

        var messageId = NewId();
        var extraClone = extra is not null ? (JsonObject)extra.DeepClone() : new JsonObject();
        var attachFolder = Path.Join(Path.GetFullPath(Root), "attachments", messageId);
        try
        {
            if (contents.Count > 0)
            {
                var arr = new JsonArray();
                for (int i = 0; i < contents.Count; i++)
                {
                    arr.Add(SaveAttachment(attachFolder, i + 1, contents[i].Path, contents[i].Data));
                }
                extraClone["attachments"] = arr;
            }
            var message = new RelayMessage(messageId, sender, to, topic, NowIso(), text, replyTo, info, vote, extraClone);
            WriteNew(Path.Join(MessagesFolder, $"{message.Id}.{sender}.{to}.{topic}.json"), message.ToJson());
            Render(topic);
            return message;
        }
        catch
        {
            try { if (Directory.Exists(attachFolder)) Directory.Delete(attachFolder, recursive: true); } catch (Exception cleanup) when (cleanup is IOException or UnauthorizedAccessException) { /* Best-effort removal; the original error is rethrown. */ }
            throw;
        }
    }

    private static JsonObject SaveAttachment(string folder, int number, string sourcePath, byte[] data)
    {
        Directory.CreateDirectory(folder);
        var origName = Path.GetFileName(sourcePath);
        var safe = SafeFileRegex().Replace(origName, "_");
        if (safe.Length > 96) safe = safe[..96];
        if (string.IsNullOrEmpty(safe)) safe = "fichier";
        var target = Path.Join(folder, $"{number:D2}-{safe}");
        using (var fs = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        {
            fs.Write(data, 0, data.Length);
        }
        var mime = GuessMime(origName);
        var sha256 = Convert.ToHexStringLower(SHA256.HashData(data));
        return new JsonObject
        {
            ["name"] = origName,
            ["path"] = target,
            ["size"] = data.Length,
            ["mime"] = mime,
            ["sha256"] = sha256,
        };
    }

    private static string GuessMime(string fileName)
    {
        var ext = Path.GetExtension(fileName).ToLowerInvariant();
        return ext switch
        {
            ".png" => "image/png",
            ".jpg" or ".jpeg" => "image/jpeg",
            ".gif" => "image/gif",
            ".webp" => "image/webp",
            ".svg" => "image/svg+xml",
            ".txt" or ".md" or ".log" => "text/plain",
            ".json" => "application/json",
            ".pdf" => "application/pdf",
            _ => "application/octet-stream",
        };
    }

    public RelayMessage? Get(string messageId)
    {
        var path = FindMessagePath(messageId);
        var data = path is not null ? ReadJson(path) : null;
        return data is not null ? RelayMessage.FromJson(data) : null;
    }

    public List<RelayMessage> All()
    {
        if (!Directory.Exists(MessagesFolder))
            return [];
        return LoadMessages(Directory.EnumerateFiles(MessagesFolder, "*.json"));
    }

    private List<RelayMessage> Since(string pattern, double sinceEpoch)
    {
        if (!Directory.Exists(MessagesFolder))
            return [];
        var stamp = sinceEpoch > 0
            ? DateTimeOffset.FromUnixTimeSeconds((long)sinceEpoch).UtcDateTime.ToString("yyyyMMddTHHmmss", CultureInfo.InvariantCulture)
            : "";
        var files = Directory.EnumerateFiles(MessagesFolder, pattern)
            .Where(p => string.CompareOrdinal(Path.GetFileName(p)[..Math.Min(15, Path.GetFileName(p).Length)], stamp) >= 0);
        return LoadMessages(files);
    }

    public List<RelayMessage> AddressedTo(string agent, double since = 0.0)
        => Since($"*.*.{RelayPaths.NormalizeParty(agent)}.*.json", since);

    public List<RelayMessage> SentBy(string agent, double since = 0.0)
        => Since($"*.{RelayPaths.NormalizeParty(agent)}.*.*.json", since);

    public List<RelayMessage> TopicMessages(string topic)
    {
        var t = CheckTopic(topic);
        if (!Directory.Exists(MessagesFolder))
            return [];
        return LoadMessages(Directory.EnumerateFiles(MessagesFolder, $"*.*.*.{t}.json"));
    }

    public List<RelayMessage> NewInTopic(string topic, HashSet<string> seen)
    {
        var t = CheckTopic(topic);
        if (!Directory.Exists(MessagesFolder))
            return [];
        var fresh = new List<string>();
        foreach (var p in Directory.EnumerateFiles(MessagesFolder, $"*.*.*.{t}.json"))
        {
            var name = Path.GetFileName(p);
            if (seen.Add(name))
                fresh.Add(p);
        }
        return LoadMessages(fresh);
    }

    public List<RelayMessage> Replies(string messageId)
    {
        var original = Get(messageId);
        var pool = original is not null ? TopicMessages(original.Topic) : All();
        return pool.Where(m => m.ReplyTo == messageId).ToList();
    }

    // --- Read / Unread markers
    private string ReadMarkPath(string agent, string messageId)
        => Path.Join(Root, "read", agent, messageId);

    public bool IsRead(RelayMessage message)
        => File.Exists(ReadMarkPath(message.To, message.Id));

    public void MarkRead(RelayMessage message)
    {
        var mark = ReadMarkPath(message.To, message.Id);
        if (!File.Exists(mark))
        {
            var dir = Path.GetDirectoryName(mark)!;
            Directory.CreateDirectory(dir);
            var tmp = Path.Join(dir, $".{Path.GetFileName(mark)}.{RandomHex(4)}.tmp");
            File.WriteAllText(tmp, NowIso(), Utf8NoBom);
            try
            {
                File.Move(tmp, mark, overwrite: true);
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                try { File.Delete(tmp); } catch (Exception cleanup) when (cleanup is IOException or UnauthorizedAccessException) { /* Best-effort removal of the temporary file. */ }
            }
        }
    }

    public string? ReadAt(RelayMessage message)
    {
        try
        {
            var mark = ReadMarkPath(message.To, message.Id);
            if (!File.Exists(mark)) return null;
            var txt = File.ReadAllText(mark, Encoding.UTF8).Trim();
            return string.IsNullOrEmpty(txt) ? null : txt;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    public List<RelayMessage> Unread(string agent)
    {
        agent = RelayPaths.NormalizeParty(agent);
        var marksDir = Path.Join(Root, "read", agent);
        var done = Directory.Exists(marksDir)
            ? Directory.EnumerateFiles(marksDir).Select(Path.GetFileName).Where(n => n is not null).ToHashSet(StringComparer.Ordinal)!
            : new HashSet<string>(StringComparer.Ordinal);
        if (!Directory.Exists(MessagesFolder))
            return [];
        var paths = Directory.EnumerateFiles(MessagesFolder, $"*.*.{agent}.*.json")
            .Where(p =>
            {
                var fileName = Path.GetFileName(p);
                var dot = fileName.IndexOf('.');
                var id = dot > 0 ? fileName[..dot] : fileName;
                return !done.Contains(id);
            });
        return LoadMessages(paths);
    }

    // --- Events
    public void Event(string messageId, string kind, JsonObject? extra = null)
    {
        var folder = Path.Combine(Root, "events", messageId);
        var nanos = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() * 1_000_000L + Random.Shared.Next(0, 999_999);
        var obj = new JsonObject
        {
            ["at"] = NowIso(),
            ["kind"] = kind,
        };
        if (extra is not null)
        {
            foreach (var kv in extra)
                obj[kv.Key] = kv.Value?.DeepClone();
        }
        WriteNew(Path.Join(folder, $"{nanos:D19}-{RandomHex(2)}.json"), obj);
    }

    public List<JsonObject> Events(string messageId)
    {
        var folder = Path.Combine(Root, "events", messageId);
        if (!Directory.Exists(folder))
            return [];
        var list = new List<JsonObject>();
        foreach (var e in Directory.EnumerateFiles(folder, "*.json").OrderBy(Path.GetFileName, StringComparer.Ordinal).Select(p => ReadJson(p)).OfType<JsonObject>())
            list.Add(e);
        return list;
    }

    public bool HasEvent(string messageId, string kind)
        => Events(messageId).Any(e => e["kind"]?.GetValue<string>() == kind);

    // --- Topic state & project topic resolution
    public JsonObject? TopicState(string topic, string name)
        => ReadJson(Path.Join(Root, "topics", CheckTopic(topic), $"{name}.json"));

    public void SetTopicState(string topic, string name, JsonObject data)
        => WriteState(Path.Join(Root, "topics", CheckTopic(topic), $"{name}.json"), data);

    public string ProjectTopic(string? topic)
    {
        var clean = CheckTopic(topic);
        if (clean == "selftest")
            return clean;

        static string Key(string name) => name.Replace("-", "", StringComparison.Ordinal);

        var topicsDir = Path.Join(Root, "topics");
        var known = Directory.Exists(topicsDir)
            ? Directory.EnumerateDirectories(topicsDir).Select(Path.GetFileName).Where(n => n is not null && n != "selftest").Cast<string>().ToList()
            : [];

        var parents = known.Where(t => Key(t).Length >= 5 && Key(clean) != Key(t) && Key(clean).StartsWith(Key(t), StringComparison.Ordinal)).ToList();
        if (parents.Count > 0)
        {
            var longest = parents.OrderByDescending(t => Key(t).Length).First();
            return ProjectTopic(longest);
        }
        var twins = known.Where(t => Key(t) == Key(clean)).ToList();
        if (twins.Count > 1 || (twins.Count == 1 && twins[0] != clean))
        {
            return twins.OrderByDescending(t => TopicLastAt(t) ?? 0.0).First();
        }
        return clean;
    }

    public double? TopicLastAt(string topic)
    {
        var t = CheckTopic(topic);
        if (!Directory.Exists(MessagesFolder))
            return null;
        var stamps = Directory.EnumerateFiles(MessagesFolder, $"*.*.*.{t}.json")
            .Select(p => Path.GetFileName(p))
            .Where(n => n.Length >= 15)
            .Select(n => n[..15])
            .ToList();
        if (stamps.Count == 0)
            return null;
        var maxStamp = stamps.Max(StringComparer.Ordinal)!;
        if (DateTime.TryParseExact(maxStamp, "yyyyMMddTHHmmss", CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out var dt))
        {
            return new DateTimeOffset(dt, TimeSpan.Zero).ToUnixTimeSeconds();
        }
        return null;
    }

    public bool TopicClosed(string topic)
        => TopicState(topic, "closed")?["closed"]?.GetValue<bool>() == true;

    public bool TopicIdle(string topic, double nowEpoch, double idleS)
    {
        if (TopicClosed(topic))
            return true;
        var last = TopicLastAt(topic);
        return last is not null && (nowEpoch - last.Value) >= idleS;
    }

    public bool TopicFinishedFor(string agent, string topic)
    {
        var agentNorm = RelayPaths.NormalizeParty(agent);
        var top = CheckTopic(topic);
        if (Unread(agentNorm).Any(m => m.Topic == top))
            return false;
        var messages = TopicMessages(top);
        var asks = messages.Where(m => m.To == agentNorm && !m.Info).ToList();
        if (asks.Count == 0)
            return false;
        return asks.All(ask => Replies(ask.Id).Any(r => r.Sender == agentNorm));
    }

    public JsonObject? State(string name)
        => ReadJson(Path.Join(Root, "state", $"{name}.json"));

    public void SetState(string name, JsonObject data)
        => WriteState(Path.Join(Root, "state", $"{name}.json"), data);

    // --- Ring states
    public JsonObject? RingState(string messageId)
        => ReadJson(Path.Combine(Root, "rings", $"{messageId}.json"));

    public void SetRingState(string messageId, JsonObject data)
        => WriteState(Path.Combine(Root, "rings", $"{messageId}.json"), data);

    public List<(string MessageId, JsonObject Data)> RingStates()
    {
        var folder = Path.Join(Root, "rings");
        if (!Directory.Exists(folder))
            return [];
        var list = new List<(string, JsonObject)>();
        foreach (var (p, d) in Directory.EnumerateFiles(folder, "*.json").OrderBy(Path.GetFileName, StringComparer.Ordinal).Select(p => (p, ReadJson(p))).Where(item => item.Item2 is not null))
            list.Add((Path.GetFileNameWithoutExtension(p), d!));
        return list;
    }

    // --- Claims (O_EXCL / FileMode.CreateNew)
    private string ClaimPath(string messageId, int attempt)
    {
        if (!MessageIdRegex().IsMatch(messageId))
        {
            throw new RelayError(RelayPaths.Tr(
                $"invalid message id: '{messageId}'",
                $"identifiant de message invalide : '{messageId}'"));
        }
        return Path.Join(Root, "claims", messageId, $"{attempt}.json");
    }

    public bool Claim(string messageId, int attempt, string owner, int? pidOverride = null, JsonObject? extra = null)
    {
        var path = ClaimPath(messageId, attempt);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        FileStream fs;
        try
        {
            fs = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None);
        }
        catch (IOException)
        {
            return false;
        }
        using (fs)
        {
            var record = new JsonObject
            {
                ["owner"] = owner,
                ["phase"] = "claimed",
                ["at"] = NowIso(),
                ["pid"] = pidOverride ?? Environment.ProcessId,
            };
            if (extra is not null)
            {
                foreach (var kv in extra)
                    record[kv.Key] = kv.Value?.DeepClone();
            }
            var bytes = Utf8NoBom.GetBytes(record.ToJsonString(JsonOptions));
            fs.Write(bytes, 0, bytes.Length);
            fs.Flush(flushToDisk: true);
        }
        return true;
    }

    public JsonObject? ClaimOf(string messageId, int attempt)
    {
        var path = ClaimPath(messageId, attempt);
        if (!File.Exists(path))
            return null;
        return ReadJson(path) ?? new JsonObject { ["phase"] = "claimed" };
    }

    public void SetClaim(string messageId, int attempt, string owner, string phase, JsonObject? extra = null)
    {
        var current = ClaimOf(messageId, attempt) ?? new JsonObject();
        if (current["owner"]?.GetValue<string>() != owner)
        {
            throw new RelayError(RelayPaths.Tr(
                $"delivery {messageId}/{attempt}: {owner} does not own it",
                $"livraison {messageId}/{attempt} : {owner} n'en est pas propriétaire"));
        }
        var updated = (JsonObject)current.DeepClone();
        if (extra is not null)
        {
            foreach (var kv in extra)
                updated[kv.Key] = kv.Value?.DeepClone();
        }
        updated["phase"] = phase;
        updated["updated"] = NowIso();
        WriteState(ClaimPath(messageId, attempt), updated);
    }

    public void Release(string messageId, int attempt, string owner)
    {
        var current = ClaimOf(messageId, attempt);
        if (current?["owner"]?.GetValue<string>() == owner)
        {
            try { File.Delete(ClaimPath(messageId, attempt)); } catch (Exception cleanup) when (cleanup is IOException or UnauthorizedAccessException) { /* Left in place: a later release or recovery retries. */ }
        }
    }

    /// <summary>
    /// Reprise après panne : si le propriétaire actuel d'une réclamation en phase "claimed" est mort (PidAlive == false),
    /// libère la réclamation orpheline pour permettre à un nouveau propriétaire de la prendre.
    /// Si elle était en phase "submitting", ClaimLabel rend "uncertain".
    /// </summary>
    public bool RecoverDeadClaim(string messageId, int attempt)
    {
        var current = ClaimOf(messageId, attempt);
        if (current is null)
            return false;
        int? pid = null;
        if (current["pid"] is JsonValue pv && pv.TryGetValue<int>(out int p))
            pid = p;
        if (PidAlive(pid))
            return false;
        var phase = current["phase"]?.GetValue<string>() ?? "claimed";
        if (phase == "claimed")
        {
            try
            {
                File.Delete(ClaimPath(messageId, attempt));
                return true;
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                return false;
            }
        }
        return false;
    }

    public List<(int Attempt, JsonObject Claim)> Claims(string messageId)
    {
        var folder = Path.Combine(Root, "claims", messageId);
        if (!Directory.Exists(folder))
            return [];
        var found = new List<(int, JsonObject)>();
        foreach (var p in Directory.EnumerateFiles(folder, "*.json"))
        {
            var stem = Path.GetFileNameWithoutExtension(p);
            if (int.TryParse(stem, NumberStyles.None, CultureInfo.InvariantCulture, out int n))
            {
                found.Add((n, ReadJson(p) ?? new JsonObject { ["phase"] = "claimed" }));
            }
        }
        return found.OrderBy(x => x.Item1).ToList();
    }

    public List<(string MessageId, int Attempt, JsonObject Claim)> AllClaims()
    {
        var folder = Path.Join(Root, "claims");
        if (!Directory.Exists(folder))
            return [];
        var all = new List<(string, int, JsonObject)>();
        foreach (var msgId in Directory.EnumerateDirectories(folder).OrderBy(Path.GetFileName, StringComparer.Ordinal).Select(d => Path.GetFileName(d)))
        {
            foreach (var (n, c) in Claims(msgId))
            {
                all.Add((msgId, n, c));
            }
        }
        return all;
    }

    // --- Salon Markdown rendering
    public string Render(string topic)
    {
        var lines = new List<string> { $"# Nexus Relay · {topic}", "" };
        foreach (var m in TopicMessages(topic))
        {
            var head = $"## {m.Created} · {m.Sender} → {m.To}";
            if (!string.IsNullOrEmpty(m.Vote))
                head += $" · vote : {m.Vote}";
            if (m.Info)
                head += " · pour info";
            lines.Add(head);
            lines.Add("");
            lines.Add(m.Text.Trim());
            lines.Add("");
            if (m.ExtraObj["attachments"] is JsonArray attachments && attachments.Count > 0)
            {
                foreach (var item in attachments.OfType<JsonObject>())
                {
                    var p = item["path"]?.GetValue<string>() ?? "";
                    var uri = !string.IsNullOrEmpty(p) ? new Uri(Path.GetFullPath(p)).AbsoluteUri : "";
                    var name = item["name"]?.GetValue<string>() ?? "?";
                    var size = item["size"]?.GetValue<int>() ?? 0;
                    lines.Add($"- Pièce jointe : [{name}]({uri}) ({size} octets)");
                }
                lines.Add("");
            }
        }
        var path = Path.Join(Root, "salon", $"{topic}.md");
        var dir = Path.GetDirectoryName(path)!;
        Directory.CreateDirectory(dir);
        var tmp = Path.Join(dir, $".{Path.GetFileName(path)}.{RandomHex(4)}.tmp");
        File.WriteAllText(tmp, string.Join("\n", lines), Utf8NoBom);
        try
        {
            File.Move(tmp, path, overwrite: true);
        }
        catch (UnauthorizedAccessException)
        {
            try { File.Delete(tmp); } catch (Exception cleanup) when (cleanup is IOException or UnauthorizedAccessException) { /* Best-effort removal of the temporary file. */ }
        }
        catch (IOException)
        {
            try { File.Delete(tmp); } catch (Exception cleanup) when (cleanup is IOException or UnauthorizedAccessException) { /* Best-effort removal of the temporary file. */ }
        }
        return path;
    }
}
