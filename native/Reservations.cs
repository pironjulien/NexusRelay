using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusRelay;

public static partial class Reservations
{
    public const int MaxPaths = 32;
    public const int TtlMin = 1;
    public const int TtlMax = 24 * 60;
    public const double KeepEndedSeconds = 24 * 3600;
    public const string AutoReason = "modification en cours (réservation automatique)";

    private static readonly Lock SyncLock = new();
    private const string Blanks = " \t\r\n\f\v";

    public static double Now()
        => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

    public static string Iso(double epochSeconds)
        => DateTimeOffset.FromUnixTimeMilliseconds((long)(epochSeconds * 1000)).UtcDateTime
            .ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture);

    private static (string Drive, List<string> Parts, bool KeepExtended) Split(string rawPath)
    {
        var path = rawPath.TrimStart(Blanks.ToCharArray()).Replace('/', '\\');
        bool extended = path.StartsWith(@"\\?\", StringComparison.Ordinal);
        if (!extended)
            path = path.TrimEnd(Blanks.ToCharArray());

        if (path.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase) ||
            path.StartsWith(@"\\.\UNC\", StringComparison.OrdinalIgnoreCase))
        {
            path = @"\\" + path[8..];
        }
        else if (extended || path.StartsWith(@"\\.\", StringComparison.Ordinal))
        {
            path = path[4..];
        }

        if (path.Any(c => c < ' ') ||
            path.StartsWith(@"\\?\", StringComparison.Ordinal) ||
            path.StartsWith(@"\\.\", StringComparison.Ordinal))
        {
            throw new RelayError(RelayPaths.Tr(
                $"reservation: absolute path expected ({rawPath})",
                $"réservation : chemin absolu attendu ({rawPath})"));
        }

        string drive;
        string rest;
        if (path.StartsWith(@"\\", StringComparison.Ordinal))
        {
            // UNC path \\server\share\...
            var withoutPrefix = path[2..];
            var slash1 = withoutPrefix.IndexOf('\\');
            if (slash1 <= 0)
            {
                throw new RelayError(RelayPaths.Tr(
                    $"reservation: network path without server and share ({rawPath})",
                    $"réservation : chemin réseau sans serveur ni partage ({rawPath})"));
            }
            var afterServer = withoutPrefix[(slash1 + 1)..];
            var slash2 = afterServer.IndexOf('\\');
            var server = withoutPrefix[..slash1];
            var share = slash2 >= 0 ? afterServer[..slash2] : afterServer;
            if (string.IsNullOrEmpty(server.Trim('.', ' ')) || string.IsNullOrEmpty(share.Trim('.', ' ')))
            {
                throw new RelayError(RelayPaths.Tr(
                    $"reservation: network path without server and share ({rawPath})",
                    $"réservation : chemin réseau sans serveur ni partage ({rawPath})"));
            }
            drive = $@"\\{server}\{share}";
            rest = slash2 >= 0 ? afterServer[(slash2 + 1)..] : "";
        }
        else if (path.Length >= 3 && char.IsAsciiLetter(path[0]) && path[1] == ':' && path[2] == '\\')
        {
            drive = path[..2];
            rest = path[3..];
        }
        else
        {
            throw new RelayError(RelayPaths.Tr(
                $"reservation: absolute path expected ({rawPath})",
                $"réservation : chemin absolu attendu ({rawPath})"));
        }

        var rawParts = rest.Split('\\', StringSplitOptions.RemoveEmptyEntries);
        var normalizedParts = new List<string>();
        foreach (var seg in rawParts.Where(s => s != "."))
        {
            if (seg == "..")
            {
                if (normalizedParts.Count > 0)
                    normalizedParts.RemoveAt(normalizedParts.Count - 1);
                continue;
            }
            normalizedParts.Add(seg);
        }

        bool keep = false;
        if (!extended)
        {
            for (int i = 0; i < normalizedParts.Count; i++)
            {
                normalizedParts[i] = normalizedParts[i].TrimEnd('.', ' ');
                if (string.IsNullOrEmpty(normalizedParts[i]))
                {
                    throw new RelayError(RelayPaths.Tr(
                        $"reservation: empty name in the path ({rawPath})",
                        $"réservation : nom vide dans le chemin ({rawPath})"));
                }
            }
        }
        else
        {
            keep = normalizedParts.Any(p => p != p.TrimEnd('.', ' '));
        }

        return (drive, normalizedParts, keep);
    }

    public static string Clean(string path)
    {
        var (drive, parts, keep) = Split(path);
        var body = drive + @"\" + string.Join(@"\", parts);
        if (!keep)
            return body;
        return body.StartsWith(@"\\", StringComparison.Ordinal)
            ? @"\\?\UNC\" + body[2..]
            : @"\\?\" + body;
    }

    public static string Norm(string path)
        => Clean(path).ToLowerInvariant();

    private static List<string> Segments(string path)
    {
        var (drive, parts, _) = Split(path);
        var list = new List<string>(parts.Count + 1) { drive.ToLowerInvariant() };
        foreach (var p in parts)
            list.Add(p.ToLowerInvariant());
        return list;
    }

    private static bool HasWildcard(string segment)
        => segment.IndexOfAny(['*', '?', '[']) >= 0;

    private static bool WildcardMatch(string text, string pattern)
    {
        // Convert fnmatch pattern to regex
        var sb = new StringBuilder("^");
        for (int i = 0; i < pattern.Length; i++)
        {
            char c = pattern[i];
            if (c == '*') sb.Append(".*");
            else if (c == '?') sb.Append('.');
            else if (c == '[')
            {
                int close = pattern.IndexOf(']', i + 1);
                if (close > i)
                {
                    var inner = pattern[(i + 1)..close];
                    if (inner.StartsWith('!'))
                        inner = "^" + inner[1..];
                    sb.Append('[').Append(inner).Append(']');
                    i = close;
                }
                else
                {
                    sb.Append(@"\[");
                }
            }
            else
            {
                sb.Append(Regex.Escape(c.ToString()));
            }
        }
        sb.Append('$');
        try
        {
            return Regex.IsMatch(text, sb.ToString(), RegexOptions.CultureInvariant);
        }
        catch (ArgumentException)
        {
            return true;
        }
    }

    private static bool Meet(string a, string b)
    {
        bool wa = HasWildcard(a);
        bool wb = HasWildcard(b);
        if (!wa && !wb)
            return a == b;
        if (!wa || !wb)
            return wb ? WildcardMatch(a, b) : WildcardMatch(b, a);

        int firstWildA = a.IndexOfAny(['*', '?', '[']);
        int firstWildB = b.IndexOfAny(['*', '?', '[']);
        var headA = firstWildA >= 0 ? a[..firstWildA] : a;
        var headB = firstWildB >= 0 ? b[..firstWildB] : b;

        int lastWildA = a.LastIndexOfAny(['*', '?', ']']);
        int lastWildB = b.LastIndexOfAny(['*', '?', ']']);
        var tailA = lastWildA >= 0 ? a[(lastWildA + 1)..] : a;
        var tailB = lastWildB >= 0 ? b[(lastWildB + 1)..] : b;

        return (headA.StartsWith(headB, StringComparison.Ordinal) || headB.StartsWith(headA, StringComparison.Ordinal)) &&
               (tailA.EndsWith(tailB, StringComparison.Ordinal) || tailB.EndsWith(tailA, StringComparison.Ordinal));
    }

    public static bool Overlap(string a, string b)
    {
        var sa = Segments(a);
        var sb = Segments(b);
        int count = Math.Min(sa.Count, sb.Count);
        for (int i = 0; i < count; i++)
        {
            var x = sa[i];
            var y = sb[i];
            if (x == "**" || y == "**")
                return true;
            if (!Meet(x, y))
                return false;
        }
        return true;
    }

    private static string Folder(RelayBox box) => Path.Join(box.Root, "reservations");

    private static IDisposable BetweenProcesses(RelayBox box, double waitSeconds = 10.0)
    {
        var folder = Folder(box);
        Directory.CreateDirectory(folder);
        var lockFile = Path.Join(folder, ".lock");
        var sw = Stopwatch.StartNew();
        while (true)
        {
            try
            {
                var fs = new FileStream(lockFile, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.ReadWrite);
                try
                {
                    fs.Lock(0, 1);
                    return new FileLockHandle(fs);
                }
                catch
                {
                    fs.Dispose();
                    throw;
                }
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                if (sw.Elapsed.TotalSeconds > waitSeconds)
                {
                    throw new RelayError(RelayPaths.Tr(
                        "reservations busy, try again",
                        "réservations occupées, réessayer"));
                }
                Thread.Sleep(25);
            }
        }
    }

    private sealed class FileLockHandle(FileStream stream) : IDisposable
    {
        public void Dispose()
        {
            try { stream.Unlock(0, 1); } catch (Exception e) when (e is IOException or ObjectDisposedException) { /* Already unlocked: closing the stream releases it anyway. */ }
            stream.Dispose();
        }
    }

    public static List<JsonObject> Active(RelayBox box, double? at = null)
    {
        double checkAt = at ?? Now();
        var folder = Folder(box);
        if (!Directory.Exists(folder))
            return [];
        var list = new List<JsonObject>();
        foreach (var r in Directory.EnumerateFiles(folder, "*.json").OrderBy(Path.GetFileName, StringComparer.Ordinal).Select(p => RelayBox.ReadJson(p)).OfType<JsonObject>())
        {
            if (r["released"] is not null)
                continue;
            double expiresAt = r["expires_at"]?.GetValue<double>() ?? 0.0;
            if (expiresAt <= checkAt)
                continue;
            if (r["owner_pid"] is JsonValue pv && pv.TryGetValue<int>(out int pid) && !RelayBox.PidAlive(pid))
                continue;
            list.Add(r);
        }
        return list;
    }

    public static bool OtherHolder(JsonObject held, string agent, string? session)
    {
        var heldAgent = held["agent"]?.GetValue<string>() ?? "";
        if (heldAgent != agent)
            return true;
        var theirs = held["session"]?.GetValue<string>();
        return !string.IsNullOrEmpty(session) && !string.IsNullOrEmpty(theirs) && theirs != session;
    }

    public static string Holder(JsonObject record, string agent)
    {
        var recAgent = record["agent"]?.GetValue<string>() ?? "";
        bool same = recAgent == agent;
        return recAgent + (same ? RelayPaths.Tr(" (other conversation)", " (autre conversation)") : "");
    }

    private static List<(JsonObject Held, string Mine, string Theirs)> Conflicts(
        RelayBox box,
        string agent,
        IReadOnlyList<string> paths,
        bool exclusive,
        string? session = null)
    {
        var found = new List<(JsonObject, string, string)>();
        foreach (var held in Active(box))
        {
            bool heldExclusive = held["exclusive"]?.GetValue<bool>() ?? true;
            if (!OtherHolder(held, agent, session) || !(exclusive || heldExclusive))
                continue;
            var theirPaths = (held["paths"] as JsonArray)?.Select(x => x?.GetValue<string>() ?? "").Where(s => s.Length > 0).ToList() ?? [];
            foreach (var mine in paths)
            {
                foreach (var theirs in theirPaths.Where(t => Overlap(mine, t)))
                    found.Add((held, mine, theirs));
            }
        }
        return found;
    }

    public static JsonObject Reserve(
        RelayBox box,
        string agent,
        IReadOnlyList<string> paths,
        string reason,
        int ttlMin = 60,
        bool exclusive = true,
        string? session = null,
        bool automatic = false,
        string? tool = null,
        int? ownerPid = null)
    {
        agent = RelayPaths.NormalizeParty(agent);
        if (paths is null || paths.Count == 0 || paths.Count > MaxPaths)
        {
            throw new RelayError(RelayPaths.Tr(
                $"reservation: 1 to {MaxPaths} paths",
                $"réservation : de 1 à {MaxPaths} chemins"));
        }
        if (string.IsNullOrWhiteSpace(reason))
        {
            throw new RelayError(RelayPaths.Tr(
                "reservation: say why (reason)",
                "réservation : dis pourquoi (reason)"));
        }
        if (ttlMin < TtlMin || ttlMin > TtlMax)
        {
            throw new RelayError(RelayPaths.Tr(
                $"reservation: ttl_min from {TtlMin} to {TtlMax}",
                $"réservation : ttl_min de {TtlMin} à {TtlMax}"));
        }

        var dedup = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var p in paths)
        {
            dedup[Norm(p)] = Clean(p);
        }
        var wanted = dedup.OrderBy(kv => kv.Key, StringComparer.Ordinal).Select(kv => kv.Value).ToList();
        session = !string.IsNullOrWhiteSpace(session) ? session.Trim()[..Math.Min(120, session.Trim().Length)] : null;

        lock (SyncLock)
        {
            using var _ = BetweenProcesses(box);
            return ReserveLocked(box, agent, wanted, reason, ttlMin, exclusive, session, automatic, tool, ownerPid);
        }
    }

    private static JsonObject ReserveLocked(
        RelayBox box,
        string agent,
        List<string> wanted,
        string reason,
        int ttlMin,
        bool exclusive,
        string? session,
        bool automatic,
        string? tool,
        int? ownerPid)
    {
        var clash = Conflicts(box, agent, wanted, exclusive, session);
        if (clash.Count > 0)
        {
            var lines = clash.Take(5).Select(c =>
            {
                var exp = c.Held["expires"]?.GetValue<string>() ?? "";
                var rsn = c.Held["reason"]?.GetValue<string>() ?? "";
                return RelayPaths.Tr(
                    $"{c.Mine} overlaps {c.Theirs}, reserved by {Holder(c.Held, agent)} ({rsn}) until {exp}",
                    $"{c.Mine} recouvre {c.Theirs}, réservé par {Holder(c.Held, agent)} ({rsn}) jusqu'à {exp}");
            });
            var sep = RelayPaths.Tr("; ", " ; ");
            throw new RelayError(RelayPaths.Tr("reservation refused: ", "réservation refusée : ") + string.Join(sep, lines));
        }

        double at = Now();
        var key = wanted.Select(Norm).ToList();
        JsonObject? same = null;
        foreach (var r in Active(box, at).Where(r => r["agent"]?.GetValue<string>() == agent))
        {
            if ((r["session"]?.GetValue<string>() ?? null) != session)
                continue;
            if ((r["automatic"]?.GetValue<bool>() ?? false) != automatic)
                continue;
            bool rIsAutoReason = (r["reason"]?.GetValue<string>() ?? "") == AutoReason;
            if (rIsAutoReason != (reason == AutoReason))
                continue;
            var rPaths = (r["paths"] as JsonArray)?.Select(x => Norm(x!.GetValue<string>())).OrderBy(x => x, StringComparer.Ordinal).ToList() ?? [];
            if (rPaths.SequenceEqual(key, StringComparer.Ordinal))
            {
                same = r;
                break;
            }
        }

        var id = same?["id"]?.GetValue<string>()
            ?? $"{DateTimeOffset.FromUnixTimeSeconds((long)at).UtcDateTime.ToString("yyyyMMddTHHmmss", CultureInfo.InvariantCulture)}-{RelayBox.RandomHex(3)}";
        var created = same?["created"]?.GetValue<string>() ?? Iso(at);
        double expiresAt = at + ttlMin * 60.0;

        var record = same is not null ? (JsonObject)same.DeepClone() : new JsonObject();
        record["id"] = id;
        record["agent"] = agent;
        if (session is not null)
            record["session"] = session;
        var pathsArray = new JsonArray();
        foreach (var w in wanted)
            pathsArray.Add(w);
        record["paths"] = pathsArray;
        var trimmedReason = reason.Trim();
        record["reason"] = trimmedReason[..Math.Min(300, trimmedReason.Length)];
        record["exclusive"] = exclusive;
        record["created"] = created;
        record["expires"] = Iso(expiresAt);
        record["expires_at"] = expiresAt;
        if (ownerPid is not null)
            record["owner_pid"] = ownerPid.Value;

        if (automatic)
        {
            record["automatic"] = true;
            if (same?["absent_at_start"] is JsonArray existingAbsent)
            {
                record["absent_at_start"] = existingAbsent.DeepClone();
            }
            else
            {
                var absentArr = new JsonArray();
                foreach (var w in wanted.Where(w => !File.Exists(w) && !Directory.Exists(w)))
                    absentArr.Add(Norm(w));
                record["absent_at_start"] = absentArr;
            }

            var pendingTools = new SortedSet<string>(StringComparer.Ordinal);
            if (same?["pending_tools"] is JsonArray existingPending)
            {
                foreach (var s in existingPending.Select(pt => pt?.GetValue<string>()).OfType<string>().Where(s => s.Length > 0))
                    pendingTools.Add(s);
            }
            if (!string.IsNullOrEmpty(tool))
                pendingTools.Add(tool);
            var pendingArr = new JsonArray();
            foreach (var pt in pendingTools)
                pendingArr.Add(pt);
            record["pending_tools"] = pendingArr;
            if (string.IsNullOrEmpty(tool))
                record["pending_unknown"] = true;
        }

        RelayBox.WriteState(Path.Join(Folder(box), $"{id}.json"), record);
        Purge(box, at);
        return record;
    }

    private static void Purge(RelayBox box, double at)
    {
        var folder = Folder(box);
        if (!Directory.Exists(folder))
            return;
        foreach (var path in Directory.EnumerateFiles(folder, "*.json"))
        {
            var record = RelayBox.ReadJson(path);
            if (record is null)
                continue;
            double ended = record["expires_at"]?.GetValue<double>() ?? 0.0;
            if (record["released"] is not null)
            {
                try
                {
                    var mtime = new DateTimeOffset(File.GetLastWriteTimeUtc(path)).ToUnixTimeSeconds();
                    ended = Math.Min(ended, mtime);
                }
                catch (Exception e) when (e is IOException or UnauthorizedAccessException)
                {
                    continue;
                }
            }
            if (ended > 0 && (at - ended) > KeepEndedSeconds)
            {
                try { File.Delete(path); } catch (Exception e) when (e is IOException or UnauthorizedAccessException) { /* Purged on a later pass. */ }
            }
        }
    }

    public static List<string> Release(RelayBox box, string agent, string? reservation = null, bool force = false)
    {
        agent = RelayPaths.NormalizeParty(agent);
        // force: the host takes over, on the user's request, an absent agent's reservation; traced, files untouched.
        if (force && (string.IsNullOrEmpty(reservation) || RelayPaths.RelayHost() is var host && host != agent && host != "all"))
            throw new RelayError(RelayPaths.Tr("release force: host only, with the reservation id", "release force : réservé à l'hôte, avec l'id de la réservation"));
        lock (SyncLock)
        {
            using var _ = BetweenProcesses(box);
            return ReleaseLocked(box, agent, reservation, force);
        }
    }

    private static List<string> ReleaseLocked(RelayBox box, string agent, string? reservation, bool force = false)
    {
        var held = Active(box);
        if (!string.IsNullOrEmpty(reservation))
        {
            var target = held.FirstOrDefault(r => r["id"]?.GetValue<string>() == reservation);
            if (target is null)
            {
                throw new RelayError(RelayPaths.Tr(
                    $"reservation {reservation} not found or already ended",
                    $"réservation {reservation} introuvable ou déjà finie"));
            }
            var targetAgent = target["agent"]?.GetValue<string>() ?? "";
            if (force && targetAgent != agent)
            {
                var taken = (JsonObject)target.DeepClone();
                taken["released"] = Iso(Now());
                taken["released_by"] = agent;
                RelayBox.WriteState(Path.Join(Folder(box), $"{reservation}.json"), taken);
                return [reservation];
            }
            if (targetAgent != agent)
            {
                throw new RelayError(RelayPaths.Tr(
                    $"reservation {reservation}: not yours ({targetAgent})",
                    $"réservation {reservation} : pas à toi ({targetAgent})"));
            }
            held = [target];
        }
        else
        {
            held = held.Where(r => r["session"] is null).ToList();
        }

        var done = new List<string>();
        foreach (var record in held.Where(r => r["agent"]?.GetValue<string>() == agent))
        {
            var id = record["id"]!.GetValue<string>();
            var clone = (JsonObject)record.DeepClone();
            clone["released"] = Iso(Now());
            RelayBox.WriteState(Path.Join(Folder(box), $"{id}.json"), clone);
            done.Add(id);
        }
        return done;
    }

    public static List<JsonObject> Blocking(string agent, IReadOnlyList<string> files, RelayBox? box = null, string? session = null)
    {
        box ??= new RelayBox();
        agent = RelayPaths.NormalizeParty(agent);
        var list = new List<JsonObject>();
        foreach (var r in Active(box))
        {
            bool exclusive = r["exclusive"]?.GetValue<bool>() ?? true;
            if (!OtherHolder(r, agent, session) || !exclusive)
                continue;
            var theirPaths = (r["paths"] as JsonArray)?.Select(x => x?.GetValue<string>() ?? "").Where(s => s.Length > 0).ToList() ?? [];
            foreach (var f in files)
            {
                var absF = Path.GetFullPath(f);
                if (theirPaths.Any(theirs => Overlap(theirs, absF)))
                {
                    list.Add(new JsonObject
                    {
                        ["agent"] = Holder(r, agent),
                        ["reason"] = r["reason"]?.GetValue<string>() ?? "",
                        ["expires"] = r["expires"]?.GetValue<string>() ?? "",
                        ["file"] = f,
                    });
                }
            }
        }
        return list;
    }

    private static bool Unchanged(string path, bool absentAtStart)
    {
        if (path.IndexOfAny(['*', '?']) >= 0 || Directory.Exists(path))
            return false;
        try
        {
            if (File.Exists(path) && (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
                return false;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or ArgumentException)
        {
            return false;
        }

        var directory = Path.GetDirectoryName(path);
        while (!string.IsNullOrEmpty(directory) && !Directory.Exists(directory))
        {
            var parent = Path.GetDirectoryName(directory);
            if (parent == directory)
                return false;
            directory = parent;
        }
        if (string.IsNullOrEmpty(directory))
            return false;

        string? RunGit(params string[] args)
        {
            try
            {
                var psi = new ProcessStartInfo("git")
                {
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    StandardOutputEncoding = Encoding.UTF8,
                };
                psi.Environment["GIT_OPTIONAL_LOCKS"] = "0";
                psi.ArgumentList.Add("--literal-pathspecs");
                psi.ArgumentList.Add("-c");
                psi.ArgumentList.Add("core.fsmonitor=false");
                psi.ArgumentList.Add("-C");
                psi.ArgumentList.Add(directory);
                foreach (var a in args)
                    psi.ArgumentList.Add(a);

                using var proc = Process.Start(psi);
                if (proc is null)
                    return null;
                var output = proc.StandardOutput.ReadToEnd();
                if (!proc.WaitForExit(5000) || proc.ExitCode != 0)
                    return null;
                return output;
            }
            catch (Exception e) when (e is System.ComponentModel.Win32Exception or InvalidOperationException or IOException)
            {
                return null;
            }
        }

        var tracked = RunGit("ls-files", "--stage", "-v", "-z", "--", path);
        if (tracked is null)
            return false;
        bool unwritten = string.IsNullOrEmpty(tracked) && absentAtStart && !File.Exists(path) && !Directory.Exists(path);
        bool ordinary = tracked.StartsWith("H ", StringComparison.Ordinal) && tracked.Split('\0').Length == 2;
        if (!unwritten && !ordinary)
            return false;

        var status = RunGit("status", "--porcelain=v1", "--untracked-files=all", "--ignored", "--", path);
        if (status is null || !string.IsNullOrEmpty(status))
            return false;
        if (unwritten)
            return true;

        var tabIdx = tracked.IndexOf('\t');
        if (tabIdx < 0)
            return false;
        var fields = tracked[..tabIdx].Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (fields.Length != 4 || (fields[1] != "100644" && fields[1] != "100755") || fields[3] != "0")
            return false;

        var hash = RunGit("hash-object", $"--path={path}", "--", path);
        return hash is not null && hash.Trim() == fields[2];
    }

    public static List<string> FinishEdits(
        string agent,
        string? session,
        string? tool = null,
        bool sessionEnded = false,
        RelayBox? box = null)
    {
        if (string.IsNullOrWhiteSpace(session) || (!sessionEnded && string.IsNullOrEmpty(tool)))
            return [];
        box ??= new RelayBox();
        agent = RelayPaths.NormalizeParty(agent);
        session = session.Trim()[..Math.Min(120, session.Trim().Length)];
        var ended = new List<string>();

        lock (SyncLock)
        {
            using var _ = BetweenProcesses(box);
            foreach (var record in Active(box))
            {
                if (record["agent"]?.GetValue<string>() != agent ||
                    record["session"]?.GetValue<string>() != session ||
                    !(record["automatic"]?.GetValue<bool>() ?? false) ||
                    record["reason"]?.GetValue<string>() != AutoReason)
                {
                    continue;
                }

                var pending = (record["pending_tools"] as JsonArray)?
                    .Select(x => x?.GetValue<string>() ?? "")
                    .Where(s => s.Length > 0)
                    .ToHashSet(StringComparer.Ordinal) ?? new HashSet<string>(StringComparer.Ordinal);

                if (!sessionEnded && (tool is null || !pending.Remove(tool)))
                    continue;

                var pendingArr = new JsonArray();
                foreach (var pt in pending.OrderBy(x => x, StringComparer.Ordinal))
                    pendingArr.Add(pt);
                record["pending_tools"] = pendingArr;

                var absentSet = (record["absent_at_start"] as JsonArray)?
                    .Select(x => x?.GetValue<string>() ?? "")
                    .ToHashSet(StringComparer.Ordinal) ?? new HashSet<string>(StringComparer.Ordinal);
                var recPaths = (record["paths"] as JsonArray)?
                    .Select(x => x?.GetValue<string>() ?? "")
                    .Where(s => s.Length > 0)
                    .ToList() ?? [];

                bool clean = pending.Count == 0 &&
                    !(record["pending_unknown"]?.GetValue<bool>() ?? false) &&
                    recPaths.All(p => Unchanged(p, absentSet.Contains(Norm(p))));

                var id = record["id"]!.GetValue<string>();
                if (clean)
                {
                    record["released"] = Iso(Now());
                    ended.Add(id);
                }
                RelayBox.WriteState(Path.Join(Folder(box), $"{id}.json"), record);
            }
        }
        return ended;
    }

    public static List<string> ReleaseCommitted(string agent, IReadOnlyList<string> paths, string? session, RelayBox? box = null)
    {
        if (string.IsNullOrWhiteSpace(session)) return [];
        box ??= new RelayBox();
        var committed = paths.Select(Norm).ToHashSet(StringComparer.Ordinal);
        lock (SyncLock)
        {
            using var owned = BetweenProcesses(box);
            var finished = Active(box).Where(r => r["agent"]?.ToString() == agent && r["session"]?.ToString() == session &&
                (r["automatic"]?.GetValue<bool>() ?? false) && r["reason"]?.ToString() == AutoReason &&
                (r["pending_tools"] as JsonArray)?.Count == 0 && !(r["pending_unknown"]?.GetValue<bool>() ?? false) &&
                (r["paths"] as JsonArray ?? []).All(p => p is not null && committed.Contains(Norm(p.ToString())) && Unchanged(p.ToString(), true))).Select(r => r["id"]!.GetValue<string>()).ToList();
            return finished.SelectMany(id => ReleaseLocked(box, agent, id)).ToList();
        }
    }

    public static string? SessionFromEnv(string agent)
    {
        string? specific = agent switch { "claude" => "CLAUDE_CODE_SESSION_ID", "codex" => "CODEX_THREAD_ID", "antigravity" => "ANTIGRAVITY_CONVERSATION_ID", _ => null };
        var names = new[] { "NEXUS_RELAY_SESSION", agent == "codex" ? "CODEX_SESSION_ID" : null, specific };
        return names.Where(n => n is not null).Select(n => Environment.GetEnvironmentVariable(n!)).FirstOrDefault(value => !string.IsNullOrWhiteSpace(value))?.Trim();
    }

    #region WhoAmI (Parent process tree inspection)

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct PROCESSENTRY32W
    {
        public uint dwSize;
        public uint cntUsage;
        public uint th32ProcessID;
        public nuint th32DefaultHeapID;
        public uint th32ModuleID;
        public uint cntThreads;
        public uint th32ParentProcessID;
        public int pcPriClassBase;
        public uint dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string szExeFile;
    }

    [LibraryImport("kernel32.dll", SetLastError = true)]
    private static partial IntPtr CreateToolhelp32Snapshot(uint dwFlags, uint th32ProcessID);

    // Process32FirstW/NextW stay DllImport: PROCESSENTRY32W carries a ByValTStr field that LibraryImport cannot marshal.
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool Process32FirstW(IntPtr hSnapshot, ref PROCESSENTRY32W lppe);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool Process32NextW(IntPtr hSnapshot, ref PROCESSENTRY32W lppe);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool CloseHandle(IntPtr hObject);

    private static Dictionary<int, (int ParentPid, string Exe)> ProcessTable()
    {
        var table = new Dictionary<int, (int, string)>();
        if (!OperatingSystem.IsWindows())
            return table;
        IntPtr snap = CreateToolhelp32Snapshot(0x2, 0);
        if (snap == IntPtr.Zero || snap == new IntPtr(-1))
            return table;
        try
        {
            var entry = new PROCESSENTRY32W { dwSize = (uint)Marshal.SizeOf<PROCESSENTRY32W>() };
            if (Process32FirstW(snap, ref entry))
            {
                do
                {
                    table[(int)entry.th32ProcessID] = ((int)entry.th32ParentProcessID, (entry.szExeFile ?? "").ToLowerInvariant());
                }
                while (Process32NextW(snap, ref entry));
            }
        }
        finally
        {
            CloseHandle(snap);
        }
        return table;
    }

    public static string WhoAmI()
    {
        var named = (Environment.GetEnvironmentVariable("NEXUS_RELAY_AGENT") ?? "").Trim();
        if (!string.IsNullOrEmpty(named))
            return RelayPaths.NormalizeParty(named);

        var table = ProcessTable();
        int pid = Environment.ProcessId;
        var seen = new HashSet<int>();
        while (table.TryGetValue(pid, out var info) && seen.Add(pid))
        {
            pid = info.ParentPid;
            var exe = info.Exe;
            if (exe.StartsWith("claude", StringComparison.Ordinal))
                return "claude";
            if (exe.StartsWith("codex", StringComparison.Ordinal))
                return "codex";
            if (exe.StartsWith("antigravity", StringComparison.Ordinal))
                return "antigravity";
        }
        return RelayPaths.NormalizeParty("user");
    }

    public static int? AncestorPid(string executable)
    {
        var table = ProcessTable();
        var visited = new HashSet<int>();
        int pid = Environment.ProcessId;
        while (table.TryGetValue(pid, out var info) && visited.Add(pid))
        {
            if (info.Exe.Equals(executable, StringComparison.OrdinalIgnoreCase)) return pid;
            pid = info.ParentPid;
        }
        return null;
    }

    #endregion
}
