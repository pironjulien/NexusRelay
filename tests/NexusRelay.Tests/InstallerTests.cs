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
        File.WriteAllText(Path.Join(iso.Root, "token.txt"), new string('x', 40));
        string pc = Path.Join(iso.Root, "pc.json"), depot = Path.Join(iso.Root, "depot.json");
        File.WriteAllText(pc, """{"codex_router": "http://127.0.0.1:1/health"}""");
        File.WriteAllText(depot, new JsonObject
        {
            ["codex_router"] = "http://ignore",
            ["token_file"] = new JsonArray(@"%NR_ABSENTE%\token.txt", @"%NR_BASE%\absent.txt", @"%NR_BASE%\token.txt"),
            ["vaults"] = new JsonArray(@"%NR_BASE%\coffre", @"%NR_ABSENTE%\coffre", @"$NR_BASE\coffre"),
            ["codex_router_start"] = new JsonArray("\"%NR_BASE%\\lb.exe\" --background"),
            ["unknown"] = "ignored",
        }.ToJsonString());
        var found = RelaySettings.Load([pc, Path.Join(iso.Root, "manquant.json"), depot]);
        Assert.Equal("http://127.0.0.1:1/health", found["codex_router"]?.ToString());  // this PC's file wins
        Assert.Equal(Path.Join(iso.Root, "token.txt"), found["token_file"]?.ToString());  // first existing file
        Assert.Equal([iso.Root + @"\coffre"], (found["vaults"] as JsonArray)!.Select(v => v!.ToString()));  // missing variable ignored, duplicate removed
        Assert.Equal([$"\"{iso.Root}\\lb.exe\" --background"], (found["codex_router_start"] as JsonArray)!.Select(v => v!.ToString()));
        Assert.Null(found["unknown"]);
    }

    [Fact]
    public void Settings_command_reads_this_pc_then_the_installed_source()
    {
        string state = Path.Join(iso.Home, ".nexus", "nexus-relay");
        Directory.CreateDirectory(state);
        File.WriteAllText(Path.Join(state, "settings.json"), """{"host": "codex", "runtime": "native"}""");
        string source = Path.Join(iso.Root, "source");
        Directory.CreateDirectory(source);
        File.WriteAllText(Path.Join(source, "settings.local.json"), """{"host": "antigravity", "lang": "fr"}""");
        var settings = Native.Json("--settings", source);
        Assert.True(JsonNode.DeepEquals(settings, new JsonObject { ["host"] = "codex", ["lang"] = "fr" }), settings.ToJsonString());
        Assert.True(JsonNode.DeepEquals(Native.Json("--settings"), new JsonObject { ["host"] = "codex" }));
    }

    [Fact]
    public void Private_workstation_settings_are_readable_and_portable()
    {
        string path = Path.Join(Repo, "settings.local.json");
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
        string path = Path.Join(iso.Root, "settings.json");
        File.WriteAllText(path, """{"wake_since": "2026-10-29T22:46:43Z", "codex_project": "2026-10-30", "env": {"timestamp": "2026-10-30T01:00:00+02:00"}}""");
        var ran = Native.Pwsh(Path.Join(Repo, "tests", "native_restart", "settings.ps1"), "-InputPath", path);
        var data = JsonNode.Parse(ran.Out)!;
        Assert.True(JsonNode.DeepEquals(data, new JsonObject { ["cutoff"] = "2026-10-29T22:46:43Z", ["label"] = "2026-10-30", ["nested"] = "2026-10-30T01:00:00+02:00" }), ran.Out + ran.Err);
    }

    [Fact]
    public void Antigravity_activation_repairs_a_missing_plugin_and_keeps_disabled_choices()
    {
        string script = Path.Join(Repo, "tests", "native_restart", "activation.ps1");
        JsonNode Run(JsonNode data)
        {
            string path = Path.Join(iso.Root, "config.json");
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
        foreach (string file in tracked.Where(f => f.EndsWith(".ps1", StringComparison.Ordinal) || f.EndsWith(".yml", StringComparison.Ordinal) || f.EndsWith(".cs", StringComparison.Ordinal) || f is "scripts/pre-commit" or "scripts/post-commit")
            .Where(f => !f.StartsWith("tests/", StringComparison.Ordinal) && File.Exists(Path.Join(Repo, f))))
        {
            string text = File.ReadAllText(Path.Join(Repo, file));
            // Nothing runs Python or uv any more (recognizing a retired install to migrate it is not running it).
            Assert.False(Regex.IsMatch(text, @"\buv (sync|run|venv|pip|build)\b|\buvx\b|\bpythonw?(\.exe)? +-|setup-python|setup-uv|& \$python"), file);
        }
    }

    private string Token(string userHome, string explicitFile = "", string legacy = "")
    {
        string script = Path.Join(iso.Root, $"token-{Guid.NewGuid():N}.ps1");
        File.WriteAllText(script, $$"""
            . '{{Path.Join(Repo, "scripts", "Resolve-NexusToken.ps1")}}'
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
        string blank = Path.Join(iso.Root, "blank");
        string first = Token(blank), second = Token(blank);
        Assert.Equal(first, second);
        var lines = first.Split('\n', StringSplitOptions.TrimEntries);
        Assert.Equal(Path.Join(blank, ".nexus", "mcp-token.txt"), lines[0]);
        Assert.Matches("^[0-9a-f]{64}$", lines[1]);

        // At the same time: exclusive creation, one winner, one token.
        string racing = Path.Join(iso.Root, "racing");
        var both = Enumerable.Range(0, 2).Select(_ => Task.Run(() => Token(racing))).ToArray();
        Task.WaitAll(both, TestContext.Current.CancellationToken);
        Assert.Equal(both[0].Result, both[1].Result);

        // An existing file (NEXUS_MCP_TOKEN_FILE, synced workstation token) is never regenerated.
        string synced = Path.Join(iso.Root, "synced", "token.txt");
        Directory.CreateDirectory(Path.GetDirectoryName(synced)!);
        File.WriteAllText(synced, "workstation-token-workstation-token-0123\n");
        Assert.Equal($"{synced}\nworkstation-token-workstation-token-0123", Token(blank, synced).ReplaceLineEndings("\n"));

        // An empty shared file is not a token; the retired relay-only token is carried over, not lost.
        string migrated = Path.Join(iso.Root, "migrated");
        Directory.CreateDirectory(Path.Join(migrated, ".nexus", "nexus-relay"));
        File.WriteAllText(Path.Join(migrated, ".nexus", "mcp-token.txt"), "");
        string legacy = Path.Join(migrated, ".nexus", "nexus-relay", "token.txt");
        File.WriteAllText(legacy, new string('a', 64));
        Assert.EndsWith(new string('a', 64), Token(migrated, legacy, legacy));
        Assert.Equal(new string('a', 64), File.ReadAllText(Path.Join(migrated, ".nexus", "mcp-token.txt")));
    }

    [Fact]
    public void Shared_token_remains_readable_while_other_installers_resolve_it()
    {
        string profile = Path.Join(iso.Root, "active-consumer");
        string token = Path.Join(profile, ".nexus", "mcp-token.txt");
        Directory.CreateDirectory(Path.GetDirectoryName(token)!);
        File.WriteAllText(token, new string('b', 64));
        // A live consumer can keep a readable handle while another installer resolves the same existing token.
        // Locking the token itself denies this handle; a stable sibling lock only serializes the installers.
        using var consumer = File.Open(token, FileMode.Open, FileAccess.Read, FileShare.Read);
        Assert.EndsWith(new string('b', 64), Token(profile));
        Assert.Equal(new string('b', 64), File.ReadAllText(token));
    }

    [Fact]
    public void Downloaded_code_is_never_run_in_memory()
    {
        // Microsoft Defender flags these forms (Trojan:Win32/Commando.A!ml, 30/10): installers run files with -File.
        var forbidden = new Regex(@"\birm\b[^\n]*\|\s*iex\b|\|\s*Invoke-Expression|\[scriptblock\]::Create|-EncodedCommand|\biex\s*\(", RegexOptions.IgnoreCase);
        foreach (string file in Native.Git(Repo, "ls-files").Split('\n', StringSplitOptions.RemoveEmptyEntries)
            .Where(f => !(f is "CHANGELOG.md" || f.StartsWith("tests/", StringComparison.Ordinal) || !File.Exists(Path.Join(Repo, f))
                || !(f.EndsWith(".ps1") || f.EndsWith(".md") || f.EndsWith(".yml") || f.EndsWith(".cs") || f.EndsWith(".json")))))
        {
            Assert.False(forbidden.IsMatch(File.ReadAllText(Path.Join(Repo, file))), file);
        }
    }

    [Fact]
    public void No_internal_antigravity_api_no_shell_and_local_port_only()
    {
        foreach (string text in Directory.EnumerateFiles(Path.Join(Repo, "native"), "*.cs").Append(Path.Join(Repo, "install.ps1")).Select(path => File.ReadAllText(path)))
        {
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
    public void Windows_powershell_child_prefers_its_own_modules_over_inherited_core_modules()
    {
        string installer = File.ReadAllText(Path.Join(Repo, "get.ps1"));
        string prefix = installer[..installer.IndexOf("$root =", StringComparison.Ordinal)];
        string script = Path.Join(iso.Root, "module-fixture.ps1");
        string input = Path.Join(iso.Root, "checksum-input.txt");
        File.WriteAllText(input, "synthetic checksum input");
        File.WriteAllText(script, prefix + "\n(Get-FileHash -LiteralPath $env:NEXUS_TEST_INPUT -Algorithm SHA256).Hash\n");
        string desktop = Path.Join(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0");
        string coreModules = Path.Join(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "PowerShell", "7", "Modules");
        Assert.True(Directory.Exists(coreModules), "PowerShell 7 is required for the inherited-module regression fixture.");
        var ran = Native.RunFile(Path.Join(desktop, "powershell.exe"), ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
            env: new Dictionary<string, string?> { ["PSModulePath"] = coreModules + ";" + Path.Join(desktop, "Modules"), ["NEXUS_TEST_INPUT"] = input });
        Assert.True(ran.Code == 0, ran.Err + ran.Out);
        Assert.Equal(Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(File.ReadAllBytes(input))), ran.Out.Trim());
    }

    [Fact]
    public void Scripts_read_by_windows_powershell_51()
    {
        // get.ps1 runs install.ps1 under Windows PowerShell 5.1 (Nexus pack): without a BOM it reads UTF-8 as ANSI;
        // get.ps1 itself, downloaded by irm, stays ASCII.
        byte[] install = File.ReadAllBytes(Path.Join(Repo, "install.ps1"));
        Assert.True(install[0] == 0xEF && install[1] == 0xBB && install[2] == 0xBF);
        Assert.All(File.ReadAllBytes(Path.Join(Repo, "get.ps1")), b => Assert.True(b < 128));
    }

    [Fact]
    public void Instructions_in_two_languages_and_one_version()
    {
        string fr = Path.Join(Repo, "fr");
        foreach (string path in Directory.EnumerateFiles(fr, "*", SearchOption.AllDirectories))
            Assert.True(File.Exists(Path.Combine(Repo, Path.GetRelativePath(fr, path))), path);
        foreach (string plugin in new[] { "plugin/antigravity/plugin.json", "fr/plugin/antigravity/plugin.json", "plugin/.claude-plugin/plugin.json", "plugin/.codex-plugin/plugin.json" })
            Assert.Equal(RelayPaths.Version, JsonNode.Parse(File.ReadAllText(Path.Join(Repo, plugin)))!["version"]?.ToString());
        Assert.Contains($"<Version>{RelayPaths.Version}</Version>", File.ReadAllText(Path.Join(Repo, "native", "NexusRelay.csproj")));
        Assert.Contains($"## [{RelayPaths.Version}]", File.ReadAllText(Path.Join(Repo, "CHANGELOG.md")));
        var server = JsonNode.Parse(File.ReadAllText(Path.Join(Repo, "server.json")))!;
        Assert.Equal(RelayPaths.Version, server["version"]?.ToString());
        Assert.All(server["packages"]!.AsArray(), p => Assert.Contains($"/v{RelayPaths.Version}/", p!["identifier"]!.ToString()));
        Assert.Equal(RelayPaths.Version, JsonNode.Parse(File.ReadAllText(Path.Join(Repo, "mcpb", "manifest.json")))!["version"]?.ToString());
        foreach (string name in new[] { "plugin/skills/nexus-relay/SKILL.md", "plugin/codex/skills/nexus-relay/SKILL.md", "plugin/antigravity/skills/nexus-relay/SKILL.md" })
            Assert.DoesNotContain("é", File.ReadAllText(Path.Join(Repo, name)));  // English for the public
    }

    [Fact]
    public void Installer_declares_the_restart_request_and_pins_the_mailbox()
    {
        string install = File.ReadAllText(Path.Join(Repo, "install.ps1"), Encoding.UTF8);
        Assert.Contains("$daemonRequest = Join-Path $mailbox 'daemon-restart.request'", install);
        Assert.Contains("restart_request = $daemonRequest", install);
        Assert.True(install.IndexOf("$daemonRequest = ", StringComparison.Ordinal) < install.IndexOf("$spec = [ordered]@{", StringComparison.Ordinal));
        Assert.Contains(@"$mailbox = Join-Path $HOME '.nexus\relay'", install);
        Assert.Contains("$daemonEnv.NEXUS_RELAY_HOME = $mailbox", install);
        Assert.Contains("exe = $nativeExe", install);
        Assert.DoesNotContain("python = $nativeExe", install);  // native supervisors read exe
    }

    [Fact]
    public void No_build_output_in_the_synchronized_repository()
    {
        foreach (string folder in new[] { "native", Path.Join("tests", "NexusRelay.Tests"), Path.Join("tests", "native_lifetime") })
            foreach (string output in new[] { "bin", "obj" })
                Assert.False(Directory.Exists(Path.Join(Repo, folder, output)), Path.Join(folder, output));
    }
}
