using System.Collections.Concurrent;
using System.IO.Pipes;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace NexusRelay;

/// <summary>What a Claude session did with a wake, as the session itself reports it. Claude Code delivers a peer message
/// silently, but answers the sender when it holds or refuses one: a <c>peer_message_status</c> control line (held,
/// delivered after a hold, denied, expired, refused, dropped) written to the reply address the message named, a local
/// named pipe that Claude Code authenticates with the key published next to the session registry
/// (<c>&lt;pid&gt;.&lt;sha256 of the pipe path&gt;.key</c>). This class is that reply address: one pipe and one key for the
/// engine's lifetime, never Anthropic's servers. Observed on Claude Code 2.1.330 (isolated test sessions, 28/11/2026):
/// accepted, nothing comes back; refused or held, the line arrives within milliseconds.</summary>
public sealed class ClaudeReceipts : IDisposable
{
    /// <summary>Status: "held", "refused" or "delivered" (a held message the user then let through). Detail: Claude
    /// Code's own word when the message was not delivered (refused, denied, expired, or the drop reason).</summary>
    public sealed record Verdict(string Status, string? Detail, string? Reason);

    private sealed class Wake(string[] ids, double at)
    {
        public readonly string[] Ids = ids;
        public readonly double At = at;
        public Verdict? First;
        public bool Returned;
    }

    private const int MaxFrameBytes = 64 * 1024;
    public const string Owner = "nexus-relay";
    private readonly string pipe, token, keyFile;
    private readonly Action<IReadOnlyList<string>, Verdict>? late;
    private readonly CancellationTokenSource stop = new();
    private readonly ConcurrentDictionary<string, Wake> wakes = new();
    private readonly Task listener;
    private int disposed;

    /// <summary>How long a wake waits for the session's answer before it is reported as written (the answer was
    /// measured within milliseconds); a later answer goes to the callback given to <see cref="Start"/>.</summary>
    private readonly TimeSpan grace;

    /// <summary>The reply address written in the wake: "uds:\\.\pipe\LOCAL\cc-msg-…".</summary>
    public string Address => @"uds:\\.\pipe\" + pipe;

