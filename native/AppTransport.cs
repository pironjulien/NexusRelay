using System.Diagnostics;
using System.Net.WebSockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json.Nodes;

namespace NexusRelay;

// The app stays the owner of the conversation. No model API, private account token or second AI engine is used.
public sealed class CdpPage : IDisposable
{
    private readonly ClientWebSocket socket = new();
    private int next;
    public CdpPage(string url)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        socket.ConnectAsync(new Uri(url), timeout.Token).GetAwaiter().GetResult();
    }
    public JsonObject Call(string method, JsonObject? args = null)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        int id = ++next;
        byte[] bytes = Encoding.UTF8.GetBytes(new JsonObject { ["id"] = id, ["method"] = method, ["params"] = args ?? new() }.ToJsonString());
        socket.SendAsync(bytes.AsMemory(), WebSocketMessageType.Text, true, timeout.Token).AsTask().GetAwaiter().GetResult();
        while (true)
        {
            using var data = new MemoryStream();
            var buffer = new byte[65536];
            ValueWebSocketReceiveResult read;
            do
            {
                read = socket.ReceiveAsync(buffer.AsMemory(), timeout.Token).AsTask().GetAwaiter().GetResult();
                if (read.MessageType == WebSocketMessageType.Close) throw new IOException("DevTools connection closed");
                data.Write(buffer, 0, read.Count);
                if (data.Length > 8 * 1024 * 1024) throw new IOException("DevTools response too large");
            } while (!read.EndOfMessage);
            var response = JsonNode.Parse(data.ToArray()) as JsonObject;
            if (response?["id"]?.GetValue<int>() != id) continue;
            if (response["error"] is JsonObject error) throw new RelayError(error["message"]?.ToString() ?? "DevTools error");
            return response["result"] as JsonObject ?? new();
        }
    }
    public JsonNode? Eval(string expression)
    {
        var result = Call("Runtime.evaluate", new() { ["expression"] = expression, ["returnByValue"] = true, ["awaitPromise"] = true });
        if (result["exceptionDetails"] is JsonObject error) throw new RelayError(error["text"]?.ToString() ?? "JavaScript exception");
        return result["result"]?["value"]?.DeepClone();
    }
    public bool Press(string element)
    {
        if (Eval($"(() => {{const e={element};if(!e)return null;const r=e.getBoundingClientRect();const x=r.x+r.width/2,y=r.y+r.height/2;const top=document.elementFromPoint(x,y);return r.width&&r.height&&top&&(top===e||e.contains(top))?[x,y]:null}})()") is not JsonArray xy) return false;
        foreach (var type in new[] { "mouseMoved", "mousePressed", "mouseReleased" })
            Call("Input.dispatchMouseEvent", new() { ["type"] = type, ["x"] = xy[0]!.DeepClone(), ["y"] = xy[1]!.DeepClone(), ["button"] = "left", ["clickCount"] = 1 });
        return true;
    }
    // Hover-revealed controls stay clipped in a hidden window (Chromium pauses CSS transitions): focus the exact
    // element and press Enter, a trusted activation that needs no hit test.
    public bool Activate(string element)
    {
        if (!AppTransport.Truth(Eval($"(()=>{{const e={element};if(!e||e.disabled)return false;e.focus();return document.activeElement===e}})()"))) return false;
        Call("Input.dispatchKeyEvent", new() { ["type"] = "rawKeyDown", ["key"] = "Enter", ["code"] = "Enter", ["windowsVirtualKeyCode"] = 13 });
        Call("Input.dispatchKeyEvent", new() { ["type"] = "char", ["key"] = "Enter", ["text"] = "\r" });
        Call("Input.dispatchKeyEvent", new() { ["type"] = "keyUp", ["key"] = "Enter", ["code"] = "Enter", ["windowsVirtualKeyCode"] = 13 });
        return true;
    }
    public bool Click(params string[] labels) => Press($"[...document.querySelectorAll('button,[role=button],[role=menuitem],[role=option]')].find(e=>e.getClientRects().length&&{System.Text.Json.JsonSerializer.Serialize(labels)}.includes((e.getAttribute('aria-label')||e.innerText||'').trim()))");
    public void Escape() { Call("Input.dispatchKeyEvent", new() { ["type"] = "keyDown", ["key"] = "Escape", ["code"] = "Escape", ["windowsVirtualKeyCode"] = 27 }); Call("Input.dispatchKeyEvent", new() { ["type"] = "keyUp", ["key"] = "Escape", ["code"] = "Escape", ["windowsVirtualKeyCode"] = 27 }); }
    public void Dispose() { socket.Dispose(); }
}

