using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusRelay;

/// <summary>Claude Code `Stop` hook run with asyncRewake: once the host's turn is over, waits in the background for
/// the answers to the requests this conversation sent and still has open, then exits 2 so Claude Code wakes the
/// conversation with them (the host no longer blocks on wait_reply). The requests are read from the conversation's
/// transcript (results of the send tool); a newer turn end of the same conversation takes over the wait.</summary>
public static partial class HostRewake
{
    public const double MaxWaitSeconds = 55 * 60;   // the hook's timeout in hooks.json is 3600 s
    public const double PartialAfterSeconds = 10 * 60;
    public const double MaxAgeSeconds = 6 * 3600;
    private const int MaxShown = 6000;

    [GeneratedRegex(@"\d{8}T\d{9}-[0-9a-f]{6}", RegexOptions.Compiled)]
    private static partial Regex IdRegex();

    // A message shown to the conversation (wait_reply, read, inbox render it as "id": "…"; a wake names "reply …"),
    // not one only listed by its id (track's "replies": ["…"]).
    [GeneratedRegex(@"(?:(?<![A-Za-z_])id(?:\x5C*""|\x5Cu0022)\s*:\s*(?:\x5C*""|\x5Cu0022)|\breply )(\d{8}T\d{9}-[0-9a-f]{6})", RegexOptions.Compiled)]
    private static partial Regex ShownRegex();

    public static int Run(string[] args)
    {
        try
        {
            int idx = Array.IndexOf(args, "--await-replies");
            string me = RelayPaths.NormalizeAgent(idx + 1 < args.Length ? args[idx + 1] : "claude");
            if (me != RelayPaths.RelayHost())
                return 0;
            var entry = JsonNode.Parse(Console.In.ReadToEnd()) as JsonObject ?? new JsonObject();
            string session = new((entry["session_id"]?.ToString() ?? "").Where(c => char.IsAsciiLetterOrDigit(c) || c is '-' or '_').Take(80).ToArray());
            string? transcript = entry["transcript_path"]?.ToString();
            if (session.Length == 0 || string.IsNullOrEmpty(transcript))
                return 0;
            var box = new RelayBox();
            string turn = RelayBox.RandomHex(6);
            string lease = Path.Join(box.Root, "rewake", session + ".json");
            // Answers already brought to this conversation by an earlier wake, in case its transcript does not keep them.
            var delivered = (RelayBox.ReadJson(lease)?["delivered"] as JsonArray ?? []).Select(n => n?.ToString() ?? "").ToHashSet(StringComparer.Ordinal);
            RelayBox.WriteState(lease, new JsonObject { ["turn"] = turn, ["pid"] = Environment.ProcessId, ["at"] = RelayBox.NowIso(), ["delivered"] = new JsonArray(delivered.Order().Select(d => (JsonNode)d).ToArray()) });
            bool Superseded() => RelayBox.ReadJson(lease)?["turn"]?.ToString() != turn;

            // The transcript is written asynchronously: the last sends of the turn may land a moment after Stop.
            var (sent, seen) = Conversation(transcript);
            seen.UnionWith(delivered);
            var pending = Pending(box, me, sent, seen);
            for (int i = 0; i < 10 && pending.Count == 0; i++)
            {
                Thread.Sleep(1000);
                (sent, seen) = Conversation(transcript);
                seen.UnionWith(delivered);
                pending = Pending(box, me, sent, seen);
            }
            if (pending.Count == 0)
                return 0;

            double started = Reservations.Now();
            double? firstAnswer = null;
            while (true)
            {
                if (Superseded())
                    return 0;
                var answered = pending.Where(m => AnswerOf(box, m) is not null).ToList();
                var closed = pending.Where(m => !answered.Contains(m) && (box.HasEvent(m.Id, McpServer.CancelledKind) || McpServer.HaltedReason(box, m) is not null)).ToList();
                if (answered.Any(m => !AnswerOf(box, m)!.Info))
                    firstAnswer ??= Reservations.Now();   // an info answer closes its request silently: no partial wake
                double now = Reservations.Now();
                bool all = answered.Count + closed.Count == pending.Count;
                bool partial = firstAnswer is double first && now - first >= PartialAfterSeconds;
                bool timeout = now - started >= MaxWaitSeconds;
                if (all || partial || timeout)
                {
                    var known = Conversation(transcript).Seen;
                    known.UnionWith(delivered);
                    if (Superseded())
                        return 0;   // a newer turn end took the wait over while the transcript was read
                    int code = Wake(box, pending, answered, closed, timeout, known);
                    if (code == 2 && !Superseded())
                    {
                        delivered.UnionWith(answered.Select(m => AnswerOf(box, m)!.Id));
                        RelayBox.WriteState(lease, new JsonObject { ["turn"] = turn, ["at"] = RelayBox.NowIso(), ["delivered"] = new JsonArray(delivered.Order().TakeLast(200).Select(d => (JsonNode)d).ToArray()) });
                    }
                    var rest = pending.Except(answered).Except(closed).ToList();
                    if (code == 2 || timeout || rest.Count == 0)
                        return code;
                    pending = rest;   // nothing to show yet (answers already seen): keep waiting for the others
                    firstAnswer = null;
                }
                Thread.Sleep(2000);
            }
        }
        catch (Exception e) // Boundary: a hook never breaks the conversation; without a wake the host uses wait_reply.
        {
            Console.Error.WriteLine($"NexusRelay await-replies: {e.Message}");
            return 0;
        }
    }

