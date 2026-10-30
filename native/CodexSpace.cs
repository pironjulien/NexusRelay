using System.Text.Json.Nodes;

namespace NexusRelay;

public static class CodexSpace
{
    private static string Setting(string variable, string key)
    {
        string? value = Environment.GetEnvironmentVariable(variable)?.Trim();
        if (!string.IsNullOrEmpty(value)) return value;
        return RelayBox.ReadJson(Path.Combine(RelayPaths.UserHome(), ".nexus", "nexus-relay", "settings.json"))?[key]?.ToString()?.Trim() ?? "";
    }
    public static JsonObject Configured()
    {
        string workspace = Setting("NEXUS_RELAY_CODEX_WORKSPACE", "codex_workspace"), label = Setting("NEXUS_RELAY_CODEX_PROJECT", "codex_project"), id = Setting("NEXUS_RELAY_CODEX_PROJECT_ID", "codex_project_id");
        bool explicitChoice = workspace.Length + label.Length + id.Length > 0;
        if (explicitChoice && (workspace.Length == 0 || label.Length == 0 || id.Length == 0)) throw new RelayError("Codex execution space requires workspace path, project label and official project ID");
        return new() { ["path"] = explicitChoice ? Path.GetFullPath(workspace) : Path.Combine(RelayPaths.RelayHome(), "codex-projet"), ["label"] = explicitChoice ? label : "Nexus Relay", ["project_id"] = id.Length > 0 ? id : null, ["explicit"] = explicitChoice, ["ownership"] = "verified Relay conversation IDs only" };
    }
    public static bool AcceptsCreated(string created)
    {
        string cutoff = Setting("NEXUS_RELAY_WAKE_SINCE", "wake_since");
        return cutoff.Length == 0 || AppSessions.Epoch(created) is double at && AppSessions.Epoch(cutoff) is double since && at >= since;
    }
    public static bool Protected(string id) => new[] { "CODEX_SESSION_ID", "CODEX_THREAD_ID" }.Any(k => Environment.GetEnvironmentVariable(k) == id);
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
        string legacy = Path.Combine(RelayPaths.RelayHome(), "codex-projet"), configured = Configured()["path"]!.ToString();
        foreach (var message in box.All().Where(m => m.To == "codex" && !m.Info))
        {
            foreach (var wake in box.Events(message.Id).Where(e => e["kind"]?.ToString() == "réveil" && e["status"]?.ToString() is "started" or "uncertain"))
            {
                if (wake["conversation"]?.ToString() is not string id || !chats.TryGetValue(id, out var chat) || chat.Child || Protected(id) || registry["threads"]?[id]?["verified"]?.GetValue<bool>() == true) continue;
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
                        foreach (var entry in AppSessions.Lines(line)) if (entry["type"]?.ToString() == "response_item" && entry["payload"]?["type"]?.ToString() == "message" && entry["payload"]?["role"]?.ToString() == "user")
                            foreach (var content in (entry["payload"]?["content"] as JsonArray ?? []).OfType<JsonObject>())
                            {
                                string text = content["text"]?.ToString() ?? "";
                                if ((text.StartsWith("Nexus Relay : message d'un pair pour toi, ", StringComparison.Ordinal) || text.StartsWith("Nexus Relay: message from a peer for you, ", StringComparison.Ordinal)) && text.Contains(marker, StringComparison.Ordinal)) proof = true;
                            }
                        if (proof) break;
                    }
                }
                catch (IOException) { continue; }
                if (!proof) continue;
                registry["threads"]![id] = new JsonObject { ["verified"] = true, ["topic"] = message.Topic, ["workspace"] = chat.Cwd, ["verified_at"] = RelayBox.NowIso(), ["origin"] = "verified-relay-history", ["message"] = message.Id };
                registered.Add(id);
            }
        }
        if (registered.Count > 0) box.SetState("codex-owned", registry);
        return new() { ["registered"] = registered, ["count"] = registered.Count, ["proof"] = "Matching mailbox delivery event, exact conversation ID, top-level app DB row, approved workspace and actual Relay user wake in its journal" };
    }
}
