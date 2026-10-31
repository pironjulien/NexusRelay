using System.Diagnostics;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Tomlyn;
using Tomlyn.Model;
using Tomlyn.Serialization;

namespace NexusRelay;

[TomlSerializable(typeof(TomlTable))]
internal partial class RelayTomlContext : TomlSerializerContext { }

/// <summary>
/// Declarations made by the installer, without touching anything else in the client files (each file is backed up,
/// written atomically and read back). NEXUS_PACK_TARGETS (AI apps chosen in the Nexus pack) is honored: a client not
/// in the list is never touched nor reported.
/// <list type="bullet">
/// <item>--register-hooks: the edit guard (NexusRelay.exe --edit-guard &lt;agent&gt;) for Codex (~/.codex/hooks.json,
/// PreToolUse/PostToolUse on apply_patch, then Stop; new handlers need Codex's own review, no trusted hash is ever
/// written, unrelated trust is preserved; when an installed nexus-relay plugin carries an event natively, its global
/// duplicate is removed instead) and Antigravity (~/.gemini/config/hooks.json, hook "nexus-relay-edit-guard").
/// Claude Code gets it from the nexus-relay plugin.</item>
/// <item>--register-clients: the MCP server (http://127.0.0.1:&lt;port&gt;/mcp?agent=…, bearer token read from the user
/// variable NEXUS_MCP_TOKEN, never written) for Claude Code and Codex; Antigravity is handled by install.ps1.</item>
/// </list>
/// </summary>
public static partial class Registration
{
    public const string Name = "nexus-relay";
    public const string TokenVar = "NEXUS_MCP_TOKEN";
    public const string LegacyMark = "nexus_relay.edit_guard";
    public const int TimeoutS = 15;
    public const string CodexMatcher = "apply_patch";
    // edit_file and write_file: real names seen in Antigravity's log on 27/10; the other three: its documentation
    public const string AgyMatcher = "edit_file|write_file|write_to_file|replace_file_content|multi_replace_file_content";
    public const string AgyName = "nexus-relay-edit-guard";
    public const string Plugin = "provided by the nexus-relay plugin";
    public static readonly (string Event, string Codex, string Phase)[] Events =
        [("PreToolUse", "pre_tool_use", ""), ("PostToolUse", "post_tool_use", "--finish-tool"), ("Stop", "stop", "--finish-session")];
    private static readonly JsonSerializerOptions Indented = new() { WriteIndented = true, Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    // ---------- shared helpers ----------

    public static HashSet<string>? PackTargets()
    {
        string? raw = Environment.GetEnvironmentVariable("NEXUS_PACK_TARGETS");
        return raw is null ? null : raw.Split(',').Select(s => s.Trim()).Where(s => s.Length > 0).ToHashSet();
    }

    public static void Backup(string path, string backups)
    {
        if (!File.Exists(path)) return;
        Directory.CreateDirectory(backups);
        string stamp = DateTime.Now.ToString("yyyyMMdd-HHmmss");
        File.Copy(path, Path.Join(backups, $"{Path.GetFileName(Path.GetDirectoryName(Path.GetFullPath(path)))}-{Path.GetFileName(path)}-{stamp}"), true);
    }

    public static void WriteAtomic(string path, string text)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        string temporary = path + ".nexus-tmp";
        File.WriteAllText(temporary, text.Replace("\r\n", "\n"), new UTF8Encoding(false));
        File.Move(temporary, path, true);
    }

    public static string Dump(JsonNode node) => node.ToJsonString(Indented).Replace("\r\n", "\n") + "\n";

    private static JsonObject ReadJsonFile(string path)
    {
        if (!File.Exists(path)) return new();
        string text = File.ReadAllText(path, Encoding.UTF8);
        if (text.Trim().Length == 0) return new();
        return JsonNode.Parse(text) as JsonObject ?? throw new InvalidDataException($"{path}: not a JSON object");
    }

