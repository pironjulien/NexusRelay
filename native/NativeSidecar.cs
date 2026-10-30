using System.Diagnostics;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusRelay;

public sealed class AgentApiFailure(string message, bool uncertain = false) : Exception(message)
{
    public bool Uncertain { get; } = uncertain;
}

public sealed class NativeAgentApi
{
    private readonly List<string> command;
    public NativeAgentApi(List<string>? command = null) { this.command = command ?? Resolve(); }
    public static List<string> Resolve()
    {
        if (Environment.GetEnvironmentVariable("NEXUS_RELAY_AGENTAPI") is string configured)
        {
            var parts = JsonNode.Parse(configured) as JsonArray ?? throw new AgentApiFailure("NEXUS_RELAY_AGENTAPI must be a JSON array");
            if (parts.Count == 0 || parts.Any(p => p is not JsonValue v || !v.TryGetValue<string>(out _))) throw new AgentApiFailure("Invalid agentapi command");
            return parts.Select(p => p!.GetValue<string>()).ToList();
        }
        string? candidate = Environment.GetEnvironmentVariable("ANTIGRAVITY_AGENTAPI_EXE");
        if (candidate is null)
            foreach (string dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(System.IO.Path.PathSeparator))
            {
                candidate = new[] { "agentapi.exe", "agentapi.bat", "agentapi.cmd" }.Select(name => System.IO.Path.Combine(dir, name)).FirstOrDefault(File.Exists);
                if (candidate is not null) break;
            }
        if (candidate is null) throw new AgentApiFailure("agentapi not found: run as Antigravity's registered sidecar");
        if (System.IO.Path.GetExtension(candidate).Equals(".bat", StringComparison.OrdinalIgnoreCase) || System.IO.Path.GetExtension(candidate).Equals(".cmd", StringComparison.OrdinalIgnoreCase))
        {
            foreach (string line in File.ReadLines(candidate))
            {
                var match = Regex.Match(line, "^\\s*\"([^\"]+\\.exe)\"\\s+([A-Za-z0-9_-]+)\\s+%\\*\\s*$", RegexOptions.IgnoreCase, TimeSpan.FromSeconds(1));
                if (match.Success) return [match.Groups[1].Value, match.Groups[2].Value];
            }
            throw new AgentApiFailure("agentapi wrapper executable not recognized; shell wrappers are never executed");
        }
        return System.IO.Path.GetFileName(candidate).Equals("language_server.exe", StringComparison.OrdinalIgnoreCase) ? [candidate, "agentapi"] : [candidate];
    }
    public JsonObject Run(params string[] arguments)
    {
        var start = new ProcessStartInfo(command[0]) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8 };
        foreach (string part in command.Skip(1).Concat(arguments)) start.ArgumentList.Add(part);
        Process process;
        try { process = Process.Start(start) ?? throw new IOException("agentapi process not created"); }
        catch (Exception e) when (e is IOException or System.ComponentModel.Win32Exception or ArgumentException) { throw new AgentApiFailure(e.Message); }
        using (process)
        {
            Task<string> output = process.StandardOutput.ReadToEndAsync(), errors = process.StandardError.ReadToEndAsync();
            if (!process.WaitForExit(120_000))
            {
                try { process.Kill(entireProcessTree: true); } catch (InvalidOperationException) { }
                throw new AgentApiFailure("agentapi timed out; delivery may have been accepted", uncertain: true);
            }
            if (!Task.WaitAll([output, errors], 10_000)) throw new AgentApiFailure("agentapi response pipe did not close", uncertain: true);
            if (process.ExitCode != 0) throw new AgentApiFailure($"agentapi {arguments[0]} failed ({process.ExitCode}): {Short(errors.Result)}");
            string raw = output.Result; int begin = raw.IndexOf('{');
            JsonObject result;
            try { result = begin >= 0 ? JsonNode.Parse(raw[begin..]) as JsonObject ?? throw new System.Text.Json.JsonException() : throw new System.Text.Json.JsonException(); }
            catch (System.Text.Json.JsonException) { throw new AgentApiFailure("agentapi response unreadable; delivery may have been accepted", uncertain: true); }
            if (result["error"] is JsonNode error) throw new AgentApiFailure(Short(error.ToString()));
            return result;
        }
    }
    public string Create(string prompt, string title)
    {
        var response = Run("new-conversation", $"--title={Short(title, 80)}", "--", prompt);
        string? conversation = response["response"]?["newConversation"]?["conversationId"]?.ToString();
        if (!RelayPaths.IsConversationId(conversation)) throw new AgentApiFailure("agentapi did not return a conversation identifier", uncertain: true);
        return conversation!;
    }
    public void Send(string conversation, string prompt, string title)
    {
        if (!RelayPaths.IsConversationId(conversation)) throw new AgentApiFailure("Invalid Antigravity conversation identifier");
        Run("send-message", $"--title={Short(title, 144)}", conversation, prompt);
    }
    private static string Short(string text, int max = 618) => new(text.Select(c => char.IsControl(c) ? ' ' : c).Take(max).ToArray());
}

