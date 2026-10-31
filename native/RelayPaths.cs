using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusRelay;

public static partial class RelayPaths
{
    public const string Version = "1.10.9";
    public static readonly string[] Agents = ["claude", "antigravity", "codex"];
    public const string User = "user";
    public static readonly string[] Parties = ["claude", "antigravity", "codex", "user"];
    public const string DefaultHost = "claude";

    private static readonly Dictionary<string, string> Aliases = new(StringComparer.OrdinalIgnoreCase)
    {
        ["claude"] = "claude",
        ["antigravity"] = "antigravity",
        ["agy"] = "antigravity",
        ["gemini"] = "antigravity",
        ["codex"] = "codex",
        ["chatgpt"] = "codex",
        ["openai"] = "codex",
    };

    [GeneratedRegex(@"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", RegexOptions.Compiled)]
    private static partial Regex ConversationRegex();

    public static string LangCode()
    {
        var env = Environment.GetEnvironmentVariable("NEXUS_RELAY_LANG");
        if (!string.IsNullOrWhiteSpace(env))
        {
            return env.Trim().StartsWith("fr", StringComparison.OrdinalIgnoreCase) ? "fr" : "en";
        }
        try
        {
            var settingsPath = Path.Join(UserHome(), ".nexus", "nexus-relay", "settings.json");
            if (File.Exists(settingsPath))
            {
                var node = JsonNode.Parse(File.ReadAllText(settingsPath, Encoding.UTF8)) as JsonObject;
                var lang = node?["lang"]?.GetValue<string>();
                if (!string.IsNullOrWhiteSpace(lang))
                {
                    return lang.Trim().StartsWith("fr", StringComparison.OrdinalIgnoreCase) ? "fr" : "en";
                }
            }
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or System.Text.Json.JsonException or InvalidOperationException)
        {
            // Ignore unreadable settings
        }
        return "en";
    }

    public static string Tr(string en, string fr) => LangCode() == "fr" ? fr : en;

    public static string UserHome()
    {
        var profile = Environment.GetEnvironmentVariable("USERPROFILE");
        if (!string.IsNullOrWhiteSpace(profile))
            return profile;
        var home = Environment.GetEnvironmentVariable("HOME");
        if (!string.IsNullOrWhiteSpace(home))
            return home;
        return Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
    }

    public static string RelayHome()
    {
        var overridePath = Environment.GetEnvironmentVariable("NEXUS_RELAY_HOME");
        return !string.IsNullOrWhiteSpace(overridePath)
            ? Path.GetFullPath(overridePath)
            : Path.Join(UserHome(), ".nexus", "relay");
    }

    public static string GeminiHome()
    {
        var overridePath = Environment.GetEnvironmentVariable("NEXUS_RELAY_GEMINI_HOME");
        return !string.IsNullOrWhiteSpace(overridePath)
            ? Path.GetFullPath(overridePath)
            : Path.Join(UserHome(), ".gemini");
    }

    public static string BrainDir() => Path.Join(GeminiHome(), "antigravity", "brain");

    public static string NormalizeAgent(string? name)
    {
        var raw = (name ?? "").Trim();
        if (Aliases.TryGetValue(raw, out var normalized))
        {
            return normalized;
        }
        throw new RelayError(Tr(
            $"unknown agent: '{name}' (claude, antigravity or codex)",
            $"agent inconnu : '{name}' (claude, antigravity ou codex)"));
    }

    public static string NormalizeParty(string? name)
    {
        var raw = (name ?? "").Trim();
        if (string.Equals(raw, User, StringComparison.OrdinalIgnoreCase))
            return User;
        return NormalizeAgent(name);
    }

