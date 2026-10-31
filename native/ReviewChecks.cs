using System.Security.Cryptography;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusRelay;

/// <summary>Checks a review against the files it talks about: the state of the cited and attached files when the
/// request is sent (HEAD and content hashes), whether they changed before the answer, whether the answer's file:line
/// citations exist, and which points of a review round were already raised in an earlier round.</summary>
public static partial class ReviewChecks
{
    public const string CitationMissing = "citation introuvable";
    private const int MaxSnapshotFiles = 50;

    // A citation names a file and a line, or a range: src/a.cs:12, b.py:3-9, .gitignore:4, C:\Mon Projet\a.cs:3.
    // An absolute Windows path may hold spaces; otherwise the name is a path, a dotfile, a known extensionless name
    // or a name with a known file extension (Kept: response.status:500 or api.v1:8080 are not files).
    [GeneratedRegex(@"(?<![\w:/.\\-])([A-Za-z]:[/\\][^:*?""<>|\r\n`'()\[\]]*?[\w-]|(?:[/\\])?(?:[\w.-]+[/\\]+)*[\w.-]*[\w-]):(\d+)(?:-(\d+))?\b", RegexOptions.Compiled)]
    private static partial Regex CitationRegex();

    private static readonly HashSet<string> Extensions = new(StringComparer.OrdinalIgnoreCase)
    {
        "cs", "csx", "csproj", "props", "targets", "sln", "slnx", "py", "pyi", "ts", "tsx", "js", "jsx", "mjs", "cjs", "json",
        "jsonc", "md", "txt", "ps1", "psm1", "psd1", "sh", "bat", "cmd", "yml", "yaml", "toml", "xml", "xaml", "html", "htm",
        "css", "scss", "go", "rs", "java", "kt", "kts", "c", "h", "cc", "cpp", "hpp", "rb", "php", "swift", "sql", "ini",
        "cfg", "config", "gradle", "vue", "svelte", "lua", "dart", "ex", "exs", "manifest", "resx", "razor", "cshtml", "proto",
    };

