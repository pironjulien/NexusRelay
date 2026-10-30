using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;

namespace NexusRelay;

public static class CodexRouter
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
    [DllImport("shell32.dll", SetLastError = true, CharSet = CharSet.Unicode)] private static extern nint CommandLineToArgvW(string command, out int count);
    [DllImport("kernel32.dll")] private static extern nint LocalFree(nint handle);
}
