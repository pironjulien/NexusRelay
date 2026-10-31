using System.Diagnostics;
using System.Net;
using System.Text.Json.Nodes;

namespace NexusRelay.Tests;

/// <summary>Black-box MCP protocol of the native engine (stdio and loopback HTTP), no AI touched.</summary>
public sealed class ProtocolTests : IDisposable
{
    private readonly Isolation iso = new();
    public ProtocolTests() => iso.Set("NEXUS_RELAY_HOST", "claude");
    public void Dispose() => iso.Dispose();

    public static readonly string[] ExpectedTools =
        ["send", "close", "inbox", "read", "reply", "progress", "cancel", "wait_reply", "track", "reserve", "release", "reservations",
         "file_reservation_paths", "release_file_reservations", "search", "summary", "status", "selftest", "where", "survey"];

    [Fact]
    public void Stdio_discover_tools_and_full_room_cycle()
    {
        var first = Native.Stdio("claude", Native.Rpc("server/discover"), Native.Rpc("tools/list"));
        var discover = first[0]["result"]!;
        Assert.Equal("2026-07-28", discover["protocolVersion"]?.ToString());
        Assert.Equal("nexus-relay", discover["serverInfo"]?["name"]?.ToString());
        Assert.Equal(RelayPaths.Version, discover["serverInfo"]?["version"]?.ToString());
        Assert.Equal(ExpectedTools.Order(), (discover["tools"] as JsonArray)!.Select(t => t!["name"]!.ToString()).Order());
        Assert.Equal(20, (first[1]["result"]!["tools"] as JsonArray)!.Count);

        var sent = Native.Data(Native.Stdio("claude", Native.Call("send", new() { ["to"] = "codex", ["text"] = "Vérifie la migration C# avec caractères accentués éèê", ["topic"] = "nexusrelay" }))[0]);
        Assert.Equal("success", sent["status"]?.ToString());
        string id = sent["id"]!.ToString();
        Assert.Equal("error", Native.Data(Native.Stdio("codex", Native.Call("send", new() { ["to"] = "claude", ["text"] = "Interdit", ["topic"] = "nexusrelay" }))[0])["status"]?.ToString());

        var codex = Native.Stdio("codex",
            Native.Call("inbox"),
            Native.Call("progress", new() { ["id"] = id, ["note"] = "Vérification en cours" }),
            Native.Call("read", new() { ["id"] = id }),
            Native.Call("reply", new() { ["id"] = id, ["text"] = "Parité confirmée dans Program.cs:10 (gravité basse).", ["vote"] = "agree", ["state"] = "done" }));
        var unread = Native.Data(codex[0])["unread"] as JsonArray;
        Assert.Single(unread!);
        Assert.Contains("⟦relay:", unread![0]!["text"]!.ToString());
        Assert.Equal("en cours", Native.Data(codex[1])["state"]?.ToString());
        Assert.Equal(id, Native.Data(codex[2])["message"]?["id"]?.ToString());
        Assert.Equal("success", Native.Data(codex[3])["status"]?.ToString());

        var follow = Native.Stdio("claude",
            Native.Call("track", new() { ["id"] = id }),
            Native.Call("search", new() { ["query"] = "accentues eee", ["topic"] = "nexusrelay" }),
            Native.Call("summary", new() { ["topic"] = "nexusrelay" }),
            Native.Call("close", new() { ["topic"] = "nexusrelay" }));
        Assert.Equal("répondu", Native.Data(follow[0])["state"]?.ToString());
        Assert.Equal(1, Native.Data(follow[1])["total"]!.GetValue<int>());
        Assert.Equal(1, Native.Data(follow[2])["answered_total"]!.GetValue<int>());
        Assert.Equal(0, Native.Data(follow[2])["open_total"]!.GetValue<int>());
        Assert.True(Native.Data(follow[3])["closed"]!.GetValue<bool>());
        Assert.True(File.Exists(Path.Join(iso.Relay, "v1", "salon", "nexusrelay.md")));
    }

