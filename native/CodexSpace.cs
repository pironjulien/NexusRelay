using System.Text.Json.Nodes;

namespace NexusRelay;

public static class CodexSpace
{
    private static string Setting(string variable, string key)
    {
        string? value = Environment.GetEnvironmentVariable(variable)?.Trim();
        if (!string.IsNullOrEmpty(value)) return value;
        return RelayBox.ReadJson(Path.Join(RelayPaths.UserHome(), ".nexus", "nexus-relay", "settings.json"))?[key]?.ToString()?.Trim() ?? "";
    }
    /// <summary>Names of the default Codex project, which the user adds by hand in the Codex app: "NexusRelay", and
    /// "Nexus Relay" as created before 1.13.2 (an existing project is never renamed for the user).</summary>
    public static readonly string[] DefaultLabels = ["NexusRelay", "Nexus Relay"];
    /// <summary>Wake prefixes, current then before 1.13.2: an older chat still proves its ownership by its old wake.</summary>
    public static readonly string[] WakePrefixes = ["NexusRelay: message from a peer for you, ", "NexusRelay : message d'un pair pour toi, ", "Nexus Relay: message from a peer for you, ", "Nexus Relay : message d'un pair pour toi, "];
    public static JsonObject Configured()
    {
        string workspace = Setting("NEXUS_RELAY_CODEX_WORKSPACE", "codex_workspace"), label = Setting("NEXUS_RELAY_CODEX_PROJECT", "codex_project"), id = Setting("NEXUS_RELAY_CODEX_PROJECT_ID", "codex_project_id");
        bool explicitChoice = workspace.Length + label.Length + id.Length > 0;
        if (explicitChoice && (workspace.Length == 0 || label.Length == 0 || id.Length == 0)) throw new RelayError("Codex execution space requires workspace path, project label and official project ID");
        return new() { ["path"] = explicitChoice ? Path.GetFullPath(workspace) : Path.Join(RelayPaths.RelayHome(), "codex-projet"), ["label"] = explicitChoice ? label : DefaultLabels[0], ["labels"] = new JsonArray((explicitChoice ? new[] { label } : DefaultLabels).Select(l => (JsonNode?)l).ToArray()), ["project_id"] = id.Length > 0 ? id : null, ["explicit"] = explicitChoice, ["ownership"] = "verified Relay conversation IDs only" };
    }
    /// <summary>Project names accepted for the configured space (the explicit one, or both default names).</summary>
    public static string[] Labels(JsonObject configured) => (configured["labels"] as JsonArray ?? []).Select(l => l!.ToString()).ToArray();
    public static bool AcceptsCreated(string created)
    {
        string cutoff = Setting("NEXUS_RELAY_WAKE_SINCE", "wake_since");
        return cutoff.Length == 0 || AppSessions.Epoch(created) is double at && AppSessions.Epoch(cutoff) is double since && at >= since;
    }
    public static bool Protected(string id) => Lists.CodexThreadVariables.Any(k => Environment.GetEnvironmentVariable(k) == id);
    public static bool Owns(AppChat chat, RelayBox box)
    {
        var item = box.State("codex-owned")?["threads"]?[chat.Id] as JsonObject;
        return !chat.Child && !Protected(chat.Id) && item?["verified"]?.GetValue<bool>() == true &&
            item["workspace"]?.ToString() is string workspace && RelayBox.PlainPath(workspace) == RelayBox.PlainPath(chat.Cwd);
    }
    public static bool Remember(string topic, string id, RelayBox box)
    {
        var chat = AppSessions.CodexChats().FirstOrDefault(c => c.Id == id);
        if (chat is null || chat.Child || Protected(id) || RelayBox.PlainPath(chat.Cwd) != RelayBox.PlainPath(Configured()["path"]!.ToString())) return false;
        var registry = box.State("codex-owned") ?? new JsonObject { ["threads"] = new JsonObject() };
        registry["threads"]![id] = new JsonObject { ["verified"] = true, ["topic"] = topic, ["workspace"] = chat.Cwd, ["verified_at"] = RelayBox.NowIso(), ["origin"] = "verified-relay-wake" };
        box.SetState("codex-owned", registry); return true;
    }
    public static JsonObject RegisterHistory(RelayBox box)
    {
        var chats = AppSessions.CodexChats().ToDictionary(c => c.Id); var registry = box.State("codex-owned") ?? new JsonObject { ["threads"] = new JsonObject() }; var registered = new JsonArray();
        string legacy = Path.Join(RelayPaths.RelayHome(), "codex-projet"), configured = Configured()["path"]!.ToString();
        foreach (var message in box.All().Where(m => m.To == "codex" && !m.Info))
        {
            var woken = box.Events(message.Id).Where(e => e["kind"]?.ToString() == "réveil" && e["status"]?.ToString() is "started" or "uncertain").Select(e => e["conversation"]?.ToString()).OfType<string>();
            foreach (var (id, chat) in woken.Select(id => (id, chats.GetValueOrDefault(id))).Where(p => p.Item2 is { Child: false } && !Protected(p.id) && registry["threads"]?[p.id]?["verified"]?.GetValue<bool>() != true).Select(p => (p.id, p.Item2!)))
            {
                if (RelayBox.PlainPath(chat.Cwd) != RelayBox.PlainPath(legacy) && RelayBox.PlainPath(chat.Cwd) != RelayBox.PlainPath(configured)) continue;
                if (!File.Exists(chat.Path)) continue;
                string marker = "read(id=\"" + message.Id + "\")";
                bool proof = false;
                try
                {
                    using var stream = new FileStream(chat.Path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete); using var reader = new StreamReader(stream);
                    string? line;
                    while ((line = reader.ReadLine()) is not null)
                    {
                        if (!line.Contains(message.Id, StringComparison.Ordinal)) continue;
                        proof = AppSessions.Lines(line).Where(e => e["type"]?.ToString() == "response_item" && e["payload"]?["type"]?.ToString() == "message" && e["payload"]?["role"]?.ToString() == "user")
                            .SelectMany(entry => (entry["payload"]?["content"] as JsonArray ?? []).OfType<JsonObject>().Select(content => content["text"]?.ToString() ?? ""))
                            .Any(text => WakePrefixes.Any(prefix => text.StartsWith(prefix, StringComparison.Ordinal)) && text.Contains(marker, StringComparison.Ordinal));
                        if (proof) break;
                    }
                }
                catch (IOException) { continue; }
                if (!proof) continue;
                registry["threads"]![id] = new JsonObject { ["verified"] = true, ["topic"] = message.Topic, ["workspace"] = chat.Cwd, ["verified_at"] = RelayBox.NowIso(), ["origin"] = "verified-relay-history", ["message"] = message.Id };
                registered.Add((JsonNode?)id);
            }
        }
        if (registered.Count > 0) box.SetState("codex-owned", registry);
        return new() { ["registered"] = registered, ["count"] = registered.Count, ["proof"] = "Matching mailbox delivery event, exact conversation ID, top-level app DB row, approved workspace and actual Relay user wake in its journal" };
    }
}
