using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusRelay;

/// <summary>
/// Common journal of the Nexus tools (NexusShared/journal/FORMAT.md, v1), product "relay": one line per action (send,
/// reply, read, wait_reply, wake, livraison, tour) in %USERPROFILE%\.nexus\journal\relay\&lt;agent&gt;\&lt;YYYY-MM&gt;.jsonl.
/// `agent`: the AI that acts (for a wake, a delivery or a stopped turn: the one that sent the request); `app`: the
/// targeted peer; `appel`: the message id, only when it has the shape of an id. NEVER the text of a message: `erreur`
/// is a short cause (filtre, quota, appli figée…) or the start of an error cut at the first quote, colon, parenthesis
/// or bracket. Never under AppData (MSIX redirection). Never blocking, never throws: a failed write loses the line.
/// Restored in 1.14.1: the native engine (1.13.0, 30/10) had dropped it with the Python engine.
/// </summary>
public static partial class Journal
{
    private static readonly object Gate = new();

    [GeneratedRegex(@"[«""“'’(\[:{]")]
    private static partial Regex Cut();

    // Known causes, from the most precise to the most general (wake and turn-watch reasons), French or English.
    private static readonly (Regex Pattern, string Label)[] Causes =
    [
        (new(@"flagged|filtre|content policy|cybersecurity", RegexOptions.IgnoreCase), "filtre"),
        (new(@"quota|utilisés|épuisé|% used|exhausted|usage limit|rate limit", RegexOptions.IgnoreCase), "quota"),
        (new(@"déjà pilotée|interface .*occupée|already driven|interface .*busy", RegexOptions.IgnoreCase), "interface occupée"),
        (new(@"sidecar muet|sidecar mort|sidecar silent|sidecar dead", RegexOptions.IgnoreCase), "sidecar muet"),
        (new(@"jeton|premier plan tenu|foreground held|holds the mouse", RegexOptions.IgnoreCase), "jeton pris"),
        (new(@"routeur|router", RegexOptions.IgnoreCase), "routeur arrêté"),
        (new(@"projet absent|projet .*introuvable|chat du sujet|project missing|project .*not found|topic chat", RegexOptions.IgnoreCase), "chat introuvable"),
        (new(@"délai dépassé|timeout|timed out", RegexOptions.IgnoreCase), "délai dépassé"),
        (new(@"figé|muet|ne répond pas|frozen|silent|not responding|hung", RegexOptions.IgnoreCase), "appli figée"),
        (new(@"fermée?\b|port .*fermé|introuvable sur ce PC|\bclosed\b|not found on this PC", RegexOptions.IgnoreCase), "appli fermée"),
        (new(@"sans réponse|without a reply", RegexOptions.IgnoreCase), "tour fini sans réponse"),
        (new(@"utilisateur|\buser\b", RegexOptions.IgnoreCase), "utilisateur actif"),
    ];

    public static readonly IReadOnlyDictionary<string, string> WakeIssues = new Dictionary<string, string>
    {
        ["started"] = "✓", ["delivered"] = "✓", ["uncertain"] = "~", ["deferred"] = "différé", ["skipped"] = "différé",
        ["failed"] = "✗", ["abandoned"] = "✗",
    };

    private static readonly Dictionary<string, string> WaitIssues = new()
    {
        ["success"] = "✓", ["unavailable"] = "✗", ["timeout"] = "~", ["restarting"] = "~", ["cancelled"] = "~",
    };

    /// <summary>Journaled tools (FORMAT.md, relay row). Others (status, search…) are reads of the room, not actions.</summary>
    private static readonly HashSet<string> Tools = ["send", "reply", "read", "wait_reply", "cancel", "close"];

    public static string Root() => Environment.GetEnvironmentVariable("NEXUS_JOURNAL_HOME") is { Length: > 0 } home
        ? home
        : Path.Join(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".nexus", "journal");

