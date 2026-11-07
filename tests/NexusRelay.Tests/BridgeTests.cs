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
}