    public static TomlTable ReadToml(string text)
    {
        try { return TomlSerializer.Deserialize(text, RelayTomlContext.Default.TomlTable) ?? new TomlTable(); }
        catch (Exception e) when (e is not InvalidDataException) { throw new InvalidDataException("unreadable TOML: " + e.Message, e); }
    }

    private static TomlTable? Table(TomlTable? parent, string key) => parent is not null && parent.TryGetValue(key, out var value) ? value as TomlTable : null;

    public static bool SameToml(object? a, object? b)
    {
        if (a is TomlTable ta && b is TomlTable tb)
            return ta.Count == tb.Count && ta.All(x => tb.TryGetValue(x.Key, out var other) && SameToml(x.Value, other));
        if (a is not string && b is not string && a is System.Collections.IEnumerable ea && b is System.Collections.IEnumerable eb)
        {
            var la = ea.Cast<object>().ToArray(); var lb = eb.Cast<object>().ToArray();
            return la.Length == lb.Length && Enumerable.Range(0, la.Length).All(i => SameToml(la[i], lb[i]));
        }
        return Equals(a, b);
    }

    private static string CodexHome(string home) => Environment.GetEnvironmentVariable("CODEX_HOME") is { Length: > 0 } codex ? codex : Path.Join(home, ".codex");

    // ---------- edit guard (hooks) ----------

    /// <summary>Hook command line. Without quotes unless needed: Antigravity runs it without a shell.</summary>
    public static string Command(string exe, string agent, string phase = "")
    {
        string quoted = exe.Contains(' ') ? $"\"{exe}\"" : exe;
        return $"{quoted} --edit-guard {agent}" + (phase.Length > 0 ? " " + phase : "");
    }

    public static bool OurHandler(JsonNode? handler)
    {
        if (handler is not JsonObject h) return false;
        string text = h["command"]?.ToString() ?? "";
        string lower = text.ToLowerInvariant();
        return text.Contains(LegacyMark, StringComparison.Ordinal)
            || (lower.Contains("nexusrelay.exe") && text.Contains("--edit-guard", StringComparison.Ordinal))
            || (lower.Contains("nexus-relay") && lower.Contains("invoke-hook.ps1"));
    }

    /// <summary>The native engine itself (not a retired Python or PowerShell relay).</summary>
    private static bool NativeHandler(JsonNode? handler) =>
        handler is JsonObject h && (h["command"]?.ToString() ?? "") is var text
        && text.Contains("nexusrelay.exe", StringComparison.OrdinalIgnoreCase) && text.Contains("--edit-guard", StringComparison.Ordinal);

    public static bool Ours(JsonNode? group) => group is JsonObject g && (g["hooks"] as JsonArray ?? []).Any(OurHandler);

    /// <summary>A manifest carries the guard for an event only with the native engine: a Python one no longer runs.</summary>
    public static bool ManifestCarriesGuard(string manifest, string @event = "PreToolUse")
    {
        try
        {
            if (JsonNode.Parse(File.ReadAllText(manifest, Encoding.UTF8)) is not JsonObject data || data["name"]?.ToString() != Name) return false;
            if (data["hooks"] is not JsonObject hooks || hooks["hooks"] is not JsonObject events) return false;
            string phase = Events.First(e => e.Event == @event).Phase;
            return (events[@event] as JsonArray ?? []).Any(group =>
                Ours(group) && (@event == "Stop" || group!["matcher"]?.ToString() == CodexMatcher)
                && (group!["hooks"] as JsonArray ?? []).Any(h => NativeHandler(h) && (h!["command"]?.ToString() ?? "").Contains(phase, StringComparison.Ordinal)));
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException) { return false; }
    }

    public static Dictionary<string, TomlTable> RelayPlugins(TomlTable config)
    {
        var result = new Dictionary<string, TomlTable>();
        if (Table(config, "plugins") is { } plugins)
            foreach (var (name, value) in plugins)
                if (name.Split('@', 2)[0] == Name && value is TomlTable settings) result[name] = settings;
        return result;
    }

