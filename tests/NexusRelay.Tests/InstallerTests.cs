using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusRelay.Tests;

/// <summary>Workstation settings, installer helpers (PowerShell) and repository conventions.</summary>
public sealed class InstallerTests : IDisposable
{
    private readonly Isolation iso = new();
    public void Dispose() => iso.Dispose();
    private static string Repo => Paths.Repo();

    [Fact]
    public void Settings_first_file_wins_key_by_key_with_variables()
    {
        iso.Set("NR_BASE", iso.Root);
        iso.Set("NR_ABSENTE", null);
        File.WriteAllText(Path.Combine(iso.Root, "token.txt"), new string('x', 40));
        string pc = Path.Combine(iso.Root, "pc.json"), depot = Path.Combine(iso.Root, "depot.json");
        File.WriteAllText(pc, """{"codex_router": "http://127.0.0.1:1/health"}""");
        File.WriteAllText(depot, new JsonObject
        {
            ["codex_router"] = "http://ignore",
            ["token_file"] = new JsonArray(@"%NR_ABSENTE%\token.txt", @"%NR_BASE%\absent.txt", @"%NR_BASE%\token.txt"),
            ["vaults"] = new JsonArray(@"%NR_BASE%\coffre", @"%NR_ABSENTE%\coffre", @"$NR_BASE\coffre"),
            ["codex_router_start"] = new JsonArray("\"%NR_BASE%\\lb.exe\" --background"),
            ["unknown"] = "ignored",
        }.ToJsonString());
        var found = RelaySettings.Load([pc, Path.Combine(iso.Root, "manquant.json"), depot]);
        Assert.Equal("http://127.0.0.1:1/health", found["codex_router"]?.ToString());  // this PC's file wins
        Assert.Equal(Path.Combine(iso.Root, "token.txt"), found["token_file"]?.ToString());  // first existing file
        Assert.Equal([iso.Root + @"\coffre"], (found["vaults"] as JsonArray)!.Select(v => v!.ToString()));  // missing variable ignored, duplicate removed
        Assert.Equal([$"\"{iso.Root}\\lb.exe\" --background"], (found["codex_router_start"] as JsonArray)!.Select(v => v!.ToString()));
        Assert.Null(found["unknown"]);
    }

    [Fact]
    public void Settings_command_reads_this_pc_then_the_installed_source()
    {
        string state = Path.Combine(iso.Home, ".nexus", "nexus-relay");
        Directory.CreateDirectory(state);
        File.WriteAllText(Path.Combine(state, "settings.json"), """{"host": "codex", "runtime": "native"}""");
        string source = Path.Combine(iso.Root, "source");
        Directory.CreateDirectory(source);
        File.WriteAllText(Path.Combine(source, "settings.local.json"), """{"host": "antigravity", "lang": "fr"}""");
        var settings = Native.Json("--settings", source);
        Assert.True(JsonNode.DeepEquals(settings, new JsonObject { ["host"] = "codex", ["lang"] = "fr" }), settings.ToJsonString());
        Assert.True(JsonNode.DeepEquals(Native.Json("--settings"), new JsonObject { ["host"] = "codex" }));
    }

