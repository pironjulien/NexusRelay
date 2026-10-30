using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusRelay;

public static partial class EditGuard
{
    public const int AutoTtlMin = 60;
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
            string? path = null;
            foreach (var key in AgyPathArgs)
            {
                var v = args[key]?.GetValue<string>();
                if (!string.IsNullOrEmpty(v))
                {
                    path = v;
                    break;
                }
            }
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

    public static string? Decide(
        string agent,
        IReadOnlyList<string> files,
        RelayBox? box = null,
        string? conversation = null,
        string? tool = null)
    {
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
            if (firstHolder.StartsWith(agent + " ", StringComparison.Ordinal))
            {
                return RelayPaths.Tr(
                    $"Nexus Relay: {lines}. Do not edit it: another conversation of {agent} is working on it; wait for its commit (automatic release) or the expiry",
                    $"Nexus Relay : {lines}. Ne pas le modifier : une autre conversation de {agent} y travaille ; attendre son commit (libération automatique) ou l'expiration");
            }
            return RelayPaths.Tr(
                $"Nexus Relay: {lines}. Do not edit it: ask {firstHolder} through the relay to release it, or wait",
                $"Nexus Relay : {lines}. Ne pas le modifier : demander à {firstHolder} par le relais de le libérer, ou attendre");
        }

        try
        {
            Reservations.Reserve(
                box,
                agent,
                files.Take(Reservations.MaxPaths).ToList(),
                Reservations.AutoReason,
                AutoTtlMin,
                session: conversation,
                automatic: true,
                tool: tool);
        }
        catch (RelayError ex)
        {
            return ex.Message;
        }
        return null;
    }

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
                conversation: Session(eventObj),
                tool: Invocation(agent, eventObj));

            if (!string.IsNullOrEmpty(reason))
            {
                Console.Out.Write(Answer(agent, reason).ToJsonString());
            }
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"Nexus Relay edit_guard: {ex.Message}");
            return 0;
        }
    }
}