    private static bool Enabled(TomlTable settings, bool fallback) => settings.TryGetValue("enabled", out var v) ? v is true : fallback;

    /// <summary>Personal source or installed repo-marketplace copy; an orphaned cache is not an installed plugin.</summary>
    public static bool CarriedByPlugin(string folder, TomlTable config, string @event = "PreToolUse")
    {
        var plugins = RelayPlugins(config);
        var active = plugins.Where(p => Enabled(p.Value, true)).Select(p => p.Key).ToList();
        if ((plugins.Count == 0 || active.Contains("nexus-relay@personal"))
            && ManifestCarriesGuard(Path.Join(folder, "plugins", Name, ".codex-plugin", "plugin.json"), @event)) return true;
        foreach (var parts in active.Select(name => name.Split('@', 2)))
        {
            if (parts.Length != 2 || parts[1] is "" or "." or ".." || parts[1].IndexOfAny(['/', '\\']) >= 0) continue;
            if (ManifestCarriesGuard(Path.Combine(folder, "plugins", "cache", parts[1], Name, "local", ".codex-plugin", "plugin.json"), @event)) return true;
        }
        return false;
    }

    [GeneratedRegex(@"(?m)^[ \t]*\[[^\r\n]*(?:\r?\n|$)")]
    private static partial Regex TableHeader();

    public static string TomlLiteral(string text) =>
        text.Contains('\'') || text.Contains('\n') ? JsonSerializer.Serialize(text, Indented) : $"'{text}'";

    private static TomlTable? StateOf(TomlTable parsed) => Table(Table(parsed, "hooks"), "state");

    private static void DropEmptyState(TomlTable parsed)
    {
        if (Table(parsed, "hooks") is { } hooks)
        {
            if (Table(hooks, "state") is { Count: 0 }) hooks.Remove("state");
            if (hooks.Count == 0) parsed.Remove("hooks");
        }
    }

    /// <summary>Move only affected trust sections when deleting handlers shifts their global group/handler indexes.</summary>
    public static string MoveTrust(string text, Dictionary<string, string?> remap)
    {
        var expected = ReadToml(text);
        if (StateOf(expected) is { Count: > 0 } state)
        {
            var moved = new TomlTable();
            foreach (var (key, value) in state) if (!remap.ContainsKey(key)) moved[key] = value;
            foreach (var (key, target) in remap) if (target is not null && state.TryGetValue(key, out var value)) moved[target] = value;
            Table(expected, "hooks")!["state"] = moved;
        }
        var matches = TableHeader().Matches(text);
        var parts = new StringBuilder(matches.Count > 0 ? text[..matches[0].Index] : text);
        for (int index = 0; index < matches.Count; index++)
        {
            var match = matches[index];
            int end = index + 1 < matches.Count ? matches[index + 1].Index : text.Length;
            string? key = null;
            try { if (StateOf(ReadToml(match.Value)) is { Count: 1 } keys) key = keys.Keys.First(); }
            catch (InvalidDataException) { /* Not a single-table header: copied unchanged below. */ }
            if (key is not null && remap.TryGetValue(key, out var target))
            {
                if (target is not null) parts.Append($"[hooks.state.{TomlLiteral(target)}]\n").Append(text, match.Index + match.Length, end - match.Index - match.Length);
            }
            else parts.Append(text, match.Index, end - match.Index);
        }
        string result = parts.ToString();
        var actual = ReadToml(result);
        DropEmptyState(actual); DropEmptyState(expected);
        if (!SameToml(actual, expected)) throw new InvalidDataException("could not preserve the unrelated Codex hook trust sections");
        return result;
    }