    [Fact]
    public void Cancel_reservations_and_edit_guard_for_codex_and_antigravity()
    {
        var sent = Native.Stdio("claude",
            Native.Call("send", new() { ["to"] = "codex", ["text"] = "A annuler", ["topic"] = "canceltest" }),
            Native.Call("send", new() { ["to"] = "codex", ["text"] = "Deja lu", ["topic"] = "canceltest" }));
        string unread = Native.Data(sent[0])["id"]!.ToString(), read = Native.Data(sent[1])["id"]!.ToString();
        Native.Stdio("codex", Native.Call("read", new() { ["id"] = read }));
        var cancels = Native.Stdio("claude", Native.Call("cancel", new() { ["id"] = unread }), Native.Call("cancel", new() { ["id"] = read }));
        Assert.Equal("annulé", Native.Data(cancels[0])["state"]?.ToString());
        Assert.Equal("error", Native.Data(cancels[1])["status"]?.ToString());

        string protectedFile = Path.Join(iso.Root, "protected.py");
        Native.Stdio("claude", Native.Call("reserve", new() { ["paths"] = new JsonArray(protectedFile), ["reason"] = "refactoring critique" }));
        var codex = Native.Run(["--edit-guard", "codex"], new JsonObject { ["session_id"] = "sess-codex-1", ["tool_name"] = "apply_patch", ["tool_input"] = new JsonObject { ["command"] = $"*** Update File: {protectedFile}\n@@\n" } }.ToJsonString());
        Assert.Equal("deny", JsonNode.Parse(codex.Out)!["hookSpecificOutput"]?["permissionDecision"]?.ToString());
        var agy = Native.Run(["--edit-guard", "antigravity"], new JsonObject { ["conversationId"] = "conv-agy-1", ["stepIdx"] = 3, ["toolCall"] = new JsonObject { ["args"] = new JsonObject { ["Path"] = protectedFile } } }.ToJsonString());
        Assert.Equal("deny", JsonNode.Parse(agy.Out)!["decision"]?.ToString());
    }

    [Fact]
    public void Http_loopback_token_discover_status_and_exchange()
    {
        using var daemon = new Daemon(fixture: false, env: new Dictionary<string, string?> { ["NEXUS_RELAY_WAKE_SINCE"] = null });
        using (var unauthorized = daemon.Post("claude", new JsonObject { ["jsonrpc"] = "2.0", ["id"] = 1, ["method"] = "ping" }, token: null))
            Assert.Equal(HttpStatusCode.Unauthorized, unauthorized.StatusCode);
        using (var http = new HttpClient())
        {
            http.DefaultRequestHeaders.Authorization = new("Bearer", Daemon.Token);
            foreach (string route in new[] { "/api/update", "/api/status" })  // the update state, read by NexusSystem
            {
                var state = JsonNode.Parse(http.GetStringAsync($"http://127.0.0.1:{daemon.Port}{route}", TestContext.Current.CancellationToken).GetAwaiter().GetResult())!;
                Assert.Equal("relay", state["product"]?.ToString());
                Assert.Equal(RelayPaths.Version, state["version"]?.ToString());
                Assert.Equal("native", state["runtime"]?.ToString());
            }
        }
        var discover = daemon.Rpc(Native.Rpc("server/discover"))!["result"]!;
        Assert.Equal("2026-07-28", discover["protocolVersion"]?.ToString());
        Assert.Equal(20, (discover["tools"] as JsonArray)!.Count);
        var status = daemon.Rpc(Native.Call("status"))!["result"]!["structuredContent"]!;
        Assert.Equal("claude", status["me"]?.ToString());
        Assert.Equal("claude", status["host"]?.ToString());
        string id = Native.Data(daemon.Rpc(Native.Call("send", new() { ["to"] = "antigravity", ["text"] = "Ping HTTP", ["topic"] = "httptest" }))!.AsObject())["id"]!.ToString();
        Assert.Single((Native.Data(daemon.Rpc(Native.Call("inbox"), "antigravity")!.AsObject())["unread"] as JsonArray)!);
        Assert.Equal(id, Native.Data(daemon.Rpc(Native.Call("read", new() { ["id"] = id }), "antigravity")!.AsObject())["message"]?["id"]?.ToString());
        Assert.Equal("success", Native.Data(daemon.Rpc(Native.Call("reply", new() { ["id"] = id, ["text"] = "Pong HTTP", ["info"] = true }), "antigravity")!.AsObject())["status"]?.ToString());
    }

