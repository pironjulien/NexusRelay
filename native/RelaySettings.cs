using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusRelay;

/// <summary>
/// Workstation settings: ~\.nexus\nexus-relay\settings.json (this PC), then settings.local.json at the root of the
/// installed source (shared by the PCs of a synchronized install), key by key, the first file giving a key wins.
/// Values accept environment variables (%OneDrive%, $LOCALAPPDATA…); a value whose variable is missing on this PC is
/// ignored. token_file keeps the first existing file; vaults and codex_router_start keep every value (lists).
/// `NexusRelay.exe --settings [source]` prints the effective settings as JSON (read by install.ps1).
/// </summary>
public static partial class RelaySettings
{
    public static readonly string[] Keys =
        ["token_file", "vaults", "codex_router", "codex_router_start", "host", "lang", "codex_workspace", "codex_project", "codex_project_id", "wake_since"];
    private static readonly HashSet<string> Scalars =
        ["codex_router", "host", "lang", "codex_workspace", "codex_project", "codex_project_id", "wake_since"];

    [GeneratedRegex(@"%[^%\s]+%|\$\{?\w+")]
    private static partial Regex Unexpanded();
    [GeneratedRegex(@"\$\{(\w+)\}|\$(\w+)")]
    private static partial Regex PosixVariable();

    public static List<string> Files(string? source) =>
    [
        Path.Combine(RelayPaths.UserHome(), ".nexus", "nexus-relay", "settings.json"),
        .. string.IsNullOrWhiteSpace(source) ? Array.Empty<string>() : [Path.Combine(source, "settings.local.json")],
    ];

    public static string? Expand(string value)
    {
        string text = Environment.ExpandEnvironmentVariables(value);
        text = PosixVariable().Replace(text, m =>
        {
            string name = m.Groups[1].Success ? m.Groups[1].Value : m.Groups[2].Value;
            return Environment.GetEnvironmentVariable(name) ?? m.Value;
        }).Trim();
        return text.Length == 0 || Unexpanded().IsMatch(text) ? null : text;
    }

    private static List<string> Values(JsonNode? raw)
    {
        var items = raw is JsonArray array ? array.ToList() : [raw];
        var result = new List<string>();
        foreach (var item in items)
        {
            if (item is null) continue;
            string text = item is JsonValue v && v.TryGetValue<string>(out var s) ? s : item.ToJsonString();
            if (Expand(text) is { } expanded) result.Add(expanded);
        }
        return result;
    }

    public static JsonObject Load(IEnumerable<string> paths)
    {
        var found = new JsonObject();
        foreach (string path in paths)
        {
            JsonObject? data;
            try { data = JsonNode.Parse(File.ReadAllText(path, Encoding.UTF8)) as JsonObject; }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException or System.Text.Json.JsonException) { continue; }
            if (data is null) continue;
            foreach (string key in Keys)
            {
                if (found.ContainsKey(key) || !data.ContainsKey(key)) continue;
                var values = Values(data[key]);
                if (key == "token_file")
                {
                    string? existing = values.FirstOrDefault(File.Exists);
                    if (existing is not null) found[key] = existing;
                }
                else if (Scalars.Contains(key)) { if (values.Count > 0) found[key] = values[0]; }
                else if (values.Count > 0) found[key] = new JsonArray(values.Distinct().Select(v => (JsonNode?)JsonValue.Create(v)).ToArray());
            }
        }
        return found;
    }

    public static int RunCli(string[] args)
    {
        int index = Array.IndexOf(args, "--settings");
        string? source = index + 1 < args.Length && !args[index + 1].StartsWith("--", StringComparison.Ordinal) ? args[index + 1] : null;
        Console.Write(Load(Files(source)).ToJsonString(new System.Text.Json.JsonSerializerOptions { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping }));
        return 0;
    }
}
