using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json.Nodes;

namespace NexusRelay;

public sealed record AppChat(string Id, string Path, string Cwd, bool Archived, bool Child);
public sealed record AppTurn(bool Finished, double? At, string? Error = null, double? Last = null);

public static class AppSessions
{
    public static string CodexHome => Environment.GetEnvironmentVariable("CODEX_HOME") ?? System.IO.Path.Combine(RelayPaths.UserHome(), ".codex");
    public static string RelayDirectory => CodexSpace.Configured()["path"]!.ToString();
    public static List<AppChat> CodexChats()
    {
        string db = System.IO.Path.Combine(CodexHome, "state_5.sqlite");
        if (!File.Exists(db)) return [];
        // Windows ships SQLite. Always read-only, including WAL-backed app databases.
        nint handle = 0, statement = 0;
        try
        {
            if (SqliteOpen(db, out handle, 1, 0) != 0) throw new RelayError("Codex app database cannot be opened read-only");
            SqliteBusy(handle, 2000);
            const string sql = "SELECT t.id,t.rollout_path,t.cwd,t.archived,EXISTS(SELECT 1 FROM thread_spawn_edges e WHERE e.child_thread_id=t.id) FROM threads t";
            if (SqlitePrepare(handle, sql, -1, out statement, 0) != 0) throw new RelayError("Codex app database schema is unreadable");
            var result = new List<AppChat>();
            int code;
            while ((code = SqliteStep(statement)) == 100)
                result.Add(new(Text(statement, 0), Text(statement, 1), Text(statement, 2), SqliteInt(statement, 3) != 0, SqliteInt(statement, 4) != 0));
            if (code != 101) throw new RelayError("Codex app database read did not finish");
            return result;
        }
        finally { if (statement != 0) SqliteFinalize(statement); if (handle != 0) SqliteClose(handle); }
    }
    private static string Text(nint statement, int i) => Marshal.PtrToStringUTF8(SqliteText(statement, i)) ?? "";
    public static bool IsRelay(AppChat chat) => !chat.Child && SameDirectory(chat.Cwd, RelayDirectory);
    private static bool SameDirectory(string left, string right)
    {
        try { return RelayBox.PlainPath(left).Equals(RelayBox.PlainPath(right), StringComparison.Ordinal); }
        catch (ArgumentException) { return false; }
    }
    public static string Tail(string path, int bytes = 262144)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        stream.Seek(Math.Max(0, stream.Length - bytes), SeekOrigin.Begin);
        using var reader = new StreamReader(stream, Encoding.UTF8);
        return reader.ReadToEnd();
    }
    public static IEnumerable<JsonObject> Lines(string text)
    {
        foreach (string line in text.Split('\n'))
        {
            JsonObject? obj = null;
            try { obj = JsonNode.Parse(line) as JsonObject; } catch (System.Text.Json.JsonException) { }
            if (obj is not null) yield return obj;
        }
    }
    public static double? Epoch(string? iso) => DateTimeOffset.TryParse(iso, out var dt) ? dt.ToUnixTimeMilliseconds() / 1000.0 : null;
    public static AppTurn CodexTurn(string path, double? since = null)
    {
        try
        {
            // Grow the tail if a long running turn has no start marker in the first window.
            int span = 262144;
            List<JsonObject> lines;
            while (true)
            {
                lines = Lines(Tail(path, span)).ToList();
                if (new FileInfo(path).Length <= span || lines.Any(e => e["payload"]?["type"]?.ToString() == "task_started" && (since is null || Epoch(e["timestamp"]?.ToString()) < since))) break;
                if (span >= 16 * 1024 * 1024) return new(false, null);
                span *= 4;
            }
            bool started = false; double? last = null; var result = new AppTurn(false, null);
            foreach (var entry in lines)
            {
                string? kind = entry["payload"]?["type"]?.ToString();
                double? at = Epoch(entry["timestamp"]?.ToString());
                if (since is null || at >= since) last = at ?? last;
                if (kind == "task_started" && (since is null || at >= since)) { started = true; result = new(false, at); }
                if (kind is "task_complete" or "turn_aborted" && (started || since is null))
                {
                    var error = entry["payload"]?["error"];
                    result = new(true, at, error is JsonObject ? error["message"]?.ToString() : error?.ToString() ?? entry["payload"]?["reason"]?.ToString());
                }
            }
            return result with { Last = last };
        }
        catch (IOException) { return new(false, null); }
    }
    public static AppTurn AntigravityTurn(string conversation)
    {
        if (!RelayPaths.IsConversationId(conversation)) return new(false, null);
        if (AntigravityError(conversation) is AppTurn error) return error;
        try
        {
            string tail = Tail(RelayPaths.TranscriptPath(conversation), 8 * 1024 * 1024);
            if (!tail.EndsWith('\n')) return new(false, null);
            var step = Lines(tail).LastOrDefault();
            string? kind = step?["type"]?.ToString();
            bool answered = kind == "PLANNER_RESPONSE" && step?["status"]?.ToString() == "DONE" && (step?["tool_calls"] is not JsonArray tools || tools.Count == 0);
            return new(answered || kind == "ERROR_MESSAGE", Epoch(step?["created_at"]?.ToString()), kind == "ERROR_MESSAGE" ? step?["error"]?.ToString() ?? step?["text"]?.ToString() : null);
        }
        catch (IOException) { return new(false, null); }
    }
    public static JsonObject? AntigravityToolErrors(string conversation, double since)
    {
        if (!RelayPaths.IsConversationId(conversation)) return null;
        try
        {
            string tail = Tail(RelayPaths.TranscriptPath(conversation), 2 * 1024 * 1024);
            bool writing = !tail.EndsWith('\n'); var steps = Lines(tail).Reverse().ToList(); int count = 0; JsonObject? last = null;
            bool ended = !writing && steps.FirstOrDefault() is JsonObject final && final["type"]?.ToString() == "PLANNER_RESPONSE" && final["status"]?.ToString() == "DONE" && (final["tool_calls"] is not JsonArray calls || calls.Count == 0);
            foreach (var step in steps)
            {
                if (step["type"]?.ToString() != "GENERIC") continue;
                if (step["status"]?.ToString() != "ERROR") break;
                if (Epoch(step["created_at"]?.ToString()) is not double at || at < since) continue;
                count++; last ??= step;
            }
            if (last is null) return null;
            string reason = (last["error"]?.ToString() ?? "Tool execution error").Trim().TrimEnd(':').Trim(); if (reason.Length > 300) reason = reason[..300];
            string? hint = reason.Contains("denied by pre-tool hook", StringComparison.Ordinal) ? "An Antigravity PreToolUse hook refuses its tools" : reason.Contains("is not enabled for server", StringComparison.Ordinal) ? "MCP tool disabled in Antigravity" : null;
            return new() { ["count"] = count, ["reason"] = reason, ["hint"] = hint, ["at"] = Epoch(last["created_at"]?.ToString()), ["ended"] = ended };
        }
        catch (IOException) { return null; }
    }
    public static AppTurn? AntigravityError(string conversation)
    {
        if (!RelayPaths.IsConversationId(conversation)) return null;
        string path = Path.Combine(RelayPaths.GeminiHome(), "antigravity", "conversations", conversation + ".db");
        nint db = 0, statement = 0;
        try
        {
            if (!File.Exists(path) || SqliteOpen(path, out db, 1, 0) != 0 || SqlitePrepare(db, "SELECT step_type, step_payload FROM steps ORDER BY idx DESC LIMIT 1", -1, out statement, 0) != 0 || SqliteStep(statement) != 100 || SqliteInt(statement, 0) != 17) return null;
            int length = SqliteBytes(statement, 1); if (length is <= 0 or > 2 * 1024 * 1024) return null;
            var payload = new byte[length]; Marshal.Copy(SqliteBlob(statement, 1), payload, 0, length);
            byte[] marker = System.Text.Encoding.UTF8.GetBytes("Agent execution terminated due to error"); int begin = payload.AsSpan().IndexOf(marker);
            if (begin < 0) return null;
            string reason = "execution error"; int at = begin + marker.Length;
            if (at + 2 < length && payload[at] == (byte)'.' && payload[at + 1] == 0x12)
            {
                int cursor = at + 2, count = 0, shift = 0;
                while (cursor < length && shift < 28) { byte b = payload[cursor++]; count |= (b & 127) << shift; if (b < 128) break; shift += 7; }
                if (count > 0 && cursor + Math.Min(count, 300) <= length) reason = System.Text.Encoding.UTF8.GetString(payload, cursor, Math.Min(count, 300)).Trim();
            }
            double? epoch = null;
            for (int i = 0; i + 3 < length; i++) if (payload[i] == 0x0a && payload[i + 1] == 0x0c && payload[i + 2] == 0x08)
            { long value = 0; int shift = 0; for (int j = i + 3; j < length && shift <= 56; j++, shift += 7) { byte b = payload[j]; value |= (long)(b & 127) << shift; if (b < 128) { epoch = value; break; } } if (epoch is not null) break; }
            return new(true, epoch, reason);
        }
        finally { if (statement != 0) SqliteFinalize(statement); if (db != 0) SqliteClose(db); }
    }
    public static JsonObject? CodexUsage()
    {
        try
        {
            var files = new[] { "sessions", "archived_sessions" }.Select(f => System.IO.Path.Combine(CodexHome, f)).Where(Directory.Exists).SelectMany(f => Directory.EnumerateFiles(f, "rollout-*.jsonl", SearchOption.AllDirectories));
            var readings = files.OrderByDescending(File.GetLastWriteTimeUtc).Take(8).SelectMany(f => Lines(Tail(f)).Where(e => e["payload"]?["rate_limits"] is JsonObject)).OrderByDescending(e => e["timestamp"]?.ToString()).FirstOrDefault();
            if (readings is null) return null;
            var windows = new JsonObject();
            foreach (string name in new[] { "primary", "secondary" }) if (readings["payload"]?["rate_limits"]?[name] is JsonObject w)
            {
                double reset = w["resets_at"]?.GetValue<double>() ?? 0;
                windows[name] = new JsonObject { ["used_pct"] = reset > Reservations.Now() ? w["used_percent"]?.DeepClone() : 0, ["resets_at"] = reset, ["current"] = reset > Reservations.Now(), ["minutes"] = w["window_minutes"]?.DeepClone() };
            }
            return new() { ["source"] = "journal Codex (lecture passive)", ["observed"] = readings["timestamp"]?.DeepClone(), ["windows"] = windows };
        }
        catch (IOException) { return null; }
    }
    public static string? CodexQuota(double usedPct = 95)
    {
        var usage = CodexUsage();
        foreach (var kv in usage?["windows"] as JsonObject ?? new())
            if (AppTransport.Truth(kv.Value?["current"]) && kv.Value?["used_pct"] is JsonValue value && value.TryGetValue<double>(out double used) && used >= usedPct) return $"Codex quota {used:0}% used ({kv.Key})";
        return null;
    }
    public static string? AntigravityQuota(RelayBox box, double remainingPct = 5)
    {
        var usage = AntigravityUsage(box);
        double? at = Epoch(usage?["read_at"]?.ToString());
        if (usage?["status"]?.ToString() != "success" || at is null || Reservations.Now() - at > 6 * 3600) return null;
        if (usage["selection_proven"]?.GetValue<bool>() == false) return null;
        string? selected = usage["selected_family"]?.ToString();
        foreach (var cap in (usage["limits"] as JsonArray ?? []).OfType<JsonObject>())
            if ((selected is null ? (cap["family"]?.ToString() ?? "").Contains("Gemini", StringComparison.OrdinalIgnoreCase) : cap["family"]?.ToString() == selected) && cap["remaining_pct"] is JsonValue value && value.TryGetValue<double>(out double pct) && pct <= remainingPct)
                return $"Antigravity : {pct:0} % restants ({cap["family"]})";
        return null;
    }
    public static JsonObject? AntigravityUsage(RelayBox box) => SharedQuota.Antigravity() ?? box.State("antigravity-usage");
    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_open_v2", CallingConvention = CallingConvention.Cdecl)] private static extern int SqliteOpen([MarshalAs(UnmanagedType.LPUTF8Str)] string path, out nint db, int flags, nint vfs);
    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_prepare_v2", CallingConvention = CallingConvention.Cdecl)] private static extern int SqlitePrepare(nint db, [MarshalAs(UnmanagedType.LPUTF8Str)] string sql, int size, out nint statement, nint tail);
    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_step", CallingConvention = CallingConvention.Cdecl)] private static extern int SqliteStep(nint statement);
    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_column_text", CallingConvention = CallingConvention.Cdecl)] private static extern nint SqliteText(nint statement, int index);
    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_column_int", CallingConvention = CallingConvention.Cdecl)] private static extern int SqliteInt(nint statement, int index);
    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_column_blob", CallingConvention = CallingConvention.Cdecl)] private static extern nint SqliteBlob(nint statement, int index);
    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_column_bytes", CallingConvention = CallingConvention.Cdecl)] private static extern int SqliteBytes(nint statement, int index);
    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_finalize", CallingConvention = CallingConvention.Cdecl)] private static extern int SqliteFinalize(nint statement);
    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_close", CallingConvention = CallingConvention.Cdecl)] private static extern int SqliteClose(nint db);
    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_busy_timeout", CallingConvention = CallingConvention.Cdecl)] private static extern int SqliteBusy(nint db, int timeout);
}
