using System.Diagnostics;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusRelay;

public static class NativeUpdater
{
    private static readonly Lock Gate = new();
    private static string StateFile => Path.Join(RelayPaths.UserHome(), ".nexus", "status", "relay.json");
    private static string ProductState => Path.Join(RelayPaths.UserHome(), ".nexus", "nexus-relay");
    private static string Marker => Path.Join(ProductState, "update.json");
    private static string Source => Environment.GetEnvironmentVariable("NEXUS_RELAY_SOURCE_ROOT") ?? RelayBox.ReadJson(Path.Join(ProductState, "settings.json"))?["installed_source"]?.ToString() ?? "";
    private static bool Managed => Source.Length > 0 && RelayBox.PlainPath(Source) == RelayBox.PlainPath(Path.Join(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "NexusRelay", "app")) && File.Exists(Path.Join(Source, "get.ps1")) && !Directory.Exists(Path.Join(Source, ".git"));
    private static Version Parse(string? value) => Version.TryParse(Regex.Match(value ?? "", @"\d+\.\d+\.\d+").Value, out var version) ? version : new(0, 0, 0);
    public static JsonObject Status(JsonObject? changes = null)
    {
        lock (Gate)
        {
            var data = RelayBox.ReadJson(StateFile) ?? new JsonObject { ["latest"] = null, ["state"] = "idle", ["checked_at"] = null };
            data["product"] = "relay"; data["version"] = RelayPaths.Version; data["runtime"] = "native"; data["self_updatable"] = Managed; data["source"] = Source;
            if (changes is not null) foreach (var kv in changes) data[kv.Key] = kv.Value?.DeepClone();
            RelayBox.WriteState(StateFile, data); return data;
        }
    }
    public static void Resume()
    {
        var marker = RelayBox.ReadJson(Marker);
        if (marker is null) { Status(); return; }
        string? target = marker["target"]?.ToString();
        if (target is not null && Parse(RelayPaths.Version) >= Parse(target)) { File.Delete(Marker); Status(new() { ["state"] = "done", ["message"] = "updated to " + RelayPaths.Version }); return; }
        if (RelayBox.PidAlive(marker["pid"]?.GetValue<int>())) { Status(new() { ["state"] = "updating" }); return; }
        if (marker["pid"] is null && marker["launch"]?.ToString() == "pending") { Status(new() { ["state"] = "updating", ["message"] = "Installer launch awaiting confirmation; no duplicate is submitted" }); return; }
        File.Delete(Marker); Status(new() { ["state"] = "failed", ["message"] = "Update did not complete; see update.log" });
    }
    public static void RefreshDue()
    {
        var data = Status(); double checkedAt = AppSessions.Epoch(data["checked_at"]?.ToString()) ?? 0;
        if (!Managed || Environment.GetEnvironmentVariable("NEXUS_RELAY_UPDATE_CHECK") == "0" || Reservations.Now() - checkedAt < 24 * 3600) return;
        using var client = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false }) { Timeout = TimeSpan.FromSeconds(10) };
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Head, "https://github.com/pironjulien/NexusRelay/releases/latest");
            using var response = client.Send(request);
            var match = Regex.Match(response.Headers.Location?.ToString() ?? "", @"/tag/v?(\d+\.\d+\.\d+)$");
            Status(new() { ["latest"] = match.Success ? match.Groups[1].Value : null, ["checked_at"] = RelayBox.NowIso(), ["message"] = match.Success ? null : "Latest version unavailable" });
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException) { Status(new() { ["checked_at"] = RelayBox.NowIso(), ["message"] = "Latest version unavailable (offline?)" }); }
    }
    public static (int Http, JsonObject Data) Start()
    {
        if (!Managed) return (409, Status(new() { ["message"] = "This development checkout follows git; no public updater is run" }));
        var data = Status(); if (data["state"]?.ToString() == "updating" && File.Exists(Marker)) return (202, data);
        RefreshDue(); data = Status(); string? latest = data["latest"]?.ToString();
        if (latest is not null && Parse(latest) <= Parse(RelayPaths.Version)) return (200, Status(new() { ["message"] = "Already up to date" }));
        Directory.CreateDirectory(ProductState);
        RelayBox.WriteState(Marker, new JsonObject { ["target"] = latest, ["started_at"] = RelayBox.NowIso(), ["launch"] = "pending" });
        bool entered = false;
        try
        {
            DetachedProcess.Start(Path.Join(Environment.SystemDirectory, "WindowsPowerShell", "v1.0", "powershell.exe"), ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", Path.Join(Source, "scripts", "update-native.ps1"), "-Root", Source], () => entered = true, show: 0);
        }
        catch (Exception e) // Boundary: any launch failure is reported as the update status, never thrown to the HTTP caller.
        {
            if (!entered) File.Delete(Marker);
            return (409, Status(new() { ["state"] = entered ? "updating" : "failed", ["message"] = e.Message }));
        }
        return (202, Status(new() { ["state"] = "updating", ["message"] = "Updating to " + (latest ?? "latest") }));
    }
}
