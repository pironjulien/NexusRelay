using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusRelay.Tests;

/// <summary>Workstation settings, installer helpers (PowerShell) and repository conventions.</summary>
public sealed partial class InstallerTests : IDisposable
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
                || !(f.EndsWith(".ps1", StringComparison.Ordinal) || f.EndsWith(".md", StringComparison.Ordinal) || f.EndsWith(".yml", StringComparison.Ordinal) || f.EndsWith(".cs", StringComparison.Ordinal) || f.EndsWith(".json", StringComparison.Ordinal)))))
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
    public void Antigravity_marketplace_copy_bridges_to_the_local_daemon()
    {
        // A Marketplace copy cannot name this PC's token (Antigravity expands only ${PLUGIN_ROOT}): like NexusWindows,
        // NexusBrowser and NexusAndroid, a fixed command (cmd.exe) runs the plugin's own launcher, ${PLUGIN_ROOT} in
        // the arguments; the launcher finds the installed engine (engine.txt) and starts its stdio bridge (see
        // BridgeTests). install.ps1 replaces this declaration with its own and adds the sidecar.
        string bundle = Path.Join(Repo, "plugin", "antigravity");
        var server = JsonNode.Parse(File.ReadAllText(Path.Join(bundle, "mcp_config.json")))!["mcpServers"]!["nexus-relay"]!;
        Assert.Equal("cmd.exe", server["command"]!.ToString());
        Assert.Equal(["/d", "/c", @"${PLUGIN_ROOT}\scripts\start.cmd", "http://127.0.0.1:16181/mcp?agent=antigravity"], server["args"]!.AsArray().Select(a => a!.ToString()));
        Assert.Equal(["start.cmd"], Directory.EnumerateFiles(Path.Join(bundle, "scripts")).Select(Path.GetFileName));  // no script compiled at each start
        Assert.Contains("\r\n", File.ReadAllText(Path.Join(bundle, "scripts", "start.cmd")), StringComparison.Ordinal);  // cmd.exe labels need CRLF
        Assert.True(File.Exists(Path.Join(bundle, "README.md")));
        string install = File.ReadAllText(Path.Join(Repo, "install.ps1"), Encoding.UTF8);
        Assert.Contains("& $nativeExe --engine-pointer", install);  // the versioned copy, never bin
        Assert.Contains("foreach ($retired in @('scripts/bridge.ps1'))", install);  // the old PowerShell bridge left behind by Copy-Tree
        Assert.True(install.IndexOf("--engine-pointer", StringComparison.Ordinal) < install.IndexOf("Remove-StaleRelayVersions -Versions $versions", StringComparison.Ordinal));
        foreach (string manifest in new[] { "plugin/antigravity/plugin.json", "fr/plugin/antigravity/plugin.json" })
            Assert.InRange(JsonNode.Parse(File.ReadAllText(Path.Join(Repo, manifest)))!["description"]!.ToString().Length, 120, 160);
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

    [Theory]
    [InlineData("fresh", "register NexusRelay|start NexusRelay", "NexusRelay=Running new.exe")]
    [InlineData("legacy", "stop Nexus Relay|unregister Nexus Relay|register NexusRelay|start NexusRelay", "NexusRelay=Running new.exe")]
    [InlineData("both", "stop Nexus Relay|unregister Nexus Relay|start NexusRelay", "NexusRelay=Running new.exe")]
    [InlineData("current", "", "NexusRelay=Running new.exe")]
    [InlineData("upgrade", "set NexusRelay|stop NexusRelay|start NexusRelay", "NexusRelay=Running new.exe")]
    [InlineData("shared", "stop Nexus Relay|unregister Nexus Relay", "")]
    [InlineData("remove", "stop NexusRelay|unregister NexusRelay|unregister Nexus Relay", "Other=Ready other.exe")]
    public void Scheduled_task_renamed_without_ever_two_supervisors(string scenario, string log, string tasks)
    {
        // The fixture's scheduler refuses a start while another relay task runs and the removal of a running task.
        var ran = Native.Pwsh(Path.Join(Repo, "tests", "native_restart", "task.ps1"), "-Scenario", scenario);
        Assert.True(ran.Code == 0, ran.Err + ran.Out);
        var data = JsonNode.Parse(ran.Out)!;
        Assert.Equal(log, string.Join("|", (data["log"] as JsonArray ?? []).Select(n => n!.ToString())));
        Assert.Equal(tasks, string.Join("|", data["tasks"]!.AsObject().Select(p => $"{p.Key}={p.Value}")));
    }

    [Fact]
    public void Installer_registers_the_task_only_through_the_migrating_helper()
    {
        string install = File.ReadAllText(Path.Join(Repo, "install.ps1"), Encoding.UTF8);
        Assert.Contains(@". (Join-Path $project 'scripts\RelayTask.ps1')", install);
        Assert.Contains("Sync-RelayTask -Executable $nativeExe", install);
        Assert.DoesNotContain("Register-ScheduledTask", install);
        Assert.DoesNotContain("'Nexus Relay'", install.Replace("@($relayProjectName, 'Nexus Relay')", ""));  // only the kept Antigravity project name
        string helper = File.ReadAllText(Path.Join(Repo, "scripts", "RelayTask.ps1"));
        Assert.Contains("$RelayTaskName = 'NexusRelay'", helper);
        Assert.Contains("$RelayLegacyTaskNames = @('Nexus Relay')", helper);
        // Every script the installer dot-sources ships in the release archive (its list is explicit).
        string release = File.ReadAllText(Path.Join(Repo, ".github", "workflows", "release.yml"));
        foreach (Match used in Regex.Matches(install, @"'scripts\\([\w-]+\.ps1)'"))
            Assert.Contains($"'{used.Groups[1].Value}'", release);
    }

    [Fact]
    public void Installer_recovers_when_stable_fails_health_check_despite_matching_stamp()
    {
        string state = Path.Join(iso.Home, ".nexus", "nexus-relay");
        Directory.CreateDirectory(state);
        string stable = Path.Join(state, "bin");
        Directory.CreateDirectory(stable);
        string oldBackup = Path.Join(state, "bin.old-20261102000000");
        Directory.CreateDirectory(oldBackup);

        // Copie complète d'une version saine dans bin.old (exécutable, runtimeconfig, DLLs)
        foreach (var file in Directory.EnumerateFiles(AppContext.BaseDirectory))
        {
            File.Copy(file, Path.Join(oldBackup, Path.GetFileName(file)));
        }

        // Calcul exact de nativeKey selon l'algorithme d'install.ps1:53-56
        var sources = Directory.EnumerateFiles(Path.Join(Repo, "native"), "*.*")
            .Where(f => new[] { ".cs", ".csproj", ".json" }.Contains(Path.GetExtension(f)))
            .Append(Path.Join(Repo, "Directory.Build.props"))
            .OrderBy(f => f)
            .Select(f => Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(File.ReadAllBytes(f))))
            .ToList();
        var digest = System.Security.Cryptography.SHA256.HashData(Encoding.UTF8.GetBytes(string.Join("", sources)));
        string nativeKey = Convert.ToHexString(digest.AsSpan(0, 8)).ToLowerInvariant();

        // Stable corrompu portant le stamp exact nativeKey : un probe non protégé lèverait
        // ApplicationFailedException sous $ErrorActionPreference='Stop' et bloquerait la reprise.
        File.WriteAllText(Path.Join(stable, "NexusRelay.exe"), "invalid dummy non-PE");
        File.WriteAllText(Path.Join(stable, "publish.complete"), nativeKey);

        string script = Path.Join(Repo, "install.ps1");
        // Utilisation de -PromoteOnly pour tester la bascule et la reprise de manière isolée sans toucher aux tâches planifiées
        var ran = Native.RunFile("pwsh.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-PromoteOnly"],
            env: new Dictionary<string, string?>
            {
                ["USERPROFILE"] = iso.Home,
                ["HOME"] = iso.Home,
                ["LOCALAPPDATA"] = Path.Join(iso.Root, "localappdata"),
            }, timeoutS: 60);
        Assert.True(ran.Code == 0, ran.Err + ran.Out);
        Assert.Contains("Promotion", ran.Out);

        // Après exécution : le stable a été réparé depuis le candidat et --schemas réussit
        var check = Native.RunFile(Path.Join(stable, "NexusRelay.exe"), ["--schemas"]);
        Assert.Equal(0, check.Code);
        // Le contrôle final ayant validé la santé, bin.old-* a été nettoyé
        Assert.Empty(Directory.EnumerateDirectories(state, "bin.old-*"));
    }

    [Fact]
    public void Installer_never_removes_the_versioned_engine_a_running_process_uses()
    {
        // NexusFree 1.3.3: a running .NET engine loads its DLLs from its folder on demand; deleting what it has not loaded
        // yet leaves it answering every call with an empty error. Old versions are cleaned at each install: the one an
        // engine still runs (here started through another spelling of the path, as from an MSIX-redirected or
        // elevated view whose process path the installer cannot match) must stay whole, an unused one goes.
        string versions = Path.Join(iso.Root, "localappdata", "NexusRelay", "versions");
        string inUse = Path.Join(versions, "0000000000000001"), current = Path.Join(versions, "0000000000000002"), unused = Path.Join(versions, "0000000000000003");
        foreach (string folder in new[] { inUse, current, unused })
        {
            Directory.CreateDirectory(folder);
            foreach (string file in Directory.EnumerateFiles(AppContext.BaseDirectory)) File.Copy(file, Path.Join(folder, Path.GetFileName(file)));
            File.WriteAllText(Path.Join(folder, "engine.lock"), "");  // as install.ps1 writes it in each version
        }
        int files = Directory.EnumerateFiles(inUse).Count();
        string alias = Path.Join(iso.Root, "alias");
        Assert.Equal(0, Native.RunFile("cmd.exe", ["/d", "/c", "mklink", "/J", alias, versions]).Code);

        using var engine = Process.Start(Native.Start(Path.Join(alias, "0000000000000001", "NexusRelay.exe"), ["--mcp", "--agent", "claude", "--protocol-fixture"]))!;
        try
        {
            string Ask(JsonObject request)
            {
                engine.StandardInput.WriteLine(request.ToJsonString()); engine.StandardInput.Flush();
                var line = engine.StandardOutput.ReadLineAsync();
                Assert.True(line.Wait(30_000), "engine did not answer");
                return line.Result ?? "";
            }
            Assert.Contains("\"result\"", Ask(Native.Rpc("initialize")));

            string script = $". '{Path.Join(Repo, "scripts", "Remove-RelayVersions.ps1")}'; Remove-StaleRelayVersions -Versions '{versions}' -Keep '0000000000000002'";
            var ran = Native.RunFile("pwsh.exe", ["-NoProfile", "-NonInteractive", "-Command", script], timeoutS: 60);
            Assert.True(ran.Code == 0, ran.Err + ran.Out);

            Assert.Equal(files, Directory.EnumerateFiles(inUse).Count());  // the running engine's folder is left whole
            Assert.True(Directory.Exists(current));
            Assert.False(Directory.Exists(unused));
            Assert.Equal([inUse, current], Directory.EnumerateDirectories(versions).Order(StringComparer.OrdinalIgnoreCase));  // no leftover beside them
            string status = Ask(Native.Call("status"));
            Assert.DoesNotContain("\"isError\":true", status);
        }
        finally
        {
            if (!engine.HasExited) { engine.Kill(true); engine.WaitForExit(10_000); }
            Directory.Delete(alias);  // the junction only, never its target
        }
    }

    private static string Cleanup(string body) =>
        $". '{Path.Join(Repo, "scripts", "Remove-RelayVersions.ps1")}'; {body}";

    [Fact]
    public void Cleanup_keeps_a_folder_whose_engine_lock_is_held()
    {
        // The lock check must decide on its own, in the case where Windows accepts to rename the folder aside. A handle
        // opened in the folder makes Windows refuse the rename and would hide a missing check, so the lock is held
        // through a hard link outside the folder: the same file (an exclusive open is refused), no open name inside.
        string versions = Path.Join(iso.Root, "versions"), held = Path.Join(versions, "0000000000000001"), keep = Path.Join(versions, "0000000000000002");
        foreach (string folder in new[] { held, keep })
        {
            Directory.CreateDirectory(folder);
            File.WriteAllText(Path.Join(folder, "NexusRelay.exe"), "engine");
        }
        File.WriteAllText(Path.Join(keep, "engine.lock"), "");
        string outside = Path.Join(iso.Root, "held-elsewhere.lock");
        File.WriteAllText(outside, "");
        Assert.True(CreateHardLinkW(Path.Join(held, "engine.lock"), outside, IntPtr.Zero), $"hard link: {Marshal.GetLastPInvokeError()}");
        string script = Cleanup($"Remove-StaleRelayVersions -Versions '{versions}' -Keep '0000000000000002' -Images ([pscustomobject]@{{ Paths = @(); Unknown = $false }})");
        using (var engine = new FileStream(outside, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        {
            // Precondition: Windows would rename this folder, and its lock is held.
            Directory.Move(held, held + ".probe"); Directory.Move(held + ".probe", held);
            Assert.Throws<IOException>(() => new FileStream(Path.Join(held, "engine.lock"), FileMode.Open, FileAccess.ReadWrite, FileShare.None).Dispose());
            var ran = Native.RunFile("pwsh.exe", ["-NoProfile", "-NonInteractive", "-Command", script], timeoutS: 60);
            Assert.True(ran.Code == 0, ran.Err + ran.Out);
            Assert.True(File.Exists(Path.Join(held, "NexusRelay.exe")));
            Assert.Equal([held, keep], Directory.EnumerateDirectories(versions).Order(StringComparer.OrdinalIgnoreCase));
        }
        var after = Native.RunFile("pwsh.exe", ["-NoProfile", "-NonInteractive", "-Command", script], timeoutS: 60);
        Assert.True(after.Code == 0, after.Err + after.Out);
        Assert.Equal([keep], Directory.EnumerateDirectories(versions));  // released: removed at the next install
    }

    [Fact]
    public void Cleanup_removes_nothing_while_a_running_engine_cannot_be_read()
    {
        string versions = Path.Join(iso.Root, "versions"), old = Path.Join(versions, "0000000000000001");
        Directory.CreateDirectory(old);
        File.WriteAllText(Path.Join(old, "NexusRelay.exe"), "engine");
        var ran = Native.RunFile("pwsh.exe", ["-NoProfile", "-NonInteractive", "-Command",
            Cleanup($"Remove-StaleRelayVersions -Versions '{versions}' -Keep 'none' -Images ([pscustomobject]@{{ Paths = @(); Unknown = $true }})")], timeoutS: 60);
        Assert.True(ran.Code == 0, ran.Err + ran.Out);
        Assert.True(File.Exists(Path.Join(old, "NexusRelay.exe")));
    }

    [Fact]
    public void Engine_images_are_read_by_QueryFullProcessImageName_and_an_engine_holds_its_lock()
    {
        string folder = Path.Join(iso.Root, "versions", "0000000000000001");
        Directory.CreateDirectory(folder);
        foreach (string file in Directory.EnumerateFiles(AppContext.BaseDirectory)) File.Copy(file, Path.Join(folder, Path.GetFileName(file)));
        File.WriteAllText(Path.Join(folder, "engine.lock"), "");
        using var engine = Process.Start(Native.Start(Path.Join(folder, "NexusRelay.exe"), ["--mcp", "--agent", "claude", "--protocol-fixture"]))!;
        try
        {
            engine.StandardInput.WriteLine(Native.Rpc("initialize").ToJsonString()); engine.StandardInput.Flush();
            Assert.True(engine.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken).AsTask().Wait(30_000, TestContext.Current.CancellationToken), "engine did not answer");
            Assert.Throws<IOException>(() => new FileStream(Path.Join(folder, "engine.lock"), FileMode.Open, FileAccess.ReadWrite, FileShare.Delete).Dispose());
            var ran = Native.RunFile("pwsh.exe", ["-NoProfile", "-NonInteractive", "-Command",
                Cleanup("$i = Get-RelayEngineImages; \"$($i.Unknown)\"; $i.Paths")], timeoutS: 60);
            Assert.True(ran.Code == 0, ran.Err + ran.Out);
            string[] lines = ran.Out.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
            Assert.Contains(Path.Join(folder, "NexusRelay.exe"), lines, StringComparer.OrdinalIgnoreCase);
        }
        finally
        {
            if (!engine.HasExited) { engine.Kill(true); engine.WaitForExit(10_000); }
        }
    }

    [LibraryImport("kernel32.dll", EntryPoint = "CreateHardLinkW", StringMarshalling = StringMarshalling.Utf16, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool CreateHardLinkW(string link, string existing, IntPtr security);

    [LibraryImport("advapi32.dll", SetLastError = true)]
    private static partial uint SetSecurityInfo(IntPtr handle, int objectType, uint information, IntPtr owner, IntPtr group, IntPtr dacl, IntPtr sacl);

    /// <summary>PowerShell prefix removing SeDebugPrivilege from its own token (no-op when the token lacks it).</summary>
    private const string DropDebugPrivilege = """
        Add-Type -TypeDefinition @'
        using System; using System.Runtime.InteropServices;
        public static class NexusRelayNoDebug {
            [StructLayout(LayoutKind.Sequential, Pack = 4)] struct Privileges { public int Count; public long Luid; public int Attributes; }
            [DllImport("advapi32.dll", SetLastError = true)] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
            [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)] static extern bool LookupPrivilegeValue(string system, string name, out long luid);
            [DllImport("advapi32.dll", SetLastError = true)] static extern bool AdjustTokenPrivileges(IntPtr token, bool none, ref Privileges state, int length, IntPtr previous, IntPtr size);
            [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
            [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
            public static void Drop() {
                IntPtr token; long luid;
                if (!OpenProcessToken(GetCurrentProcess(), 0x28, out token)) return;  // TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY
                try {
                    if (!LookupPrivilegeValue(null, "SeDebugPrivilege", out luid)) return;
                    var state = new Privileges { Count = 1, Luid = luid, Attributes = 4 };  // SE_PRIVILEGE_REMOVED
                    AdjustTokenPrivileges(token, false, ref state, 0, IntPtr.Zero, IntPtr.Zero);
                } finally { CloseHandle(token); }
            }
        }
        '@
        [NexusRelayNoDebug]::Drop();

        """;

    /// <summary>Sets a process object's DACL: empty (nobody may open it, its owner included) or, with null, NULL (open).</summary>
    private static unsafe void SetProcessDacl(Process process, byte[]? dacl)
    {
        fixed (byte* acl = dacl)
            Assert.Equal(0u, SetSecurityInfo(process.Handle, 6 /* SE_KERNEL_OBJECT */, 4 /* DACL_SECURITY_INFORMATION */, IntPtr.Zero, IntPtr.Zero, (IntPtr)acl, IntPtr.Zero));
    }

    private static byte[] EmptyDacl()
    {
        var acl = new System.Security.AccessControl.RawAcl(System.Security.AccessControl.RawAcl.AclRevision, 0);
        byte[] binary = new byte[acl.BinaryLength];
        acl.GetBinaryForm(binary, 0);
        return binary;
    }

    private static void CopyEngine(string folder)
    {
        Directory.CreateDirectory(folder);
        foreach (string file in Directory.EnumerateFiles(AppContext.BaseDirectory)) File.Copy(file, Path.Join(folder, Path.GetFileName(file)), true);
    }

    [Fact]
    public void Cleanup_removes_nothing_while_a_real_running_engine_cannot_be_queried()
    {
        // A real NexusRelay.exe that QueryFullProcessImageNameW cannot open (empty DACL on its process object: even its
        // owner may not query it, as for an engine of another user): no folder is removed while it runs.
        string versions = Path.Join(iso.Root, "versions"), old = Path.Join(versions, "0000000000000001");
        Directory.CreateDirectory(old);
        File.WriteAllText(Path.Join(old, "NexusRelay.exe"), "engine");
        string elsewhere = Path.Join(iso.Root, "unreadable");
        CopyEngine(elsewhere);
        using var engine = Process.Start(Native.Start(Path.Join(elsewhere, "NexusRelay.exe"), ["--mcp", "--agent", "claude", "--protocol-fixture"]))!;
        try
        {
            engine.StandardInput.WriteLine(Native.Rpc("initialize").ToJsonString()); engine.StandardInput.Flush();
            Assert.True(engine.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken).AsTask().Wait(30_000, TestContext.Current.CancellationToken), "engine did not answer");
            SetProcessDacl(engine, EmptyDacl());
            // An elevated runner may hold SeDebugPrivilege enabled, which opens any process: the cleanup's PowerShell
            // drops it first, as an ordinary user has it.
            var ran = Native.RunFile("pwsh.exe", ["-NoProfile", "-NonInteractive", "-Command", DropDebugPrivilege + Cleanup(
                $"\"probe=$($null -eq [NexusRelayProcessImage]::Read({engine.Id}))\"; \"unknown=$((Get-RelayEngineImages).Unknown)\"; " +
                $"Remove-StaleRelayVersions -Versions '{versions}' -Keep 'none'")], timeoutS: 60);
            Assert.True(ran.Code == 0, ran.Err + ran.Out);
            Assert.Contains("probe=True", ran.Out, StringComparison.Ordinal);  // precondition: the engine cannot be queried
            Assert.Contains("unknown=True", ran.Out, StringComparison.Ordinal);
            Assert.True(File.Exists(Path.Join(old, "NexusRelay.exe")));
        }
        finally
        {
            if (!engine.HasExited) { SetProcessDacl(engine, null); engine.Kill(true); engine.WaitForExit(10_000); }
        }
    }

    [Fact]
    public void An_engine_run_from_the_stable_copy_holds_no_lock_there()
    {
        // ~\.nexus\nexus-relay\bin is replaced at every update: an engine started from it (here a bin from 1.13.10, which
        // still carries an engine.lock) must leave every file in it free.
        string bin = Path.Join(iso.Home, ".nexus", "nexus-relay", "bin");
        CopyEngine(bin);
        File.WriteAllText(Path.Join(bin, "engine.lock"), "");
        using var engine = Process.Start(Native.Start(Path.Join(bin, "NexusRelay.exe"), ["--mcp", "--agent", "claude", "--protocol-fixture"]))!;
        try
        {
            engine.StandardInput.WriteLine(Native.Rpc("initialize").ToJsonString()); engine.StandardInput.Flush();
            Assert.True(engine.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken).AsTask().Wait(30_000, TestContext.Current.CancellationToken), "engine did not answer");
            new FileStream(Path.Join(bin, "engine.lock"), FileMode.Open, FileAccess.ReadWrite, FileShare.None).Dispose();
        }
        finally
        {
            if (!engine.HasExited) { engine.Kill(true); engine.WaitForExit(10_000); }
        }
    }

    [Fact]
    public void Update_replaces_the_stable_copy_while_a_process_started_from_it_waits()
    {
        // 1.13.10: a Stop hook (--await-replies, up to 55 min) ran from bin and held bin\engine.lock; the next update
        // failed to rename bin aside and stopped before the pointer, the task and the clients. Here a 1.13.10-like
        // waiter runs from bin (its images mapped there) and holds bin\engine.lock: the update must still succeed.
        string state = Path.Join(iso.Home, ".nexus", "nexus-relay"), bin = Path.Join(state, "bin");
        CopyEngine(bin);
        File.WriteAllText(Path.Join(bin, "engine.lock"), "");
        File.WriteAllText(Path.Join(bin, "publish.complete"), "0000000000000000");  // an older version
        var env = new Dictionary<string, string?> { ["USERPROFILE"] = iso.Home, ["HOME"] = iso.Home, ["LOCALAPPDATA"] = Path.Join(iso.Root, "localappdata") };
        string[] promote = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", Path.Join(Repo, "install.ps1"), "-PromoteOnly"];
        using var waiter = Process.Start(Native.Start(Path.Join(bin, "NexusRelay.exe"), ["--mcp", "--agent", "claude", "--protocol-fixture"]))!;
        var heldLock = new FileStream(Path.Join(bin, "engine.lock"), FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        try
        {
            waiter.StandardInput.WriteLine(Native.Rpc("initialize").ToJsonString()); waiter.StandardInput.Flush();
            Assert.True(waiter.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken).AsTask().Wait(30_000, TestContext.Current.CancellationToken), "engine did not answer");
            Assert.ThrowsAny<IOException>(() => Directory.Move(bin, bin + ".probe"));  // precondition: Windows refuses the rename

            var ran = Native.RunFile("pwsh.exe", promote, env: env, timeoutS: 300);
            Assert.True(ran.Code == 0, ran.Err + ran.Out);
            Assert.Equal(NativeKey(), File.ReadAllText(Path.Join(bin, "publish.complete")));
            Assert.Equal(0, Native.RunFile(Path.Join(bin, "NexusRelay.exe"), ["--schemas"]).Code);
            waiter.StandardInput.WriteLine(Native.Call("status").ToJsonString()); waiter.StandardInput.Flush();
            var status = waiter.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken).AsTask();
            Assert.True(status.Wait(30_000, TestContext.Current.CancellationToken), "the waiting engine stopped answering");
            Assert.DoesNotContain("\"isError\":true", status.Result);  // its files, moved aside, stay whole
        }
        finally
        {
            heldLock.Dispose();
            if (!waiter.HasExited) { waiter.Kill(true); waiter.WaitForExit(10_000); }
        }
        // Once the waiter is gone: the next run removes the old lock from bin and the copy set aside.
        var after = Native.RunFile("pwsh.exe", promote, env: env, timeoutS: 300);
        Assert.True(after.Code == 0, after.Err + after.Out);
        Assert.False(File.Exists(Path.Join(bin, "engine.lock")));
        Assert.Empty(Directory.EnumerateDirectories(state, "bin.old-*"));
    }

    [Fact]
    public void Stop_hook_waits_from_the_versioned_copy_named_in_engine_txt()
    {
        // The asyncRewake Stop hook waits up to 55 min: it runs the immutable versioned copy named in engine.txt (as the
        // Antigravity bridge), never bin, so nothing long-running holds the folder an update replaces. Run by Git's bash,
        // as Claude Code runs hook commands on Windows; with no bin, only the versioned copy can answer.
        var hooks = JsonNode.Parse(File.ReadAllText(Path.Join(Repo, "plugin", "hooks", "hooks.json")))!;
        string command = hooks["hooks"]!["Stop"]!.AsArray().SelectMany(g => g!["hooks"]!.AsArray())
            .Single(h => h!["asyncRewake"]?.GetValue<bool>() == true)!["command"]!.ToString();
        Assert.Contains("--await-replies claude", command, StringComparison.Ordinal);
        string bash = Environment.GetEnvironmentVariable("PATH")!.Split(';', StringSplitOptions.RemoveEmptyEntries)
            .Select(d => Path.GetFullPath(Path.Join(d, "..", "bin", "bash.exe"))).Append(@"C:\Program Files\Git\bin\bash.exe")
            .First(p => File.Exists(p) && File.Exists(Path.Join(Path.GetDirectoryName(p)!, "..", "cmd", "git.exe")));
        string state = Path.Join(iso.Home, ".nexus", "nexus-relay"), bin = Path.Join(state, "bin");
        CopyEngine(Path.Join(iso.Home, "AppData", "Local", "NexusRelay", "versions", "0000000000000001"));
        Directory.CreateDirectory(state);
        var env = new Dictionary<string, string?> { ["HOME"] = iso.Home, ["USERPROFILE"] = iso.Home };
        Ran Hook() => Native.RunFile(bash, ["-c", command], input: "{}", env: env, timeoutS: 60);

        File.WriteAllText(Path.Join(state, "engine.txt"), $"{RelayPaths.Version}|AppData\\Local\\NexusRelay\\versions\\0000000000000001\\NexusRelay.exe\r\n");
        var versioned = Hook();
        Assert.True(versioned.Code == 0, versioned.Err + versioned.Out);
        Assert.False(Directory.Exists(bin));

        File.Delete(Path.Join(state, "engine.txt"));  // no pointer (engine before 1.13.8): the stable copy
        Assert.NotEqual(0, Hook().Code);  // nothing to run yet: the hook does run an engine
        CopyEngine(bin);
        var stable = Hook();
        Assert.True(stable.Code == 0, stable.Err + stable.Out);
    }

    private static string NativeKey()
    {
        var sources = Directory.EnumerateFiles(Path.Join(Repo, "native"), "*.*")
            .Where(f => new[] { ".cs", ".csproj", ".json" }.Contains(Path.GetExtension(f)))
            .Append(Path.Join(Repo, "Directory.Build.props"))
            .OrderBy(f => f)
            .Select(f => Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(File.ReadAllBytes(f))))
            .ToList();
        var digest = System.Security.Cryptography.SHA256.HashData(Encoding.UTF8.GetBytes(string.Join("", sources)));
        return Convert.ToHexString(digest.AsSpan(0, 8)).ToLowerInvariant();
    }

    [Fact]
    public void No_build_output_in_the_synchronized_repository()
    {
        foreach (string folder in new[] { "native", Path.Join("tests", "NexusRelay.Tests"), Path.Join("tests", "native_lifetime") })
            foreach (string output in new[] { "bin", "obj" })
                Assert.False(Directory.Exists(Path.Join(Repo, folder, output)), Path.Join(folder, output));
    }
}
