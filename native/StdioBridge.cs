using System.Text;
using System.Text.Json.Nodes;

namespace NexusRelay;

/// <summary>
/// <c>NexusRelay.exe --bridge &lt;url&gt;</c>: the stdio side of the Antigravity Marketplace plugin. Antigravity starts
/// plugin servers over stdio and expands only <c>${PLUGIN_ROOT}</c>, so a Marketplace copy cannot name this PC's token
/// (<c>NEXUS_MCP_TOKEN</c>, else <c>%USERPROFILE%\.nexus\mcp-token.txt</c>). Each JSON-RPC line read on stdin is posted
/// to the local daemon (MCP Streamable HTTP) with that token, and every reply, JSON or event stream, is written back
/// one line each. A request that cannot be delivered is answered with a JSON-RPC error saying how to get the daemon.
/// </summary>
public sealed class StdioBridge
{
    public const string Flag = "--bridge";
    public const string Product = "NexusRelay";

    private static readonly UTF8Encoding Utf8 = new(false);
    private readonly object gate = new();
    private readonly HttpClient http;
    private readonly Stream output;
    private readonly TextWriter error;
    private readonly Uri url;
    private readonly string? token;
    private volatile string? session;

    public StdioBridge(HttpClient http, Stream output, TextWriter error, Uri url, string? token)
    {
        this.http = http; this.output = output; this.error = error; this.url = url; this.token = token;
    }

    /// <summary>Entry point of <c>--bridge &lt;url&gt;</c> on the process's own stdin, stdout and stderr.</summary>
    public static async Task<int> RunCliAsync(string[] args)
    {
        int at = Array.IndexOf(args, Flag);
        if (at < 0 || at + 1 >= args.Length || !Uri.TryCreate(args[at + 1], UriKind.Absolute, out var url))
        {
            await Console.Error.WriteLineAsync("Usage: NexusRelay.exe --bridge <http://127.0.0.1:port/mcp?agent=...>").ConfigureAwait(false);
            return 2;
        }
        using var handler = new SocketsHttpHandler { UseProxy = false };
        using var http = new HttpClient(handler) { Timeout = Timeout.InfiniteTimeSpan };
        using var input = new StreamReader(Console.OpenStandardInput(), Utf8);
        using var output = Console.OpenStandardOutput();
        var error = new StreamWriter(Console.OpenStandardError(), Utf8) { AutoFlush = true };
        await using (error.ConfigureAwait(false))
            return await new StdioBridge(http, output, error, url, Token()).RunAsync(input).ConfigureAwait(false);
    }

    /// <summary>The local token: <c>NEXUS_MCP_TOKEN</c>, else the trimmed <c>~\.nexus\mcp-token.txt</c>, else none.</summary>
    public static string? Token()
    {
        string? token = Environment.GetEnvironmentVariable("NEXUS_MCP_TOKEN");
        if (!string.IsNullOrWhiteSpace(token)) return token;
        string file = Path.Combine(RelayPaths.UserHome(), ".nexus", "mcp-token.txt");
        return File.Exists(file) ? File.ReadAllText(file).Trim() : null;
    }

    /// <summary>Relays every line of <paramref name="input"/> until it ends, then closes the HTTP session.</summary>
    public async Task<int> RunAsync(TextReader input)
    {
        var pending = new List<Task>();
        string? line;
        while ((line = await input.ReadLineAsync().ConfigureAwait(false)) is not null)
        {
            if (line.Trim().Length == 0) continue;
            string message = line;
            var task = Task.Run(() => ForwardAsync(message));
            // The session id comes with the initialize reply: nothing else is sent before it.
            if (session is null && message.Contains("\"initialize\"", StringComparison.Ordinal)) await task.ConfigureAwait(false);
            lock (pending) { pending.RemoveAll(t => t.IsCompleted); pending.Add(task); }
        }
        Task[] left;
        lock (pending) left = [.. pending];
        try { await Task.WhenAll(left).WaitAsync(TimeSpan.FromSeconds(5)).ConfigureAwait(false); }
        catch (TimeoutException) { /* Replies still streaming after 5 s are dropped with the process, as before. */ }
        if (session is not null)
        {
            try
            {
                using var bye = new HttpRequestMessage(HttpMethod.Delete, url);
                Authorize(bye);
                using var cut = new CancellationTokenSource(TimeSpan.FromSeconds(2));
                using var _ = await http.SendAsync(bye, cut.Token).ConfigureAwait(false);
            }
            catch (Exception e) when (e is HttpRequestException or OperationCanceledException) { /* The daemon ends idle sessions itself. */ }
        }
        return 0;
    }