    /// <summary>Ids returned by the relay's send tool in this conversation, and the messages the conversation was shown
    /// (rendered by a tool or brought by a wake). Seen is per conversation: an answer marked read by another session's inbox
    /// still wakes the conversation that asked.</summary>
    public static (List<string> Sent, HashSet<string> Seen) Conversation(string transcript)
    {
        var ids = new List<string>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        if (!File.Exists(transcript))
            return (ids, seen);
        var sends = new HashSet<string>(StringComparer.Ordinal);
        using var stream = new FileStream(transcript, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        using var reader = new StreamReader(stream, Encoding.UTF8);
        while (reader.ReadLine() is string line)
        {
            // Shown = delivered by a tool result or a wake; a tool call's arguments (read(id)) or a failed result are not.
            if ((line.Contains("id", StringComparison.Ordinal) || line.Contains("reply ", StringComparison.Ordinal))
                && !line.StartsWith("{\"type\":\"assistant\"", StringComparison.Ordinal) && !line.Contains("\"type\":\"tool_use\"", StringComparison.Ordinal)
                && !line.Contains("\"is_error\":true", StringComparison.Ordinal))
                seen.UnionWith(ShownRegex().Matches(line).Select(m => m.Groups[1].Value));
            bool call = line.Contains("nexus-relay__send\"", StringComparison.Ordinal);
            bool result = sends.Count > 0 && line.Contains("tool_result", StringComparison.Ordinal);
            if (!call && !result)
                continue;
            JsonNode? node;
            try
            {
                node = JsonNode.Parse(line);
            }
            catch (System.Text.Json.JsonException)
            {
                continue;   // the line being written (the transcript is appended asynchronously): read again later
            }
            if (node is not JsonObject row || row["message"]?["content"] is not JsonArray content)
                continue;
            foreach (var part in content.OfType<JsonObject>())
            {
                if (part["type"]?.ToString() == "tool_use" && part["name"]?.ToString() is string name && name.EndsWith("nexus-relay__send", StringComparison.Ordinal))
                    sends.Add(part["id"]?.ToString() ?? "");
                else if (part["type"]?.ToString() == "tool_result" && sends.Contains(part["tool_use_id"]?.ToString() ?? "\0"))
                    ids.AddRange(IdRegex().Matches(part["content"]?.ToJsonString() ?? "").Select(m => m.Value));
            }
        }
        return (ids.Distinct(StringComparer.Ordinal).ToList(), seen);
    }

    private static RelayMessage? AnswerOf(RelayBox box, RelayMessage ask)
        => box.Replies(ask.Id).LastOrDefault(r => r.Sender == ask.To && r.To == ask.Sender);

    /// <summary>Recent requests of this host still waiting for their answer, minus those already given up on.</summary>
    public static List<RelayMessage> Pending(RelayBox box, string me, IEnumerable<string> ids, IReadOnlySet<string> seen)
    {
        var given = box.State("rewake-given-up") ?? new JsonObject();
        double now = Reservations.Now();
        return ids.Where(id => given[id] is null).Select(box.Get).OfType<RelayMessage>()
            .Where(m => m.Sender == me && !m.Info && m.To != me && m.Topic != "selftest")
            .Where(m => now - ParseEpoch(m.Created) <= MaxAgeSeconds)
            .Where(m => !box.HasEvent(m.Id, McpServer.CancelledKind))
            .Where(m => AnswerOf(box, m) is not { } answer || (!answer.Info && !seen.Contains(answer.Id)))
            .ToList();
    }

    private static double ParseEpoch(string iso)
        => DateTimeOffset.TryParse(iso, System.Globalization.CultureInfo.InvariantCulture, System.Globalization.DateTimeStyles.AssumeUniversal, out var at) ? at.ToUnixTimeMilliseconds() / 1000.0 : 0;

    private static int Wake(RelayBox box, List<RelayMessage> pending, List<RelayMessage> answered, List<RelayMessage> closed, bool timeout, IReadOnlySet<string> seen)
    {
        var unread = answered.Select(m => (Ask: m, Answer: AnswerOf(box, m)!)).Where(p => !p.Answer.Info && !seen.Contains(p.Answer.Id)).ToList();   // info: closes the request, wakes nobody
        var waiting = pending.Except(answered).Except(closed).ToList();
        if (unread.Count == 0 && closed.Count == 0 && !timeout)
            return 0;   // read meanwhile (wait_reply, inbox): nothing to wake for

        var nonce = RelayBox.RandomHex(4);
        var text = new StringBuilder();
        text.AppendLine(RelayPaths.Tr(
            $"Nexus Relay: {unread.Count} answer(s) to this conversation's requests arrived after your turn ended (marked read).",
            $"Nexus Relay : {unread.Count} réponse(s) aux demandes de cette conversation arrivée(s) après la fin de ton tour (marquées lues)."));
        foreach (var (ask, answer) in unread)
        {
            var body = answer.Text.Length > MaxShown ? answer.Text[..MaxShown] + RelayPaths.Tr($"… (read({answer.Id}) for the rest)", $"… (read({answer.Id}) pour la suite)") : answer.Text;
            text.Append($"\n— {answer.Sender} → {ask.Id} (topic {ask.Topic}, reply {answer.Id}");
            foreach (var key in new[] { "state", "quality" })
                if (answer.ExtraObj[key]?.GetValue<string>() is string value) text.Append($", {key}: {value}");
            text.AppendLine(")");
            if (answer.ExtraObj["bad_citations"] is JsonArray bad && bad.Count > 0)
                text.AppendLine(RelayPaths.Tr("citations not found: ", "citations introuvables : ") + string.Join("; ", bad.Select(b => b?.ToString())));
            if (answer.ExtraObj["stale"] is JsonObject stale)
                text.AppendLine($"{stale["note"]} {string.Join(", ", (stale["changed"] as JsonArray ?? []).Select(c => c?.ToString()))}".TrimEnd());
            text.AppendLine($"⟦relay:{nonce} …⟧\n{body}\n⟦/relay:{nonce}⟧");
            box.MarkRead(answer);
        }
        foreach (var ask in closed)
            text.AppendLine(RelayPaths.Tr($"{ask.Id} ({ask.To}): closed without an answer: {McpServer.StateOf(box, ask)}", $"{ask.Id} ({ask.To}) : clos sans réponse : {McpServer.StateOf(box, ask)}"));
        if (waiting.Count > 0)
        {
            text.AppendLine(RelayPaths.Tr(
                $"Still without an answer: {string.Join(", ", waiting.Select(m => $"{m.Id} ({m.To})"))}; track(id) for its state.",
                $"Toujours sans réponse : {string.Join(", ", waiting.Select(m => $"{m.Id} ({m.To})"))} ; track(id) pour son état."));
            foreach (var ask in waiting)
                McpServer.MarkAbandonedWait(box, ask.Id);
        }
        // Presented once: a request closed without an answer, or still unanswered at the deadline, wakes nobody again.
        var settled = closed.Concat(timeout ? waiting : []).ToList();
        if (settled.Count > 0)
        {
            var given = box.State("rewake-given-up") ?? new JsonObject();
            foreach (var ask in settled)
                given[ask.Id] = Reservations.Now();
            box.SetState("rewake-given-up", given);
        }
        if (unread.Count > 0)
            text.AppendLine(McpServer.PeerNote(nonce));
        Console.Error.Write(text.ToString());
        return 2;
    }
}
