using System.Collections.Concurrent;
using System.IO.Pipes;
using System.Text;
using System.Text.Json.Nodes;

namespace NexusRelay.Tests;

/// <summary>What the relay says of a Claude wake that the session delivers, holds or refuses, against a stand-in for a
/// Claude Code session that answers on the reply pipe exactly as 2.1.330 was observed to (28/11/2026).</summary>
public sealed class ClaudeReceiptsTests : IDisposable
{
    private readonly Isolation iso = new();
    public void Dispose() => iso.Dispose();

    private static JsonObject Tool(string name, JsonObject args, string agent) => McpServer.CallToolPayload(name, args, agent);

    /// <summary>A Claude Code session's inbox: registry entry, peer key, pipe. It answers a wake whose text names a
    /// message listed in <see cref="Verdicts"/> the way Claude Code does: it finds the sender's key by the hash of the
    /// reply pipe, then writes the auth line and one peer_message_status line there.</summary>
    private sealed class FakeClaudeSession : IDisposable
    {
        public string Sessions { get; }
        public ConcurrentDictionary<string, string> Verdicts { get; } = new();
        public ConcurrentQueue<JsonObject> Frames { get; } = new();
        private readonly string pipe = "cc-msg-" + Guid.NewGuid().ToString("N"), token = Guid.NewGuid().ToString("N");
        private readonly CancellationTokenSource stop = new();
        private readonly Task loop;