    private static readonly HashSet<string> BareNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "Dockerfile", "Makefile", "README", "LICENSE", "CHANGELOG", "SECURITY", "Gemfile", "Rakefile", "Procfile", "Jenkinsfile",
    };

    /// <summary>Whether a matched name is a file reference rather than code (item.count:0) or a host and port.</summary>
    private static bool Kept(string path)
    {
        var name = Path.GetFileName(path.Replace('\\', '/'));
        if (name.Length == 0 || name.All(char.IsDigit))
            return false;
        if (path.IndexOfAny(['/', '\\']) >= 0 && (name.Contains('.') || BareNames.Contains(name)))
            return true;
        if (name.StartsWith('.') && name.Length > 1 && !name[1..].Contains('.'))
            return true;   // dotfile: .gitignore, .editorconfig
        var dot = name.LastIndexOf('.');
        return dot < 0 ? BareNames.Contains(name) : Extensions.Contains(name[(dot + 1)..]);
    }

    [GeneratedRegex(@"\b(?:tour|round)\s*(?:n°\s*|#)?(\d{1,3})\b", RegexOptions.IgnoreCase | RegexOptions.Compiled)]
    private static partial Regex RoundRegex();

    // An absolute Windows path named in the text (the repository of the request, without a line): C:\Dev\Depot.
    [GeneratedRegex(@"(?<![\w])[A-Za-z]:[/\\][^\s:*?""<>|`'()\[\],;]+", RegexOptions.Compiled)]
    private static partial Regex AbsolutePathRegex();

    [GeneratedRegex(@"[\p{L}\p{N}_]{3,}", RegexOptions.Compiled)]
    private static partial Regex WordRegex();

    public sealed record Citation(string Path, int Line, int LastLine)
    {
        public override string ToString() => LastLine > Line ? $"{Path}:{Line}-{LastLine}" : $"{Path}:{Line}";
    }

    public static List<Citation> Citations(string text)
    {
        var found = new List<Citation>();
        foreach (Match m in CitationRegex().Matches(text))
        {
            var path = m.Groups[1].Value;
            // An absolute path with spaces that is no file ran into the prose when its first word is itself an existing
            // folder or file ("C:\Depot, see a.cs:2"): keep its last word. Otherwise it stays as written (unchecked),
            // never redirected to a namesake.
            bool delimited = m.Index > 0 && text[m.Index - 1] is '`' or '"' or '\'' or '«' or '“';
            if (path.Contains(' ') && !delimited && !File.Exists(path))
            {
                var head = path[..path.IndexOf(' ')].TrimEnd(',', ';', '.');
                if (Directory.Exists(head) || File.Exists(head))
                    path = path[(path.LastIndexOfAny([' ', '\t']) + 1)..];
            }
            if (!int.TryParse(m.Groups[2].Value, out int line) || !Kept(path))
                continue;
            int last = m.Groups[3].Success && int.TryParse(m.Groups[3].Value, out int l) && l >= line ? l : line;
            found.Add(new(path, line, last));
        }
        return found.DistinctBy(c => c.ToString()).ToList();
    }

    /// <summary>Round number written in the head of a request ("tour 2 de relecture", "round 3").</summary>
    public static int? RoundIn(string text)
    {
        var head = text.Length > 160 ? text[..160] : text;
        var m = RoundRegex().Match(head);
        return m.Success && int.TryParse(m.Groups[1].Value, out int n) && n >= 1 ? n : null;
    }

    private static string? Git(string directory, params string[] args) => Reservations.Git(directory, args)?.Trim();

    private static string? ExistingFolder(string path)
    {
        var folder = Directory.Exists(path) ? path : Path.GetDirectoryName(path);
        while (!string.IsNullOrEmpty(folder) && !Directory.Exists(folder))
            folder = Path.GetDirectoryName(folder);
        return string.IsNullOrEmpty(folder) ? null : folder;
    }

    /// <summary>The folder relative citations are read from: the given one (its Git root when it has one), otherwise
    /// the Git root of the first absolute path that exists.</summary>
    public static string? RepoOf(string? repo, IEnumerable<string> paths)
    {
        var candidates = string.IsNullOrWhiteSpace(repo)
            ? paths.Where(p => Path.IsPathFullyQualified(p) && (File.Exists(p) || Directory.Exists(p)))
            : [repo];
        foreach (var path in candidates)
        {
            if (ExistingFolder(Path.GetFullPath(path)) is not string folder)
                continue;
            if (Git(folder, "rev-parse", "--show-toplevel") is string top && top.Length > 0)
                return Path.GetFullPath(top);
            if (!string.IsNullOrWhiteSpace(repo))
                return folder;
        }
        return null;
    }

    /// <summary>Resolves a cited path. Null: cannot be checked (relative path without a known folder); empty: no such
    /// file. A bare or partial path (McpServer.cs) matches the tracked files that end with it.</summary>
    public static List<string>? Resolve(string cited, string? repo, Lazy<List<string>>? tracked = null)
    {
        var path = cited.Replace('\\', '/');
        if (Path.IsPathFullyQualified(cited))
        {
            if (File.Exists(cited))
                return [Path.GetFullPath(cited)];
            return cited.Contains(' ') ? null : [];   // with spaces, the match may have run into the prose: unchecked
        }
        if (repo is null)
            return null;
        var direct = Path.GetFullPath(Path.Join(repo, path.TrimStart('/')));
        if (File.Exists(direct))
            return [direct];
        var files = (tracked ?? Tracked(repo)).Value;
        var suffix = "/" + path.TrimStart('.', '/');
        return files.Where(f => ("/" + f).EndsWith(suffix, StringComparison.OrdinalIgnoreCase))
            .Select(f => Path.GetFullPath(Path.Join(repo, f))).Where(File.Exists).ToList();
    }

    public static Lazy<List<string>> Tracked(string repo) => new(() =>
        (Git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard") ?? "")
            .Split('\0', StringSplitOptions.RemoveEmptyEntries).ToList());

    private static int LineCount(string path)
    {
        try
        {
            return File.ReadLines(path).Count();
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return int.MaxValue;
        }
    }

    /// <summary>The citations of a review answer that point to no file, or past the end of the file.</summary>
    public static List<string> MissingCitations(string text, string? repo)
    {
        var bad = new List<string>();
        var tracked = repo is not null ? Tracked(repo) : null;
        foreach (var citation in Citations(text))
        {
            var files = Resolve(citation.Path, repo, tracked);
            if (files is null)
                continue;
            if (files.Count == 0)
                bad.Add(RelayPaths.Tr($"{citation}: file not found", $"{citation} : fichier introuvable"));
            else if (citation.Line < 1)
                bad.Add(RelayPaths.Tr($"{citation}: lines start at 1", $"{citation} : les lignes commencent à 1"));
            else if (files.All(f => LineCount(f) < citation.LastLine))
            {
                int lines = files.Select(LineCount).Max();
                bad.Add(RelayPaths.Tr($"{citation}: past the end of the file ({lines} lines)", $"{citation} : au-delà de la fin du fichier ({lines} lignes)"));
            }
        }
        return bad;
    }

    private static string Hash(string path)
    {
        try
        {
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            return Convert.ToHexStringLower(SHA256.HashData(stream));
        }
        catch (Exception e) when (e is FileNotFoundException or DirectoryNotFoundException)
        {
            return "absent";
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return "illisible";
        }
    }

    /// <summary>State of the attached and cited files when a request is sent: folder, HEAD, content hash of each.
    /// Null when the request names no file that can be found.</summary>
    public static JsonObject? Snapshot(string? repo, IReadOnlyList<string> attached, string text)
    {
        var cited = Citations(text).Select(c => c.Path).ToList();
        var root = RepoOf(repo, attached.Concat(cited).Concat(AbsolutePathRegex().Matches(text).Select(m => m.Value.TrimEnd('.'))));
        var tracked = root is not null ? Tracked(root) : null;
        var files = new JsonObject();
        foreach (var path in attached.Concat(cited).Distinct(StringComparer.OrdinalIgnoreCase))
        {
            if (files.Count >= MaxSnapshotFiles)
                break;
            if (Resolve(path, root, tracked) is [string one] && !files.ContainsKey(one))
                files[one] = Hash(one);
        }
        if (files.Count == 0 && root is null)
            return null;
        var snapshot = new JsonObject { ["repo"] = root, ["files"] = files };
        if (root is not null && Git(root, "rev-parse", "HEAD") is string head && head.Length > 0)
            snapshot["head"] = head;
        return snapshot;
    }

    /// <summary>What changed since the snapshot: HEAD moved, files whose content differs. Null when nothing did.</summary>
    public static JsonObject? Changes(JsonObject? snapshot)
    {
        if (snapshot is null)
            return null;
        var repo = snapshot["repo"]?.GetValue<string>();
        var changed = new JsonArray();
        if (snapshot["files"] is JsonObject files)
        {
            foreach (var (path, hash) in files)
            {
                if (Hash(path) != hash?.GetValue<string>())
                    changed.Add(repo is not null && path.StartsWith(repo, StringComparison.OrdinalIgnoreCase) ? Path.GetRelativePath(repo, path).Replace('\\', '/') : path);
            }
        }
        var head = snapshot["head"]?.GetValue<string>();
        string? now = repo is not null && head is not null ? Git(repo, "rev-parse", "HEAD") : null;
        bool moved = head is not null && now is not null && now != head;
        if (changed.Count == 0 && !moved)
            return null;
        var result = new JsonObject { ["changed"] = changed, ["head_moved"] = moved };
        if (moved)
            result["head"] = $"{Short(head!)} → {Short(now!)}";
        result["note"] = changed.Count > 0
            ? RelayPaths.Tr("files of the request changed after it was sent: the answer may be about an outdated state", "des fichiers de la demande ont changé après son envoi : la réponse peut porter sur un état périmé")
            : RelayPaths.Tr("new commits since the request (its files are unchanged)", "nouveaux commits depuis la demande (ses fichiers n'ont pas changé)");
        return result;
    }

    private static string Short(string sha) => sha[..Math.Min(12, sha.Length)];

    /// <summary>Points of an answer: its non-trivial lines, list markers removed.</summary>
    public static List<string> Points(string text)
        => text.Split('\n')
            .Select(l => l.Trim().TrimStart('-', '*', '•', '·', ' ', '\t').Trim())
            .Select(l => Regex.Replace(l, @"^\d{1,2}[.)]\s+", ""))
            .Where(l => WordRegex().Matches(l).Count >= 3)
            .ToList();

    private static HashSet<string> Words(string point)
        => WordRegex().Matches(point.ToLowerInvariant()).Select(m => m.Value).ToHashSet(StringComparer.Ordinal);

    private static HashSet<string> Files(string point)
        => Citations(point).Select(c => Path.GetFileName(c.Path.Replace('\\', '/')).ToLowerInvariant()).ToHashSet(StringComparer.Ordinal);

    /// <summary>A point already made in an earlier round: close wording, or the same file with a similar wording
    /// (line numbers move between rounds).</summary>
    public static bool Repeated(string point, IEnumerable<string> earlier)
    {
        var words = Words(point);
        var files = Files(point);
        foreach (var before in earlier)
        {
            var other = Words(before);
            int union = words.Union(other).Count();
            double similarity = union == 0 ? 0 : (double)words.Intersect(other).Count() / union;
            if (similarity >= 0.5 || (files.Count > 0 && files.Overlaps(Files(before)) && similarity >= 0.3))
                return true;
        }
        return false;
    }
}
