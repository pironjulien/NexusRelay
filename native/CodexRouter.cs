using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;

namespace NexusRelay;

public static partial class CodexRouter
{
    public static string? ConfiguredUrl => Environment.GetEnvironmentVariable("NEXUS_RELAY_CODEX_ROUTER")?.Trim() is string url && url.Length > 0 ? url : null;
    public static bool Ready()
    {
        if (ConfiguredUrl is not string raw) return true;
        if (!Uri.TryCreate(raw, UriKind.Absolute, out var url) || !url.IsLoopback || url.Scheme is not ("http" or "https")) return false;
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
            return JsonNode.Parse(client.GetStringAsync(url).GetAwaiter().GetResult())?["status"]?.ToString() == "ok";
        }
        catch (Exception e) when (e is HttpRequestException or OperationCanceledException or System.Text.Json.JsonException) { return false; }
    }
    // Pooled usage of the router (Codex LB: /api/usage/summary on the health address' host), in the journal's shape.
    public static JsonObject? Usage()
    {
        if (ConfiguredUrl is not string raw || !Uri.TryCreate(raw, UriKind.Absolute, out var health) || !health.IsLoopback || health.Scheme is not ("http" or "https")) return null;
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
            return UsageFromSummary(JsonNode.Parse(client.GetStringAsync(new Uri(health, "/api/usage/summary")).GetAwaiter().GetResult()), DateTimeOffset.UtcNow);
        }
        catch (Exception e) when (e is HttpRequestException or OperationCanceledException or System.Text.Json.JsonException or InvalidOperationException) { return null; }
    }
    public static JsonObject? UsageFromSummary(JsonNode? summary, DateTimeOffset now)
    {
        var windows = new JsonObject();
        foreach (var (name, key) in new[] { ("primary", "primaryWindow"), ("secondary", "secondaryWindow") })
        {
            if (summary?[key] is not JsonObject w || w["remainingPercent"] is not JsonValue left || !left.TryGetValue<double>(out double remaining)) continue;
            double reset = DateTimeOffset.TryParse(w["resetAt"]?.ToString(), System.Globalization.CultureInfo.InvariantCulture, System.Globalization.DateTimeStyles.AssumeUniversal, out var at) ? at.ToUnixTimeSeconds() : 0;
            bool current = reset > now.ToUnixTimeSeconds();
            windows[name] = new JsonObject { ["used_pct"] = current ? Math.Round(100 - remaining, 1) : 0, ["resets_at"] = reset, ["current"] = current, ["minutes"] = w["windowMinutes"]?.DeepClone() };
        }
        return windows.Count == 0 ? null : new() { ["source"] = "routeur Codex (quota cumulé)", ["observed"] = now.ToString("o"), ["windows"] = windows };
    }
    public static List<string> SplitCommand(string command)
    {
        nint array = CommandLineToArgvW(command, out int count);
        if (array == 0) return [];
        try { return Enumerable.Range(0, count).Select(i => Marshal.PtrToStringUni(Marshal.ReadIntPtr(array, i * IntPtr.Size)) ?? "").ToList(); }
        finally { LocalFree(array); }
    }
    public static bool Ensure(double timeoutSeconds = 60)
    {
        if (Ready()) return true;
        if (AppTransport.EmergencyStop()) return false;
        var candidates = (Environment.GetEnvironmentVariable("NEXUS_RELAY_CODEX_ROUTER_START") ?? "").Split(';', StringSplitOptions.RemoveEmptyEntries).Select(s => SplitCommand(s.Trim()));
        var command = candidates.FirstOrDefault(args => args.Count > 0 && Path.IsPathRooted(args[0]) && File.Exists(args[0]));
        if (command is null) return false;
        if (Path.GetExtension(command[0]).Equals(".ps1", StringComparison.OrdinalIgnoreCase)) command = ["pwsh", "-NoProfile", "-NonInteractive", "-File", .. command];
        var start = new ProcessStartInfo(command[0]) { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden };
        foreach (string arg in command.Skip(1)) start.ArgumentList.Add(arg);
        try { using var process = Process.Start(start); }
        catch (Exception e) when (e is IOException or System.ComponentModel.Win32Exception or ArgumentException) { return false; }
        return AppTransport.Wait(() => !AppTransport.EmergencyStop() && Ready(), timeoutSeconds);
    }
    public static JsonObject Inspect() => new() { ["configured"] = ConfiguredUrl is not null, ["status"] = ConfiguredUrl is null ? "not configured" : Ready() ? "ready" : "unavailable" };
    [LibraryImport("shell32.dll", SetLastError = true, StringMarshalling = StringMarshalling.Utf16)] private static partial nint CommandLineToArgvW(string command, out int count);
    [LibraryImport("kernel32.dll")] private static partial nint LocalFree(nint handle);
}