    private ClaudeReceipts(string sessions, Action<IReadOnlyList<string>, Verdict>? late, TimeSpan grace)
    {
        this.late = late; this.grace = grace;
        pipe = @"LOCAL\cc-msg-" + Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(16));
        token = Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(16));
        keyFile = Path.Join(sessions, KeyFileName(Environment.ProcessId, pipe));
        var first = Listen();
        try { File.WriteAllText(keyFile, new JsonObject { ["peerToken"] = token, ["owner"] = Owner }.ToJsonString()); }
        catch { first.Dispose(); throw; }
        listener = Task.Run(() => Accept(first));
    }

    /// <summary>Opens the reply pipe and publishes its key in Claude Code's session registry; null when either fails
    /// (the wake then goes out without a reply address, as before 1.17.0).</summary>
    public static ClaudeReceipts? Start(string sessions, Action<IReadOnlyList<string>, Verdict>? late = null, TimeSpan? grace = null)
    {
        if (!Directory.Exists(sessions)) return null;
        try { Sweep(sessions); return new(sessions, late, grace ?? TimeSpan.FromSeconds(1)); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { Console.Error.WriteLine($"Claude receipts unavailable: {e.Message}"); return null; }
    }

    /// <summary>Claude Code names a session's key after the canonical pipe path: "\\.\pipe\" + the lower-cased name.</summary>
    public static string KeyFileName(int pid, string pipeName) =>
        $"{pid}.{Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(@"\\.\pipe\" + pipeName.ToLowerInvariant())))}.key";

    /// <summary>Removes the keys an earlier engine left when it was killed: ours only (marked), and only of a dead process.</summary>
    private static void Sweep(string sessions)
    {
        foreach (string file in Directory.EnumerateFiles(sessions, "*.key"))
        {
            try
            {
                if (!int.TryParse(Path.GetFileName(file).Split('.')[0], out int pid) || pid == Environment.ProcessId || RelayBox.PidAlive(pid)) continue;
                if (new FileInfo(file).Length > 4096 || (JsonNode.Parse(File.ReadAllText(file)) as JsonObject)?["owner"]?.ToString() != Owner) continue;
                File.Delete(file);
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException or System.Text.Json.JsonException) { /* Boundary: a key in use or being rewritten is left for the next start. */ }
        }
    }

    private NamedPipeServerStream Listen() =>
        new(pipe, PipeDirection.InOut, NamedPipeServerStream.MaxAllowedServerInstances, PipeTransmissionMode.Byte, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);

    private async Task Accept(NamedPipeServerStream server)
    {
        try
        {
            while (true)
            {
                await server.WaitForConnectionAsync(stop.Token).ConfigureAwait(false);
                var connected = server;
                _ = Task.Run(() => Serve(connected));
                server = Listen();
            }
        }
        catch (Exception e) when (e is OperationCanceledException or IOException or ObjectDisposedException) { /* Stopping, or the pipe broke: no more receipts; wakes stay reported as written. */ }
        finally { server.Dispose(); }
    }

    private async Task Serve(NamedPipeServerStream connection)
    {
        using var _ = connection;
        try
        {
            using var limit = CancellationTokenSource.CreateLinkedTokenSource(stop.Token);
            limit.CancelAfter(TimeSpan.FromSeconds(5));
            var buffer = new byte[MaxFrameBytes]; int length = 0;
            while (length < buffer.Length)
            {
                int read = await connection.ReadAsync(buffer.AsMemory(length), limit.Token).ConfigureAwait(false);
                if (read == 0) break;
                length += read;
            }
            Accept(Encoding.UTF8.GetString(buffer, 0, length));
        }
        catch (Exception e) when (e is OperationCanceledException or IOException or ObjectDisposedException) { /* Boundary: a silent or broken connection carries no receipt. */ }
    }

    /// <summary>One connection's text: the auth line with this pipe's token, then control lines. Anything else is dropped.</summary>
    public void Accept(string text)
    {
        string[] lines = text.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        if (lines.Length < 2 || Json(lines[0]) is not JsonObject auth || auth["type"]?.ToString() != "auth") return;
        byte[] given = Encoding.UTF8.GetBytes(auth["token"]?.ToString() ?? "");
        if (!CryptographicOperations.FixedTimeEquals(given, Encoding.UTF8.GetBytes(token))) return;
        foreach (string line in lines.Skip(1))
        {
            if (Json(line) is not JsonObject frame || Parse(frame) is not Verdict verdict) continue;
            if (frame["orig_msg_id"]?.ToString() is not string id || !wakes.TryGetValue(id, out var wake)) continue;
            bool isLate;
            lock (wake)
            {
                isLate = wake.Returned || wake.First is not null;
                if (!isLate) { wake.First = verdict; Monitor.PulseAll(wake); }
            }
            if (isLate) late?.Invoke(wake.Ids, verdict);
        }
    }

    private static JsonObject? Json(string line)
    {
        try { return JsonNode.Parse(line) as JsonObject; }
        catch (System.Text.Json.JsonException) { return null; }
    }

    /// <summary>A <c>peer_message_status</c> control line read as a verdict; null for any other line.</summary>
    public static Verdict? Parse(JsonObject frame)
    {
        if (frame["type"]?.ToString() != "control" || frame["action"]?.ToString() != "peer_message_status") return null;
        string? reason = frame["reason"]?.ToString();
        if (reason?.Length > 300) reason = reason[..300];
        return frame["status"]?.ToString() switch
        {
            "held" => new("held", null, reason),
            "delivered" => new("delivered", null, reason),
            "refused" => new("refused", "refused", reason),
            "denied" => new("refused", "denied", reason),
            "expired" => new("refused", frame["status_detail"]?.ToString() == "refused" ? "refused" : "expired", reason),
            "dropped" => new("refused", frame["drop_reason"]?.ToString() is { Length: > 0 and <= 40 } why ? why : "dropped", reason),
            _ => null,
        };
    }

    /// <summary>Registers a wake about to be written and returns the id to send as its <c>msg_id</c>.</summary>
    public string Expect(IEnumerable<string> messageIds)
    {
        double now = Reservations.Now();
        foreach (var old in wakes.Where(w => now - w.Value.At > 2 * 3600)) wakes.TryRemove(old.Key, out _); // A request expires after one hour; so does interest in its receipt.
        string id = Guid.NewGuid().ToString("D");
        wakes[id] = new(messageIds.ToArray(), now);
        return id;
    }

    public void Forget(string wakeId) => wakes.TryRemove(wakeId, out _);

    /// <summary>The session's answer within the grace period, or null: nothing came back, which is what an accepted
    /// message looks like. Answers after this call go to the callback.</summary>
    public Verdict? Wait(string wakeId)
    {
        if (!wakes.TryGetValue(wakeId, out var wake)) return null;
        lock (wake)
        {
            if (wake.First is null) Monitor.Wait(wake, grace);
            wake.Returned = true;
            return wake.First;
        }
    }

    public void Dispose()
    {
        if (Interlocked.Exchange(ref disposed, 1) != 0) return;
        stop.Cancel();
        try { listener.Wait(2000); } catch (AggregateException) { /* The listener ends on cancellation. */ }
        try { File.Delete(keyFile); } catch (Exception e) when (e is IOException or UnauthorizedAccessException) { /* Left for the next start's sweep. */ }
        stop.Dispose();
    }
}
