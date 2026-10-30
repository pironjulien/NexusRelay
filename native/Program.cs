using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;

namespace NexusRelay;

public static class Program
{
    public static async Task<int> Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);

        if (args.Contains("--selftest", StringComparer.Ordinal))
        {
            return await RunSelfTestAsync();
        }
        if (args.Contains("--runtime-selftest", StringComparer.Ordinal)) return RuntimeTests.Run();
        if (args.Contains("--router-status", StringComparer.Ordinal)) { Console.WriteLine(CodexRouter.Inspect().ToJsonString()); return 0; }
        if (args.Contains("--schemas", StringComparer.Ordinal)) { Console.WriteLine(McpServer.BuildToolsList().ToJsonString()); return 0; }
        if (args.Length == 1 && args[0] == "--ui-contracts") { Console.WriteLine(NativeCards.Contracts().ToJsonString()); return 0; }
        if (args.Length == 3 && args[0] == "--card-index") { Console.WriteLine(NativeCards.CodexIndex(args[1], args[2]).ToJsonString()); return 0; }
        if (args.Contains("--quota-status", StringComparer.Ordinal)) { Console.WriteLine(new JsonObject { ["usage"] = AppSessions.AntigravityUsage(new()), ["blocked_reason"] = AppSessions.AntigravityQuota(new()) }.ToJsonString()); return 0; }
        if (args.Length == 1 && args[0] == "--register-known-relay-chats") { Console.WriteLine(CodexSpace.RegisterHistory(new()).ToJsonString()); return 0; }
        if (args.Length == 1 && args[0] == "--activity-status") { Console.WriteLine(new JsonObject { ["codex"] = AppLifecycle.Activity("codex"), ["antigravity"] = AppLifecycle.Activity("antigravity") }.ToJsonString()); return 0; }
        if (args.Length is 2 or 3 && args[0] == "--recover-app") { string app = RelayPaths.NormalizeAgent(args[1]); if (app is not ("codex" or "antigravity") || args.Length == 3 && args[2] != "--force-frozen") return 2; Console.WriteLine(AppLifecycle.Ensure(app, args.Length == 3).ToJsonString()); return 0; }

        if (args.Contains("--edit-guard", StringComparer.Ordinal))
        {
            int idx = Array.IndexOf(args, "--edit-guard");
            var guardArgs = args.Skip(idx + 1).ToList();
            return EditGuard.RunCli(guardArgs);
        }

        if (args.Contains("--daemon", StringComparer.Ordinal))
        {
            return DaemonSupervisor.RunMain();
        }

        if (args.Contains("--sidecar", StringComparer.Ordinal)) return NativeSidecar.Run();
        if (args.Contains("--notice", StringComparer.Ordinal)) return NativeHooks.Notice();
        if (args.Contains("--staged", StringComparer.Ordinal) || args.Contains("--release-committed", StringComparer.Ordinal)) return NativeHooks.Commit(args);

        if (args.Contains("--inspect", StringComparer.Ordinal))
        {
            int idx = Array.IndexOf(args, "--inspect");
            string app = idx + 1 < args.Length ? RelayPaths.NormalizeAgent(args[idx + 1]) : "codex";
            if (app is not ("codex" or "antigravity")) return 2;
            Console.WriteLine(AppAutomation.Inspect(app).ToJsonString()); return 0;
        }

        if (args.Contains("--http", StringComparer.Ordinal))
        {
            int idx = Array.IndexOf(args, "--http");
            int port = 16181;
            if (idx + 1 < args.Length && int.TryParse(args[idx + 1], out int p))
                port = p;
            using var cts = new CancellationTokenSource();
            Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };
            using var fixtureRuntime = args.Contains("--protocol-fixture", StringComparer.Ordinal) ? new RelayRuntime(waker: (_, _, _) => AppTransport.Result("deferred", "isolated protocol fixture; app transport disabled"), tidy: () => { }) : null;
            await McpServer.RunHttpAsync(port, cts.Token, fixtureRuntime);
            return 0;
        }

        // Default or --mcp: stdio JSON-RPC 2.0
        string? agent = null;
        int agentIdx = Array.IndexOf(args, "--agent");
        if (agentIdx >= 0 && agentIdx + 1 < args.Length)
            agent = args[agentIdx + 1];

        using var stdioFixture = args.Contains("--protocol-fixture", StringComparer.Ordinal) ? new RelayRuntime(waker: (_, _, _) => AppTransport.Result("deferred", "isolated protocol fixture; app transport disabled"), tidy: () => { }) : null;
        return McpServer.RunStdio(agent, stdioFixture);
    }

    private static void Assert(bool condition, string message)
    {
        if (!condition)
            throw new InvalidOperationException($"Selftest assertion failed: {message}");
    }

    private static async Task<int> RunSelfTestAsync()
    {
        var tempRoot = Path.Combine(Path.GetTempPath(), $"nexus-relay-selftest-{Guid.NewGuid():N}");
        Directory.CreateDirectory(tempRoot);
        var prevHome = Environment.GetEnvironmentVariable("NEXUS_RELAY_HOME");
        var prevAgent = Environment.GetEnvironmentVariable("NEXUS_RELAY_AGENT");
        var prevHost = Environment.GetEnvironmentVariable("NEXUS_RELAY_HOST");
        var prevLang = Environment.GetEnvironmentVariable("NEXUS_RELAY_LANG");
        var prevToken = Environment.GetEnvironmentVariable("NEXUS_MCP_TOKEN");
        var prevVaults = Environment.GetEnvironmentVariable("NEXUS_RELAY_VAULTS");
        string[] isolatedKeys = ["USERPROFILE", "CODEX_HOME", "NEXUS_RELAY_WAKE_SINCE", "NEXUS_RELAY_CODEX_WORKSPACE", "NEXUS_RELAY_CODEX_PROJECT", "NEXUS_RELAY_CODEX_PROJECT_ID", "NEXUS_RELAY_KEYPAD_QUOTA"];
        var isolatedSaved = isolatedKeys.ToDictionary(k => k, Environment.GetEnvironmentVariable);

        try
        {
            Environment.SetEnvironmentVariable("NEXUS_RELAY_HOME", Path.Combine(tempRoot, "relay"));
            Environment.SetEnvironmentVariable("NEXUS_RELAY_AGENT", "claude");
            Environment.SetEnvironmentVariable("NEXUS_RELAY_HOST", "claude");
            Environment.SetEnvironmentVariable("NEXUS_RELAY_LANG", "fr");
            foreach (string key in isolatedKeys) Environment.SetEnvironmentVariable(key, null);
            Environment.SetEnvironmentVariable("USERPROFILE", Path.Combine(tempRoot, "profile"));
            Environment.SetEnvironmentVariable("CODEX_HOME", Path.Combine(tempRoot, "profile", ".codex"));
            Environment.SetEnvironmentVariable("NEXUS_RELAY_KEYPAD_QUOTA", Path.Combine(tempRoot, "quota.json"));

            var report = new JsonObject();

            // 1. Cycle complet du salon : send, inbox, read, progress, reply, track, search, summary, close
            {
                var sendRes = McpServer.CallToolPayload("send", new JsonObject
                {
                    ["to"] = "codex",
                    ["text"] = "Vérifie l'architecture C# de NexusRelay avec accents éèà",
                    ["topic"] = "nexusrelay",
                }, "claude");
                Assert(sendRes["status"]?.GetValue<string>() == "success", "send status");
                var msgId = sendRes["id"]?.GetValue<string>() ?? "";
                Assert(!string.IsNullOrEmpty(msgId), "send id");

                // Host rule: non-host cannot open exchange when host is restricted to claude
                Environment.SetEnvironmentVariable("NEXUS_RELAY_HOST", "claude");
                var nonHostSend = McpServer.CallToolPayload("send", new JsonObject
                {
                    ["to"] = "claude",
                    ["text"] = "Tentative non-hote",
                    ["topic"] = "nexusrelay",
                }, "codex");
                Assert(nonHostSend["status"]?.GetValue<string>() == "error", "non-host send must fail");

                // Symmetric Trinity mode: all agents can send when host=all
                Environment.SetEnvironmentVariable("NEXUS_RELAY_HOST", "all");
                var peerSend = McpServer.CallToolPayload("send", new JsonObject
                {
                    ["to"] = "claude",
                    ["text"] = "Message de pair",
                    ["topic"] = "peer-test",
                }, "codex");
                Assert(peerSend["status"]?.GetValue<string>() == "success", "peer send must succeed when host=all");
                Environment.SetEnvironmentVariable("NEXUS_RELAY_HOST", null);

                // Inbox for codex
                var inboxRes = McpServer.CallToolPayload("inbox", new JsonObject(), "codex");
                var unread = inboxRes["unread"] as JsonArray;
                Assert(unread is not null && unread.Count == 1, "codex inbox unread == 1");
                Assert((unread![0]?["text"]?.GetValue<string>() ?? "").Contains("⟦relay:", StringComparison.Ordinal), "relay framing in inbox");

                // Progress by codex
                var progRes = McpServer.CallToolPayload("progress", new JsonObject
                {
                    ["id"] = msgId,
                    ["note"] = "Analyse du code en cours",
                }, "codex");
                Assert(progRes["state"]?.GetValue<string>() == "en cours", "progress state");

                // Track by claude while in progress
                var trackProg = McpServer.CallToolPayload("track", new JsonObject { ["id"] = msgId }, "claude");
                Assert((trackProg["state"]?.GetValue<string>() ?? "").Contains("en cours", StringComparison.Ordinal), "track shows en cours");

                // Read by codex
                var readRes = McpServer.CallToolPayload("read", new JsonObject { ["id"] = msgId }, "codex");
                Assert(readRes["message"]?["id"]?.GetValue<string>() == msgId, "read message id");

                // Reply by codex
                var replyRes = McpServer.CallToolPayload("reply", new JsonObject
                {
                    ["id"] = msgId,
                    ["text"] = "Tout est conforme dans RelayBox.cs:42 (gravité basse).",
                    ["vote"] = "agree",
                    ["state"] = "done",
                }, "codex");
                Assert(replyRes["status"]?.GetValue<string>() == "success", "reply success");
                var replyId = replyRes["id"]?.GetValue<string>() ?? "";
                Assert(!string.IsNullOrEmpty(replyId), "reply id");

                // Track after reply
                var trackDone = McpServer.CallToolPayload("track", new JsonObject { ["id"] = msgId }, "claude");
                Assert(trackDone["state"]?.GetValue<string>() == "répondu", "track shows répondu");

                // Search (accent & case insensitive)
                var searchRes = McpServer.CallToolPayload("search", new JsonObject
                {
                    ["query"] = "architecture eea",
                    ["topic"] = "nexusrelay",
                }, "claude");
                Assert((searchRes["total"]?.GetValue<int>() ?? 0) >= 1, "search total >= 1");

                // Summary
                var summaryRes = McpServer.CallToolPayload("summary", new JsonObject { ["topic"] = "nexusrelay" }, "claude");
                Assert(summaryRes["answered_total"]?.GetValue<int>() == 1, "summary answered_total == 1");
                Assert(summaryRes["open_total"]?.GetValue<int>() == 0, "summary open_total == 0");

                // Close topic
                var closeRes = McpServer.CallToolPayload("close", new JsonObject { ["topic"] = "nexusrelay" }, "claude");
                Assert(closeRes["closed"]?.GetValue<bool>() == true, "close topic");

                // Salon markdown generated
                var box = new RelayBox();
                Assert(File.Exists(Path.Combine(box.Root, "salon", "nexusrelay.md")), "salon markdown exists");

                report["workflow"] = "ok";
            }

            // 2. Concurrence : 2 réclamations simultanées (claims/<id>/1.json en CreateNew) -> 1 seul gagnant
            {
                var box = new RelayBox();
                var m = box.Post("claude", "antigravity", "Test concurrence", "concurrence");
                using var barrier = new Barrier(8);
                int winners = 0;
                var tasks = Enumerable.Range(0, 8).Select(i => Task.Run(() =>
                {
                    barrier.SignalAndWait();
                    if (box.Claim(m.Id, 1, $"worker-{i}"))
                        Interlocked.Increment(ref winners);
                })).ToArray();
                await Task.WhenAll(tasks);
                Assert(winners == 1, $"expected exactly 1 claim winner, got {winners}");
                var claim = box.ClaimOf(m.Id, 1);
                Assert(claim is not null && claim["phase"]?.GetValue<string>() == "claimed", "claim record valid");
                report["concurrency"] = "ok";
            }

            // 3. Annulation (cancel) : message non pris annulé vs message déjà pris refusé
            {
                var send1 = McpServer.CallToolPayload("send", new JsonObject
                {
                    ["to"] = "codex",
                    ["text"] = "Demande à annuler avant lecture",
                    ["topic"] = "annulation",
                }, "claude");
                var id1 = send1["id"]!.GetValue<string>();
                var cancel1 = McpServer.CallToolPayload("cancel", new JsonObject { ["id"] = id1 }, "claude");
                Assert(cancel1["status"]?.GetValue<string>() == "success", "cancel unread succeeds");
                Assert(cancel1["state"]?.GetValue<string>() == "annulé", "cancel state is annulé");

                // Reply on cancelled message must be refused
                var replyCancelled = McpServer.CallToolPayload("reply", new JsonObject
                {
                    ["id"] = id1,
                    ["text"] = "Réponse tardive",
                }, "codex");
                Assert(replyCancelled["status"]?.GetValue<string>() == "error", "reply on cancelled must fail");

                // Second message: read by codex first, then cancel must fail
                var send2 = McpServer.CallToolPayload("send", new JsonObject
                {
                    ["to"] = "codex",
                    ["text"] = "Demande lue avant annulation",
                    ["topic"] = "annulation",
                }, "claude");
                var id2 = send2["id"]!.GetValue<string>();
                McpServer.CallToolPayload("read", new JsonObject { ["id"] = id2 }, "codex");
                var cancel2 = McpServer.CallToolPayload("cancel", new JsonObject { ["id"] = id2 }, "claude");
                Assert(cancel2["status"]?.GetValue<string>() == "error", "cancel after read must fail");

                report["cancel"] = "ok";
            }

            // 4. Reprise après panne (crash recovery) : PID mort libère la livraison ou la réservation expirée
            {
                var box = new RelayBox();
                int deadPid = 99_999_999; // Non-existent PID
                Assert(!RelayBox.PidAlive(deadPid), "deadPid must not be alive");
                Assert(RelayBox.PidAlive(Environment.ProcessId), "current PID must be alive");

                var msg = box.Post("claude", "antigravity", "Test crash recovery", "recovery");
                Assert(box.Claim(msg.Id, 1, "crashed-sidecar", pidOverride: deadPid), "initial claim by dead PID");
                Assert(!box.Claim(msg.Id, 1, "live-sidecar"), "cannot claim before recovery");
                Assert(box.RecoverDeadClaim(msg.Id, 1), "RecoverDeadClaim releases dead-PID claim");
                Assert(box.Claim(msg.Id, 1, "live-sidecar"), "live-sidecar claims after dead-PID recovery");

                // Submitting with dead PID -> uncertain label
                box.SetClaim(msg.Id, 1, "live-sidecar", "submitting", new JsonObject { ["pid"] = deadPid });
                Assert(RelayBox.ClaimLabel(box.ClaimOf(msg.Id, 1)!) == "uncertain", "submitting + dead PID == uncertain");

                // Reservation with dead ownerPid is automatically inactive and can be reserved by another agent
                var targetFile = Path.Combine(tempRoot, "shared.cs");
                Reservations.Reserve(box, "codex", [targetFile], "édition avant crash", ttlMin: 60, ownerPid: deadPid);
                var reclaimed = Reservations.Reserve(box, "claude", [targetFile], "reprise après crash", ttlMin: 60);
                Assert(reclaimed["agent"]?.GetValue<string>() == "claude", "claude reclaims reservation from dead PID");
                Reservations.Release(box, "claude", reclaimed["id"]?.GetValue<string>());

                // The host takes over another agent's reservation only by id, traced; peers cannot
                var left = Reservations.Reserve(box, "codex", [targetFile], "travail laissé", ttlMin: 60);
                string leftId = left["id"]!.GetValue<string>();
                bool peerRefused = false, allRefused = false;
                try { Reservations.Release(box, "antigravity", leftId, force: true); } catch (RelayError) { peerRefused = true; }
                try { Reservations.Release(box, "claude", force: true); } catch (RelayError) { allRefused = true; }
                Assert(peerRefused && allRefused, "force release: host only, with an id");
                Assert(Reservations.Release(box, "claude", leftId, force: true).SequenceEqual([leftId]), "host takes over codex reservation");
                Assert(Reservations.Active(box).All(r => r["id"]?.GetValue<string>() != leftId), "taken-over reservation no longer active");

                // Vault check via GetFinalPathNameByHandleW
                var vaultDir = Path.Combine(tempRoot, "vault");
                Directory.CreateDirectory(vaultDir);
                var secretFile = Path.Combine(vaultDir, "secret.txt");
                File.WriteAllText(secretFile, "top-secret");
                Environment.SetEnvironmentVariable("NEXUS_RELAY_VAULTS", vaultDir);
                bool vaultRefused = false;
                try
                {
                    box.Post("claude", "codex", "Test coffre", "recovery", files: [secretFile]);
                }
                catch (RelayError)
                {
                    vaultRefused = true;
                }
                Assert(vaultRefused, "vault attachment must be refused");

                report["crash_recovery"] = "ok";
            }

            // 5. Serveur MCP C# (--http loopback & JSON-RPC server/discover, tools/list, tools/call)
            {
                var token = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"; // gitleaks:allow (fake test token)
                Environment.SetEnvironmentVariable("NEXUS_MCP_TOKEN", token);

                int port;
                using (var tempListener = new TcpListener(IPAddress.Loopback, 0))
                {
                    tempListener.Start();
                    port = ((IPEndPoint)tempListener.LocalEndpoint).Port;
                }

                using var cts = new CancellationTokenSource();
                var serverTask = McpServer.RunHttpAsync(port, cts.Token, new RelayRuntime(waker: (_, _, _) => AppTransport.Result("deferred", "isolated self-test adapter"), tidy: () => { }));
                try
                {
                    using var http = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}") };

                    // Wait for /health
                    HttpResponseMessage? healthResp = null;
                    for (int i = 0; i < 30; i++)
                    {
                        try
                        {
                            healthResp = await http.GetAsync("/health");
                            if (healthResp.IsSuccessStatusCode)
                                break;
                        }
                        catch
                        {
                            await Task.Delay(50);
                        }
                    }
                    Assert(healthResp is not null && healthResp.IsSuccessStatusCode, "/health 200 OK");

                    // Unauthorized without token
                    var unauthResp = await http.PostAsync("/mcp?agent=claude",
                        new StringContent("""{"jsonrpc":"2.0","id":1,"method":"ping"}""", Encoding.UTF8, "application/json"));
                    Assert(unauthResp.StatusCode == HttpStatusCode.Unauthorized, "401 without token");

                    http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);

                    async Task<JsonObject> RpcCallAsync(string agent, string method, JsonObject? @params = null)
                    {
                        var req = new JsonObject
                        {
                            ["jsonrpc"] = "2.0",
                            ["id"] = 1,
                            ["method"] = method,
                            ["params"] = @params ?? new JsonObject(),
                        };
                        var resp = await http.PostAsync($"/mcp?agent={agent}",
                            new StringContent(req.ToJsonString(), Encoding.UTF8, "application/json"));
                        Assert(resp.IsSuccessStatusCode, $"HTTP RPC {method} status {resp.StatusCode}");
                        var body = await resp.Content.ReadAsStringAsync();
                        return (JsonNode.Parse(body) as JsonObject)?["result"] as JsonObject
                            ?? throw new InvalidOperationException($"Missing result in {body}");
                    }

                    // server/discover
                    var discover = await RpcCallAsync("claude", "server/discover");
                    Assert(discover["protocolVersion"]?.GetValue<string>() == "2026-07-28", "discover protocolVersion");
                    Assert((discover["tools"] as JsonArray)?.Count == 20, "discover 20 tools");

                    // tools/list
                    var toolsList = await RpcCallAsync("claude", "tools/list");
                    Assert((toolsList["tools"] as JsonArray)?.Count == 20, "tools/list 20 tools");

                    // tools/call status
                    var statusCall = await RpcCallAsync("claude", "tools/call", new JsonObject
                    {
                        ["name"] = "status",
                        ["arguments"] = new JsonObject(),
                    });
                    Assert(statusCall["structuredContent"]?["me"]?.GetValue<string>() == "claude", "status me == claude");

                    // tools/call reserve, reservations, release
                    var fileToReserve = Path.Combine(tempRoot, "http_reserved.py");
                    var reserveCall = await RpcCallAsync("claude", "tools/call", new JsonObject
                    {
                        ["name"] = "reserve",
                        ["arguments"] = new JsonObject
                        {
                            ["paths"] = new JsonArray(fileToReserve),
                            ["reason"] = "test HTTP reserve",
                        },
                    });
                    var reserveData = JsonNode.Parse(reserveCall["content"]![0]!["text"]!.GetValue<string>()) as JsonObject;
                    Assert(reserveData?["status"]?.GetValue<string>() == "success", "HTTP reserve success");
                    var resId = reserveData!["id"]!.GetValue<string>();

                    var listResCall = await RpcCallAsync("claude", "tools/call", new JsonObject
                    {
                        ["name"] = "reservations",
                        ["arguments"] = new JsonObject(),
                    });
                    Assert((listResCall["structuredContent"]?["active"] as JsonArray)?.Count == 1, "1 active reservation");

                    var releaseCall = await RpcCallAsync("claude", "tools/call", new JsonObject
                    {
                        ["name"] = "release",
                        ["arguments"] = new JsonObject { ["id"] = resId },
                    });
                    var releaseData = JsonNode.Parse(releaseCall["content"]![0]!["text"]!.GetValue<string>()) as JsonObject;
                    Assert((releaseData?["released"] as JsonArray)?.Count == 1, "released 1 reservation");
                }
                finally
                {
                    cts.Cancel();
                    try { await serverTask; } catch { }
                }

                report["mcp_server"] = "ok";
            }

            var finalOut = new JsonObject
            {
                ["ok"] = true,
                ["version"] = RelayPaths.Version,
                ["tests"] = report,
            };
            Console.WriteLine(finalOut.ToJsonString());
            return 0;
        }
        finally
        {
            Environment.SetEnvironmentVariable("NEXUS_RELAY_HOME", prevHome);
            Environment.SetEnvironmentVariable("NEXUS_RELAY_AGENT", prevAgent);
            Environment.SetEnvironmentVariable("NEXUS_RELAY_HOST", prevHost);
            Environment.SetEnvironmentVariable("NEXUS_RELAY_LANG", prevLang);
            Environment.SetEnvironmentVariable("NEXUS_MCP_TOKEN", prevToken);
            Environment.SetEnvironmentVariable("NEXUS_RELAY_VAULTS", prevVaults);
            foreach (var pair in isolatedSaved) Environment.SetEnvironmentVariable(pair.Key, pair.Value);
            try { Directory.Delete(tempRoot, recursive: true); } catch { }
        }
    }
}
