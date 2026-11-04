using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusRelay;

public static partial class EditGuard
{
    private static readonly string[] AgyPathArgs = ["Path", "TargetFile"];

    [GeneratedRegex(@"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*$", RegexOptions.Multiline | RegexOptions.Compiled)]
    private static partial Regex PatchLineRegex();

    private static string MakeAbsolute(string path, string? baseDir)
    {
        if (Path.IsPathRooted(path))
            return Path.GetFullPath(path);
        return Path.GetFullPath(Path.Combine(baseDir ?? Directory.GetCurrentDirectory(), path));
    }

    public static List<string> Targets(string agent, JsonObject eventObj)
    {
        if (agent == "antigravity")
        {
            var args = (eventObj["toolCall"] as JsonObject)?["args"] as JsonObject;
            if (args is null)
                return [];
            string? path = AgyPathArgs.Select(key => args[key]?.GetValue<string>()).FirstOrDefault(v => !string.IsNullOrEmpty(v));
            if (string.IsNullOrEmpty(path))
                return [];
            string? ws = (eventObj["workspacePaths"] as JsonArray)?.FirstOrDefault()?.GetValue<string>();
            return [MakeAbsolute(path, ws)];
        }

        var data = eventObj["tool_input"] as JsonObject;
        if (data is null)
            return [];
        var cwd = eventObj["cwd"]?.GetValue<string>();
        var toolName = eventObj["tool_name"]?.GetValue<string>();
        if (toolName == "apply_patch")
        {
            var command = data["command"]?.GetValue<string>() ?? "";
            var list = new List<string>();
            foreach (Match m in PatchLineRegex().Matches(command))
            {
                list.Add(MakeAbsolute(m.Groups[1].Value, cwd));
            }
            return list;
        }

        var filePath = data["file_path"]?.GetValue<string>() ?? data["notebook_path"]?.GetValue<string>();
        return !string.IsNullOrEmpty(filePath) ? [MakeAbsolute(filePath, cwd)] : [];
    }

    public static string? Session(JsonObject eventObj)
    {
        var val = eventObj["session_id"]?.GetValue<string>() ?? eventObj["conversationId"]?.GetValue<string>();
        return !string.IsNullOrEmpty(val) ? val : null;
    }

    public static string? Invocation(string agent, JsonObject eventObj)
    {
        if (agent == "antigravity")
        {
            if (eventObj["stepIdx"] is JsonValue sv && sv.GetValueKind() == JsonValueKind.Number && sv.TryGetValue<int>(out int idx))
                return $"step:{idx}";
            return null;
        }
        var val = eventObj["tool_use_id"]?.GetValue<string>() ?? eventObj["tool_call_id"]?.GetValue<string>();
        return !string.IsNullOrEmpty(val) ? val : null;
    }

    /// <summary>Safety expiry of an automatic lease whose completion event never comes (crash, missing hook). The lease
    /// normally ends with the tool call (PostToolUse); it is advisory and never refuses anyone.</summary>
    public const int AutoTtlMin = 10;

    /// <summary>Refusal reason when another holder explicitly reserved one of the files (exclusive), else null. Otherwise
    /// takes a short advisory lease for the duration of the tool call; <paramref name="note"/> names other agents or
    /// conversations whose edit tool is running on the same file right now.</summary>
    public static string? Decide(
        string agent,
        IReadOnlyList<string> files,
        RelayBox? box = null,
        string? conversation = null,
        string? tool = null)
        => Decide(agent, files, out _, box, conversation, tool);

