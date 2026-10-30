using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json.Nodes;

[assembly: CollectionBehavior(CollectionBehavior.CollectionPerAssembly)]

namespace NexusRelay.Tests;

/// <summary>
/// Common guards: temporary mailbox, profile and app folders; no network; no installed AI touched. Process-wide
/// environment (tests are serialized), inherited by every native process a test starts.
/// </summary>
public sealed class Isolation : IDisposable
{
    private static readonly string[] Cleared =
    [
        "NEXUS_RELAY_VAULTS", "NEXUS_RELAY_CODEX_ROUTER", "NEXUS_RELAY_CODEX_ROUTER_START", "NEXUS_RELAY_HOST",
        "NEXUS_RELAY_CODEX_WORKSPACE", "NEXUS_RELAY_CODEX_PROJECT", "NEXUS_RELAY_CODEX_PROJECT_ID", "NEXUS_RELAY_WAKE_SINCE",
        "NEXUS_RELAY_AGENTAPI", "NEXUS_MCP_TOKEN", "NEXUS_MCP_TOKEN_FILE", "NEXUS_PACK", "NEXUS_PACK_TARGETS",
        "CODEX_SESSION_ID", "CODEX_THREAD_ID", "NEXUS_RELAY_SESSION", "NEXUS_RELAY_SOURCE_ROOT",
    ];
    private readonly Dictionary<string, string?> saved = new();
    public string Root { get; }
    public string Home => Path.Combine(Root, "home");
    public string Relay => Path.Combine(Root, "relay");
    public string CodexHome => Path.Combine(Root, "codex-home");
    public string Gemini => Path.Combine(Root, "gemini");
    public static readonly string RealProfile = Environment.GetEnvironmentVariable("USERPROFILE") ?? Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);

    public Isolation()
    {
        Root = Path.Combine(Paths.Scratch, Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Home);
        Set("USERPROFILE", Home); Set("HOME", Home);
        foreach (string name in Cleared) Set(name, null);
        Set("NEXUS_RELAY_HOME", Relay);
        Set("NEXUS_RELAY_GEMINI_HOME", Gemini);
        Set("NEXUS_JOURNAL_HOME", Path.Combine(Root, "journal"));
        Set("NEXUS_RELAY_AGENT", "claude");
        Set("NEXUS_RELAY_LANG", "fr");
        Set("CODEX_HOME", CodexHome);
        Set("NEXUS_RELAY_KEYPAD_QUOTA", Path.Combine(Root, "keypad-quota.json"));
        Set("NEXUS_RELAY_CODEX_PORT", "1");
        Set("NEXUS_RELAY_AGY_PORT", "1");
        Set("NEXUS_RELAY_UPDATE_CHECK", "0");
    }

    public RelayBox Box() => new(Path.Combine(Relay, "v1"));

    public void Set(string name, string? value)
    {
        if (!saved.ContainsKey(name)) saved[name] = Environment.GetEnvironmentVariable(name);
        Environment.SetEnvironmentVariable(name, value);
    }

    public void Dispose()
    {
        foreach (var (name, value) in saved) Environment.SetEnvironmentVariable(name, value);
        for (int i = 0; i < 5; i++)
        {
            try { if (Directory.Exists(Root)) Directory.Delete(Root, true); return; }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException) { Thread.Sleep(200); }
        }
    }
}

public static class Paths
{
    public static string Repo([CallerFilePath] string file = "") => Path.GetFullPath(Path.Combine(Path.GetDirectoryName(file)!, "..", ".."));
    /// <summary>Test files live outside OneDrive and AppData, with the build outputs.</summary>
    public static string Scratch => Path.Combine(Isolation.RealProfile, ".nexus", "build", "nexusrelay", "test-scratch");
    public static string Golden(string name) => Path.Combine(AppContext.BaseDirectory, "Golden", name);
    public static string Fixture(string name) => Path.Combine(AppContext.BaseDirectory, "Fixtures", name);
}

public sealed record Ran(int Code, string Out, string Err);

public static class Native
{
    /// <summary>The engine built by the project reference, next to the tests (apphost, else its dll through dotnet).</summary>
    public static string Exe
    {
        get
        {
            string exe = Path.Combine(AppContext.BaseDirectory, "NexusRelay.exe");
            if (!File.Exists(exe)) throw new FileNotFoundException("NexusRelay.exe not copied next to the tests", exe);
            return exe;
        }
    }

    public static ProcessStartInfo Start(string file, IEnumerable<string> args, string? cwd = null, IDictionary<string, string?>? env = null)
    {
        var start = new ProcessStartInfo(file)
        {
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = new UTF8Encoding(false), StandardErrorEncoding = new UTF8Encoding(false), StandardInputEncoding = new UTF8Encoding(false),
        };
        if (cwd is not null) start.WorkingDirectory = cwd;
        foreach (string arg in args) start.ArgumentList.Add(arg);
        if (env is not null) foreach (var (k, v) in env) { if (v is null) start.Environment.Remove(k); else start.Environment[k] = v; }
        return start;
    }

    public static Ran RunFile(string file, IEnumerable<string> args, string? input = null, string? cwd = null, IDictionary<string, string?>? env = null, int timeoutS = 60)
    {
        using var process = Process.Start(Start(file, args, cwd, env))!;
        var output = process.StandardOutput.ReadToEndAsync(); var error = process.StandardError.ReadToEndAsync();
        if (input is not null) process.StandardInput.Write(input);
        process.StandardInput.Close();
        if (!process.WaitForExit(timeoutS * 1000)) { process.Kill(true); throw new TimeoutException($"{file} {string.Join(' ', args)}"); }
        process.WaitForExit();
        return new(process.ExitCode, output.Result, error.Result);
    }

