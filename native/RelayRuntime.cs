using System.Collections.Concurrent;
using System.Text.Json.Nodes;

namespace NexusRelay;

public sealed class RelayActivity : IDisposable
{
    private static readonly Lock Gate = new();
    private static readonly Dictionary<long, (string Label, double At)> Running = new();
    private static long next;
    private static bool closed;
    private readonly long id;
    private RelayActivity(long id) { this.id = id; }
    public static RelayActivity? Enter(string label)
    {
        lock (Gate) { if (closed) return null; long id = ++next; Running[id] = (label, Reservations.Now()); return new(id); }
    }
    public void Dispose() { lock (Gate) Running.Remove(id); }
    public static void BeginClosing() { lock (Gate) closed = true; }
    public static bool CloseIfIdle() { lock (Gate) { if (Running.Count == 0) { closed = true; } return Running.Count == 0 && closed; } }
    public static JsonArray Snapshot() { lock (Gate) return new JsonArray(Running.Values.OrderBy(x => x.At).Select(x => (JsonNode)new JsonObject { ["quoi"] = x.Label, ["depuis_s"] = Math.Round(Reservations.Now() - x.At, 1) }).ToArray()); }
}

public sealed class RelayRuntime : IDisposable
{
    private readonly RelayBox box;
    private readonly CancellationTokenSource stop = new();
    private readonly ConcurrentDictionary<string, Task> workers = new();
    private readonly ConcurrentDictionary<string, double> beats = new();
    private readonly ConcurrentDictionary<string, string> errors = new();
    private readonly ConcurrentDictionary<string, double> pending = new();
    private readonly Func<string, List<RelayMessage>, Action, JsonObject> waker;
    private readonly Action? tidyOverride;
    private int disposed;
    public RelayRuntime(RelayBox? box = null, Func<string, List<RelayMessage>, Action, JsonObject>? waker = null, Action? tidy = null)
    { this.box = box ?? new(); this.waker = waker ?? Wake; tidyOverride = tidy; }
    public void Start()
    {
        foreach (string name in new[] { "codex", "antigravity", "rangement" })
            workers.TryAdd(name, Task.Run(() => Run(name)));
        Recover();
    }
    public int Recover()
    {
        int count = 0;
        foreach (string agent in new[] { "codex", "antigravity" }) foreach (var message in box.Unread(agent).Where(m => !(Reservations.Now() - AppSessions.Epoch(m.Created) >= 3600 || !NeedsWake(m) || box.RingState(m.Id)?["incident"] is not null)))
        {
            RecoverDelivery(message);
            pending.TryAdd(message.Id, Reservations.Now() - 2); count++;
        }
        return count;
    }
    private void RecoverDelivery(RelayMessage message)
    {
        var claims = box.Claims(message.Id).Where(c => c.Claim["owner"]?.ToString() == "native-codex").ToList();
        if (claims.Any(c => RelayBox.ClaimLabel(c.Claim) is "uncertain" or "submitting"))
        {
            box.SetRingState(message.Id, new() { ["attempt"] = claims.Max(c => c.Attempt), ["status"] = "uncertain", ["recheck_at"] = Reservations.Now() + 300 }); return;
        }
        foreach (var (attempt, claim) in claims)
        {
            string phase = RelayBox.ClaimLabel(claim);
            if (phase == "abandoned_dead_pid") { box.RecoverDeadClaim(message.Id, attempt); continue; }
            if (phase is "uncertain" or "submitting" or "delivered")
            {
                string status = phase == "delivered" ? "started" : "uncertain";
                var ring = box.RingState(message.Id);
                if (ring is null || (ring["attempt"]?.GetValue<int>() ?? 0) < attempt || status == "uncertain")
                    box.SetRingState(message.Id, new() { ["attempt"] = attempt, ["status"] = status, ["recheck_at"] = Reservations.Now() + 300, ["conversation"] = claim["conversation"]?.DeepClone() });
            }
        }
        if (box.RingState(message.Id) is null && box.Events(message.Id).LastOrDefault(e => e["kind"]?.ToString() == "réveil" && e["status"]?.ToString() is "started" or "uncertain") is JsonObject last)
            box.SetRingState(message.Id, new() { ["attempt"] = 1, ["status"] = last["status"]?.DeepClone(), ["recheck_at"] = Reservations.Now() + 300 });
    }
    public static bool NeedsWake(RelayMessage message)
    {
        if (message.To is not ("codex" or "antigravity") || message.Info || message.Sender == message.To || message.To == RelayPaths.RelayHost()) return false;
        if ((RelayPaths.RelayHost() != "all" && message.Sender != RelayPaths.RelayHost()) || !CodexSpace.AcceptsCreated(message.Created)) return false;
        if (message.ReplyTo is null) return true;
        var waited = new RelayBox().State("waited");
        return waited?[message.ReplyTo] is not JsonValue expiry || !expiry.TryGetValue<double>(out double until) || until <= Reservations.Now();
    }
    public bool Ring(RelayMessage message)
    {
        if (!NeedsWake(message)) return false;
        pending.TryAdd(message.Id, Reservations.Now()); return true;
    }
    private async Task Run(string name)
    {
        while (!stop.IsCancellationRequested)
        {
            beats[name] = Reservations.Now();
            using var activity = RelayActivity.Enter(name == "rangement" ? "rangement et diagnostic des tours" : $"réveil de {name}");
            if (activity is null) return;
            try
            {
                if (!AppTransport.EmergencyStop())
                {
                    if (name == "rangement")
                    {
                        if (tidyOverride is not null) tidyOverride();
                        else
                        {
                            WatchCodex();
                            AppLifecycle.MaintainAntigravity(box);
                            NativeCards.RefreshDue(box, () => beats[name] = Reservations.Now());
                            NativeUpdater.RefreshDue();
                            box.SetState("codex-tidy", WithAt(AppAutomation.TidyCodex(box)));
                            box.SetState("agy-tidy", WithAt(AppAutomation.TidyAntigravity(box)));
                        }
                    }
                    else Step(name);
                }
                errors.TryRemove(name, out _);
            }
            catch (Exception e) { errors[name] = $"{e.GetType().Name}: {e.Message}"; Console.Error.WriteLine($"{name}: {e.Message}"); } // Boundary: a worker failure is reported in health, the worker loop goes on.
            activity.Dispose();
            beats[name] = Reservations.Now();
            try { await Task.Delay(TimeSpan.FromSeconds(name == "rangement" ? 10 : 2), stop.Token); } catch (OperationCanceledException) { /* Stopping: the loop condition ends the worker. */ }
        }
    }
    private static JsonObject WithAt(JsonObject result) { result["at"] = RelayBox.NowIso(); return result; }
    public JsonObject? Step(string agent)
    {
        double now = Reservations.Now();
        var ready = new List<RelayMessage>();
        foreach (var pair in pending.ToArray())
        {
            var message = box.Get(pair.Key);
            if (message is null || box.IsRead(message)) { pending.TryRemove(pair.Key, out _); continue; }
            if (message.To != agent || now - pair.Value < 2 || !NeedsWake(message)) continue;
            if (agent == "codex") RecoverDelivery(message);
            var ring = box.RingState(message.Id);
            if (ring?["incident"] is not null) { pending.TryRemove(message.Id, out _); continue; }
            int attempt = ring?["attempt"]?.GetValue<int>() ?? 0;
            if (ring?["status"]?.ToString() == "uncertain") continue; // Unknown send outcome is never replayed.
            if (attempt > 0 && (ring?["recheck_at"]?.GetValue<double>() ?? now + 300) > now) continue;
            if (attempt >= 3 || now - (AppSessions.Epoch(message.Created) ?? now) > 3600)
            {
                var incident = ring ?? new(); incident["incident"] = attempt >= 3 ? "Unread after three wakes" : "No wake confirmed within one hour"; incident["incident_at"] = RelayBox.NowIso();
                box.SetRingState(message.Id, incident); box.Event(message.Id, "incident", new() { ["reason"] = incident["incident"]!.DeepClone() }); pending.TryRemove(message.Id, out _); continue;
            }
            ready.Add(message);
        }
        var previous = box.State($"wake-{agent}") ?? new();
        var holds = previous["holds"] as JsonObject ?? new();
        var group = ready.GroupBy(m => m.Topic).OrderBy(g => g.Min(m => m.Created)).FirstOrDefault(g =>
        {
            var hold = holds[g.Key] as JsonObject;
            if (hold?["status"]?.ToString() is not ("deferred" or "failed")) return true;
            int streak = hold["streak"]?.GetValue<int>() ?? 1;
            double delay = Math.Min(300, 20 * Math.Pow(1.618, Math.Clamp(streak - 1, 0, 20)));
            return now - (hold["tried"]?.GetValue<double>() ?? 0) >= delay;
        });
        if (group is null) return null;
        var messages = group.ToList();
        var owned = new List<(RelayMessage Message, int Attempt)>();
        if (agent == "codex")
        {
            foreach (var message in messages)
            {
                int attempt = (box.RingState(message.Id)?["attempt"]?.GetValue<int>() ?? 0) + 1;
                if (box.Claim(message.Id, attempt, "native-codex")) owned.Add((message, attempt));
            }
            messages = owned.Select(item => item.Message).ToList();
            if (messages.Count == 0) return null;
        }
        void Submitting()
        {
            foreach (var item in owned) box.SetClaim(item.Message.Id, item.Attempt, "native-codex", "submitting", new() { ["submitted"] = RelayBox.NowIso() });
        }
        JsonObject result;
        try { result = waker(agent, messages, Submitting); }
        catch
        {
            foreach (var item in owned)
            {
                if (box.ClaimOf(item.Message.Id, item.Attempt)?["phase"]?.ToString() == "claimed") box.Release(item.Message.Id, item.Attempt, "native-codex");
                else box.SetClaim(item.Message.Id, item.Attempt, "native-codex", "uncertain", new() { ["reason"] = "Wake interrupted after submission intent; never replayed" });
            }
            throw;
        }
        foreach (var item in owned)
        {
            string? phase = box.ClaimOf(item.Message.Id, item.Attempt)?["phase"]?.ToString();
            string? outcome = result["status"]?.ToString();
            if (phase == "submitting" && outcome is not ("started" or "uncertain")) { result["status"] = "uncertain"; result["reason"] = "Submission intent persisted but send outcome was not confirmed; never replayed"; }
            if (result["status"]?.ToString() is "started" or "uncertain")
                box.SetClaim(item.Message.Id, item.Attempt, "native-codex", result["status"]!.ToString() == "started" ? "delivered" : "uncertain", new() { ["conversation"] = result["conversation"]?.DeepClone() });
            else box.Release(item.Message.Id, item.Attempt, "native-codex");
        }
        string? status = result["status"]?.ToString();
        int failed = status is "deferred" or "failed" ? (holds[group.Key]?["streak"]?.GetValue<int>() ?? 0) + 1 : 0;
        holds[group.Key] = new JsonObject { ["status"] = status, ["streak"] = failed, ["tried"] = now, ["reason"] = result["reason"]?.DeepClone() };
        result["at"] = RelayBox.NowIso(); result["tried"] = now; result["topic"] = group.Key; result["holds"] = holds.DeepClone(); result["streak"] = failed;
        result["messages"] = new JsonArray(messages.Select(m => JsonValue.Create(m.Id)).ToArray()); box.SetState($"wake-{agent}", result);
        foreach (var message in messages)
        {
            var last = box.Events(message.Id).LastOrDefault(e => e["kind"]?.ToString() == "réveil");
            if (status is not ("deferred" or "failed") || last is null || last["status"]?.ToString() != status || last["reason"]?.ToString() != result["reason"]?.ToString())
                box.Event(message.Id, "réveil", new() { ["status"] = status, ["reason"] = result["reason"]?.DeepClone(), ["via"] = result["via"]?.DeepClone(), ["conversation"] = result["conversation"]?.DeepClone(), ["tried_at"] = now });
            if (status is "started" or "uncertain")
            {
                var old = box.RingState(message.Id);
                box.SetRingState(message.Id, new() { ["agent"] = agent, ["attempt"] = (old?["attempt"]?.GetValue<int>() ?? 0) + 1, ["max"] = 3, ["rung_at"] = RelayBox.NowIso(), ["recheck_at"] = now + 300, ["via"] = result["via"]?.DeepClone(), ["status"] = status });
            }
        }
        return result;
    }
    private JsonObject Wake(string agent, List<RelayMessage> messages, Action beforeSubmit)
    {
        if (agent == "codex") return AppSessions.CodexQuota() is string codexQuota ? AppTransport.Result("deferred", codexQuota) : AppAutomation.WakeCodex(box, messages, beforeSubmit);
        if (AppSessions.AntigravityQuota(box) is string quota) return AppTransport.Result("deferred", quota);
        var sidecar = SidecarHealth(box);
        if (AppTransport.Truth(sidecar["alive"])) return new() { ["status"] = "started", ["via"] = "sidecar", ["confirmed"] = false, ["note"] = "Handed to Antigravity's official agentapi sidecar; track confirms delivery" };
        if (AppTransport.PageUrl("antigravity") is not null) return AppTransport.Result("deferred", "Antigravity open but sidecar heartbeat is stale");
        var launch = AppTransport.EnsureApp("antigravity");
        return AppTransport.Result("deferred", launch["status"]?.ToString() == "ready" ? "Antigravity started; waiting for its registered sidecar" : launch["reason"]?.ToString());
    }
    public static JsonObject SidecarHealth(RelayBox box)
    {
        var beat = box.State("sidecar"); var loop = box.State("sidecar-loop");
        double age = Reservations.Now() - (AppSessions.Epoch(beat?["at"]?.ToString()) ?? 0);
        double loopAge = Reservations.Now() - (AppSessions.Epoch(loop?["at"]?.ToString()) ?? 0);
        bool pid = int.TryParse(beat?["pid"]?.ToString(), out int process) && RelayBox.PidAlive(process);
        return new() { ["alive"] = pid && age < 30 && loopAge < 300, ["pid"] = process, ["pid_alive"] = pid, ["heartbeat_age_s"] = Math.Round(age, 1), ["loop_age_s"] = Math.Round(loopAge, 1), ["runtime"] = beat?["runtime"]?.DeepClone() };
    }
    private void WatchCodex()
    {
        var chats = AppSessions.CodexChats();
        var recent = box.AddressedTo("codex", Reservations.Now() - 6 * 3600).Where(m => CodexSpace.AcceptsCreated(m.Created) && (RelayPaths.RelayHost() == "all" || m.Sender == RelayPaths.RelayHost())).ToList();
        var latest = recent.SelectMany(m => box.Events(m.Id)).Where(e => e["kind"]?.ToString() == "réveil" && e["conversation"] is not null).GroupBy(e => e["conversation"]!.ToString()).ToDictionary(g => g.Key, g => g.Max(e => e["tried_at"]?.GetValue<double>() ?? AppSessions.Epoch(e["at"]?.ToString()) ?? 0));
        foreach (var message in recent.Where(m => !m.Info && !box.Replies(m.Id).Any(r => r.Sender == "codex") && McpServer.HaltedReason(box, m) is null))
        {
            var events = box.Events(message.Id);
            if (!box.IsRead(message) && !events.Any(e => e["kind"]?.ToString() == "en cours")) continue;
            var wake = events.LastOrDefault(e => e["kind"]?.ToString() == "réveil" && e["status"]?.ToString() is "started" or "uncertain");
            if (wake is null || events.Any(e => e["kind"]?.ToString() == "annulé")) continue;
            string? id = wake["conversation"]?.ToString() ?? box.TopicState(message.Topic, "codex")?["conversation"]?.ToString();
            var chat = chats.FirstOrDefault(c => c.Id == id);
            if (chat is null || !CodexSpace.Owns(chat, box)) continue;
            double since = (wake["tried_at"]?.GetValue<double>() ?? AppSessions.Epoch(wake["at"]?.ToString()) ?? 0) - 2;
            var turn = AppSessions.CodexTurn(chat.Path, since); double now = Reservations.Now();
            string? reason = turn.Finished && turn.At is double ended && now - ended >= 5 ? turn.Error ?? "Turn ended without a Relay reply" : !turn.Finished && turn.Last is double last && now - last >= 1200 ? "Turn silent for twenty minutes" : null;
            if (reason is null || box.Events(message.Id).Count != events.Count || box.Replies(message.Id).Any(r => r.Sender == "codex") || AppSessions.CodexTurn(chat.Path, since) != turn) continue;
            box.Event(message.Id, "arrêt sur erreur", new() { ["conversation"] = id, ["reason"] = reason, ["via"] = "journal Codex" });
            var ring = box.RingState(message.Id) ?? new(); ring["agent"] = "codex"; ring["incident"] = reason; ring["incident_at"] = RelayBox.NowIso(); ring["halt"] = true; box.SetRingState(message.Id, ring);
            if (wake["conversation"]?.ToString() == id && box.TopicState(message.Topic, "codex")?["conversation"]?.ToString() == id && latest.GetValueOrDefault(id!) <= since + 3)
                box.SetTopicState(message.Topic, "codex", new() { ["conversation"] = null, ["dropped"] = id, ["at"] = RelayBox.NowIso(), ["why"] = reason });
        }
    }
    public JsonObject Health()
    {
        var ages = new JsonObject(); var missing = new JsonArray(); var stuck = new JsonArray();
        foreach (string name in new[] { "codex", "antigravity", "rangement" })
        {
            if (!workers.TryGetValue(name, out var task) || task.IsCompleted || !beats.TryGetValue(name, out double at)) { missing.Add(name); continue; }
            double age = Reservations.Now() - at; ages[name] = Math.Round(age, 1); if (age > 420) stuck.Add(name);
        }
        return new() { ["ages_s"] = ages, ["missing"] = missing, ["stuck"] = stuck, ["errors"] = new JsonObject(errors.Select(e => new KeyValuePair<string, JsonNode?>(e.Key, JsonValue.Create(e.Value)))) };
    }
    public JsonObject HealthResponse()
    {
        var health = Health(); bool ok = (health["missing"] as JsonArray)?.Count == 0 && (health["stuck"] as JsonArray)?.Count == 0 && (health["errors"] as JsonObject)?.Count == 0;
        return new() { ["status"] = ok ? "ok" : "degraded", ["service"] = "nexus-relay", ["runtime"] = "native", ["version"] = RelayPaths.Version, ["pid"] = Environment.ProcessId, ["host"] = RelayPaths.RelayHost(), ["workers"] = health, ["activities"] = RelayActivity.Snapshot() };
    }
    public void Dispose() { if (Interlocked.Exchange(ref disposed, 1) != 0) return; stop.Cancel(); try { Task.WaitAll(workers.Values.ToArray(), 15_000); } catch (AggregateException) { /* Workers end on cancellation; disposal goes on. */ } stop.Dispose(); }
}
