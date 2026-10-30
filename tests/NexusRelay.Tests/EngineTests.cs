using System.Net;
using System.Text;
using System.Text.Json.Nodes;

namespace NexusRelay.Tests;

/// <summary>Former --selftest and --runtime-selftest of the engine, in process with an isolated mailbox.</summary>
public sealed class EngineTests : IDisposable
{
    private readonly Isolation iso = new();
    public void Dispose() => iso.Dispose();

    private static JsonObject Tool(string name, JsonObject args, string agent) => McpServer.CallToolPayload(name, args, agent);

    [Fact]
    public void Room_cycle_send_inbox_progress_read_reply_track_search_summary_close()
    {
        iso.Set("NEXUS_RELAY_HOST", "claude");
        var sent = Tool("send", new() { ["to"] = "codex", ["text"] = "Vérifie l'architecture C# de NexusRelay avec accents éèà", ["topic"] = "nexusrelay" }, "claude");
        Assert.Equal("success", sent["status"]?.ToString());
        string id = sent["id"]!.ToString();
        Assert.Equal("error", Tool("send", new() { ["to"] = "claude", ["text"] = "Tentative non-hote", ["topic"] = "nexusrelay" }, "codex")["status"]?.ToString());
        iso.Set("NEXUS_RELAY_HOST", "all");
        Assert.Equal("success", Tool("send", new() { ["to"] = "claude", ["text"] = "Message de pair", ["topic"] = "peer-test" }, "codex")["status"]?.ToString());
        iso.Set("NEXUS_RELAY_HOST", null);
        var unread = Tool("inbox", new(), "codex")["unread"] as JsonArray;
        Assert.Single(unread!);
        Assert.Contains("⟦relay:", unread![0]!["text"]!.ToString());
        Assert.Equal("en cours", Tool("progress", new() { ["id"] = id, ["note"] = "Analyse du code en cours" }, "codex")["state"]?.ToString());
        Assert.Contains("en cours", Tool("track", new() { ["id"] = id }, "claude")["state"]!.ToString());
        Assert.Equal(id, Tool("read", new() { ["id"] = id }, "codex")["message"]?["id"]?.ToString());
        var reply = Tool("reply", new() { ["id"] = id, ["text"] = "Tout est conforme dans RelayBox.cs:42 (gravité basse).", ["vote"] = "agree", ["state"] = "done" }, "codex");
        Assert.Equal("success", reply["status"]?.ToString());
        Assert.False(string.IsNullOrEmpty(reply["id"]?.ToString()));
        Assert.Equal("répondu", Tool("track", new() { ["id"] = id }, "claude")["state"]?.ToString());
        Assert.True(Tool("search", new() { ["query"] = "architecture eea", ["topic"] = "nexusrelay" }, "claude")["total"]!.GetValue<int>() >= 1);
        var summary = Tool("summary", new() { ["topic"] = "nexusrelay" }, "claude");
        Assert.Equal(1, summary["answered_total"]!.GetValue<int>());
        Assert.Equal(0, summary["open_total"]!.GetValue<int>());
        Assert.True(Tool("close", new() { ["topic"] = "nexusrelay" }, "claude")["closed"]!.GetValue<bool>());
        Assert.True(File.Exists(Path.Combine(new RelayBox().Root, "salon", "nexusrelay.md")));
    }

    [Fact]
    public async Task Concurrent_claims_have_exactly_one_winner()
    {
        var box = new RelayBox();
        var message = box.Post("claude", "antigravity", "Test concurrence", "concurrence");
        using var barrier = new Barrier(8);
        int winners = 0;
        await Task.WhenAll(Enumerable.Range(0, 8).Select(i => Task.Run(() => { barrier.SignalAndWait(); if (box.Claim(message.Id, 1, $"worker-{i}")) Interlocked.Increment(ref winners); })));
        Assert.Equal(1, winners);
        Assert.Equal("claimed", box.ClaimOf(message.Id, 1)?["phase"]?.ToString());
    }