    [Fact]
    public void Http_health_reports_runtime_identity_and_graceful_restart()
    {
        using var daemon = new Daemon();
        var health = JsonNode.Parse(daemon.Health().Content.ReadAsStringAsync().GetAwaiter().GetResult())!;
        Assert.Equal("ok", health["status"]?.ToString());
        Assert.Equal("nexus-relay", health["service"]?.ToString());
        Assert.Equal("native", health["runtime"]?.ToString());
        Assert.Equal(daemon.Process.Id, health["pid"]!.GetValue<int>());
        Assert.Null(health["activities"]);
        File.WriteAllText(Path.Join(iso.Relay, "daemon-restart.request"), "force test");
        Assert.True(daemon.Process.WaitForExit(10_000));
        Assert.Equal(0, daemon.Process.ExitCode);
    }

    private static JsonObject WaitCall(JsonArray ids, int requestId = 89, int timeout = 1) => new()
    {
        ["jsonrpc"] = "2.0", ["id"] = requestId, ["method"] = "tools/call",
        ["params"] = new JsonObject { ["name"] = "wait_reply", ["arguments"] = new JsonObject { ["id"] = ids, ["timeout_s"] = timeout }, ["_meta"] = new JsonObject { ["progressToken"] = "parity-progress" } },
    };
    private static JsonObject Cancelled(int requestId) => new() { ["jsonrpc"] = "2.0", ["method"] = "notifications/cancelled", ["params"] = new JsonObject { ["requestId"] = requestId, ["reason"] = "fixture interrupted" } };
    private static JsonObject Payload(JsonNode? response) => JsonNode.Parse(response!["result"]!["content"]![0]!["text"]!.ToString())!.AsObject();

    [Fact]
    public void Group_wait_has_one_deadline_and_real_sse_progress()
    {
        using var daemon = new Daemon();
        var box = iso.Box();
        var first = box.Post("claude", "codex", "fixture", "parity-a");
        var second = box.Post("claude", "antigravity", "fixture", "parity-b");
        var clock = Stopwatch.StartNew();
        var events = (JsonArray)daemon.Rpc(WaitCall([first.Id, second.Id]), accept: "text/event-stream")!;
        Assert.True(clock.Elapsed.TotalSeconds < 1.8, clock.Elapsed.ToString());
        var progress = events.Where(e => e?["method"]?.ToString() == "notifications/progress").ToList();
        Assert.NotEmpty(progress);
        Assert.Equal("parity-progress", progress[0]!["params"]!["progressToken"]?.ToString());
        var result = Payload(events[^1]);
        Assert.Equal("timeout", result["status"]?.ToString());
        Assert.Equal(2, result["total"]!.GetValue<int>());
        Assert.Equal(["timeout", "timeout"], (result["replies"] as JsonArray)!.Select(r => r!["status"]!.ToString()));
    }

    [Fact]
    public void Group_cancellation_does_not_consume_the_held_first_reply()
    {
        using var daemon = new Daemon();
        var box = iso.Box();
        var first = box.Post("claude", "codex", "fixture", "parity-a");
        var second = box.Post("claude", "antigravity", "fixture", "parity-b");
        var answer = box.Post("codex", "claude", "fixture answer", "parity-a", replyTo: first.Id);
        var waiting = Task.Run(() => daemon.Rpc(WaitCall([first.Id, second.Id], timeout: 10)));
        Wait.Until(() => (box.State("waited") ?? []).Select(p => p.Key).Order().SequenceEqual(new[] { first.Id, second.Id }.Order()), 5, "both waits reserved");
        Thread.Sleep(200);
        Assert.False(box.IsRead(answer));
        daemon.Rpc(Cancelled(89));
        Assert.True(waiting.Wait(5000));
        Assert.NotNull(waiting.Result!["error"]);
        Assert.False(box.IsRead(answer));
        Assert.True(box.State("waited") is null or { Count: 0 });
    }

    [Fact]
    public void Second_wait_cannot_release_the_first_sessions_reservation()
    {
        using var daemon = new Daemon();
        var box = iso.Box();
        var ask = box.Post("claude", "codex", "fixture", "parity-exclusive");
        var first = Task.Run(() => daemon.Rpc(WaitCall([ask.Id], 100, 10)));
        Wait.Until(() => box.State("waited")?.ContainsKey(ask.Id) == true, 5, "wait reserved");
        var rejected = Payload(daemon.Rpc(WaitCall([ask.Id], 101, 1)));
        Assert.Equal("error", rejected["status"]?.ToString());
        Assert.Contains("reserved", rejected["error"]!.ToString());
        Assert.True(box.State("waited")?.ContainsKey(ask.Id) == true && !first.IsCompleted);
        daemon.Rpc(Cancelled(100));
        Assert.True(first.Wait(5000));
        Assert.True(box.State("waited") is null or { Count: 0 });
    }