    public static string? Decide(
        string agent,
        IReadOnlyList<string> files,
        out string? note,
        RelayBox? box = null,
        string? conversation = null,
        string? tool = null)
    {
        note = null;
        if (files.Count == 0)
            return null;
        box ??= new RelayBox();
        var hits = Reservations.Blocking(agent, files, box, conversation);
        if (hits.Count > 0)
        {
            var lines = string.Join("; ", hits.Take(5).Select(h =>
                RelayPaths.Tr(
                    $"{h["file"]?.GetValue<string>()} reserved by {h["agent"]?.GetValue<string>()} ({h["reason"]?.GetValue<string>()}) until {h["expires"]?.GetValue<string>()}",
                    $"{h["file"]?.GetValue<string>()} réservé par {h["agent"]?.GetValue<string>()} ({h["reason"]?.GetValue<string>()}) jusqu'à {h["expires"]?.GetValue<string>()}")));
            var firstHolder = hits[0]["agent"]?.GetValue<string>() ?? "";
            return RelayPaths.Tr(
                $"Nexus Relay: {lines}. Do not edit it: ask {firstHolder} through the relay to release it, or wait",
                $"Nexus Relay : {lines}. Ne pas le modifier : demander à {firstHolder} par le relais de le libérer, ou attendre");
        }

        var busy = Reservations.Editing(agent, files, box, conversation);
        if (busy.Count > 0)
        {
            var who = string.Join("; ", busy.Take(5).Select(h => $"{h["file"]?.GetValue<string>()} ({h["agent"]?.GetValue<string>()})"));
            note = RelayPaths.Tr(
                $"Nexus Relay: another edit is running on {who}. Not blocked: if your edit is refused because the text changed, read the file again and redo it.",
                $"Nexus Relay : une autre modification est en cours sur {who}. Rien n'est bloqué : si ta modification est refusée parce que le texte a changé, relis le fichier et refais-la.");
        }

        try
        {
            Reservations.Reserve(
                box,
                agent,
                files.Take(Reservations.MaxPaths).ToList(),
                Reservations.AutoReason,
                AutoTtlMin,
                exclusive: false,
                session: conversation,
                automatic: true,
                tool: tool);
        }
        catch (RelayError ex)
        {
            // Bookkeeping only: a lease that cannot be written never blocks the edit.
            Console.Error.WriteLine(ex.Message);
        }
        return null;
    }

    /// <summary>Non-blocking context for the agent (Claude reads additionalContext; the other hosts get it on stderr).</summary>
    public static JsonObject? Note(string agent, string note)
        => agent == "claude"
            ? new JsonObject { ["hookSpecificOutput"] = new JsonObject { ["hookEventName"] = "PreToolUse", ["additionalContext"] = note } }
            : null;

    public static JsonObject Answer(string agent, string reason)
    {
        if (agent == "antigravity")
        {
            return new JsonObject
            {
                ["decision"] = "deny",
                ["reason"] = reason,
            };
        }
        return new JsonObject
        {
            ["hookSpecificOutput"] = new JsonObject
            {
                ["hookEventName"] = "PreToolUse",
                ["permissionDecision"] = "deny",
                ["permissionDecisionReason"] = reason,
            }
        };
    }
    public static int RunCli(IReadOnlyList<string> args)
    {
        try
        {
            var nonFlagArgs = args.Where(a => !a.StartsWith("--", StringComparison.Ordinal)).ToList();
            var agent = nonFlagArgs.Count > 0
                ? RelayPaths.NormalizeAgent(nonFlagArgs[0])
                : Reservations.WhoAmI();

            string input;
            using (var reader = new StreamReader(Console.OpenStandardInput(), Encoding.UTF8))
            {
                input = reader.ReadToEnd();
            }
            var eventObj = (string.IsNullOrWhiteSpace(input) ? null : JsonNode.Parse(input) as JsonObject) ?? new JsonObject();

            bool finishTool = args.Contains("--finish-tool", StringComparer.Ordinal);
            bool finishSession = args.Contains("--finish-session", StringComparer.Ordinal);
            if (finishTool || finishSession)
            {
                Reservations.FinishEdits(
                    agent,
                    Session(eventObj),
                    tool: Invocation(agent, eventObj),
                    sessionEnded: finishSession);
                Console.Out.Write("{}");
                return 0;
            }

            var reason = Decide(
                agent,
                Targets(agent, eventObj),
                out var note,
                conversation: Session(eventObj),
                tool: Invocation(agent, eventObj));

            if (!string.IsNullOrEmpty(reason))
            {
                Console.Out.Write(Answer(agent, reason).ToJsonString());
            }
            else if (!string.IsNullOrEmpty(note))
            {
                if (Note(agent, note) is { } context)
                    Console.Out.Write(context.ToJsonString());
                else
                    Console.Error.WriteLine(note);
            }
            return 0;
        }
        catch (Exception ex)
        {
            // Boundary: the edit hook reports and never blocks or crashes the agent.
            Console.Error.WriteLine($"Nexus Relay edit_guard: {ex.Message}");
            return 0;
        }
    }
}