    [Fact]
    public void Cancel_only_an_unread_request()
    {
        string first = Tool("send", new() { ["to"] = "codex", ["text"] = "Demande à annuler avant lecture", ["topic"] = "annulation" }, "claude")["id"]!.ToString();
        var cancelled = Tool("cancel", new() { ["id"] = first }, "claude");
        Assert.Equal("success", cancelled["status"]?.ToString());
        Assert.Equal("annulé", cancelled["state"]?.ToString());
        Assert.Equal("error", Tool("reply", new() { ["id"] = first, ["text"] = "Réponse tardive" }, "codex")["status"]?.ToString());
        string second = Tool("send", new() { ["to"] = "codex", ["text"] = "Demande lue avant annulation", ["topic"] = "annulation" }, "claude")["id"]!.ToString();
        Tool("read", new() { ["id"] = second }, "codex");
        Assert.Equal("error", Tool("cancel", new() { ["id"] = second }, "claude")["status"]?.ToString());
    }

    [Fact]
    public void Crash_recovery_dead_pids_host_takeover_and_vaults()
    {
        var box = new RelayBox();
        const int dead = 99_999_999;
        Assert.False(RelayBox.PidAlive(dead));
        Assert.True(RelayBox.PidAlive(Environment.ProcessId));
        var message = box.Post("claude", "antigravity", "Test crash recovery", "recovery");
        Assert.True(box.Claim(message.Id, 1, "crashed-sidecar", pidOverride: dead));
        Assert.False(box.Claim(message.Id, 1, "live-sidecar"));
        Assert.True(box.RecoverDeadClaim(message.Id, 1));
        Assert.True(box.Claim(message.Id, 1, "live-sidecar"));
        box.SetClaim(message.Id, 1, "live-sidecar", "submitting", new JsonObject { ["pid"] = dead });
        Assert.Equal("uncertain", RelayBox.ClaimLabel(box.ClaimOf(message.Id, 1)!));

        string target = Path.Combine(iso.Root, "shared.cs");
        Reservations.Reserve(box, "codex", [target], "édition avant crash", ttlMin: 60, ownerPid: dead);
        var reclaimed = Reservations.Reserve(box, "claude", [target], "reprise après crash", ttlMin: 60);
        Assert.Equal("claude", reclaimed["agent"]?.ToString());
        Reservations.Release(box, "claude", reclaimed["id"]!.ToString());
        string left = Reservations.Reserve(box, "codex", [target], "travail laissé", ttlMin: 60)["id"]!.ToString();
        Assert.Throws<RelayError>(() => Reservations.Release(box, "antigravity", left, force: true));
        Assert.Throws<RelayError>(() => Reservations.Release(box, "claude", force: true));
        Assert.Equal([left], Reservations.Release(box, "claude", left, force: true));
        Assert.DoesNotContain(Reservations.Active(box), r => r["id"]?.ToString() == left);

        string vault = Path.Combine(iso.Root, "vault");
        Directory.CreateDirectory(vault);
        string secret = Path.Combine(vault, "secret.txt");
        File.WriteAllText(secret, "top-secret");
        iso.Set("NEXUS_RELAY_VAULTS", vault);
        Assert.Throws<RelayError>(() => box.Post("claude", "codex", "Test coffre", "recovery", files: [secret]));
    }