    private void Authorize(HttpRequestMessage request)
    {
        if (!string.IsNullOrEmpty(token)) request.Headers.TryAddWithoutValidation("Authorization", "Bearer " + token);
        if (session is not null) request.Headers.TryAddWithoutValidation("Mcp-Session-Id", session);
    }

    private async Task ForwardAsync(string message)
    {
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, url) { Content = new StringContent(message, Utf8, "application/json") };
            request.Headers.TryAddWithoutValidation("Accept", "application/json, text/event-stream");
            Authorize(request);
            using var response = await http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead).ConfigureAwait(false);
            if (response.Headers.TryGetValues("Mcp-Session-Id", out var ids)) foreach (string id in ids) session = id;
            if (!response.IsSuccessStatusCode)
            {
                Fail(message, $"{Product} answered HTTP {(int)response.StatusCode}.");
                return;
            }
            string type = response.Content.Headers.ContentType?.MediaType ?? "";
            using var body = new StreamReader(await response.Content.ReadAsStreamAsync().ConfigureAwait(false), Utf8);
            if (type.Equals("text/event-stream", StringComparison.OrdinalIgnoreCase))
            {
                var data = new StringBuilder();
                string? row;
                while ((row = await body.ReadLineAsync().ConfigureAwait(false)) is not null)
                {
                    if (row.StartsWith("data:", StringComparison.Ordinal)) data.Append(row.AsSpan(row.Length > 5 && row[5] == ' ' ? 6 : 5));
                    else if (row.Length == 0 && data.Length > 0) { Write(data.ToString()); data.Clear(); }
                }
                if (data.Length > 0) Write(data.ToString());
            }
            else
            {
                string text = (await body.ReadToEndAsync().ConfigureAwait(false)).Trim();
                if (text.Length > 0) Write(text.Replace("\r", "", StringComparison.Ordinal).Replace("\n", "", StringComparison.Ordinal));
            }
        }
        catch (Exception e)
        {
            var root = e;
            while (root.InnerException is not null) root = root.InnerException;
            Fail(message, $"{Product} is not reachable on this PC ({root.Message}). Install or start it: ask the agent to follow the {Product} skill.");
        }
    }

    /// <summary>Reports <paramref name="reason"/> on stderr and, for a request (id and method), as its JSON-RPC error.</summary>
    private void Fail(string message, string reason)
    {
        lock (gate) error.WriteLine(reason);
        JsonObject? parsed;
        try { parsed = JsonNode.Parse(message) as JsonObject; }
        catch (System.Text.Json.JsonException) { return; }
        if (parsed is null || !parsed.TryGetPropertyValue("id", out var id) || !parsed.ContainsKey("method")) return;
        var reply = new JsonObject
        {
            ["jsonrpc"] = "2.0",
            ["id"] = id?.DeepClone(),
            ["error"] = new JsonObject { ["code"] = -32000, ["message"] = reason },
        };
        Write(reply.ToJsonString());
    }

    private void Write(string json)
    {
        byte[] bytes = Utf8.GetBytes(json + "\n");
        lock (gate) { output.Write(bytes, 0, bytes.Length); output.Flush(); }
    }
}
