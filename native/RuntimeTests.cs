using System.Text.Json.Nodes;

namespace NexusRelay;

public static class RuntimeTests
{
    public static int Run()
    {
        string temp = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "NexusRelay", "test-runtime", Guid.NewGuid().ToString("N"));
        string[] keys = ["NEXUS_RELAY_HOME", "NEXUS_RELAY_HOST", "USERPROFILE", "CODEX_HOME", "NEXUS_RELAY_WAKE_SINCE", "NEXUS_RELAY_CODEX_WORKSPACE", "NEXUS_RELAY_CODEX_PROJECT", "NEXUS_RELAY_CODEX_PROJECT_ID"];
        var saved = keys.ToDictionary(k => k, Environment.GetEnvironmentVariable);
        Directory.CreateDirectory(temp);
        try
        {
            Environment.SetEnvironmentVariable("NEXUS_RELAY_HOME", temp); Environment.SetEnvironmentVariable("NEXUS_RELAY_HOST", "claude");
            Environment.SetEnvironmentVariable("USERPROFILE", Path.Combine(temp, "profile")); Environment.SetEnvironmentVariable("CODEX_HOME", Path.Combine(temp, "profile", ".codex"));
            foreach (string key in keys.Skip(4)) Environment.SetEnvironmentVariable(key, null);
            var box = new RelayBox(); int calls = 0;
            using var runtime = new RelayRuntime(box, (_, messages, submit) => { calls++; submit(); return new() { ["status"] = "started", ["messages"] = new JsonArray(messages.Select(m => JsonValue.Create(m.Id)).ToArray()), ["via"] = "test adapter" }; }, () => { });
            void Check(bool ok, string label) { if (!ok) throw new InvalidOperationException(label); }
            Check(AppSessions.IsRelay(new("id", "", @"\\?\" + AppSessions.RelayDirectory, false, false)), "Windows extended path prefixes must preserve Relay ownership");
            Check(!AppSessions.IsRelay(new("id", "", AppSessions.RelayDirectory + "-other", false, false)), "Neighbor directories must remain outside Relay ownership");
            Check((runtime.Health()["missing"] as JsonArray)?.Count == 3, "Undeclared workers must be reported missing");
            var info = box.Post("claude", "codex", "Information", "runtime-test", info: true);
            Check(!runtime.Ring(info), "FYI never wakes an agent");
            var request = box.Post("claude", "codex", "Read and answer", "runtime-test");
            Check(runtime.Recover() == 1, "Crash recovery restores a pending unread request");
            Check(runtime.Step("codex")?["status"]?.ToString() == "started" && calls == 1, "Recovered message must invoke its waker");
            Check(box.RingState(request.Id)?["attempt"]?.GetValue<int>() == 1, "Wake attempt must be persisted");
            Check(runtime.Step("codex") is null && calls == 1, "Wake is not repeated before confirmation deadline");
            var ring = box.RingState(request.Id)!; ring["recheck_at"] = Reservations.Now() - 1; box.SetRingState(request.Id, ring);
            Check(runtime.Step("codex") is not null && calls == 2, "Unread confirmed wake gets a bounded retry");
            box.MarkRead(request);
            Check(runtime.Step("codex") is null && calls == 2, "Read messages are never reawoken");
            var awaited = box.Post("codex", "antigravity", "Awaited answer", "runtime-test", replyTo: request.Id);
            box.SetState("waited", new() { [request.Id] = Reservations.Now() + 30 });
            Check(!runtime.Ring(awaited), "A reply reserved by wait_reply must not wake another conversation");
            var noSidecar = RelayRuntime.SidecarHealth(box);
            Check(!AppTransport.Truth(noSidecar["alive"]), "No heartbeat cannot mean live sidecar");
            var uncertain = box.Post("claude", "codex", "Uncertain send", "runtime-test");
            box.SetRingState(uncertain.Id, new() { ["attempt"] = 1, ["status"] = "uncertain", ["recheck_at"] = Reservations.Now() - 1 });
            runtime.Recover();
            Check(runtime.Step("codex") is null && calls == 2, "An uncertain delivery must never be replayed");
            var eventOnly = box.Post("claude", "codex", "Lost ring after uncertain event", "event-recovery");
            box.Event(eventOnly.Id, "réveil", new() { ["status"] = "uncertain" });
            runtime.Recover();
            var recovered = box.RingState(eventOnly.Id)!; recovered["recheck_at"] = Reservations.Now() - 1; box.SetRingState(eventOnly.Id, recovered);
            Check(runtime.Step("codex") is null && calls == 2, "Recovering an uncertain event must retain its status after its deadline");
            var interrupted = box.Post("claude", "codex", "Crash after Enter, before acknowledgement", "crash-submit");
            using (var crashing = new RelayRuntime(box, (_, batch, submit) =>
            {
                Check(batch.All(m => box.Claims(m.Id).Any(c => c.Claim["phase"]?.ToString() == "claimed")), "Durable claims must precede preparation");
                submit();
                Check(batch.All(m => box.Claims(m.Id).Any(c => c.Claim["phase"]?.ToString() == "submitting")), "Submission intent must be durable before any key event");
                throw new IOException("fixture: process interrupted after Enter");
            }, () => { }))
            {
                crashing.Recover();
                try { crashing.Step("codex"); throw new InvalidOperationException("Crash fixture did not throw"); }
                catch (IOException) { }
            }
            using (var restarted = new RelayRuntime(box, (_, _, _) => { calls++; return AppTransport.Result("started"); }, () => { }))
            {
                restarted.Recover();
                Check(box.RingState(interrupted.Id)?["status"]?.ToString() == "uncertain", "A crash in submission must recover as uncertain");
                Check(restarted.Step("codex") is null && calls == 2, "A restart after Enter must never double-deliver");
            }
            var dead = box.Post("claude", "codex", "Terminated process without finally", "dead-submit");
            Check(box.Claim(dead.Id, 1, "native-codex", pidOverride: -1), "Fixture owns delivery");
            box.SetClaim(dead.Id, 1, "native-codex", "submitting");
            runtime.Recover();
            Check(runtime.Step("codex") is null && box.RingState(dead.Id)?["status"]?.ToString() == "uncertain", "Hard termination during submission must remain uncertain");
            var held = box.Post("claude", "codex", "Backoff", "backoff-test");
            using var failed = new RelayRuntime(box, (_, _, _) => { calls++; return AppTransport.Result("failed", "known rejection"); }, () => { });
            failed.Recover(); failed.Step("codex"); int after = calls;
            failed.Step("codex");
            Check(calls == after && box.State("wake-codex")?["holds"]?[held.Topic]?["status"]?.ToString() == "failed", "Failed wakes use per-topic persisted backoff");
            var unfinished = box.Post("claude", "antigravity", "Await reply before archiving", "tidy-proof");
            box.SetTopicState(unfinished.Topic, "closed", new() { ["closed"] = true });
            double now = Reservations.Now();
            Check(!AppAutomation.AntigravityTidyReady(box, unfinished.Topic, false, new(true, now - 20), now), "A completed turn without a Relay reply must not be archived");
            box.MarkRead(unfinished); box.Post("antigravity", "claude", "Reply", unfinished.Topic, replyTo: unfinished.Id);
            Check(AppAutomation.AntigravityTidyReady(box, unfinished.Topic, false, new(true, now - 20), now), "Closed answered topic is eligible for archival");
            Check(!AppAutomation.AntigravityTidyReady(box, unfinished.Topic, true, new(false, now - 20), now), "Replaced conversation still working must stay live");
            Check(AppAutomation.AntigravityTidyReady(box, unfinished.Topic, true, new(false, now - 7201), now), "Replaced stale conversation is eligible even when its replacement topic stays active");
            var configured = new JsonObject { ["project_id"] = "official-id", ["path"] = Path.Combine(temp, "Documents"), ["label"] = "Documents" };
            var evidence = new JsonObject { ["count"] = 1, ["id"] = "official-id", ["path"] = Path.Combine(temp, "Documents"), ["label"] = "Documents" };
            Check(AppAutomation.ProjectMatches(evidence, configured), "Exact official UUID and canonical workspace must match");
            evidence["id"] = "other-id"; Check(!AppAutomation.ProjectMatches(evidence, configured), "Same label with another UUID must be rejected before typing");
            evidence["id"] = "official-id"; evidence["path"] = temp; Check(!AppAutomation.ProjectMatches(evidence, configured), "Same UUID with another path must be rejected before typing");
            evidence["path"] = configured["path"]!.ToString(); evidence["count"] = 2; Check(!AppAutomation.ProjectMatches(evidence, configured), "Duplicate project rows must fail closed");
            var registry = new JsonObject { ["threads"] = new JsonObject { ["relay"] = new JsonObject { ["verified"] = true, ["workspace"] = temp }, ["child"] = new JsonObject { ["verified"] = true, ["workspace"] = temp }, ["parent"] = new JsonObject { ["verified"] = true, ["workspace"] = temp } } };
            box.SetState("codex-owned", registry);
            Check(CodexSpace.Owns(new("relay", "", temp, false, false), box), "Verified Relay ID must remain eligible");
            Check(!CodexSpace.Owns(new("julien", "", temp, false, false), box), "Shared Documents cwd does not own a human chat");
            Check(!CodexSpace.Owns(new("child", "", temp, false, true), box), "Child chats must be protected even if registered");
            string? session = Environment.GetEnvironmentVariable("CODEX_SESSION_ID"); Environment.SetEnvironmentVariable("CODEX_SESSION_ID", "parent");
            Check(!CodexSpace.Owns(new("parent", "", temp, false, false), box), "Current parent chat must always be protected");
            Environment.SetEnvironmentVariable("CODEX_SESSION_ID", session);
            var (folded, offsets) = SearchAndSummary.Fold("🎯Été𐐀");
            Check(folded == "🎯ete𐐨" && offsets.Count == folded.Length && offsets[2] == 2, "Unicode folding must preserve supplementary characters and UTF16 source offsets");
            string requestPath = Path.Combine(temp, "restart.request");
            File.WriteAllText(requestPath, "code sha256:" + RelayPaths.CodeFingerprint());
            Check(!RelayPaths.RestartWanted(requestPath, RelayPaths.CodeFingerprint()), "Same immutable artifact must not restart");
            File.WriteAllText(requestPath, "code sha256:" + new string('a', 64));
            Check(RelayPaths.RestartWanted(requestPath, RelayPaths.CodeFingerprint()), "A declared new immutable artifact must restart the old process");
            foreach (string phase in new[] { "submitting", "uncertain" })
            {
                var launch = new JsonObject { ["status"] = phase };
                Check(AppLifecycle.PendingLaunch(launch, false) is not null, "Unconfirmed launch must block both kill and replay after restart");
                Check(AppLifecycle.PendingLaunch(launch, true) is null, "A genuinely responsive app page confirms an uncertain launch");
            }
            Check(AppLifecycle.RestartGuard("codex", true, false, true, true) is not null, "A frozen window must never override a working Codex agent");
            Check(AppLifecycle.RestartGuard("antigravity", false, false, true, true) is not null, "Background Antigravity work must prevent restart");
            Check(AppLifecycle.RestartGuard("antigravity", false, true, false, true) is not null, "User presence must prevent restart");
            Check(!AppLifecycle.InstalledExecutable("codex", Path.Combine(temp, "ChatGPT.exe")), "A same-name executable outside the installed package is not the app");
            Check(!AppLifecycle.InstalledExecutable("antigravity", Path.Combine(temp, "Antigravity.exe")), "A portable homonym must not be started or stopped");
            Console.WriteLine(new JsonObject { ["ok"] = true, ["tests"] = "worker liveness, crash recovery, delivery, confirmation, bounded retries, read suppression, wait reservation, sidecar liveness, uncertain suppression, backoff" }.ToJsonString());
            return 0;
        }
        finally
        {
            foreach (var pair in saved) Environment.SetEnvironmentVariable(pair.Key, pair.Value);
            Directory.Delete(temp, recursive: true);
        }
    }
}