public sealed class NativeSidecar(RelayBox box, NativeAgentApi api)
{
    private readonly Dictionary<string, double> failures = new();
    private bool recovered;
    public static string Prompt(IReadOnlyList<RelayMessage> messages)
    {
        string asks = AppAutomation.WakeText(messages.Where(m => !m.Info).Select(m => m.Id));
        string infos = string.Join(", ", messages.Where(m => m.Info).Select(m => $"read(id=\"{m.Id}\")"));
        return asks + (infos.Length == 0 ? "" : $" Pour info (sans réponse) : {infos}.") + " Serveur nexus-relay.";
    }
    public static int Attempt(RelayBox box, RelayMessage message) => Math.Max(1, box.RingState(message.Id)?["attempt"]?.GetValue<int>() ?? 0);
    public List<JsonObject> Step()
    {
        if (!recovered)
        {
            foreach (var (id, attempt, claim) in box.AllClaims())
                if (claim["owner"]?.ToString() == "sidecar" && RelayBox.ClaimLabel(claim) == "uncertain")
                { box.SetClaim(id, attempt, "sidecar", "uncertain", new() { ["reason"] = "Previous sidecar stopped during submission" }); box.Event(id, "livraison incertaine", new() { ["via"] = "sidecar", ["attempt"] = attempt }); }
            recovered = true;
        }
        box.SetState("sidecar-loop", new() { ["at"] = RelayBox.NowIso(), ["pid"] = Environment.ProcessId, ["runtime"] = "native" });
        if (AppTransport.EmergencyStop()) return [];
        if (AppSessions.AntigravityQuota(box) is string reason) { box.SetState("sidecar-paused", new() { ["at"] = RelayBox.NowIso(), ["reason"] = reason }); return []; }
        var pending = box.Unread("antigravity").Where(m =>
        {
            if ((RelayPaths.RelayHost() != "all" && m.Sender != RelayPaths.RelayHost()) || !CodexSpace.AcceptsCreated(m.Created)) return false;
            foreach (var (attempt, claim) in box.Claims(m.Id))
            {
                if (RelayBox.ClaimLabel(claim) is "uncertain" or "submitting") return false;
                box.RecoverDeadClaim(m.Id, attempt);
            }
            return box.ClaimOf(m.Id, Attempt(box, m)) is null && !(Attempt(box, m) == 1 && box.HasEvent(m.Id, "livré"));
        }).GroupBy(m => m.Topic);
        var results = new List<JsonObject>();
        foreach (var topic in pending)
        {
            if (!topic.Any(m => !m.Info) || topic.Any(m => failures.TryGetValue(m.Id, out double at) && Reservations.Now() - at < 20)) continue;
            results.Add(Deliver(topic.Key, topic.ToList()));
        }
        WatchErrors();
        return results;
    }
    public JsonObject Deliver(string topic, List<RelayMessage> messages)
    {
        var owned = messages.Select(m => (Message: m, Attempt: Attempt(box, m))).Where(item => box.Claim(item.Message.Id, item.Attempt, "sidecar")).ToList();
        if (!owned.Any(item => !item.Message.Info)) { foreach (var item in owned) box.Release(item.Message.Id, item.Attempt, "sidecar"); return AppTransport.Result("skipped", "Delivery already owned"); }
        if (owned.Any(item => box.Claims(item.Message.Id).Any(c => c.Attempt != item.Attempt && RelayBox.ClaimLabel(c.Claim) is "uncertain" or "submitting")))
        { foreach (var item in owned) box.Release(item.Message.Id, item.Attempt, "sidecar"); return AppTransport.Result("skipped", "A previous delivery is uncertain"); }
        string? conversation = box.TopicState(topic, "agy")?["conversation"]?.ToString();
        string? old = conversation;
        try
        {
            foreach (var item in owned) box.SetClaim(item.Message.Id, item.Attempt, "sidecar", "submitting", new() { ["submitted"] = RelayBox.NowIso() });
            if (conversation is not null && (!RelayPaths.ConversationAlive(conversation) || AppSessions.AntigravityError(conversation) is not null || AppSessions.AntigravityTurn(conversation).Error is not null)) conversation = null;
            if (conversation is not null && AppSessions.AntigravityTurn(conversation) is { Finished: false, At: not null } stalled && Reservations.Now() - stalled.At > 30 * 60) conversation = null;
            string prompt = Prompt(owned.Select(item => item.Message).ToList());
            string title = $"{messages[0].Sender} : {messages[0].Text.Split('\n')[0]}";
            if (conversation is not null)
            {
                try { api.Send(conversation, prompt, title); }
                catch (AgentApiFailure e) when (!e.Uncertain) { conversation = null; }
            }
            if (conversation is null)
            {
                conversation = api.Create(prompt, $"Nexus Relay · {topic}");
                box.SetTopicState(topic, "agy", new() { ["conversation"] = conversation, ["created"] = RelayBox.NowIso() });
                if (old is not null && old != conversation)
                {
                    var retired = box.TopicState(topic, "agy_retired")?["conversations"] as JsonArray ?? [];
                    retired.Add(old); box.SetTopicState(topic, "agy_retired", new() { ["conversations"] = retired.DeepClone() });
                }
            }
            foreach (var item in owned)
            {
                box.SetClaim(item.Message.Id, item.Attempt, "sidecar", "delivered", new() { ["conversation"] = conversation });
                box.Event(item.Message.Id, "livré", new() { ["conversation"] = conversation, ["via"] = "sidecar", ["attempt"] = item.Attempt }); failures.Remove(item.Message.Id);
            }
            return new() { ["status"] = "delivered", ["conversation"] = conversation };
        }
        catch (Exception exception)
        {
            // Only a known API rejection is replayable. Unexpected failures after the claim remain uncertain.
            bool uncertain = exception is not AgentApiFailure failure || failure.Uncertain;
            foreach (var item in owned)
            {
                if (uncertain) box.SetClaim(item.Message.Id, item.Attempt, "sidecar", "uncertain", new() { ["reason"] = exception.Message });
                else { box.Release(item.Message.Id, item.Attempt, "sidecar"); failures[item.Message.Id] = Reservations.Now(); }
                box.Event(item.Message.Id, uncertain ? "livraison incertaine" : "livraison ratée", new() { ["via"] = "sidecar", ["attempt"] = item.Attempt, ["reason"] = exception.Message });
            }
            return AppTransport.Result(uncertain ? "uncertain" : "failed", exception.Message);
        }
    }
    private void WatchErrors()
    {
        foreach (var (id, _, claim) in box.AllClaims())
        {
            if (claim["owner"]?.ToString() != "sidecar" || RelayBox.ClaimLabel(claim) != "delivered" || claim["conversation"]?.ToString() is not string conversation || box.HasEvent(id, "arrêt sur erreur") || box.Replies(id).Any(m => m.Sender == "antigravity")) continue;
            var message = box.Get(id);
            if (message is null || !CodexSpace.AcceptsCreated(message.Created) || RelayPaths.RelayHost() != "all" && message.Sender != RelayPaths.RelayHost()) continue;
            var turn = AppSessions.AntigravityError(conversation) ?? AppSessions.AntigravityTurn(conversation);
            double since = AppSessions.Epoch(claim["submitted"]?.ToString()) ?? 0;
            if (Reservations.Now() - since > 6 * 3600) continue;
            if (turn.Error is not null && (turn.At is null || turn.At >= since - 5)) box.Event(id, "arrêt sur erreur", new() { ["conversation"] = conversation, ["reason"] = turn.Error, ["via"] = "sidecar" });
        }
    }
    public static int Run()
    {
        using var instance = DaemonSupervisor.AcquireLock(Path.Combine(RelayPaths.RelayHome(), "sidecar-native.lock"));
        if (instance is null) return 0;
        var sidecar = new NativeSidecar(new(), new());
        RelayPaths.MarkRestartable("sidecar");
        string loaded = RelayPaths.CodeFingerprint();
        int? hostPid = Reservations.AncestorPid("antigravity.exe");
        using Process? appHost = hostPid is int parent ? Process.GetProcessById(parent) : null;
        using var cancelled = new CancellationTokenSource();
        var beat = Task.Run(async () =>
        {
            while (!cancelled.IsCancellationRequested)
            {
                try { new RelayBox().SetState("sidecar", new() { ["at"] = RelayBox.NowIso(), ["pid"] = Environment.ProcessId, ["runtime"] = "native" }); }
                catch (IOException e) { Console.Error.WriteLine(e.Message); }
                try { await Task.Delay(TimeSpan.FromSeconds(5), cancelled.Token); } catch (OperationCanceledException) { }
            }
        });
        try
        {
            while (true)
            {
                try { sidecar.Step(); } catch (Exception e) { Console.Error.WriteLine($"sidecar: {e.Message}"); }
                if (RelayPaths.RestartWanted(Path.Combine(RelayPaths.RelayHome(), "sidecar-restart.request"), loaded)) return 0;
                if (appHost?.HasExited == true) return 0;
                Thread.Sleep(1000);
            }
        }
        finally { cancelled.Cancel(); beat.GetAwaiter().GetResult(); }
    }
}
