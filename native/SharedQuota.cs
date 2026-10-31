using System.Text.Json.Nodes;

namespace NexusRelay;

public static class SharedQuota
{
    public static JsonObject? Antigravity()
    {
        string path = Environment.GetEnvironmentVariable("NEXUS_RELAY_KEYPAD_QUOTA") ?? Path.Join(Environment.GetEnvironmentVariable("LOCALAPPDATA") ?? Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "NexusKeypad", "quota-antigravity.json");
        if (!File.Exists(path)) return null;
        JsonObject Unknown() => new() { ["status"] = "unknown", ["source"] = "NexusKeypad shared quota", ["selection_proven"] = false, ["limits"] = new JsonArray() };
        try
        {
            var data = RelayBox.ReadJson(path); double? observed = AppSessions.Epoch(data?["lu"]?.ToString());
            if (data is null || observed is null || Reservations.Now() - observed > 6 * 3600 || observed > Reservations.Now() + 60) return Unknown();
            string? selected = data["groupeSelectionne"]?.ToString();
            if (selected is not ("gemini" or "claude-gpt" or "argon")) selected = null;
            var limits = new JsonArray();
            foreach (var group in (data["groupes"] as JsonArray ?? []).OfType<JsonObject>())
            {
                string? family = group["groupe"]?.ToString();
                if (family is not ("gemini" or "claude-gpt" or "argon") || group["utilise"] is not JsonValue value || !value.TryGetValue<double>(out double used) || !double.IsFinite(used) || used is < 0 or > 1) continue;
                bool five = group["cinq"]?.GetValue<bool>() ?? false;
                double reset = AppSessions.Epoch(group["fin"]?.ToString()) ?? observed.Value + (five ? 5 * 3600 : 7 * 24 * 3600);
                limits.Add(new JsonObject { ["family"] = family, ["limit"] = five ? "5 h" : "semaine", ["remaining_pct"] = Reservations.Now() < reset ? 100 * (1 - used) : 100.0, ["resets_at"] = reset });
            }
            return new() { ["status"] = "success", ["source"] = "NexusKeypad shared quota", ["read_at"] = data["lu"]?.DeepClone(), ["selected_family"] = selected, ["selection_proven"] = selected is not null && limits.OfType<JsonObject>().Any(l => l["family"]?.ToString() == selected), ["limits"] = limits };
        }
        catch (Exception e) when (e is IOException or InvalidOperationException or FormatException) { return Unknown(); }
    }
}