    [Fact]
    public async Task Http_server_in_process_discover_list_call_reserve_release()
    {
        const string token = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"; // gitleaks:allow (fake test token)
        iso.Set("NEXUS_MCP_TOKEN", token);
        int port = Native.FreePort();
        using var cts = new CancellationTokenSource();
        var server = McpServer.RunHttpAsync(port, cts.Token, new RelayRuntime(waker: (_, _, _) => AppTransport.Result("deferred", "isolated test adapter"), tidy: () => { }));
        try
        {
            using var http = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}") };
            HttpResponseMessage? health = null;
            for (int i = 0; i < 60 && health?.IsSuccessStatusCode != true; i++)
            {
                try { health = await http.GetAsync("/health"); } catch (HttpRequestException) { await Task.Delay(50); }
            }
            Assert.True(health?.IsSuccessStatusCode);
            var unauthorized = await http.PostAsync("/mcp?agent=claude", new StringContent("""{"jsonrpc":"2.0","id":1,"method":"ping"}""", Encoding.UTF8, "application/json"));
            Assert.Equal(HttpStatusCode.Unauthorized, unauthorized.StatusCode);
            http.DefaultRequestHeaders.Authorization = new("Bearer", token);
            async Task<JsonObject> Rpc(string method, JsonObject? parameters = null)
            {
                var request = new JsonObject { ["jsonrpc"] = "2.0", ["id"] = 1, ["method"] = method, ["params"] = parameters ?? new JsonObject() };
                var response = await http.PostAsync("/mcp?agent=claude", new StringContent(request.ToJsonString(), Encoding.UTF8, "application/json"));
                Assert.True(response.IsSuccessStatusCode);
                return JsonNode.Parse(await response.Content.ReadAsStringAsync())!["result"]!.AsObject();
            }
            var discover = await Rpc("server/discover");
            Assert.Equal("2026-07-28", discover["protocolVersion"]?.ToString());
            Assert.Equal(20, (discover["tools"] as JsonArray)!.Count);
            Assert.Equal(20, ((await Rpc("tools/list"))["tools"] as JsonArray)!.Count);
            Assert.Equal("claude", (await Rpc("tools/call", new() { ["name"] = "status", ["arguments"] = new JsonObject() }))["structuredContent"]?["me"]?.ToString());
            var reserved = JsonNode.Parse((await Rpc("tools/call", new() { ["name"] = "reserve", ["arguments"] = new JsonObject { ["paths"] = new JsonArray(Path.Combine(iso.Root, "http_reserved.py")), ["reason"] = "test HTTP reserve" } }))["content"]![0]!["text"]!.ToString())!;
            Assert.Equal("success", reserved["status"]?.ToString());
            Assert.Single(((await Rpc("tools/call", new() { ["name"] = "reservations", ["arguments"] = new JsonObject() }))["structuredContent"]?["active"] as JsonArray)!);
            var released = JsonNode.Parse((await Rpc("tools/call", new() { ["name"] = "release", ["arguments"] = new JsonObject { ["id"] = reserved["id"]!.ToString() } }))["content"]![0]!["text"]!.ToString())!;
            Assert.Single((released["released"] as JsonArray)!);
        }
        finally
        {
            cts.Cancel();
            try { await server; } catch (OperationCanceledException) { }
        }
    }

    [Fact]
    public void Workers_recover_confirm_retry_and_never_replay_uncertain_deliveries()
    {
        iso.Set("NEXUS_RELAY_HOST", "claude");
        var box = new RelayBox(); int calls = 0;
        using var runtime = new RelayRuntime(box, (_, messages, submit) => { calls++; submit(); return new() { ["status"] = "started", ["messages"] = new JsonArray(messages.Select(m => JsonValue.Create(m.Id)).ToArray()), ["via"] = "test adapter" }; }, () => { });
        Assert.True(AppSessions.IsRelay(new("id", "", @"\\?\" + AppSessions.RelayDirectory, false, false)), "Windows extended path prefixes must preserve Relay ownership");
        Assert.False(AppSessions.IsRelay(new("id", "", AppSessions.RelayDirectory + "-other", false, false)), "Neighbor directories must remain outside Relay ownership");
        Assert.Equal(3, (runtime.Health()["missing"] as JsonArray)?.Count);
        var info = box.Post("claude", "codex", "Information", "runtime-test", info: true);
        Assert.False(runtime.Ring(info), "FYI never wakes an agent");
        var request = box.Post("claude", "codex", "Read and answer", "runtime-test");
        Assert.Equal(1, runtime.Recover());
        Assert.True(runtime.Step("codex")?["status"]?.ToString() == "started" && calls == 1);
        Assert.Equal(1, box.RingState(request.Id)?["attempt"]?.GetValue<int>());
        Assert.True(runtime.Step("codex") is null && calls == 1, "Wake is not repeated before confirmation deadline");
        var ring = box.RingState(request.Id)!; ring["recheck_at"] = Reservations.Now() - 1; box.SetRingState(request.Id, ring);
        Assert.True(runtime.Step("codex") is not null && calls == 2, "Unread confirmed wake gets a bounded retry");
        box.MarkRead(request);
        Assert.True(runtime.Step("codex") is null && calls == 2, "Read messages are never reawoken");
        var awaited = box.Post("codex", "antigravity", "Awaited answer", "runtime-test", replyTo: request.Id);
        box.SetState("waited", new() { [request.Id] = Reservations.Now() + 30 });
        Assert.False(runtime.Ring(awaited), "A reply reserved by wait_reply must not wake another conversation");
        Assert.False(AppTransport.Truth(RelayRuntime.SidecarHealth(box)["alive"]), "No heartbeat cannot mean live sidecar");
        var uncertain = box.Post("claude", "codex", "Uncertain send", "runtime-test");
        box.SetRingState(uncertain.Id, new() { ["attempt"] = 1, ["status"] = "uncertain", ["recheck_at"] = Reservations.Now() - 1 });
        runtime.Recover();
        Assert.True(runtime.Step("codex") is null && calls == 2, "An uncertain delivery must never be replayed");
        var eventOnly = box.Post("claude", "codex", "Lost ring after uncertain event", "event-recovery");
        box.Event(eventOnly.Id, "réveil", new() { ["status"] = "uncertain" });
        runtime.Recover();
        var recovered = box.RingState(eventOnly.Id)!; recovered["recheck_at"] = Reservations.Now() - 1; box.SetRingState(eventOnly.Id, recovered);
        Assert.True(runtime.Step("codex") is null && calls == 2, "Recovering an uncertain event must retain its status after its deadline");
        var interrupted = box.Post("claude", "codex", "Crash after Enter, before acknowledgement", "crash-submit");
        using (var crashing = new RelayRuntime(box, (_, batch, submit) =>
        {
            Assert.True(batch.All(m => box.Claims(m.Id).Any(c => c.Claim["phase"]?.ToString() == "claimed")), "Durable claims must precede preparation");
            submit();
            Assert.True(batch.All(m => box.Claims(m.Id).Any(c => c.Claim["phase"]?.ToString() == "submitting")), "Submission intent must be durable before any key event");
            throw new IOException("fixture: process interrupted after Enter");
        }, () => { }))
        {
            crashing.Recover();
            Assert.Throws<IOException>(() => crashing.Step("codex"));
        }
        using (var restarted = new RelayRuntime(box, (_, _, _) => { calls++; return AppTransport.Result("started"); }, () => { }))
        {
            restarted.Recover();
            Assert.Equal("uncertain", box.RingState(interrupted.Id)?["status"]?.ToString());
            Assert.True(restarted.Step("codex") is null && calls == 2, "A restart after Enter must never double-deliver");
        }
        var dead = box.Post("claude", "codex", "Terminated process without finally", "dead-submit");
        Assert.True(box.Claim(dead.Id, 1, "native-codex", pidOverride: -1));
        box.SetClaim(dead.Id, 1, "native-codex", "submitting");
        runtime.Recover();
        Assert.True(runtime.Step("codex") is null && box.RingState(dead.Id)?["status"]?.ToString() == "uncertain", "Hard termination during submission must remain uncertain");
        var held = box.Post("claude", "codex", "Backoff", "backoff-test");
        using var failed = new RelayRuntime(box, (_, _, _) => { calls++; return AppTransport.Result("failed", "known rejection"); }, () => { });
        failed.Recover(); failed.Step("codex"); int after = calls;
        failed.Step("codex");
        Assert.True(calls == after && box.State("wake-codex")?["holds"]?[held.Topic]?["status"]?.ToString() == "failed", "Failed wakes use per-topic persisted backoff");
    }

    [Fact]
    public void Tidy_project_matching_ownership_folding_restart_and_app_guards()
    {
        var box = new RelayBox();
        var unfinished = box.Post("claude", "antigravity", "Await reply before archiving", "tidy-proof");
        box.SetTopicState(unfinished.Topic, "closed", new() { ["closed"] = true });
        double now = Reservations.Now();
        Assert.False(AppAutomation.AntigravityTidyReady(box, unfinished.Topic, false, new(true, now - 20), now));
        box.MarkRead(unfinished); box.Post("antigravity", "claude", "Reply", unfinished.Topic, replyTo: unfinished.Id);
        Assert.True(AppAutomation.AntigravityTidyReady(box, unfinished.Topic, false, new(true, now - 20), now));
        Assert.False(AppAutomation.AntigravityTidyReady(box, unfinished.Topic, true, new(false, now - 20), now));
        Assert.True(AppAutomation.AntigravityTidyReady(box, unfinished.Topic, true, new(false, now - 7201), now));
        string temp = iso.Root;
        var configured = new JsonObject { ["project_id"] = "official-id", ["path"] = Path.Combine(temp, "Documents"), ["label"] = "Documents" };
        var evidence = new JsonObject { ["count"] = 1, ["id"] = "official-id", ["path"] = Path.Combine(temp, "Documents"), ["label"] = "Documents" };
        Assert.True(AppAutomation.ProjectMatches(evidence, configured));
        evidence["id"] = "other-id"; Assert.False(AppAutomation.ProjectMatches(evidence, configured));
        evidence["id"] = "official-id"; evidence["path"] = temp; Assert.False(AppAutomation.ProjectMatches(evidence, configured));
        evidence["path"] = configured["path"]!.ToString(); evidence["count"] = 2; Assert.False(AppAutomation.ProjectMatches(evidence, configured));
        box.SetState("codex-owned", new() { ["threads"] = new JsonObject { ["relay"] = new JsonObject { ["verified"] = true, ["workspace"] = temp }, ["child"] = new JsonObject { ["verified"] = true, ["workspace"] = temp }, ["parent"] = new JsonObject { ["verified"] = true, ["workspace"] = temp } } });
        Assert.True(CodexSpace.Owns(new("relay", "", temp, false, false), box));
        Assert.False(CodexSpace.Owns(new("julien", "", temp, false, false), box));
        Assert.False(CodexSpace.Owns(new("child", "", temp, false, true), box));
        iso.Set("CODEX_SESSION_ID", "parent");
        Assert.False(CodexSpace.Owns(new("parent", "", temp, false, false), box));
        var (folded, offsets) = SearchAndSummary.Fold("🎯Été𐐀");
        Assert.True(folded == "🎯ete𐐨" && offsets.Count == folded.Length && offsets[2] == 2);
        string requestPath = Path.Combine(temp, "restart.request");
        File.WriteAllText(requestPath, "code sha256:" + RelayPaths.CodeFingerprint());
        Assert.False(RelayPaths.RestartWanted(requestPath, RelayPaths.CodeFingerprint()));
        File.WriteAllText(requestPath, "code sha256:" + new string('a', 64));
        Assert.True(RelayPaths.RestartWanted(requestPath, RelayPaths.CodeFingerprint()));
        foreach (string phase in new[] { "submitting", "uncertain" })
        {
            Assert.NotNull(AppLifecycle.PendingLaunch(new JsonObject { ["status"] = phase }, false));
            Assert.Null(AppLifecycle.PendingLaunch(new JsonObject { ["status"] = phase }, true));
        }
        Assert.NotNull(AppLifecycle.RestartGuard("codex", true, false, true, true));
        Assert.NotNull(AppLifecycle.RestartGuard("antigravity", false, false, true, true));
        Assert.NotNull(AppLifecycle.RestartGuard("antigravity", false, true, false, true));
        Assert.False(AppLifecycle.InstalledExecutable("codex", Path.Combine(temp, "ChatGPT.exe")));
        Assert.False(AppLifecycle.InstalledExecutable("antigravity", Path.Combine(temp, "Antigravity.exe")));
    }
}