    public static string? CleanError(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return null;
        string flat = string.Join(' ', text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
        string start = Cut().Split(flat, 2)[0].Trim(' ', '.', ',', ';', '-');
        return start.Length == 0 ? "error" : start.Length <= 80 ? start : start[..80];
    }

    /// <summary>Short cause of a failure, without text from a message or a screen.</summary>
    public static string? Cause(string? reason)
    {
        if (string.IsNullOrWhiteSpace(reason)) return null;
        foreach (var (pattern, label) in Causes) if (pattern.IsMatch(reason)) return label;
        return CleanError(reason);
    }

    public static void Note(string agent, string action, string? app, string issue, double? ms = null, string? erreur = null,
        string? appel = null, string? technique = null, bool premierPlan = false)
    {
        if (Environment.GetEnvironmentVariable("NEXUS_JOURNAL") is "0" or "off" or "false" or "no") return;
        try
        {
            if (!RelayPaths.Agents.Contains(agent)) return;
            var now = DateTimeOffset.UtcNow;
            var row = new JsonObject
            {
                ["v"] = 1,
                ["ts"] = now.ToString("yyyy-MM-ddTHH:mm:ss.fffZ", CultureInfo.InvariantCulture),
                ["agent"] = agent,
                ["outil"] = RelayPaths.Version,
                ["appel"] = appel is not null && RelayBox.MessageIdRegex().IsMatch(appel) ? appel : null,
                ["app"] = app is not null && RelayPaths.Parties.Contains(app) ? app : null,
                ["cadre"] = null,
                ["action"] = action,
                ["technique"] = technique,
                ["issue"] = issue,
                ["ms"] = ms is double d ? (int)Math.Round(Math.Max(0, d)) : null,
                ["premier_plan"] = premierPlan,
                ["erreur"] = issue is "✓" or "=" ? null : Cause(erreur),
            };
            string dir = Path.Join(Root(), "relay", agent);
            string file = Path.Join(dir, $"{now:yyyy-MM}.jsonl");
            byte[] bytes = new UTF8Encoding(false).GetBytes(row.ToJsonString() + "\n");
            lock (Gate)
            {
                Directory.CreateDirectory(dir);
                // Several processes append (daemon, MCP bridges, sidecar): shared append, a few short retries.
                for (int attempt = 0; ; attempt++)
                {
                    try
                    {
                        using var stream = new FileStream(file, FileMode.Append, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete);
                        stream.Write(bytes);
                        break;
                    }
                    catch (IOException) when (attempt < 4) { Thread.Sleep(15); }
                }
            }
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or ArgumentException or NotSupportedException)
        {
            // The journal never blocks nor breaks a tool: the line is lost.
        }
    }

    /// <summary>The line(s) of one MCP tool call, from its arguments and its JSON result.</summary>
    public static void Tool(string? me, string tool, JsonObject args, JsonObject result, Stopwatch watch, RelayBox? box)
    {
        if (me is null || !Tools.Contains(tool)) return;
        try
        {
            double ms = watch.Elapsed.TotalMilliseconds;
            string status = result["status"]?.ToString() ?? "success";
            string? argId = args["id"] is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;
            string? error = result["error"]?.ToString();
            string? Peer(string? id)
            {
                var message = id is not null && RelayBox.MessageIdRegex().IsMatch(id) ? box?.Get(id) : null;
                return message is null ? null : message.Sender == me ? message.To : message.Sender;
            }
            if (status == "error")
            {
                string? to = args["to"] is JsonValue tv && tv.TryGetValue<string>(out var t) ? Safe(t) : null;
                Note(me, tool, to ?? Peer(argId), "✗", ms, error, argId);
                return;
            }
            switch (tool)
            {
                case "send":
                    if (result["messages"] is JsonArray many)
                        foreach (var item in many.OfType<JsonObject>()) Note(me, tool, item["to"]?.ToString(), "✓", ms, null, item["id"]?.ToString());
                    else Note(me, tool, result["to"]?.ToString(), "✓", ms, null, result["id"]?.ToString());
                    break;
                case "wait_reply":
                    if (result["replies"] is JsonArray replies)
                        foreach (var one in replies.OfType<JsonObject>()) Wait(me, one, ms, Peer(one["id"]?.ToString()), one["id"]?.ToString());
                    else Wait(me, result, ms, Peer(argId), argId);
                    break;
                case "read":
                    Note(me, tool, result["message"]?["from"]?.ToString() ?? Peer(argId), "✓", ms, null, argId);
                    break;
                default:
                    Note(me, tool, result["to"]?.ToString() ?? Peer(argId), "✓", ms, null, argId);
                    break;
            }
        }
        catch (Exception e) when (e is InvalidOperationException or FormatException or IOException or ArgumentException)
        {
            // Journal only.
        }
    }

    private static void Wait(string me, JsonObject one, double ms, string? peer, string? id)
    {
        string status = one["status"]?.ToString() ?? "error";
        string issue = WaitIssues.GetValueOrDefault(status, "✗");
        string? why = one["reason"]?.ToString() ?? one["error"]?.ToString() ?? (status == "timeout" ? "délai dépassé" : status == "success" ? null : status);
        Note(me, "wait_reply", peer, issue, ms, why, id);
    }

    private static string? Safe(string agent)
    {
        try { return RelayPaths.NormalizeAgent(agent); }
        catch (RelayError) { return null; }
    }
}