    public static string RelayHost()
    {
        var raw = (Environment.GetEnvironmentVariable("NEXUS_RELAY_HOST") ?? "").Trim();
        if (string.Equals(raw, "all", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(raw, "peers", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(raw, "trinite", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(raw, "*", StringComparison.OrdinalIgnoreCase))
        {
            return "all";
        }
        if (string.IsNullOrEmpty(raw))
            return DefaultHost;
        try
        {
            return NormalizeAgent(raw);
        }
        catch (RelayError)
        {
            return DefaultHost;
        }
    }

    public static bool IsConversationId(string? value)
        => !string.IsNullOrEmpty(value) && ConversationRegex().IsMatch(value);

    public static string TranscriptPath(string conversation, string? brain = null)
    {
        if (!IsConversationId(conversation))
        {
            throw new RelayError(Tr(
                $"invalid conversation id: '{conversation}'",
                $"identifiant de conversation invalide : '{conversation}'"));
        }
        return Path.Join(brain ?? BrainDir(), conversation, ".system_generated", "logs", "transcript.jsonl");
    }

    public static bool ConversationAlive(string conversation)
    {
        var root = Path.Join(GeminiHome(), "antigravity");
        var dbPath = Path.Combine(root, "conversations", $"{conversation}.db");
        if (!File.Exists(dbPath))
            return false;
        try
        {
            var notePath = Path.Combine(root, "annotations", $"{conversation}.pbtxt");
            if (!File.Exists(notePath))
                return true;
            var note = File.ReadAllText(notePath, Encoding.UTF8);
            return !note.Replace(" ", "", StringComparison.Ordinal).Contains("archived:true", StringComparison.Ordinal);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return true;
        }
    }

    public static string CodeFingerprint()
    {
        try
        {
            var exePath = Assembly.GetExecutingAssembly().Location;
            if (!string.IsNullOrEmpty(exePath) && File.Exists(exePath))
            {
                var bytes = File.ReadAllBytes(exePath);
                return Convert.ToHexStringLower(SHA256.HashData(bytes));
            }
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            // Fallback
        }
        return Version;
    }

    public static bool RestartWanted(string requestPath, string loadedFingerprint)
    {
        bool TargetedElsewhere(string text)
        {
            var target = System.Text.RegularExpressions.Regex.Match(text, @"(?:^|\s)pid:(\d+)(?:\s|$)");
            return target.Success && target.Groups[1].Value != Environment.ProcessId.ToString(System.Globalization.CultureInfo.InvariantCulture);
        }
        try { if (TargetedElsewhere(File.ReadAllText(requestPath, Encoding.UTF8))) return false; }
        catch (FileNotFoundException) { return false; }
        catch (DirectoryNotFoundException) { return false; }
        var taken = requestPath + ".lue";
        try
        {
            File.Move(requestPath, taken, overwrite: true);
        }
        catch (FileNotFoundException)
        {
            return false;
        }
        catch (DirectoryNotFoundException)
        {
            return false;
        }
        try
        {
            var content = File.ReadAllText(taken, Encoding.UTF8).Trim();
            if (TargetedElsewhere(content)) { GiveBack(taken, requestPath); return false; }
            string? target = content.Split(' ', StringSplitOptions.RemoveEmptyEntries).FirstOrDefault(s => s.StartsWith("sha256:", StringComparison.Ordinal))?[7..];
            var wanted = content.StartsWith("force", StringComparison.Ordinal) ||
                (target is not null ? !target.Equals(loadedFingerprint, StringComparison.OrdinalIgnoreCase) : CodeFingerprint() != loadedFingerprint);
            try { File.Delete(taken); } catch (Exception cleanup) when (cleanup is IOException or UnauthorizedAccessException) { /* A leftover taken request is harmless. */ }
            return wanted;
        }
        catch
        {
            GiveBack(taken, requestPath);
            throw;
        }
    }

    public static void GiveBack(string takenPath, string requestPath)
    {
        bool forced = false;
        try
        {
            forced = File.ReadAllText(takenPath, Encoding.UTF8).Trim().StartsWith("force", StringComparison.Ordinal);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            forced = false;
        }
        try
        {
            if (forced || !File.Exists(requestPath))
            {
                File.Move(takenPath, requestPath, overwrite: true);
            }
            else
            {
                File.Delete(takenPath);
            }
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            // Ignore cleanup failure
        }
    }

    public static void MarkRestartable(string name)
    {
        var path = Path.Join(RelayHome(), $"{name}-restart.pid");
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, Environment.ProcessId.ToString(), new UTF8Encoding(false));
    }
}
