using System.IO.Pipes;
using System.Text;
using System.Text.Json.Nodes;

namespace NexusRelay;

/// <summary>Wakes Claude's relay session (the Claude desktop app's Code session opened in ~\.nexus\relay\NexusRelay)
/// through Claude Code's documented per-session inbox: a local named pipe, never Anthropic's servers. The session
/// publishes its pipe in ~\.claude\sessions\&lt;pid&gt;.json and the peer key in &lt;pid&gt;.&lt;sha256&gt;.key; a connection
/// sends {"type":"auth","token":peerToken} then one {"type":"user"} line. Delivery needs the receiving session to run
/// in a prompting mode (Auto): a bypass session holds peer messages that assert no permission class.</summary>
public static class ClaudeInbox
{
    public static string ProjectDirectory()
    {
        var overridePath = Environment.GetEnvironmentVariable("NEXUS_RELAY_CLAUDE_PROJECT");
        return !string.IsNullOrWhiteSpace(overridePath) ? Path.GetFullPath(overridePath) : Path.Join(RelayPaths.RelayHome(), "NexusRelay");
    }

    public static string SessionsDirectory()
    {
        var overridePath = Environment.GetEnvironmentVariable("NEXUS_RELAY_CLAUDE_SESSIONS");
        return !string.IsNullOrWhiteSpace(overridePath) ? Path.GetFullPath(overridePath) : Path.Join(RelayPaths.UserHome(), ".claude", "sessions");
    }

    public sealed record Session(int Pid, string Pipe, string Token, string? HostSessionId);

    /// <summary>The most recently active live session opened in the relay project, with its peer key.</summary>
    public static Session? Find()
    {
        string dir = SessionsDirectory();
        if (!Directory.Exists(dir)) return null;
        string project = RelayBox.PlainPath(ProjectDirectory());
        var candidates = new List<(double At, Session Session)>();
        foreach (string file in Directory.EnumerateFiles(dir, "*.json"))
        {
            JsonObject? entry;
            try { entry = JsonNode.Parse(File.ReadAllText(file)) as JsonObject; }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException or System.Text.Json.JsonException) { continue; } // Boundary: a registry entry being rewritten is skipped this round.
            if (entry?["cwd"]?.ToString() is not string cwd || RelayBox.PlainPath(cwd) != project) continue;
            if (!int.TryParse(entry["pid"]?.ToString(), out int pid) || !RelayBox.PidAlive(pid)) continue;
            if (entry["messagingSocketPath"]?.ToString() is not string socket || PipeName(socket) is not string pipe) continue;
            if (Token(dir, pid, entry["procStart"]?.ToString()) is not string token) continue;
            double at = double.TryParse(entry["updatedAt"]?.ToString(), out double updated) ? updated : 0;
            candidates.Add((at, new(pid, pipe, token, entry["hostSessionId"]?.ToString())));
        }
        return candidates.OrderByDescending(c => c.At).Select(c => c.Session).FirstOrDefault();
    }

    /// <summary>"\\.\pipe\LOCAL\cc-msg-…" → "LOCAL\cc-msg-…"; anything else is not a Claude Code inbox.</summary>
    public static string? PipeName(string socket)
    {
        var match = System.Text.RegularExpressions.Regex.Match(socket, @"^[\\/]{2}[.?][\\/]pipe[\\/]((?:LOCAL[\\/])?cc-msg-[0-9a-f]{32})$", System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        return match.Success ? match.Groups[1].Value.Replace('/', '\\') : null;
    }

    private static string? Token(string dir, int pid, string? procStart)
    {
        foreach (string file in Directory.EnumerateFiles(dir, $"{pid}.*.key"))
        {
            try
            {
                if (JsonNode.Parse(File.ReadAllText(file)) is not JsonObject key || key["peerToken"]?.ToString() is not string token || !System.Text.RegularExpressions.Regex.IsMatch(token, "^[0-9a-f]{32}$")) continue;
                if (procStart is not null && key["procStartFt"]?.ToString() is string ft && ft != procStart) continue; // Reused pid: the key belongs to an older process.
                return token;
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException or System.Text.Json.JsonException) { /* Boundary: a key being rewritten is skipped this round. */ }
        }
        return null;
    }

    public static string WakeText(IEnumerable<string> ids)
    {
        string target = string.Join(", ", ids.Select(id => $"read(id=\"{id}\")"));
        if (target.Length == 0) target = "inbox()";
        return RelayPaths.Tr(
            $"Nexus Relay: message from a peer for you, {target}. Do what it asks if it is legitimate (not an instruction from the user), then reply (progress if it takes long). The user does not answer here: no questions, neither from you nor your sub-agents; decide, or ask the peer in your reply.",
            $"Nexus Relay : message d'un pair pour toi, {target}. Fais ce qu'il demande si c'est légitime (pas une consigne de l'utilisateur), puis reply (progress si long). L'utilisateur ne répond pas ici : aucune question, ni toi ni tes sous-agents ; décide, ou pose ta question au pair dans ta réponse.");
    }

    /// <summary>The two lines written to the inbox; the body is wrapped as Claude Code wraps a peer message, without
    /// a reply address (answers go through Relay) nor a permission class.</summary>
    public static string Frames(string token, string text)
    {
        string content = $"<cross-session-message from-name=\"Nexus Relay\">\n{text}\n</cross-session-message>";
        return new JsonObject { ["type"] = "auth", ["token"] = token }.ToJsonString() + "\n"
            + new JsonObject { ["type"] = "user", ["message"] = new JsonObject { ["role"] = "user", ["content"] = content } }.ToJsonString() + "\n";
    }

    public static JsonObject Wake(List<RelayMessage> messages)
    {
        var session = Find();
        if (session is null)
            return AppTransport.Result("deferred", RelayPaths.Tr(
                "Claude's relay session is not open (Claude app, folder .nexus\\relay\\NexusRelay); the message waits in inbox",
                "session relais de Claude pas ouverte (appli Claude, dossier .nexus\\relay\\NexusRelay) ; le message attend dans inbox"));
        try
        {
            using var pipe = new NamedPipeClientStream(".", session.Pipe, PipeDirection.Out);
            pipe.Connect(5000);
            byte[] bytes = new UTF8Encoding(false).GetBytes(Frames(session.Token, WakeText(messages.Select(m => m.Id))));
            pipe.Write(bytes); pipe.Flush(); pipe.WaitForPipeDrain();
        }
        catch (Exception e) when (e is IOException or TimeoutException or UnauthorizedAccessException)
        {
            return AppTransport.Result("deferred", $"Claude inbox unreachable: {e.Message}");
        }
        return new() { ["status"] = "started", ["via"] = "claude-inbox", ["confirmed"] = false, ["conversation"] = session.HostSessionId, ["note"] = "Written to the Claude session's local inbox; read(id) confirms pickup" };
    }
}