    [Fact]
    public void Private_workstation_settings_are_readable_and_portable()
    {
        string path = Path.Combine(Repo, "settings.local.json");
        if (!File.Exists(path)) return;  // public copy: no workstation settings
        var data = JsonNode.Parse(File.ReadAllText(path))!.AsObject();
        Assert.All(data, p => Assert.Contains(p.Key, RelaySettings.Keys));
        foreach (string key in new[] { "token_file", "vaults", "codex_router_start" })
            foreach (var value in data[key] as JsonArray ?? [])
                Assert.True(value!.ToString().Contains('%') && !value.ToString().Contains(@":\"), value.ToString());
    }

    [Fact]
    public void Installer_json_parser_keeps_iso_strings_verbatim()
    {
        string path = Path.Combine(iso.Root, "settings.json");
        File.WriteAllText(path, """{"wake_since": "2026-10-29T22:46:43Z", "codex_project": "2026-10-30", "env": {"timestamp": "2026-10-30T01:00:00+02:00"}}""");
        var ran = Native.Pwsh(Path.Combine(Repo, "tests", "native_restart", "settings.ps1"), "-InputPath", path);
        var data = JsonNode.Parse(ran.Out)!;
        Assert.True(JsonNode.DeepEquals(data, new JsonObject { ["cutoff"] = "2026-10-29T22:46:43Z", ["label"] = "2026-10-30", ["nested"] = "2026-10-30T01:00:00+02:00" }), ran.Out + ran.Err);
    }

    [Fact]
    public void Antigravity_activation_repairs_a_missing_plugin_and_keeps_disabled_choices()
    {
        string script = Path.Combine(Repo, "tests", "native_restart", "activation.ps1");
        JsonNode Run(JsonNode data)
        {
            string path = Path.Combine(iso.Root, "config.json");
            File.WriteAllText(path, data.ToJsonString());
            var ran = Native.Pwsh(script, "-InputPath", path);
            return JsonNode.Parse(ran.Out) ?? throw new InvalidOperationException(ran.Err);
        }
        var config = JsonNode.Parse("""{"plugins": {"other": {"enabled": true, "nested": "keep"}}, "sidecars": {"nexus-relay/relay": {"enabled": true, "projectId": "fixture-project", "schemaVersion": 1}}, "userSettings": {"keep": "value"}}""")!;
        var result = Run(config);
        Assert.True(result["Changed"]!.GetValue<bool>());
        Assert.True(result["Config"]!["plugins"]!["nexus-relay"]!["enabled"]!.GetValue<bool>());
        Assert.True(JsonNode.DeepEquals(result["Config"]!["userSettings"], config["userSettings"]));
        Assert.True(JsonNode.DeepEquals(result["Config"]!["plugins"]!["other"], config["plugins"]!["other"]));
        Assert.Equal(1, result["Config"]!["sidecars"]!["nexus-relay/relay"]!["schemaVersion"]!.GetValue<int>());
        Assert.False(Run(result["Config"]!)["Changed"]!.GetValue<bool>());
        foreach (var (section, name) in new[] { ("plugins", "nexus-relay"), ("sidecars", "nexus-relay/relay") })
        {
            var disabled = result["Config"]!.DeepClone();
            disabled[section]![name]!["enabled"] = false;
            var preserved = Run(disabled);
            Assert.True(preserved["Disabled"]!.GetValue<bool>());
            Assert.False(preserved["Changed"]!.GetValue<bool>());
            Assert.True(JsonNode.DeepEquals(preserved["Config"], disabled));
        }
    }

    [Fact]
    public void No_python_left_in_the_product()
    {
        var tracked = Native.Git(Repo, "ls-files").Split('\n', StringSplitOptions.RemoveEmptyEntries);
        Assert.DoesNotContain(tracked, f => f.EndsWith(".py", StringComparison.Ordinal) || f is "pyproject.toml" or "uv.lock");
        foreach (string file in tracked.Where(f => f.EndsWith(".ps1", StringComparison.Ordinal) || f.EndsWith(".yml", StringComparison.Ordinal) || f.EndsWith(".cs", StringComparison.Ordinal) || f is "scripts/pre-commit" or "scripts/post-commit"))
        {
            if (file.StartsWith("tests/", StringComparison.Ordinal) || !File.Exists(Path.Combine(Repo, file))) continue;
            string text = File.ReadAllText(Path.Combine(Repo, file));
            // Nothing runs Python or uv any more (recognizing a retired install to migrate it is not running it).
            Assert.False(Regex.IsMatch(text, @"\buv (sync|run|venv|pip|build)\b|\buvx\b|\bpythonw?(\.exe)? +-|setup-python|setup-uv|& \$python"), file);
        }
    }

    private string Token(string userHome, string explicitFile = "", string legacy = "")
    {
        string script = Path.Combine(iso.Root, $"token-{Guid.NewGuid():N}.ps1");
        File.WriteAllText(script, $$"""
            . '{{Path.Combine(Repo, "scripts", "Resolve-NexusToken.ps1")}}'
            $explicit = @('{{explicitFile}}') | Where-Object { $_ }
            $file = Resolve-NexusTokenFile -Explicit $explicit -UserHome '{{userHome}}' -Legacy '{{legacy}}'
            $file
            [IO.File]::ReadAllText($file).Trim()
            """);
        var ran = Native.Pwsh(script);
        Assert.True(ran.Code == 0, ran.Err);
        return ran.Out.Trim();
    }

    [Fact]
    public void Shared_token_is_created_once_then_reused_by_every_installer()
    {
        // Two installers in a row on a blank profile (for instance Android, then Relay) end with the same token.
        string blank = Path.Combine(iso.Root, "blank");
        string first = Token(blank), second = Token(blank);
        Assert.Equal(first, second);
        var lines = first.Split('\n', StringSplitOptions.TrimEntries);
        Assert.Equal(Path.Combine(blank, ".nexus", "mcp-token.txt"), lines[0]);
        Assert.Matches("^[0-9a-f]{64}$", lines[1]);

        // At the same time: exclusive creation, one winner, one token.
        string racing = Path.Combine(iso.Root, "racing");
        var both = Enumerable.Range(0, 2).Select(_ => Task.Run(() => Token(racing))).ToArray();
        Task.WaitAll(both, TestContext.Current.CancellationToken);
        Assert.Equal(both[0].Result, both[1].Result);

        // An existing file (NEXUS_MCP_TOKEN_FILE, synced workstation token) is never regenerated.
        string synced = Path.Combine(iso.Root, "synced", "token.txt");
        Directory.CreateDirectory(Path.GetDirectoryName(synced)!);
        File.WriteAllText(synced, "workstation-token-workstation-token-0123\n");
        Assert.Equal($"{synced}\nworkstation-token-workstation-token-0123", Token(blank, synced).ReplaceLineEndings("\n"));

        // An empty shared file is not a token; the retired relay-only token is carried over, not lost.
        string migrated = Path.Combine(iso.Root, "migrated");
        Directory.CreateDirectory(Path.Combine(migrated, ".nexus", "nexus-relay"));
        File.WriteAllText(Path.Combine(migrated, ".nexus", "mcp-token.txt"), "");
        string legacy = Path.Combine(migrated, ".nexus", "nexus-relay", "token.txt");
        File.WriteAllText(legacy, new string('a', 64));
        Assert.EndsWith(new string('a', 64), Token(migrated, legacy, legacy));
        Assert.Equal(new string('a', 64), File.ReadAllText(Path.Combine(migrated, ".nexus", "mcp-token.txt")));
    }

    [Fact]
    public void Downloaded_code_is_never_run_in_memory()
    {
        // Microsoft Defender flags these forms (Trojan:Win32/Commando.A!ml, 30/10): installers run files with -File.
        var forbidden = new Regex(@"\birm\b[^\n]*\|\s*iex\b|\|\s*Invoke-Expression|\[scriptblock\]::Create|-EncodedCommand|\biex\s*\(", RegexOptions.IgnoreCase);
        foreach (string file in Native.Git(Repo, "ls-files").Split('\n', StringSplitOptions.RemoveEmptyEntries))
        {
            if (file is "CHANGELOG.md" || file.StartsWith("tests/", StringComparison.Ordinal) || !File.Exists(Path.Combine(Repo, file))
                || !(file.EndsWith(".ps1") || file.EndsWith(".md") || file.EndsWith(".yml") || file.EndsWith(".cs") || file.EndsWith(".json"))) continue;
            Assert.False(forbidden.IsMatch(File.ReadAllText(Path.Combine(Repo, file))), file);
        }
    }

    [Fact]
    public void No_internal_antigravity_api_no_shell_and_local_port_only()
    {
        foreach (string path in Directory.EnumerateFiles(Path.Combine(Repo, "native"), "*.cs").Append(Path.Combine(Repo, "install.ps1")))
        {
            string text = File.ReadAllText(path);
            Assert.DoesNotContain("LanguageServerService", text);
            Assert.DoesNotContain("x-codeium-csrf-token", text);
            Assert.DoesNotContain("ANTIGRAVITY_CSRF_TOKEN", text);
            Assert.DoesNotContain("0.0.0.0", text);
            Assert.DoesNotContain("Set-Clipboard", text);
        }
    }

    [Fact]
    public void Skill_headers_are_readable_yaml()
    {
        // Antigravity rejects (every 10 s) a header whose bare value contains ": " or " #".
        foreach (string path in Directory.EnumerateFiles(Repo, "SKILL.md", SearchOption.AllDirectories).Where(p => !p.Contains(@"\.git\")))
        {
            var lines = File.ReadAllLines(path);
            Assert.Equal("---", lines[0]);
            foreach (string line in lines[1..Array.IndexOf(lines, "---", 1)])
            {
                int colon = line.IndexOf(": ", StringComparison.Ordinal);
                if (colon < 0) continue;
                string value = line[(colon + 2)..];
                if (value.Length > 0 && value[0] is not ('"' or '\'')) Assert.False(value.Contains(": ") || value.Contains(" #"), $"{path}: quote {line[..colon]}");
            }
        }
    }

    [Fact]
    public void Scripts_read_by_windows_powershell_51()
    {
        // get.ps1 runs install.ps1 under Windows PowerShell 5.1 (Nexus pack): without a BOM it reads UTF-8 as ANSI;
        // get.ps1 itself, downloaded by irm, stays ASCII.
        byte[] install = File.ReadAllBytes(Path.Combine(Repo, "install.ps1"));
        Assert.True(install[0] == 0xEF && install[1] == 0xBB && install[2] == 0xBF);
        Assert.All(File.ReadAllBytes(Path.Combine(Repo, "get.ps1")), b => Assert.True(b < 128));
    }

    [Fact]
    public void Instructions_in_two_languages_and_one_version()
    {
        string fr = Path.Combine(Repo, "fr");
        foreach (string path in Directory.EnumerateFiles(fr, "*", SearchOption.AllDirectories))
            Assert.True(File.Exists(Path.Combine(Repo, Path.GetRelativePath(fr, path))), path);
        foreach (string plugin in new[] { "plugins/nexus-relay/plugin.json", "fr/plugins/nexus-relay/plugin.json", "codex/.codex-plugin/plugin.json" })
            Assert.Equal(RelayPaths.Version, JsonNode.Parse(File.ReadAllText(Path.Combine(Repo, plugin)))!["version"]?.ToString());
        Assert.Contains($"<Version>{RelayPaths.Version}</Version>", File.ReadAllText(Path.Combine(Repo, "native", "NexusRelay.csproj")));
        Assert.Contains($"## [{RelayPaths.Version}]", File.ReadAllText(Path.Combine(Repo, "CHANGELOG.md")));
        var server = JsonNode.Parse(File.ReadAllText(Path.Combine(Repo, "server.json")))!;
        Assert.Equal(RelayPaths.Version, server["version"]?.ToString());
        Assert.All(server["packages"]!.AsArray(), p => Assert.Contains($"/v{RelayPaths.Version}/", p!["identifier"]!.ToString()));
        Assert.Equal(RelayPaths.Version, JsonNode.Parse(File.ReadAllText(Path.Combine(Repo, "mcpb", "manifest.json")))!["version"]?.ToString());
        foreach (string name in new[] { "claude/skills/nexus-relay/SKILL.md", "plugins/nexus-relay/skills/nexus-relay/SKILL.md" })
            Assert.DoesNotContain("é", File.ReadAllText(Path.Combine(Repo, name)));  // English for the public
    }

    [Fact]
    public void Installer_declares_the_restart_request_and_pins_the_mailbox()
    {
        string install = File.ReadAllText(Path.Combine(Repo, "install.ps1"), Encoding.UTF8);
        Assert.Contains("$daemonRequest = Join-Path $mailbox 'daemon-restart.request'", install);
        Assert.Contains("restart_request = $daemonRequest", install);
        Assert.True(install.IndexOf("$daemonRequest = ", StringComparison.Ordinal) < install.IndexOf("$spec = [ordered]@{", StringComparison.Ordinal));
        Assert.Contains(@"$mailbox = Join-Path $HOME '.nexus\relay'", install);
        Assert.Contains("$daemonEnv.NEXUS_RELAY_HOME = $mailbox", install);
        Assert.Contains("exe = $nativeExe", install);
        Assert.Contains("python = $nativeExe", install);  // duplicate of exe for the deployed workstation supervisor
    }

    [Fact]
    public void No_build_output_in_the_synchronized_repository()
    {
        foreach (string folder in new[] { "native", Path.Combine("tests", "NexusRelay.Tests"), Path.Combine("tests", "native_lifetime") })
            foreach (string output in new[] { "bin", "obj" })
                Assert.False(Directory.Exists(Path.Combine(Repo, folder, output)), Path.Combine(folder, output));
    }
}
