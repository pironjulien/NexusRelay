using System.Net;
using System.Text;
using System.Text.Json.Nodes;

namespace NexusRelay.Tests;

/// <summary>
/// <c>NexusRelay.exe --bridge</c>, the Antigravity Marketplace plugin's stdio side: stdin lines posted to the daemon,
/// replies written back one line each. Simulated stdio against a recording HTTP handler, then the real process
/// against an isolated daemon. Antigravity itself is never started.
/// </summary>
public sealed class BridgeTests : IDisposable
{
    private static readonly Uri Url = new("http://127.0.0.1:16181/mcp?agent=antigravity");
    private readonly Isolation iso = new();
    public void Dispose() => iso.Dispose();

    private sealed class Fake(Func<HttpRequestMessage, string, HttpResponseMessage> answer) : HttpMessageHandler
    {
        public List<(HttpMethod Method, string Body, string? Auth, string? Session, string? Accept)> Seen { get; } = [];
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            string body = request.Content is null ? "" : await request.Content.ReadAsStringAsync(cancellationToken);
            string? Header(string name) => request.Headers.TryGetValues(name, out var v) ? string.Join(", ", v) : null;
            lock (Seen) Seen.Add((request.Method, body, Header("Authorization"), Header("Mcp-Session-Id"), Header("Accept")));
            return answer(request, body);
        }
    }

    private static HttpResponseMessage Json(string text, string? session = null)
    {
        var response = new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(text, Encoding.UTF8, "application/json") };
        if (session is not null) response.Headers.Add("Mcp-Session-Id", session);
        return response;
    }

    private static (List<string> Out, string Err, int Code) Run(Fake fake, string? token, params string[] lines)
    {
        using var http = new HttpClient(fake);
        using var output = new MemoryStream();
        using var error = new StringWriter();
        int code = new StdioBridge(http, output, error, Url, token).RunAsync(new StringReader(string.Join("\n", lines) + "\n")).GetAwaiter().GetResult();
        var rows = Encoding.UTF8.GetString(output.ToArray()).Split('\n', StringSplitOptions.RemoveEmptyEntries).ToList();
        return (rows, error.ToString(), code);
    }

    [Fact]
    public void Initialize_opens_the_session_then_every_request_carries_it_and_the_session_is_closed()
    {
        var fake = new Fake((request, body) => request.Method == HttpMethod.Delete ? new HttpResponseMessage(HttpStatusCode.NoContent)
            : body.Contains("\"initialize\"", StringComparison.Ordinal) ? Json("{\"jsonrpc\":\"2.0\",\"id\":1,\n\"result\":{}}\r\n", "s-42")
            : body.Contains("\"id\"", StringComparison.Ordinal) ? Json("{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"tools\":[]}}")
            : new HttpResponseMessage(HttpStatusCode.Accepted));
        var (rows, err, code) = Run(fake, "secret-token",
            "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{}}", "", "   ",
            "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}",
            "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\"}");

        Assert.Equal(0, code);
        Assert.Equal("", err);
        Assert.Equal(["{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}", "{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"tools\":[]}}"], rows.Order(StringComparer.Ordinal));
        Assert.Equal(4, fake.Seen.Count);  // blank lines are not sent
        Assert.All(fake.Seen, s => Assert.Equal("Bearer secret-token", s.Auth));
        Assert.Null(fake.Seen[0].Session);
        Assert.All(fake.Seen.Skip(1), s => Assert.Equal("s-42", s.Session));
        Assert.All(fake.Seen.Where(s => s.Method == HttpMethod.Post), s => Assert.Equal("application/json, text/event-stream", s.Accept));
        Assert.Equal(HttpMethod.Delete, fake.Seen[^1].Method);
    }

    [Fact]
    public void Event_stream_events_are_written_one_line_each()
    {
        const string stream = "event: message\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\ndata:{\"jsonrpc\":\"2.0\",\n\ndata: {\"jsonrpc\":\"2.0\",\"id\":7,\"result\":{\"text\":\"é\"}}";
        var fake = new Fake((_, _) => new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(stream, Encoding.UTF8, "text/event-stream") });
        var (rows, _, _) = Run(fake, null, "{\"jsonrpc\":\"2.0\",\"id\":7,\"method\":\"tools/call\"}");

        Assert.Equal(["{\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}", "{\"jsonrpc\":\"2.0\",", "{\"jsonrpc\":\"2.0\",\"id\":7,\"result\":{\"text\":\"é\"}}"], rows);
        Assert.Null(fake.Seen.Single().Auth);  // no token: no Authorization header
        Assert.DoesNotContain(fake.Seen, s => s.Method == HttpMethod.Delete);  // no session to close
    }

    [Fact]
    public void An_HTTP_error_answers_a_request_but_not_a_notification()
    {
        var fake = new Fake((_, _) => new HttpResponseMessage(HttpStatusCode.Unauthorized));
        var (rows, err, _) = Run(fake, "t", "{\"jsonrpc\":\"2.0\",\"id\":\"a\",\"method\":\"ping\"}", "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}", "not json");

        var reply = JsonNode.Parse(Assert.Single(rows))!;
        Assert.Equal("a", reply["id"]!.ToString());
        Assert.Equal(-32000, reply["error"]!["code"]!.GetValue<int>());
        Assert.Equal("NexusRelay answered HTTP 401.", reply["error"]!["message"]!.ToString());
        Assert.Equal(3, err.Split('\n', StringSplitOptions.RemoveEmptyEntries).Count(l => l.Trim() == "NexusRelay answered HTTP 401."));
    }

    [Fact]
    public void An_unreachable_daemon_says_how_to_get_NexusRelay()
    {
        var fake = new Fake((_, _) => throw new HttpRequestException("outer", new IOException("connection refused")));
        var (rows, _, _) = Run(fake, "t", "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"initialize\"}");

        var reply = JsonNode.Parse(Assert.Single(rows))!;
        Assert.Equal(3, reply["id"]!.GetValue<int>());
        Assert.Equal("NexusRelay is not reachable on this PC (connection refused). Install or start it: ask the agent to follow the NexusRelay skill.", reply["error"]!["message"]!.ToString());
    }

    [Fact]
    public void Token_comes_from_the_environment_then_the_profile_file()
    {
        Assert.Null(StdioBridge.Token());
        Directory.CreateDirectory(Path.Join(iso.Home, ".nexus"));
        File.WriteAllText(Path.Join(iso.Home, ".nexus", "mcp-token.txt"), "  from-file \r\n");
        Assert.Equal("from-file", StdioBridge.Token());
        iso.Set("NEXUS_MCP_TOKEN", "from-env");
        Assert.Equal("from-env", StdioBridge.Token());
    }

    [Fact]
    public void Native_bridge_process_relays_stdio_to_the_daemon()
    {
        using var daemon = new Daemon();
        string url = $"http://127.0.0.1:{daemon.Port}/mcp?agent=antigravity";
        var ran = Native.Run([StdioBridge.Flag, url],
            "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},\"clientInfo\":{\"name\":\"bridge-test\",\"version\":\"1\"}}}\n" +
            "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n" +
            "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\"}\n",
            env: new Dictionary<string, string?> { ["NEXUS_MCP_TOKEN"] = Daemon.Token });

        Assert.True(ran.Code == 0, ran.Err);
        var replies = ran.Out.Split('\n', StringSplitOptions.RemoveEmptyEntries).Select(l => JsonNode.Parse(l)!).ToDictionary(r => r["id"]!.GetValue<int>());
        Assert.Equal("nexus-relay", replies[1]["result"]!["serverInfo"]!["name"]!.ToString());
        Assert.Equal(ProtocolTests.ExpectedTools.Order(), replies[2]["result"]!["tools"]!.AsArray().Select(t => t!["name"]!.ToString()).Order());

        var refused = Native.Run([StdioBridge.Flag, url], "{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"ping\"}\n", env: new Dictionary<string, string?> { ["NEXUS_MCP_TOKEN"] = "wrong" });
        Assert.Equal("NexusRelay answered HTTP 401.", JsonNode.Parse(refused.Out.Trim())!["error"]!["message"]!.ToString());
        Assert.Equal(2, Native.Run([StdioBridge.Flag]).Code);
    }

    // ---- Antigravity's command line (cmd.exe /d /c ${PLUGIN_ROOT}\scripts\start.cmd <url>) on real stdio.

    private string Pointer => Path.Join(iso.Home, ".nexus", "nexus-relay", "engine.txt");

    private static string MinimumOf(string launcher) =>
        System.Text.RegularExpressions.Regex.Match(File.ReadAllText(launcher), "set \"MINIMUM=([0-9.]+)\"").Groups[1].Value;

    /// <summary>The Marketplace copy of the launcher, in a plugin folder whose path has a space (a profile such as
    /// "Jean Dupont"), as Antigravity installs it.</summary>
    private string Launcher()
    {
        string root = Path.Join(iso.Root, "Jean Dupont", ".gemini", "config", "plugins", "nexus-relay");
        Directory.CreateDirectory(Path.Join(root, "scripts"));
        string launcher = Path.Join(root, "scripts", "start.cmd");
        File.Copy(Path.Join(Paths.Repo(), "plugin", "antigravity", "scripts", "start.cmd"), launcher, true);
        return launcher;
    }

    private static System.Diagnostics.ProcessStartInfo Cmd(string launcher, string url) =>
        Native.Start(Path.Join(Environment.SystemDirectory, "cmd.exe"), ["/d", "/c", launcher, url], env: new Dictionary<string, string?> { ["NEXUS_MCP_TOKEN"] = "e2e-token" });

    [Fact]
    public void Engine_pointer_names_the_versioned_copy_relative_to_the_profile_else_absolute()
    {
        string versioned = Path.Join(iso.Home, "AppData", "Local", "NexusRelay", "versions", "0123456789abcdef", "NexusRelay.exe");
        Assert.Equal(Pointer, StdioBridge.WritePointer(versioned));
        Assert.Equal($"{RelayPaths.Version}|AppData\\Local\\NexusRelay\\versions\\0123456789abcdef\\NexusRelay.exe\r\n", File.ReadAllText(Pointer));
        StdioBridge.WritePointer(@"D:\Portable\NexusRelay\NexusRelay.exe");
        Assert.Equal($"{RelayPaths.Version}|D:\\Portable\\NexusRelay\\NexusRelay.exe\r\n", File.ReadAllText(Pointer));
        Assert.Single(Directory.EnumerateFiles(Path.GetDirectoryName(Pointer)!));  // no temporary file left

        var ran = Native.Run(StdioBridge.PointerFlag);  // the running engine records itself
        Assert.True(ran.Code == 0, ran.Err);
        Assert.Equal(Pointer, ran.Out.Trim());
        Assert.Equal($"{RelayPaths.Version}|{Native.Exe}\r\n", File.ReadAllText(Pointer));
    }

    [Fact]
    public void Launcher_minimum_is_a_released_bridge_version_this_engine_meets()
    {
        var minimum = Version.Parse(MinimumOf(Path.Join(Paths.Repo(), "plugin", "antigravity", "scripts", "start.cmd")));
        Assert.InRange(minimum, new Version(1, 13, 8), Version.Parse(RelayPaths.Version));  // 1.13.8: first engine writing engine.txt
    }

    [Fact]
    public async Task Launcher_runs_the_recorded_engine_as_the_bridge_to_the_service()
    {
        var cancel = TestContext.Current.CancellationToken;
        Directory.CreateDirectory(Path.GetDirectoryName(Pointer)!);
        File.WriteAllText(Pointer, $"{RelayPaths.Version}|{Native.Exe}\r\n");

        using var listener = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        int port = ((IPEndPoint)listener.LocalEndpoint).Port;
        var seen = new List<string>();
        var service = Task.Run(async () =>
        {
            for (int i = 0; i < 2; i++)  // initialize (POST), then the session's DELETE
            {
                using var client = await listener.AcceptTcpClientAsync(cancel);
                using var stream = client.GetStream();
                string head = await ReadHead(stream, cancel);
                lock (seen) seen.Add(head);
                int length = head.Split("\r\n").Select(h => h.Split(':', 2)).Where(h => h.Length == 2 && h[0].Equals("Content-Length", StringComparison.OrdinalIgnoreCase)).Select(h => int.Parse(h[1].Trim(), System.Globalization.CultureInfo.InvariantCulture)).FirstOrDefault();
                await stream.ReadExactlyAsync(new byte[length], cancel);
                string body = head.StartsWith("POST", StringComparison.Ordinal) ? "{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"serverInfo\":{\"name\":\"fixture\"}}}" : "";
                string reply = $"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nMcp-Session-Id: e2e-session\r\nContent-Length: {Encoding.UTF8.GetByteCount(body)}\r\nConnection: close\r\n\r\n{body}";
                await stream.WriteAsync(Encoding.UTF8.GetBytes(reply), cancel);
            }
        }, cancel);

        using var process = System.Diagnostics.Process.Start(Cmd(Launcher(), $"http://127.0.0.1:{port}/mcp?agent=antigravity"))!;
        try
        {
            await process.StandardInput.WriteLineAsync("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{}}".AsMemory(), cancel);
            await process.StandardInput.FlushAsync(cancel);
            string? line = await process.StandardOutput.ReadLineAsync(cancel).AsTask().WaitAsync(TimeSpan.FromSeconds(30), cancel);
            Assert.Equal("{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"serverInfo\":{\"name\":\"fixture\"}}}", line);
            process.StandardInput.Close();
            Assert.True(process.WaitForExit(15_000));
            Assert.Equal(0, process.ExitCode);
            await service.WaitAsync(TimeSpan.FromSeconds(10), cancel);
        }
        finally
        {
            if (!process.HasExited) process.Kill(true);
        }
        Assert.StartsWith("POST /mcp?agent=antigravity HTTP/1.1", seen[0], StringComparison.Ordinal);
        Assert.Contains("Authorization: Bearer e2e-token", seen[0], StringComparison.Ordinal);
        Assert.StartsWith("DELETE /mcp?agent=antigravity HTTP/1.1", seen[1], StringComparison.Ordinal);
        Assert.Contains("Mcp-Session-Id: e2e-session", seen[1], StringComparison.Ordinal);
    }

    [Fact]
    public void Launcher_refuses_a_missing_engine_and_an_engine_older_than_its_bridge()
    {
        string launcher = Launcher();
        Ran Launch()
        {
            using var process = System.Diagnostics.Process.Start(Cmd(launcher, "http://127.0.0.1:9/mcp?agent=antigravity"))!;
            var output = process.StandardOutput.ReadToEndAsync(); var error = process.StandardError.ReadToEndAsync();
            process.StandardInput.Close();
            Assert.True(process.WaitForExit(15_000));
            return new(process.ExitCode, output.Result, error.Result);
        }
        void Record(string text) { Directory.CreateDirectory(Path.GetDirectoryName(Pointer)!); File.WriteAllText(Pointer, text); }

        var none = Launch();  // nothing recorded (no install, or one older than 1.13.8)
        Assert.Equal((1, ""), (none.Code, none.Out));  // stdout carries the protocol only
        Assert.Contains("NexusRelay is not installed on this PC.", none.Err, StringComparison.Ordinal);

        Record($"{RelayPaths.Version}|AppData\\Local\\NexusRelay\\versions\\gone\\NexusRelay.exe\r\n");  // folder removed since
        Assert.Contains("NexusRelay is not installed on this PC.", Launch().Err, StringComparison.Ordinal);

        string minimum = MinimumOf(launcher);
        foreach (string old in new[] { "1.13.7", "0.9.99", "garbage" })
        {
            Record($"{old}|{Native.Exe}\r\n");
            var refused = Launch();
            Assert.Equal((1, ""), (refused.Code, refused.Out));
            Assert.Contains($"NexusRelay {old} is installed on this PC, but this plugin needs {minimum} or later.", refused.Err, StringComparison.Ordinal);
        }
    }

    private static async Task<string> ReadHead(System.Net.Sockets.NetworkStream stream, CancellationToken cancel)
    {
        var head = new List<byte>();
        var one = new byte[1];
        while (!(head.Count >= 4 && head[^4] == '\r' && head[^3] == '\n' && head[^2] == '\r' && head[^1] == '\n'))
        {
            if (await stream.ReadAsync(one, cancel) == 0) break;
            head.Add(one[0]);
        }
        return Encoding.ASCII.GetString([.. head]);
    }
}