    /// <summary>Remove our global handlers, preserving other handlers and their trust even when their indexes move.</summary>
    public static string CodexDrop(string folder, string backups, string reason = "carried by the nexus-relay plugin", ISet<string>? events = null)
    {
        string hooksPath = Path.Join(folder, "hooks.json");
        var data = ReadJsonFile(hooksPath);
        var remap = new Dictionary<string, string?>();
        foreach (var (eventName, codexName, _) in Events)
        {
            if (events is not null && !events.Contains(eventName)) continue;
            if ((data["hooks"] as JsonObject)?[eventName] is not JsonArray groups || !groups.Any(Ours)) continue;
            var kept = new JsonArray();
            for (int index = 0; index < groups.Count; index++)
            {
                var group = groups[index];
                var handlers = (group?["hooks"] as JsonArray ?? []).ToList();
                var remaining = handlers.Where(h => !OurHandler(h)).ToList();
                for (int handlerIndex = 0; handlerIndex < handlers.Count; handlerIndex++)
                {
                    string key = $"{hooksPath}:{codexName}:{index}:{handlerIndex}";
                    if (OurHandler(handlers[handlerIndex])) remap[key] = null;
                    else if (remaining.Count > 0)
                    {
                        int newIndex = handlers.Take(handlerIndex).Count(h => !OurHandler(h));
                        remap[key] = $"{hooksPath}:{codexName}:{kept.Count}:{newIndex}";
                    }
                }
                if (remaining.Count > 0 || !Ours(group))
                {
                    if (Ours(group))
                    {
                        var copy = group!.DeepClone().AsObject();
                        copy["hooks"] = new JsonArray(remaining.Select(h => h?.DeepClone()).ToArray());
                        kept.Add(copy);
                    }
                    else kept.Add(group?.DeepClone());
                }
            }
            var hooks = data["hooks"]!.AsObject();
            if (kept.Count > 0) hooks[eventName] = kept; else hooks.Remove(eventName);
        }
        if (remap.Count == 0) return reason;
        string config = Path.Join(folder, "config.toml");
        string text = "", updated = "";
        if (File.Exists(config)) { text = File.ReadAllText(config, Encoding.UTF8); updated = MoveTrust(text, remap); }
        Backup(hooksPath, backups);
        WriteAtomic(hooksPath, Dump(data));
        if (updated != text) { Backup(config, backups); WriteAtomic(config, updated); }
        return $"{reason}, global entry removed from {hooksPath}";
    }

    public static string? CodexHooks(string home, string exe, string backups)
    {
        string folder = CodexHome(home);
        if (!Directory.Exists(folder)) return null;
        string config = Path.Join(folder, "config.toml");
        var settings = ReadToml(File.Exists(config) ? File.ReadAllText(config, Encoding.UTF8) : "");
        var plugins = RelayPlugins(settings);
        if (plugins.Count > 0 && !plugins.Values.Any(p => Enabled(p, true))) return CodexDrop(folder, backups, "kept disabled (nexus-relay plugin)");
        var carried = Events.Select(e => e.Event).Where(e => CarriedByPlugin(folder, settings, e)).ToHashSet();
        string migration = CodexDrop(folder, backups, events: carried);
        if (carried.Count == Events.Length) return migration;
        string hooksPath = Path.Join(folder, "hooks.json");
        var data = ReadJsonFile(hooksPath);
        // An older owned handler can share a group with foreign handlers: never replace that whole group or change its
        // matcher; remove only ours (with the trust remapping), then append a separate canonical group.
        var mixed = new HashSet<string>();
        foreach (var (eventName, _, _) in Events)
        {
            if (carried.Contains(eventName)) continue;
            var owned = ((data["hooks"] as JsonObject)?[eventName] as JsonArray ?? []).Where(Ours).ToList();
            string matcher = eventName != "Stop" ? CodexMatcher : "";
            if (owned.Count > 0 && !(owned.Count == 1 && (owned[0]!["hooks"] as JsonArray)?.Count == 1 && (owned[0]!["matcher"]?.ToString() ?? "") == matcher))
                mixed.Add(eventName);
        }
        if (mixed.Count > 0) { CodexDrop(folder, backups, "updating mixed relay handlers", mixed); data = ReadJsonFile(hooksPath); }
        if (data["hooks"] is not JsonObject events) data["hooks"] = events = new JsonObject();
        bool changed = false;
        foreach (var (eventName, _, phase) in Events)
        {
            if (carried.Contains(eventName)) continue;
            if (events[eventName] is not JsonArray groups) events[eventName] = groups = new JsonArray();
            var handler = new JsonObject { ["type"] = "command", ["command"] = Command(exe, "codex", phase), ["timeout"] = TimeoutS, ["async"] = false };
            var group = new JsonObject { ["matcher"] = eventName != "Stop" ? CodexMatcher : "", ["hooks"] = new JsonArray(handler) };
            int index = groups.Select((g, i) => (g, i)).FirstOrDefault(x => Ours(x.g), (null, groups.Count)).Item2;
            if (index == groups.Count) { groups.Add(group); changed = true; }
            else if (!JsonNode.DeepEquals(groups[index], group)) { groups[index] = group; changed = true; }
        }
        if (changed) { Backup(hooksPath, backups); WriteAtomic(hooksPath, Dump(data)); }
        return $"{(changed ? "declared" : "already declared")} in {hooksPath}; Codex review required for new hooks";
    }