    [Fact]
    public void Restart_returns_held_results_and_preserves_unanswered_reservation()
    {
        using var daemon = new Daemon();
        var box = iso.Box();
        var first = box.Post("claude", "codex", "fixture", "parity-a");
        var second = box.Post("claude", "antigravity", "fixture", "parity-b");
        var answer = box.Post("codex", "claude", "fixture answer", "parity-a", replyTo: first.Id);
        var waiting = Task.Run(() => daemon.Rpc(WaitCall([first.Id, second.Id], timeout: 10)));
        Wait.Until(() => box.State("waited")?.Count == 2, 5, "both waits reserved");
        File.WriteAllText(Path.Join(iso.Relay, "daemon-restart.request"), "force fixture");
        Assert.True(waiting.Wait(6000));
        var group = Payload(waiting.Result);
        Assert.Equal("partial", group["status"]?.ToString());
        Assert.Equal(["success", "restarting"], (group["replies"] as JsonArray)!.Select(r => r!["status"]!.ToString()));
        Assert.True(box.IsRead(answer) && box.State("waited")?.ContainsKey(second.Id) == true);
        Assert.True(daemon.Process.WaitForExit(15_000));
        Assert.Equal(0, daemon.Process.ExitCode);
    }

    [Fact]
    public void Production_worker_refreshes_card_watch_state_and_status_exposes_stored_map()
    {
        using var daemon = new Daemon(fixture: false);
        var box = iso.Box();
        Wait.Until(() => box.State("native-cards-check") is not null, 10, "card watch state");
        string folder = Path.Join(box.Root, "cartes");
        Directory.CreateDirectory(folder);
        File.WriteAllText(Path.Join(folder, "codex-fixture.json"), new JsonObject
        {
            ["app"] = "codex", ["version"] = "fixture", ["surveyed_at"] = "2026-10-29T22:00:00Z", ["elements"] = new JsonArray(new JsonObject { ["role"] = "button" }),
            ["landmarks"] = new JsonObject { ["NEW_CHAT"] = new JsonObject { ["path"] = "vue de départ" }, ["ARCHIVE"] = null },
        }.ToJsonString());
        var data = Payload(daemon.Rpc(Native.Call("status")));
        Assert.Equal("fixture", data["agents"]?["codex"]?["carte"]?["version"]?.ToString());
        Assert.Equal(["ARCHIVE"], (data["agents"]!["codex"]!["carte"]!["missing"] as JsonArray)!.Select(n => n!.ToString()));
    }

    [Fact]
    public void Targeted_installer_restart_preserves_another_instance_on_another_port()
    {
        using var first = new Daemon();
        using var second = new Daemon();
        string probe = Path.Join(Paths.Repo(), "tests", "native_restart", "probe.ps1");
        string[] common = ["-Mailbox", iso.Relay, "-Executable", Native.Exe];
        var wrong = JsonNode.Parse(Native.Pwsh(probe, [.. common, "-Port", first.Port.ToString(), "-Request"]).Out)!;
        Assert.False(wrong["ok"]!.GetValue<bool>());
        Assert.Contains("port", wrong["reason"]!.ToString());
        Assert.False(first.Process.HasExited || second.Process.HasExited);
        var correct = JsonNode.Parse(Native.Pwsh(probe, [.. common, "-Port", second.Port.ToString(), "-Request", "-ConnectionOnly"]).Out)!;
        Assert.True(correct["ok"]!.GetValue<bool>(), correct.ToJsonString());
        Assert.Equal(second.Process.Id, correct["pid"]!.GetValue<int>());
        Assert.True(second.Process.WaitForExit(10_000));
        Assert.Equal(0, second.Process.ExitCode);
        Thread.Sleep(2100);  // the other watcher gets a full polling cycle and must remain alive
        Assert.False(first.Process.HasExited);
    }
}