public static class AppTransport
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(3) };
    public static readonly Dictionary<string, SemaphoreSlim> Locks = new() { ["codex"] = new(1), ["antigravity"] = new(1) };
    public static int Port(string app) => int.TryParse(Environment.GetEnvironmentVariable(app == "codex" ? "NEXUS_RELAY_CODEX_PORT" : "NEXUS_RELAY_AGY_PORT"), out int port) ? port : app == "codex" ? 9223 : 9227;
    public static JsonArray? Targets(string app)
    {
        try { return JsonNode.Parse(Http.GetStringAsync($"http://127.0.0.1:{Port(app)}/json/list").GetAwaiter().GetResult()) as JsonArray; }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or System.Text.Json.JsonException) { return null; }
    }
    public static string? PageUrl(string app) => Targets(app)?.OfType<JsonObject>().FirstOrDefault(t => t["type"]?.ToString() == "page" && (app == "codex" ? t["url"]?.ToString() == "app://-/index.html" : !(t["url"]?.ToString() ?? "").StartsWith("data:", StringComparison.Ordinal)))?["webSocketDebuggerUrl"]?.ToString();
    public static bool Wait(Func<bool> probe, double seconds = 6)
    {
        var deadline = Stopwatch.StartNew();
        do { if (probe()) return true; Thread.Sleep(100); } while (deadline.Elapsed.TotalSeconds < seconds);
        return false;
    }
    public static string Quote(string? text) => System.Text.Json.JsonSerializer.Serialize(text);
    public static bool Truth(JsonNode? node) => node is JsonValue v && v.TryGetValue<bool>(out var b) && b;
    public static bool UserPresent(string app)
    {
        var info = new LastInput { Size = (uint)Marshal.SizeOf<LastInput>() };
        if (!GetLastInputInfo(ref info)) return true; // No evidence of absence: keep the user's window alone.
        if (unchecked((uint)Environment.TickCount - info.Tick) > 120_000) return false;
        GetWindowThreadProcessId(GetForegroundWindow(), out uint pid);
        try { return Process.GetProcessById((int)pid).ProcessName.Equals(app == "codex" ? "ChatGPT" : "Antigravity", StringComparison.OrdinalIgnoreCase); }
        catch (ArgumentException) { return false; }
    }
    public static uint InputTick() { var info = new LastInput { Size = (uint)Marshal.SizeOf<LastInput>() }; return GetLastInputInfo(ref info) ? info.Tick : unchecked((uint)Environment.TickCount); }
    public static bool EmergencyStop()
    {
        string stop = Path.Combine(RelayPaths.UserHome(), ".nexus", "stop");
        return File.Exists(stop) && DateTime.UtcNow - File.GetLastWriteTimeUtc(stop) < TimeSpan.FromSeconds(120);
    }
    public static JsonObject Result(string status, string? reason = null) => new() { ["status"] = status, ["reason"] = reason };
    public static string? Executable(string app)
    {
        var names = app == "codex" ? new[] { "ChatGPT" } : new[] { "Antigravity" };
        foreach (var name in names) foreach (var process in Process.GetProcessesByName(name))
            using (process) { try { if (process.MainModule?.FileName is string exe && AppLifecycle.InstalledExecutable(app, exe)) return exe; } catch (System.ComponentModel.Win32Exception) { } }
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (app == "antigravity")
            return new[] { Path.Combine(local, "Programs", "Antigravity", "Antigravity.exe"), Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Antigravity", "Antigravity.exe") }.FirstOrDefault(File.Exists);
        return null;
    }
    public static JsonObject EnsureApp(string app)
        => AppLifecycle.Ensure(app);
    [StructLayout(LayoutKind.Sequential)] private struct LastInput { public uint Size; public uint Tick; }
    [DllImport("user32.dll")] private static extern bool GetLastInputInfo(ref LastInput input);
    [DllImport("user32.dll")] private static extern nint GetForegroundWindow();
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(nint hwnd, out uint pid);
}