    public static string? AntigravityHooks(string home, string exe, string backups)
    {
        string folder = Path.Join(home, ".gemini", "config");
        if (!Directory.Exists(folder)) return null;
        string path = Path.Join(folder, "hooks.json");
        var data = ReadJsonFile(path);
        JsonObject Handler(string phase) => new() { ["type"] = "command", ["command"] = Command(exe, "antigravity", phase), ["timeout"] = TimeoutS };
        var entry = new JsonObject
        {
            ["PreToolUse"] = new JsonArray(new JsonObject { ["matcher"] = AgyMatcher, ["hooks"] = new JsonArray(Handler("")) }),
            ["PostToolUse"] = new JsonArray(new JsonObject { ["matcher"] = AgyMatcher, ["hooks"] = new JsonArray(Handler("--finish-tool")) }),
            ["Stop"] = new JsonArray(Handler("--finish-session")),
        };
        if (data[AgyName]?["enabled"] is JsonValue enabled && enabled.TryGetValue<bool>(out bool on) && !on) entry["enabled"] = false;
        if (JsonNode.DeepEquals(data[AgyName], entry)) return "already declared";
        data[AgyName] = entry;
        Backup(path, backups);
        WriteAtomic(path, Dump(data));
        return $"declared in {path}";
    }

    public static int RegisterHooks(string home, string exe, string backups)
    {
        var targets = PackTargets();
        bool failed = false;
        foreach (var (client, pack, declare) in new (string, string, Func<string, string, string, string?>)[] { ("Codex", "codex", CodexHooks), ("Antigravity", "antigravity", AntigravityHooks) })
        {
            if (targets is not null && !targets.Contains(pack)) continue;
            string? result;
            try { result = declare(home, exe, backups); }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException or InvalidDataException or JsonException) { result = "failed: " + e.Message; }
            if (result is null) continue;
            Console.WriteLine($"{client} (edit guard): {result}");
            failed |= result.StartsWith("failed", StringComparison.Ordinal);
        }
        return failed ? 1 : 0;
    }

    // ---------- MCP clients ----------

    public static string Url(int port, string agent) => $"http://127.0.0.1:{port}/mcp?agent={agent}";
    public static JsonObject ClaudeEntry(int port) => new() { ["type"] = "http", ["url"] = Url(port, "claude"), ["headers"] = new JsonObject { ["Authorization"] = $"Bearer ${{{TokenVar}}}" } };

    /// <summary>An entry written by this installer (the local relay daemon), or none at all.</summary>
    public static bool OursEntry(object? existing) => existing switch
    {
        null => true,
        JsonObject j => (j["url"]?.ToString() ?? "").StartsWith("http://127.0.0.1:", StringComparison.Ordinal),
        TomlTable t => t.TryGetValue("url", out var url) && url is string s && s.StartsWith("http://127.0.0.1:", StringComparison.Ordinal),
        _ => false,
    };

    public static bool ClaudePluginEnabled(string home)
    {
        try
        {
            var settings = ReadJsonFile(Path.Join(home, ".claude", "settings.json"));
            return (settings["enabledPlugins"] as JsonObject ?? []).Any(p => p.Key.Split('@')[0] == Name && p.Value is JsonValue v && v.TryGetValue<bool>(out bool on) && on);
        }
        catch (Exception e) when (e is IOException or JsonException or InvalidDataException) { return false; }
    }

    public static string? Which(string name)
    {
        var extensions = (Environment.GetEnvironmentVariable("PATHEXT") ?? ".EXE;.CMD;.BAT").Split(';', StringSplitOptions.RemoveEmptyEntries);
        foreach (string folder in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
            foreach (string extension in extensions)
            {
                string candidate = Path.Combine(folder.Trim('"'), name + extension.ToLowerInvariant());
                if (File.Exists(candidate)) return candidate;
            }
        return null;
    }

    private static (int Code, string Output) Run(string file, params string[] args)
    {
        var start = new ProcessStartInfo(file) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
        foreach (string arg in args) start.ArgumentList.Add(arg);
        using var process = Process.Start(start) ?? throw new IOException($"{file} not started");
        var output = process.StandardOutput.ReadToEndAsync(); var error = process.StandardError.ReadToEndAsync();
        if (!process.WaitForExit(60_000)) { process.Kill(true); return (1, "timed out"); }
        return (process.ExitCode, (error.Result + output.Result).Trim());
    }

    public static string? ClaudeCode(string home, int port, string backups)
    {
        if (ClaudePluginEnabled(home)) return Plugin;
        string? cli = Which("claude");
        string config = Path.Join(home, ".claude.json");
        var data = ReadJsonFile(config);
        var current = (data["mcpServers"] as JsonObject)?[Name];
        if (!OursEntry(current)) return "kept (an entry of yours already exists)";
        if (current is not null && JsonNode.DeepEquals(current, ClaudeEntry(port))) return "already declared";
        if (cli is not null)
        {
            Run(cli, "mcp", "remove", Name, "--scope", "user");
            var (code, output) = Run(cli, "mcp", "add-json", Name, ClaudeEntry(port).ToJsonString(), "--scope", "user");
            return code != 0 ? "failed: " + output : "declared (claude mcp add-json, user scope)";
        }
        if (!Directory.Exists(Path.Join(home, ".claude")) && !File.Exists(config)) return null;
        Backup(config, backups);
        if (data["mcpServers"] is not JsonObject servers) data["mcpServers"] = servers = new JsonObject();
        servers[Name] = ClaudeEntry(port);
        WriteAtomic(config, Dump(data));
        return $"declared in {config}";
    }

    [GeneratedRegex(@"^\[(?<name>[^\]]+)\]\s*$", RegexOptions.Multiline)]
    private static partial Regex TomlTableLine();

    private static bool CodexEntryMatches(object? current, int port) =>
        current is TomlTable t && t.Count == 2 && t.TryGetValue("url", out var url) && url as string == Url(port, "codex")
        && t.TryGetValue("bearer_token_env_var", out var variable) && variable as string == TokenVar;

    public static string? Codex(string home, int port, string backups)
    {
        string folder = CodexHome(home);
        if (!Directory.Exists(folder) && Which("codex") is null) return null;
        string config = Path.Join(folder, "config.toml");
        string text = File.Exists(config) ? File.ReadAllText(config, Encoding.UTF8) : "";
        var data = ReadToml(text);
        // nexus-relay plugin of any catalog (« nexus » private, « nexus-plugins » of NexusSystem)
        if (Table(data, "plugins") is { } plugins && plugins.Any(p => p.Key.Split('@')[0] == Name && p.Value is TomlTable t && Enabled(t, false))) return Plugin;
        object? current = Table(data, "mcp_servers") is { } servers && servers.TryGetValue(Name, out var c) ? c : null;
        if (!OursEntry(current)) return "kept (an entry of yours already exists)";
        if (CodexEntryMatches(current, port)) return "already declared";
        if (current is not null)
        {
            // Our own tables ([mcp_servers.nexus-relay] and its subtables) are cut out, each up to the next table.
            var parts = new StringBuilder(); bool dropping = false; int position = 0;
            foreach (Match match in TomlTableLine().Matches(text))
            {
                if (!dropping) parts.Append(text, position, match.Index - position);
                string name = match.Groups["name"].Value.Trim();
                dropping = name == $"mcp_servers.{Name}" || name.StartsWith($"mcp_servers.{Name}.", StringComparison.Ordinal);
                position = match.Index;
            }
            if (!dropping) parts.Append(text, position, text.Length - position);
            text = parts.ToString();
            if (Table(ReadToml(text), "mcp_servers") is { } left && left.ContainsKey(Name)) return "kept (entry written in a form this installer does not edit)";
        }
        string block = $"[mcp_servers.{Name}]\nurl = {JsonSerializer.Serialize(Url(port, "codex"))}\nbearer_token_env_var = {JsonSerializer.Serialize(TokenVar)}\n";
        string updated = text.TrimEnd('\n', '\r') + (text.Trim().Length > 0 ? "\n\n" : "") + block;
        if (!CodexEntryMatches(Table(ReadToml(updated), "mcp_servers")?[Name], port)) return "failed: the edited config.toml would not read back";
        Backup(config, backups);
        WriteAtomic(config, updated);
        return $"declared in {config}";
    }

    public static string Short(string? result)
    {
        if (result is null) return "absent";
        foreach (var (prefix, word) in new[] { ("failed", "failed"), ("kept", "kept"), ("provided by", "plugin"), ("plugin", "plugin") })
            if (result.StartsWith(prefix, StringComparison.Ordinal)) return word;
        return "declared";
    }

    public static int RegisterClients(string home, int port, string backups, string? antigravity)
    {
        var targets = PackTargets();
        var results = new List<(string Client, string? Result)>();
        foreach (var (client, pack, declare) in new (string, string, Func<string, int, string, string?>)[] { ("Claude Code", "claude-code", ClaudeCode), ("Codex", "codex", Codex) })
        {
            if (targets is not null && !targets.Contains(pack)) continue;  // outside the user's choice: never touched
            try { results.Add((client, declare(home, port, backups))); }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException or InvalidDataException or JsonException) { results.Add((client, "failed: " + e.Message)); }
        }
        if (!string.IsNullOrEmpty(antigravity) && (targets is null || targets.Contains("antigravity"))) results.Add(("Antigravity", antigravity));
        bool found = false;
        foreach (var (client, result) in results) if (result is not null) { found = true; Console.WriteLine($"{client}: {result}"); }
        if (!found) Console.WriteLine("No AI client found (Claude Code, Codex, Antigravity): install one, then run the installer again.");
        if (Environment.GetEnvironmentVariable("NEXUS_PACK") == "1")
            Console.WriteLine("NEXUS_PACK_RESULT " + string.Join("; ", results.Select(r => $"{r.Client}={Short(r.Result)}")));
        return results.Any(r => r.Result?.StartsWith("failed", StringComparison.Ordinal) == true) ? 1 : 0;
    }

    private static string? Option(string[] args, string name) { int i = Array.IndexOf(args, name); return i >= 0 && i + 1 < args.Length ? args[i + 1] : null; }

    public static int RunCli(string[] args)
    {
        string home = RelayPaths.UserHome();
        string backups = Option(args, "--backups") ?? Path.Join(home, ".nexus", "nexus-relay", "backups");
        if (args.Contains("--register-hooks", StringComparer.Ordinal))
            return RegisterHooks(home, Option(args, "--command") ?? Environment.ProcessPath ?? throw new InvalidOperationException("executable path unknown"), backups);
        int port = int.TryParse(Option(args, "--port"), out int p) ? p : 16181;
        return RegisterClients(home, port, backups, Option(args, "--antigravity"));
    }
}