        public FakeClaudeSession(Isolation iso)
        {
            Sessions = Path.Join(iso.Root, "claude-sessions"); string project = Path.Join(iso.Root, "claude-projet");
            Directory.CreateDirectory(Sessions); Directory.CreateDirectory(project);
            iso.Set("NEXUS_RELAY_CLAUDE_SESSIONS", Sessions); iso.Set("NEXUS_RELAY_CLAUDE_PROJECT", project);
            int pid = Environment.ProcessId;
            File.WriteAllText(Path.Join(Sessions, $"{pid}.json"), new JsonObject { ["pid"] = pid, ["cwd"] = project, ["procStart"] = "1", ["messagingSocketPath"] = @"\\.\pipe\" + pipe, ["hostSessionId"] = "local_test", ["updatedAt"] = 1 }.ToJsonString());
            File.WriteAllText(Path.Join(Sessions, ClaudeReceipts.KeyFileName(pid, pipe)), new JsonObject { ["peerToken"] = token, ["procStartFt"] = "1" }.ToJsonString());
            loop = Task.Run(Serve);
        }

        private async Task Serve()
        {
            try
            {
                while (true)
                {
                    using var server = new NamedPipeServerStream(pipe, PipeDirection.In, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
                    await server.WaitForConnectionAsync(stop.Token);
                    using var reader = new StreamReader(server, Encoding.UTF8);
                    string? auth = await reader.ReadLineAsync(stop.Token), line = await reader.ReadLineAsync(stop.Token);
                    if (JsonNode.Parse(auth ?? "{}")?["token"]?.ToString() != token || line is null) continue;
                    var frame = JsonNode.Parse(line)!.AsObject();
                    string text = frame["message"]!["content"]!.ToString();
                    Frames.Enqueue(frame);
                    foreach (var verdict in Verdicts.Where(v => text.Contains(v.Key, StringComparison.Ordinal))) Answer(frame, verdict.Value);
                }
            }
            catch (OperationCanceledException) { /* The test is over. */ }
        }

        public void Answer(JsonObject frame, string status, string? token = null)
        {
            const string scheme = @"uds:\\.\pipe\";
            string from = frame["from"]!.ToString();
            Assert.StartsWith(scheme, from);
            string name = from[scheme.Length..], suffix = ClaudeReceipts.KeyFileName(0, name)[1..];
            string key = Directory.EnumerateFiles(Sessions, "*" + suffix).Single();
            token ??= JsonNode.Parse(File.ReadAllText(key))!["peerToken"]!.ToString();
            var control = new JsonObject { ["type"] = "control", ["action"] = "peer_message_status", ["status"] = status, ["reason"] = "Claude Code's own words.", ["from"] = @"uds:\\.\pipe\" + pipe, ["orig_msg_id"] = frame["msg_id"]?.DeepClone(), ["msgV"] = 1, ["msg_id"] = Guid.NewGuid().ToString() };
            if (status == "refused") { control["status"] = "expired"; control["status_detail"] = "refused"; } // As Claude Code 2.1.330 writes a refusal.
            using var client = new NamedPipeClientStream(".", name, PipeDirection.Out);
            client.Connect(5000);
            client.Write(new UTF8Encoding(false).GetBytes(new JsonObject { ["type"] = "auth", ["token"] = token }.ToJsonString() + "\n" + control.ToJsonString() + "\n"));
            client.Flush();
        }

        public void Dispose() { stop.Cancel(); try { loop.Wait(5000); } catch (AggregateException) { /* Cancelled. */ } stop.Dispose(); }
    }

    [Fact]
    public void A_wake_claude_holds_or_refuses_is_never_counted_as_delivered()
    {
        iso.Set("NEXUS_RELAY_HOST", "all");
        using var session = new FakeClaudeSession(iso);
        var box = new RelayBox();
        var held = box.Post("codex", "claude", "Demande que Claude retient", "receipts-held");
        var lapsed = box.Post("codex", "claude", "Demande retenue puis expirée", "receipts-lapsed");
        var refused = box.Post("codex", "claude", "Demande que Claude refuse", "receipts-refused");
        var accepted = box.Post("codex", "claude", "Demande que Claude accepte", "receipts-accepted");
        session.Verdicts[held.Id] = "held"; session.Verdicts[lapsed.Id] = "held"; session.Verdicts[refused.Id] = "refused";

        RelayRuntime? runtime = null;
        using var receipts = ClaudeReceipts.Start(session.Sessions, (ids, verdict) => runtime!.ClaudeReceipt(ids, verdict), TimeSpan.FromSeconds(2));
        Assert.NotNull(receipts);
        using var owned = runtime = new RelayRuntime(box, (_, messages, _) => ClaudeInbox.Wake(messages, receipts), () => { });
        foreach (var message in new[] { held, lapsed, refused, accepted }) Assert.True(runtime.Ring(message));
        Thread.Sleep(2100);
        var results = new Dictionary<string, JsonObject>();
        for (int i = 0; i < 4; i++) { var result = runtime.Step("claude")!; results[result["messages"]![0]!.ToString()] = result; }
        Assert.True(session.Frames.Count == 4, string.Join("\n", results.Values.Select(r => r.ToJsonString())));
        Assert.All(session.Frames, frame => Assert.Matches("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", frame["msg_id"]!.ToString()));

        // Held: said so everywhere, counted as one ring, never replayed.
        Assert.Equal("held", results[held.Id]["status"]?.ToString());
        Assert.Equal("Claude Code's own words.", results[held.Id]["detail"]?.ToString());
        Assert.Equal("held", box.RingState(held.Id)?["status"]?.ToString());
        Assert.StartsWith("Retenu par la session relais de Claude", Tool("track", new() { ["id"] = held.Id }, "codex")["state"]!.ToString());
        var waited = Tool("wait_reply", new() { ["id"] = held.Id, ["timeout_s"] = 5 }, "codex");
        Assert.Equal("unavailable", waited["status"]?.ToString());
        Assert.StartsWith("Retenu par la session relais de Claude", waited["reason"]!.ToString());

        // Refused: not a ring at all, retried later like any failed wake.
        Assert.Equal("refused", results[refused.Id]["status"]?.ToString());
        Assert.Null(box.RingState(refused.Id));
        Assert.StartsWith("Refusé par la session relais de Claude (refused)", Tool("track", new() { ["id"] = refused.Id }, "codex")["state"]!.ToString());
        Assert.StartsWith("Refusé par la session relais de Claude", Tool("wait_reply", new() { ["id"] = refused.Id, ["timeout_s"] = 5 }, "codex")["reason"]!.ToString());

        // No answer from the session: written, pickup unconfirmed, as before.
        Assert.Equal("started", results[accepted.Id]["status"]?.ToString());
        Assert.False(results[accepted.Id]["confirmed"]!.GetValue<bool>());
        Assert.StartsWith("réveillé, prise en charge non confirmée", Tool("track", new() { ["id"] = accepted.Id }, "codex")["state"]!.ToString());
        var lastWake = Tool("status", new(), "codex")["agents"]!["claude"]!["last_wake"]!;
        Assert.Equal(results[lastWake["messages"]![0]!.ToString()]["status"]?.ToString(), lastWake["status"]?.ToString());

        // Even past its recheck time, a held wake is not written again, and a refusal waits for its back-off.
        foreach (var message in new[] { held, lapsed }) { var ring = box.RingState(message.Id)!; ring["recheck_at"] = 0; box.SetRingState(message.Id, ring); }
        Assert.Null(runtime.Step("claude"));
        Assert.Equal(4, session.Frames.Count);

        // The user lets the held message through: delivered, on the session's word.
        session.Answer(session.Frames.Single(f => f["message"]!["content"]!.ToString().Contains(held.Id, StringComparison.Ordinal)), "delivered");
        Wait.Until(() => box.RingState(held.Id)?["status"]?.ToString() == "started", what: "the delivered receipt");
        Assert.Equal("claude receipt", box.RingState(held.Id)?["proof"]?.ToString());
        Assert.StartsWith("réveillé, prise en charge non confirmée", Tool("track", new() { ["id"] = held.Id }, "codex")["state"]!.ToString());

        // A held message that expires undelivered is an incident, never a new wake.
        session.Answer(session.Frames.Single(f => f["message"]!["content"]!.ToString().Contains(lapsed.Id, StringComparison.Ordinal)), "expired");
        Wait.Until(() => box.RingState(lapsed.Id)?["incident"] is not null, what: "the expired receipt");
        Assert.StartsWith("incident : Refusé par la session relais de Claude (expired)", Tool("track", new() { ["id"] = lapsed.Id }, "codex")["state"]!.ToString());
        Assert.Null(runtime.Step("claude"));
        Assert.Equal(4, session.Frames.Count);
    }

    [Fact]
    public void Receipts_need_this_pipe_s_token_and_a_wake_that_was_sent()
    {
        using var session = new FakeClaudeSession(iso);
        var late = new ConcurrentQueue<(string Id, string Status)>();
        using var receipts = ClaudeReceipts.Start(session.Sessions, (ids, verdict) => late.Enqueue((ids[0], verdict.Status)), TimeSpan.FromSeconds(2))!;
        string wake = receipts.Expect(["m1"]);
        var frame = new JsonObject { ["from"] = receipts.Address, ["msg_id"] = wake };

        session.Answer(frame, "held", token: new string('0', 32));
        Assert.Null(receipts.Wait(wake)); // A wrong token is not a receipt: the wake stays "written".
        session.Answer(new JsonObject { ["from"] = receipts.Address, ["msg_id"] = Guid.NewGuid().ToString() }, "held");
        session.Answer(frame, "held");
        Wait.Until(() => !late.IsEmpty, what: "the late held receipt");
        Assert.Equal([("m1", "held")], late.ToArray());

        // A receipt within the grace period is the wake's own result.
        string second = receipts.Expect(["m2"]);
        var answering = Task.Run(() => session.Answer(new JsonObject { ["from"] = receipts.Address, ["msg_id"] = second }, "refused"), TestContext.Current.CancellationToken);
        var verdict = receipts.Wait(second);
        Assert.True(answering.Wait(5000, TestContext.Current.CancellationToken));
        Assert.Equal(new ClaudeReceipts.Verdict("refused", "refused", "Claude Code's own words."), verdict);
    }

    [Fact]
    public void Status_lines_read_as_held_refused_or_delivered()
    {
        static ClaudeReceipts.Verdict? Read(string json) => ClaudeReceipts.Parse(JsonNode.Parse(json)!.AsObject());
        static ClaudeReceipts.Verdict? Status(string fields) => Read("""{"type":"control","action":"peer_message_status",""" + fields + "}");
        Assert.Equal("held", Status(""" "status":"held" """)?.Status);
        Assert.Equal("delivered", Status(""" "status":"delivered" """)?.Status);
        var refusal = Status(""" "status":"expired","status_detail":"refused" """)!; // How Claude Code 2.1.330 writes a refusal.
        Assert.Equal(("refused", "refused"), (refusal.Status, refusal.Detail));
        Assert.Equal(("refused", "expired"), (Status(""" "status":"expired" """)!.Status, Status(""" "status":"expired" """)!.Detail));
        Assert.Equal("denied", Status(""" "status":"denied" """)?.Detail);
        Assert.Equal("queue-full", Status(""" "status":"dropped","drop_reason":"queue-full" """)?.Detail);
        Assert.Null(Status(""" "status":"someday" """));
        Assert.Null(Read("""{"type":"user","message":{"role":"user","content":"hello"}}"""));
        Assert.Null(Read("""{"type":"control","action":"rename","status":"held"}"""));
    }

    [Fact]
    public void The_reply_pipe_key_follows_claude_code_s_naming_and_leaves_with_the_engine()
    {
        // A session and its key file as Claude Code 2.1.330 wrote them (isolated test session, 28/11/2026).
        Assert.Equal("36464.bcbb50cd1cd4abc1c58a9979dec370b03df50e0b7d0e0cd644db6251a19afcea.key", ClaudeReceipts.KeyFileName(36464, @"LOCAL\cc-msg-644f1b58f131ebc5329581109cc0d1bc"));

        string sessions = Path.Join(iso.Root, "sessions"); Directory.CreateDirectory(sessions);
        string stale = Path.Join(sessions, $"99999999.{new string('a', 64)}.key"), foreign = Path.Join(sessions, $"99999998.{new string('b', 64)}.key");
        File.WriteAllText(stale, """{"peerToken":"00000000000000000000000000000000","owner":"nexus-relay"}""");
        File.WriteAllText(foreign, """{"peerToken":"00000000000000000000000000000000"}""");
        string own;
        using (var receipts = ClaudeReceipts.Start(sessions)!)
        {
            Assert.False(File.Exists(stale), "The key of a dead engine is swept");
            Assert.True(File.Exists(foreign), "A Claude session's key is never touched");
            Assert.Matches(@"^uds:\\\\\.\\pipe\\LOCAL\\cc-msg-[0-9a-f]{32}$", receipts.Address);
            own = Directory.EnumerateFiles(sessions, $"{Environment.ProcessId}.*.key").Single();
            Assert.Equal(ClaudeReceipts.KeyFileName(Environment.ProcessId, receipts.Address[@"uds:\\.\pipe\".Length..]), Path.GetFileName(own));
            Assert.Matches("^[0-9a-f]{32}$", JsonNode.Parse(File.ReadAllText(own))!["peerToken"]!.ToString());
        }
        Assert.False(File.Exists(own));
        Assert.Null(ClaudeReceipts.Start(Path.Join(iso.Root, "no-such-folder")));

        // Without a reply pipe the wake is the two lines of before 1.17.0.
        Assert.Null(JsonNode.Parse(ClaudeInbox.Frames("t", "x").Split('\n')[1])!["from"]);
        var framed = JsonNode.Parse(ClaudeInbox.Frames("t", "x", @"uds:\\.\pipe\LOCAL\cc-msg-" + new string('c', 32), "id").Split('\n')[1])!;
        Assert.Equal("id", framed["msg_id"]?.ToString());
        Assert.Equal("<cross-session-message from-name=\"NexusRelay\">\nx\n</cross-session-message>", framed["message"]!["content"]!.ToString());
    }
}
