using System.Globalization;
using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusRelay;

/// <summary>
/// <c>NexusRelay.exe --update</c>, started by the daemon (POST /api/update) without a window and outside its job: the
/// same steps as get.ps1, without PowerShell. It downloads this PC's release package (NexusRelay-win-x64.zip or
/// NexusRelay-win-arm64.zip) of the latest release (or NEXUS_RELEASE_TAG), checks it against the release's
/// SHA256SUMS.txt and, when the GitHub CLI is signed in, its build provenance, unpacks it, and starts the package's own
/// engine: <c>--install --mirror --register</c> (NEXUS_PACK=1). Everything goes to ~\.nexus\nexus-relay\update.log.
/// </summary>
public static partial class SelfUpdate
{
    public const string Repository = "pironjulien/NexusRelay";

    [GeneratedRegex(@"^v\d+\.\d+\.\d+(-dev\.\d+)?$")]
    public static partial Regex Tag();

    [GeneratedRegex(@"^v\d+\.\d+\.\d+$")]
    private static partial Regex StableTag();

    /// <summary>Expected SHA-256 of one file in a SHA256SUMS.txt ("&lt;hash&gt;  &lt;name&gt;" lines); any malformed or
    /// repeated line refuses the installation.</summary>
    public static string ExpectedHash(string sums, string name)
    {
        string? expected = null;
        foreach (string line in sums.Split('\n').Select(l => l.Trim()).Where(l => l.Length > 0))
        {
            var match = Regex.Match(line, @"^([0-9a-fA-F]{64})\s+\*?(\S+)$");
            if (!match.Success) throw new InvalidDataException("SHA256SUMS.txt has an invalid line: installation refused.");
            if (!string.Equals(match.Groups[2].Value, name, StringComparison.Ordinal)) continue;
            if (expected is not null) throw new InvalidDataException($"SHA256SUMS.txt lists {name} twice: installation refused.");
            expected = match.Groups[1].Value.ToLowerInvariant();
        }
        return expected ?? throw new InvalidDataException($"{name} is absent from SHA256SUMS.txt: installation refused.");
    }

    public static string Rid() => RuntimeInformation.OSArchitecture switch
    {
        Architecture.Arm64 => "win-arm64",
        Architecture.X64 => "win-x64",
        var other => throw new PlatformNotSupportedException($"Unsupported Windows architecture: {other} (x64 or ARM64 needed)."),
    };

    private static string LatestTag(HttpClient redirectless)
    {
        string? pinned = Environment.GetEnvironmentVariable("NEXUS_RELEASE_TAG");
        if (!string.IsNullOrEmpty(pinned))
            return Tag().IsMatch(pinned) ? pinned : throw new InvalidDataException($"NEXUS_RELEASE_TAG is not a release tag: {pinned}");
        using var request = new HttpRequestMessage(HttpMethod.Head, new Uri($"https://github.com/{Repository}/releases/latest"));
        using var response = redirectless.Send(request);
        string tag = (response.Headers.Location?.ToString() ?? "").Split("/tag/").Last();
        return StableTag().IsMatch(tag) ? tag : throw new InvalidDataException($"Latest NexusRelay release not found (redirect: '{response.Headers.Location}').");
    }

    /// <summary>Build provenance (Sigstore, signed by NexusSystem's shared release workflow) when the GitHub CLI is
    /// installed and signed in; a failed check refuses the package. Not signed in: nothing checked, said in the log.</summary>
    private static void Provenance(string file, TextWriter log)
    {
        if (InstallNative.OnPath("gh") is not { } gh || !gh.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)) return;
        if (InstallNative.Run(gh, ["auth", "status"], 60).Code != 0) { log.WriteLine("Build provenance not checked (GitHub CLI not signed in); SHA-256 checked."); return; }
        if (InstallNative.Run(gh, ["attestation", "verify", file, "--repo", Repository, "--signer-repo", "pironjulien/NexusSystem"], 180).Code != 0)
            throw new InvalidDataException($"Build provenance of {Path.GetFileName(file)} not verified (gh attestation verify): installation refused.");
        log.WriteLine("Build provenance verified.");
    }

    public static int Run()
    {
        string state = Path.Join(RelayPaths.UserHome(), ".nexus", "nexus-relay");
        Directory.CreateDirectory(state);
        string marker = Path.Join(state, "update.json");
        var data = RelayBox.ReadJson(marker) ?? [];
        data["pid"] = Environment.ProcessId;
        data["launch"] = "started";
        RelayBox.WriteState(marker, data);
        using var log = new StreamWriter(Path.Join(state, "update.log"), append: false, InstallFiles.Utf8) { AutoFlush = true };
        string local = Environment.GetEnvironmentVariable("LOCALAPPDATA") is { Length: > 0 } appData ? appData : Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string root = Environment.GetEnvironmentVariable("NEXUS_RELAY_ROOT") is { Length: > 0 } custom ? custom : Path.Join(local, "NexusRelay");
        string work = Path.Join(root, "download", Guid.NewGuid().ToString("N")[..12]);
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromMinutes(5) };
            http.DefaultRequestHeaders.UserAgent.ParseAdd("NexusRelay-update");
            using var redirectless = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false }) { Timeout = TimeSpan.FromSeconds(30) };
            redirectless.DefaultRequestHeaders.UserAgent.ParseAdd("NexusRelay-update");
            string tag = LatestTag(redirectless), rid = Rid(), name = $"NexusRelay-{rid}.zip";
            log.WriteLine($"Downloading NexusRelay {tag} ({rid})...");
            Directory.CreateDirectory(work);
            string zip = Path.Join(work, name), baseUrl = $"https://github.com/{Repository}/releases/download/{tag}";
            byte[] package = http.GetByteArrayAsync(new Uri($"{baseUrl}/{name}")).GetAwaiter().GetResult();
            string sums = http.GetStringAsync(new Uri($"{baseUrl}/SHA256SUMS.txt")).GetAwaiter().GetResult();
            if (!Convert.ToHexStringLower(SHA256.HashData(package)).Equals(ExpectedHash(sums, name), StringComparison.Ordinal))
                throw new InvalidDataException($"SHA-256 mismatch for {name}: the download is not the published package.");
            File.WriteAllBytes(zip, package);
            log.WriteLine("Package checked against SHA256SUMS.txt.");
            Provenance(zip, log);
            string unpacked = Path.Join(work, "package");
            ZipFile.ExtractToDirectory(zip, unpacked);
            string engine = Path.Join(unpacked, "bin", "NexusRelay.exe");
            if (!File.Exists(engine) || !InstallNative.IsPeImage(engine)) throw new InvalidDataException("Not a NexusRelay release package (bin\\NexusRelay.exe missing).");
            var (code, _) = InstallNative.Run(engine, ["--install", "--source", unpacked, "--mirror", "--register"], 900,
                new Dictionary<string, string?> { ["NEXUS_PACK"] = "1" }, line => log.WriteLine(line));
            log.WriteLine(code == 0 ? "Update installed." : $"Update failed (exit code {code.ToString(CultureInfo.InvariantCulture)}).");
            return code;
        }
        catch (Exception e) // Boundary: the update runs alone in the background; every failure ends in its log and exit code.
        {
            log.WriteLine("Update failed: " + e.Message);
            return 1;
        }
        finally
        {
            InstallFiles.TryDeleteTree(work);
            string downloads = Path.GetDirectoryName(work)!;
            try { if (Directory.Exists(downloads) && !Directory.EnumerateFileSystemEntries(downloads).Any()) Directory.Delete(downloads); }
            catch (IOException) { /* another download in progress */ }
        }
    }
}