    public static Ran Run(params string[] args) => RunFile(Exe, args);
    public static Ran Run(string[] args, string? input = null, string? cwd = null, IDictionary<string, string?>? env = null) => RunFile(Exe, args, input, cwd, env);

    public static JsonNode Json(params string[] args)
    {
        var ran = Run(args);
        Assert.True(ran.Code == 0, ran.Err + ran.Out);
        return JsonNode.Parse(ran.Out)!;
    }

    public static Ran Pwsh(string script, params string[] args) => RunFile("pwsh", ["-NoProfile", "-NonInteractive", "-File", script, .. args]);

    public static List<JsonObject> Stdio(string agent, params JsonObject[] requests)
    {
        string payload = string.Join("\n", requests.Select(r => r.ToJsonString())) + "\n";
        var ran = Run(["--mcp", "--agent", agent, "--protocol-fixture"], payload);
        Assert.True(ran.Code == 0, ran.Err);
        return ran.Out.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).Select(l => JsonNode.Parse(l)!.AsObject()).ToList();
    }

    private static int nextId = 1;
    public static JsonObject Call(string name, JsonObject? arguments = null) =>
        new() { ["jsonrpc"] = "2.0", ["id"] = nextId++, ["method"] = "tools/call", ["params"] = new JsonObject { ["name"] = name, ["arguments"] = arguments ?? new JsonObject() } };
    public static JsonObject Rpc(string method) => new() { ["jsonrpc"] = "2.0", ["id"] = nextId++, ["method"] = method, ["params"] = new JsonObject() };

    public static JsonObject Data(JsonObject response) => JsonNode.Parse(response["result"]!["content"]![0]!["text"]!.ToString())!.AsObject();

    public static int FreePort()
    {
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        return ((IPEndPoint)listener.LocalEndpoint).Port;
    }

    public static string Git(string cwd, params string[] args)
    {
        var ran = RunFile("git", ["-C", cwd, .. args]);
        Assert.True(ran.Code == 0, ran.Err);
        return ran.Out;
    }
}

/// <summary>A native daemon (--http) on a free port, isolated mailbox and closed app ports.</summary>
public sealed class Daemon : IDisposable
{
    public const string Token = "isolated-parity-token-isolated-parity-token-isolated-parity-token"; // gitleaks:allow (fake test token)
    public int Port { get; } = Native.FreePort();
    public Process Process { get; }
    private readonly HttpClient http = new() { Timeout = TimeSpan.FromSeconds(20) };

    public Daemon(bool fixture = true, IDictionary<string, string?>? env = null)
    {
        var all = new Dictionary<string, string?> { ["NEXUS_RELAY_HOST"] = "claude", ["NEXUS_MCP_TOKEN"] = Token, ["NEXUS_RELAY_WAKE_SINCE"] = "2099-01-01T00:00:00Z" };
        if (env is not null) foreach (var (k, v) in env) all[k] = v;
        string[] args = fixture ? ["--http", Port.ToString(), "--protocol-fixture"] : ["--http", Port.ToString()];
        var start = Native.Start(Native.Exe, args, env: all);
        start.RedirectStandardInput = false; start.RedirectStandardOutput = false; start.RedirectStandardError = false;
        start.StandardInputEncoding = null; start.StandardOutputEncoding = null; start.StandardErrorEncoding = null;
        Process = Process.Start(start)!;
        var deadline = DateTime.UtcNow.AddSeconds(15);
        while (true)
        {
            try { if (Health().IsSuccessStatusCode) break; }
            catch (HttpRequestException) { }
            Assert.True(DateTime.UtcNow < deadline, "native daemon did not answer /health");
            Thread.Sleep(50);
        }
    }

    public HttpResponseMessage Health() => http.GetAsync($"http://127.0.0.1:{Port}/health").GetAwaiter().GetResult();

    public HttpResponseMessage Post(string agent, JsonObject body, string? token = Token, string accept = "application/json")
    {
        var request = new HttpRequestMessage(HttpMethod.Post, $"http://127.0.0.1:{Port}/mcp?agent={agent}") { Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json") };
        if (token is not null) request.Headers.Authorization = new("Bearer", token);
        request.Headers.Accept.ParseAdd(accept);
        request.Headers.Add("Mcp-Session-Id", "parity-session");
        return http.Send(request);
    }

    public JsonNode? Rpc(JsonObject body, string agent = "claude", string accept = "application/json")
    {
        using var response = Post(agent, body, accept: accept);
        string text = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();
        Assert.True(response.IsSuccessStatusCode, $"{response.StatusCode} {text}");
        if (accept == "text/event-stream")
            return new JsonArray(text.Split('\n').Where(l => l.StartsWith("data: ", StringComparison.Ordinal)).Select(l => JsonNode.Parse(l[6..])).ToArray());
        return text.Length == 0 ? null : JsonNode.Parse(text);
    }

    public void Dispose()
    {
        if (!Process.HasExited) { Process.Kill(true); Process.WaitForExit(10_000); }
        Process.Dispose(); http.Dispose();
    }
}

public static class Wait
{
    public static void Until(Func<bool> condition, double seconds = 10, string what = "condition")
    {
        var deadline = DateTime.UtcNow.AddSeconds(seconds);
        while (!condition()) { Assert.True(DateTime.UtcNow < deadline, "timed out waiting for " + what); Thread.Sleep(20); }
    }
}
