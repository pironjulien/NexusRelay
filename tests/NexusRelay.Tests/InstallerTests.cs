using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusRelay.Tests;

/// <summary>Workstation settings, the installer (NexusRelay.exe --install), its starters and repository conventions.</summary>
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
    public void Antigravity_activation_repairs_a_missing_plugin_and_keeps_disabled_choices()
    {
        var config = JsonNode.Parse("""{"plugins": {"other": {"enabled": true, "nested": "keep"}}, "sidecars": {"nexus-relay/relay": {"enabled": true, "projectId": "fixture-project", "schemaVersion": 1}}, "userSettings": {"keep": "value"}}""")!.AsObject();
        var original = config.DeepClone();
        var result = AntigravityActivation.Apply(config, "fixture-project");
        Assert.True(result.Changed);
        Assert.True(result.Config["plugins"]!["nexus-relay"]!["enabled"]!.GetValue<bool>());
        Assert.True(JsonNode.DeepEquals(result.Config["userSettings"], original["userSettings"]));
        Assert.True(JsonNode.DeepEquals(result.Config["plugins"]!["other"], original["plugins"]!["other"]));
        Assert.Equal(1, result.Config["sidecars"]!["nexus-relay/relay"]!["schemaVersion"]!.GetValue<int>());
        Assert.False(AntigravityActivation.Apply(result.Config.DeepClone().AsObject(), "fixture-project").Changed);
        Assert.True(AntigravityActivation.Apply(result.Config.DeepClone().AsObject(), "another-project").Changed);  // the project id follows
        foreach (var (section, name) in new[] { ("plugins", "nexus-relay"), ("sidecars", "nexus-relay/relay") })
        {
            var disabled = result.Config.DeepClone().AsObject();
            disabled[section]![name]!["enabled"] = false;
            var preserved = AntigravityActivation.Apply(disabled.DeepClone().AsObject(), "fixture-project");
            Assert.True(preserved.Disabled);
            Assert.False(preserved.Changed);
            Assert.True(JsonNode.DeepEquals(preserved.Config, disabled));
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

    [Fact]
    public void Shared_token_is_created_once_then_reused_by_every_installer()
    {
        string Token(string userHome, string? explicitFile = null, string? legacy = null)
        {
            string file = SharedToken.ResolveFile([explicitFile], userHome, legacy);
            return file + "\n" + File.ReadAllText(file).Trim();
        }
        // Two installers in a row on a blank profile (for instance Android, then Relay) end with the same token.
        string blank = Path.Join(iso.Root, "blank");
        string first = Token(blank), second = Token(blank);
        Assert.Equal(first, second);
        var lines = first.Split('\n', StringSplitOptions.TrimEntries);
        Assert.Equal(Path.Join(blank, ".nexus", "mcp-token.txt"), lines[0]);
        Assert.Matches("^[0-9a-f]{64}$", lines[1]);

        // At the same time: exclusive creation, one winner, one token.
        string racing = Path.Join(iso.Root, "racing");
        var both = Enumerable.Range(0, 4).Select(_ => Task.Run(() => Token(racing))).ToArray();
        Task.WaitAll(both, TestContext.Current.CancellationToken);
        Assert.Single(both.Select(t => t.Result).Distinct());

        // An existing file (NEXUS_MCP_TOKEN_FILE, synced workstation token) is never regenerated.
        string synced = Path.Join(iso.Root, "synced", "token.txt");
        Directory.CreateDirectory(Path.GetDirectoryName(synced)!);
        File.WriteAllText(synced, "workstation-token-workstation-token-0123\n");
        Assert.Equal($"{synced}\nworkstation-token-workstation-token-0123", Token(blank, synced));

        // An empty shared file is not a token; the retired relay-only token is carried over, not lost, and never chosen.
        string migrated = Path.Join(iso.Root, "migrated");
        Directory.CreateDirectory(Path.Join(migrated, ".nexus", "nexus-relay"));
        File.WriteAllText(Path.Join(migrated, ".nexus", "mcp-token.txt"), "");
        string legacy = Path.Join(migrated, ".nexus", "nexus-relay", "token.txt");
        File.WriteAllText(legacy, new string('a', 64));
        Assert.Equal($"{Path.Join(migrated, ".nexus", "mcp-token.txt")}\n{new string('a', 64)}", Token(migrated, legacy, legacy));
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
        Assert.Equal(token, SharedToken.ResolveFile([], profile, null));
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
        // An older get.ps1 runs install.ps1 under Windows PowerShell 5.1: without a BOM it reads UTF-8 as ANSI; get.ps1
        // itself stays ASCII.
        byte[] install = File.ReadAllBytes(Path.Join(Repo, "install.ps1"));
        Assert.True(install[0] == 0xEF && install[1] == 0xBB && install[2] == 0xBF);
        Assert.All(File.ReadAllBytes(Path.Join(Repo, "get.ps1")), b => Assert.True(b < 128));
    }

    [Fact]
    public void Installer_messages_are_english_unless_windows_shows_french()
    {
        // Rule of the public Nexus products: English by default, French when Windows is displayed in French. Every
        // message of the starters that carries French text goes through Tr (English first); the engine's installer
        // follows the UI culture of Windows.
        foreach (string script in new[] { "install.ps1", Path.Join("scripts", "install_commit_guard.ps1") })
        {
            string text = File.ReadAllText(Path.Join(Repo, script), Encoding.UTF8);
            Assert.Contains("CurrentUICulture.TwoLetterISOLanguageName -eq 'fr'", text);
            foreach (string line in text.Split('\n').Select(l => l.Trim()))
            {
                if (line.StartsWith('#') || !Regex.IsMatch(line, @"\b(throw|Write-(Output|Warning|Error|Host))\b")) continue;
                if (Regex.IsMatch(line, "[éèàêçôù«]")) Assert.True(Regex.IsMatch(line, @"[({]\s*Tr\s"), $"{script}: {line}");
            }
        }
        bool saved = InstallText.French;
        try
        {
            InstallText.French = false;
            Assert.Equal("en", InstallText.Tr("en", "fr"));
            InstallText.French = true;
            Assert.Equal("fr", InstallText.Tr("en", "fr"));
        }
        finally { InstallText.French = saved; }
        // Every French message of the engine's installer is the second argument of Tr.
        foreach (string file in new[] { "Installer.cs", "InstallSupport.cs" })
            foreach (string line in File.ReadAllLines(Path.Join(Repo, "native", file)).Where(l => Regex.IsMatch(l, "[éèàêçôù«]") && !l.TrimStart().StartsWith("//", StringComparison.Ordinal)))
                Assert.True(Regex.IsMatch(line, @"Tr\(|french\s*\?|^\s*\?\s"""), $"{file}: {line.Trim()}");  // Tr, or the French branch of a choice on the lang setting
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
        // get.ps1 refuses releases whose engine cannot install itself: its floor is at most this version.
        var floor = Regex.Match(File.ReadAllText(Path.Join(Repo, "get.ps1")), @"\$firstNative = \[version\]'([\d.]+)'");
        Assert.True(floor.Success && Version.Parse(floor.Groups[1].Value) <= Version.Parse(RelayPaths.Version));
    }

    [Fact]
    public void Antigravity_marketplace_copy_bridges_to_the_local_daemon()
    {
        // A Marketplace copy cannot name this PC's token (Antigravity expands only ${PLUGIN_ROOT}): like NexusWindows,
        // NexusBrowser and NexusAndroid, a fixed command (cmd.exe) runs the plugin's own launcher, ${PLUGIN_ROOT} in
        // the arguments; the launcher finds the installed engine (engine.txt) and starts its stdio bridge (see
        // BridgeTests). The installer replaces this declaration with its own and adds the sidecar.
        string bundle = Path.Join(Repo, "plugin", "antigravity");
        var server = JsonNode.Parse(File.ReadAllText(Path.Join(bundle, "mcp_config.json")))!["mcpServers"]!["nexus-relay"]!;
        Assert.Equal("cmd.exe", server["command"]!.ToString());
        Assert.Equal(["/d", "/c", @"${PLUGIN_ROOT}\scripts\start.cmd", "http://127.0.0.1:16181/mcp?agent=antigravity"], server["args"]!.AsArray().Select(a => a!.ToString()));
        Assert.Equal(["start.cmd"], Directory.EnumerateFiles(Path.Join(bundle, "scripts")).Select(Path.GetFileName));  // no script compiled at each start
        Assert.Contains("\r\n", File.ReadAllText(Path.Join(bundle, "scripts", "start.cmd")), StringComparison.Ordinal);  // cmd.exe labels need CRLF
        Assert.True(File.Exists(Path.Join(bundle, "README.md")));
        foreach (string manifest in new[] { "plugin/antigravity/plugin.json", "fr/plugin/antigravity/plugin.json" })
            Assert.InRange(JsonNode.Parse(File.ReadAllText(Path.Join(Repo, manifest)))!["description"]!.ToString().Length, 120, 160);
    }

    [Fact]
    public void Starters_carry_no_installation_logic_and_updates_run_no_powershell()
    {
        // get.ps1 and install.ps1 only download, check, build and start the engine: every installation step is in
        // NexusRelay.exe --install. The daemon's update starts the engine itself (--update), never PowerShell.
        string[] logic = ["Register-ScheduledTask", "Start-ScheduledTask", "SetEnvironmentVariable", "robocopy", "--register-hooks", "--register-clients", "mcp_config", "sidecar.json", "daemons\\specs", "Copy-Item"];
        foreach (string starter in new[] { "get.ps1", "install.ps1" })
        {
            string text = File.ReadAllText(Path.Join(Repo, starter));
            foreach (string step in logic) Assert.False(text.Contains(step, StringComparison.OrdinalIgnoreCase), $"{starter}: {step}");
            Assert.Contains("--install", text);
        }
        string updater = File.ReadAllText(Path.Join(Repo, "native", "NativeUpdater.cs"));
        Assert.DoesNotContain("powershell", updater, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("\"--update\"", updater);
        Assert.False(Directory.Exists(Path.Join(Repo, "tests", "native_restart")));
        Assert.Equal(["install_commit_guard.ps1"], Directory.EnumerateFiles(Path.Join(Repo, "scripts"), "*.ps1").Select(Path.GetFileName));
        // The release archive ships what an older get.ps1 runs (install.ps1) and the files the engine deploys.
        string release = File.ReadAllText(Path.Join(Repo, ".github", "workflows", "release.yml"));
        foreach (string shipped in new[] { "'install.ps1'", "'get.ps1'", "'codex-projet'", "'claude-projet'", "'plugin/antigravity'", "'fr/plugin/antigravity'" })
            Assert.Contains(shipped, release);
        Assert.Equal(RelayPaths.Version, Native.Run("--version").Out.Trim());
    }

    [Fact]
    public void Release_checksums_are_read_strictly()
    {
        string sums = $"{new string('a', 64)}  NexusRelay-win-x64.zip\n{new string('b', 64)} *get.ps1\n";
        Assert.Equal(new string('a', 64), SelfUpdate.ExpectedHash(sums, "NexusRelay-win-x64.zip"));
        Assert.Equal(new string('b', 64), SelfUpdate.ExpectedHash(sums, "get.ps1"));
        Assert.Throws<InvalidDataException>(() => SelfUpdate.ExpectedHash(sums, "NexusRelay-win-arm64.zip"));
        Assert.Throws<InvalidDataException>(() => SelfUpdate.ExpectedHash(sums + $"{new string('c', 64)}  get.ps1\n", "get.ps1"));
        Assert.Throws<InvalidDataException>(() => SelfUpdate.ExpectedHash("not a checksum line\n", "get.ps1"));
        Assert.Matches(SelfUpdate.Tag(), "v1.14.0");
        Assert.Matches(SelfUpdate.Tag(), "v1.14.0-dev.3");
        Assert.DoesNotMatch(SelfUpdate.Tag(), "1.14.0");
        Assert.True(SelfUpdate.Rid() is "win-x64" or "win-arm64");
    }

    // ------------------------------------------------------------------------------------------- scheduled task

    /// <summary>An in-memory Task Scheduler that refuses what would leave two supervisors: a start while another
    /// relay task runs, or the removal of a running task.</summary>
    private sealed class FakeTasks : ITaskStore
    {
        public readonly Dictionary<string, (string Xml, string State)> Tasks = [];
        public readonly List<string> Log = [];
        public void Add(string name, string exe, string state) => Tasks[name] = (TaskXml.Logon("fixture", exe, "--daemon", "state", @"PC\user"), state);
        public string? Xml(string name) => Tasks.TryGetValue(name, out var task) ? task.Xml : null;
        public string State(string name) => Tasks.TryGetValue(name, out var task) ? task.State : "Absent";
        public void Register(string name, string xml)
        {
            Log.Add((Tasks.ContainsKey(name) ? "set " : "register ") + name);
            Tasks[name] = (xml, Tasks.TryGetValue(name, out var old) ? old.State : "Ready");
        }
        public void Stop(string name) { Log.Add("stop " + name); Tasks[name] = (Tasks[name].Xml, "Ready"); }
        public void Delete(string name)
        {
            Assert.False(Tasks[name].State == "Running", $"unregistered while running: {name}");
            Log.Add("unregister " + name); Tasks.Remove(name);
        }
        public void Run(string name)
        {
            var running = Tasks.Where(t => t.Value.State == "Running").Select(t => t.Key).ToList();
            Assert.True(running.Count == 0, $"two supervisors: {name} while {string.Join(", ", running)} runs");
            Log.Add("start " + name); Tasks[name] = (Tasks[name].Xml, "Running");
        }
        public string Summary() => string.Join("|", Tasks.Select(t => $"{t.Key}={t.Value.State} {TaskXml.Command(t.Value.Xml)}"));
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
        var store = new FakeTasks();
        switch (scenario)
        {
            case "legacy": store.Add("Nexus Relay", "old.exe", "Running"); break;
            case "both": store.Add("Nexus Relay", "old.exe", "Running"); store.Add("NexusRelay", "new.exe", "Ready"); break;
            case "current": store.Add("NexusRelay", "new.exe", "Running"); break;
            case "upgrade": store.Add("NexusRelay", "old.exe", "Running"); break;
            case "shared": store.Add("Nexus Relay", "old.exe", "Running"); break;
            case "remove": store.Add("Nexus Relay", "old.exe", "Ready"); store.Add("NexusRelay", "new.exe", "Running"); store.Add("Other", "other.exe", "Ready"); break;
        }
        if (scenario == "remove") RelayTask.Remove(store, [RelayTask.Name, .. RelayTask.Legacy]);
        else RelayTask.Sync(store, "new.exe", "state", scenario == "shared", @"PC\user");
        Assert.Equal(log, string.Join("|", store.Log));
        Assert.Equal(tasks, store.Summary());
        if (scenario == "fresh")
        {
            var task = System.Xml.Linq.XDocument.Parse(store.Tasks["NexusRelay"].Xml).Root!;
            Assert.Equal("--daemon", task.Descendants(TaskXml.Ns + "Arguments").Single().Value);
            Assert.Equal("true", task.Descendants(TaskXml.Ns + "Hidden").Single().Value);
            Assert.Equal(@"PC\user", task.Descendants(TaskXml.Ns + "LogonTrigger").Single().Element(TaskXml.Ns + "UserId")!.Value);
        }
    }

    // ------------------------------------------------------------------------------------------- engine folders

    [Fact]
    public void Processes_started_by_tests_inherit_the_no_error_box_mode()
    {
        // NoErrorBoxes (TestSupport.cs): set in the test process, inherited by its children, so no Windows error box
        // (invalid image, missing file, crash) can ever appear while the tests run.
        Assert.Equal(NoErrorBoxes.Mode, NoErrorBoxes.GetErrorMode() & NoErrorBoxes.Mode);
        var ran = Native.RunFile("pwsh.exe", ["-NoProfile", "-NonInteractive", "-Command",
            "Add-Type -Namespace NexusRelayTests -Name ErrorMode -MemberDefinition '[DllImport(\"kernel32.dll\")] public static extern uint GetErrorMode();'; [NexusRelayTests.ErrorMode]::GetErrorMode()"], timeoutS: 60);
        Assert.True(ran.Code == 0, ran.Err + ran.Out);
        Assert.Equal(NoErrorBoxes.Mode, uint.Parse(ran.Out.Trim(), System.Globalization.CultureInfo.InvariantCulture) & NoErrorBoxes.Mode);
        Assert.False(InstallNative.Healthy(Path.Join(iso.Root, "absent.exe")));
        File.WriteAllText(Path.Join(iso.Root, "text.exe"), "not a program image");
        Assert.False(InstallNative.Healthy(Path.Join(iso.Root, "text.exe")));  // never started
    }

    /// <summary>The engine files of the test output (the project reference's framework-dependent build).</summary>
    private static void CopyEngine(string folder)
    {
        Directory.CreateDirectory(folder);
        foreach (string file in Directory.EnumerateFiles(AppContext.BaseDirectory).Where(f => Path.GetFileName(f) is var name && (name.StartsWith("NexusRelay.", StringComparison.Ordinal) && !name.StartsWith("NexusRelay.Tests", StringComparison.Ordinal) || name == "Tomlyn.dll")))
            File.Copy(file, Path.Join(folder, Path.GetFileName(file)), true);
    }

    private static Process StartEngine(string exe)
    {
        var engine = Process.Start(Native.Start(exe, ["--mcp", "--agent", "claude", "--protocol-fixture"]))!;
        engine.StandardInput.WriteLine(Native.Rpc("initialize").ToJsonString()); engine.StandardInput.Flush();
        Assert.True(engine.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken).AsTask().Wait(30_000, TestContext.Current.CancellationToken), "engine did not answer");
        return engine;
    }

    private static string Status(Process engine)
    {
        engine.StandardInput.WriteLine(Native.Call("status").ToJsonString()); engine.StandardInput.Flush();
        var line = engine.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken).AsTask();
        Assert.True(line.Wait(30_000, TestContext.Current.CancellationToken), "the engine stopped answering");
        return line.Result ?? "";
    }

    private static void Stop(Process engine)
    {
        if (!engine.HasExited) { engine.Kill(true); engine.WaitForExit(10_000); }
        engine.Dispose();
    }

    [Fact]
    public void Cleanup_never_removes_the_versioned_engine_a_running_process_uses()
    {
        // NexusFree 1.3.3: a running .NET engine loads its DLLs from its folder on demand; deleting what it has not loaded
        // yet leaves it answering every call with an empty error. The folder an engine still runs (here started through
        // another spelling of the path, as from an MSIX-redirected or elevated view) stays whole, an unused one goes.
        string versions = Path.Join(iso.Root, "localappdata", "NexusRelay", "versions");
        string inUse = Path.Join(versions, "0000000000000001"), current = Path.Join(versions, "0000000000000002"), unused = Path.Join(versions, "0000000000000003");
        foreach (string folder in new[] { inUse, current, unused })
        {
            CopyEngine(folder);
            File.WriteAllText(Path.Join(folder, "engine.lock"), "");  // as the installer writes it in each version
        }
        int files = Directory.EnumerateFiles(inUse).Count();
        string alias = Path.Join(iso.Root, "alias");
        Assert.Equal(0, Native.RunFile("cmd.exe", ["/d", "/c", "mklink", "/J", alias, versions]).Code);
        var engine = StartEngine(Path.Join(alias, "0000000000000001", "NexusRelay.exe"));
        try
        {
            EngineFolders.RemoveStale(versions, "0000000000000002", InstallNative.Images.Of());
            Assert.Equal(files, Directory.EnumerateFiles(inUse).Count());  // the running engine's folder is left whole
            Assert.True(Directory.Exists(current));
            Assert.False(Directory.Exists(unused));
            Assert.Equal([inUse, current], Directory.EnumerateDirectories(versions).Order(StringComparer.OrdinalIgnoreCase));  // no leftover beside them
            Assert.DoesNotContain("\"isError\":true", Status(engine));
        }
        finally
        {
            Stop(engine);
            Directory.Delete(alias);  // the junction only, never its target
        }
    }

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
        var none = new InstallNative.Images([], false);
        using (var engine = new FileStream(outside, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        {
            // Precondition: Windows would rename this folder, and its lock is held.
            Directory.Move(held, held + ".probe"); Directory.Move(held + ".probe", held);
            Assert.Throws<IOException>(() => new FileStream(Path.Join(held, "engine.lock"), FileMode.Open, FileAccess.ReadWrite, FileShare.None).Dispose());
            EngineFolders.RemoveStale(versions, "0000000000000002", none);
            Assert.True(File.Exists(Path.Join(held, "NexusRelay.exe")));
            Assert.Equal([held, keep], Directory.EnumerateDirectories(versions).Order(StringComparer.OrdinalIgnoreCase));
        }
        EngineFolders.RemoveStale(versions, "0000000000000002", none);
        Assert.Equal([keep], Directory.EnumerateDirectories(versions));  // released: removed at the next install
    }

    [Fact]
    public void Cleanup_removes_nothing_while_a_running_engine_cannot_be_read()
    {
        string versions = Path.Join(iso.Root, "versions"), old = Path.Join(versions, "0000000000000001");
        Directory.CreateDirectory(old);
        File.WriteAllText(Path.Join(old, "NexusRelay.exe"), "engine");
        EngineFolders.RemoveStale(versions, "none", new InstallNative.Images([], true));
        Assert.True(File.Exists(Path.Join(old, "NexusRelay.exe")));
    }

    [Fact]
    public void Engine_images_are_read_by_QueryFullProcessImageName_and_an_engine_holds_its_lock()
    {
        string folder = Path.Join(iso.Root, "versions", "0000000000000001");
        CopyEngine(folder);
        File.WriteAllText(Path.Join(folder, "engine.lock"), "");
        var engine = StartEngine(Path.Join(folder, "NexusRelay.exe"));
        try
        {
            Assert.Throws<IOException>(() => new FileStream(Path.Join(folder, "engine.lock"), FileMode.Open, FileAccess.ReadWrite, FileShare.Delete).Dispose());
            Assert.Contains(Path.Join(folder, "NexusRelay.exe"), InstallNative.Images.Of().Paths, StringComparer.OrdinalIgnoreCase);
            var read = InstallNative.Read(engine.Id);
            Assert.False(read.Exited);
            Assert.NotNull(read.Birth);
            Assert.True(EngineFolders.InUse(folder, new InstallNative.Images([], false)));  // by its lock alone
        }
        finally { Stop(engine); }
    }

    [LibraryImport("kernel32.dll", EntryPoint = "CreateHardLinkW", StringMarshalling = StringMarshalling.Utf16, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool CreateHardLinkW(string link, string existing, IntPtr security);

    [LibraryImport("advapi32.dll", SetLastError = true)]
    private static partial uint SetSecurityInfo(IntPtr handle, int objectType, uint information, IntPtr owner, IntPtr group, IntPtr dacl, IntPtr sacl);

    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    private struct Privileges { public int Count; public long Luid; public int Attributes; }

    [LibraryImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);

    [LibraryImport("advapi32.dll", EntryPoint = "LookupPrivilegeValueW", StringMarshalling = StringMarshalling.Utf16, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool LookupPrivilegeValue(string? system, string name, out long luid);

    [LibraryImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool AdjustTokenPrivileges(IntPtr token, [MarshalAs(UnmanagedType.Bool)] bool none, ref Privileges state, int length, IntPtr previous, IntPtr size);

    [LibraryImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool CloseHandle(IntPtr handle);

    /// <summary>Removes SeDebugPrivilege from this test process's token (no-op when the token lacks it): an elevated
    /// runner may hold it enabled, which opens any process; an ordinary user's installer has it not.</summary>
    private static void DropDebugPrivilege()
    {
        if (!OpenProcessToken(Process.GetCurrentProcess().Handle, 0x28 /* TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY */, out IntPtr token)) return;
        try
        {
            if (!LookupPrivilegeValue(null, "SeDebugPrivilege", out long luid)) return;
            var state = new Privileges { Count = 1, Luid = luid, Attributes = 4 /* SE_PRIVILEGE_REMOVED */ };
            AdjustTokenPrivileges(token, false, ref state, 0, IntPtr.Zero, IntPtr.Zero);
        }
        finally { CloseHandle(token); }
    }

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
        var engine = StartEngine(Path.Join(elsewhere, "NexusRelay.exe"));
        try
        {
            SetProcessDacl(engine, EmptyDacl());
            DropDebugPrivilege();
            Assert.Null(InstallNative.Read(engine.Id).Image);  // precondition: the engine cannot be queried
            var images = InstallNative.Images.Of();
            Assert.True(images.Unknown);
            EngineFolders.RemoveStale(versions, "none", images);
            Assert.True(File.Exists(Path.Join(old, "NexusRelay.exe")));
        }
        finally
        {
            if (!engine.HasExited) SetProcessDacl(engine, null);
            Stop(engine);
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
        var engine = StartEngine(Path.Join(bin, "NexusRelay.exe"));
        try { new FileStream(Path.Join(bin, "engine.lock"), FileMode.Open, FileAccess.ReadWrite, FileShare.None).Dispose(); }
        finally { Stop(engine); }
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

    [Fact]
    public void No_build_output_in_the_synchronized_repository()
    {
        foreach (string folder in new[] { "native", Path.Join("tests", "NexusRelay.Tests"), Path.Join("tests", "native_lifetime") })
            foreach (string output in new[] { "bin", "obj" })
                Assert.False(Directory.Exists(Path.Join(Repo, folder, output)), Path.Join(folder, output));
    }

    // ------------------------------------------------------------------------------------------- the installer

    private string LocalAppData => Path.Join(iso.Root, "localappdata");
    private string State => Path.Join(iso.Home, ".nexus", "nexus-relay");
    private string Versions => Path.Join(LocalAppData, "NexusRelay", "versions");
    private string SpecPath => Path.Join(iso.Home, ".nexus", "daemons", "specs", "nexus-relay.json");

    /// <summary>A release package as get.ps1 unpacks it: the engine in bin\ (the test build; extra marks one release
    /// apart from another), and what the engine deploys.</summary>
    private string Package(string name, string? extra = null)
    {
        string package = Path.Join(iso.Root, "packages", name);
        CopyEngine(Path.Join(package, "bin"));
        if (extra is not null) File.WriteAllText(Path.Join(package, "bin", "release.txt"), extra);
        foreach (string folder in new[] { Path.Join("plugin", "antigravity"), Path.Join("fr", "plugin", "antigravity"), "codex-projet", "claude-projet", Path.Join("fr", "codex-projet"), Path.Join("fr", "claude-projet") })
            InstallFiles.CopyTree(Path.Join(Repo, folder), Path.Join(package, folder));
        File.Copy(Path.Join(Repo, "get.ps1"), Path.Join(package, "get.ps1"), true);
        return package;
    }

    private sealed class Run : IDisposable
    {
        public readonly FakeTasks Tasks = new();
        public readonly Dictionary<string, string?> UserVariables = [];
        public readonly StringWriter Out = new();
        public void Dispose() => Out.Dispose();
        public string? FailAt;
        public Installer.Host Host() => new()
        {
            Tasks = Tasks, UserVariable = n => UserVariables.GetValueOrDefault(n), SetUserVariable = (n, v) => UserVariables[n] = v,
            OnPath = _ => null, UserId = @"PC\user", Out = Out,
            Step = step => { if (step == FailAt) throw new IOException($"fixture failure at {step}"); },
        };
    }

    private int Port { get; } = Native.FreePort();

    private void Install(string package, Run run, bool mirror = true, bool register = false, bool promoteOnly = false) =>
        Assert.Equal(0, new Installer(new Installer.Options { Source = package, Mirror = mirror, Register = register, PromoteOnly = promoteOnly, Port = Port }, run.Host()).Run());

    private void Sandbox(bool antigravity = true)
    {
        iso.Set("LOCALAPPDATA", LocalAppData);
        iso.Set("NEXUS_RELAY_ROOT", null);
        string token = Path.Join(iso.Home, ".nexus", "mcp-token.txt");
        Directory.CreateDirectory(Path.GetDirectoryName(token)!);
        File.WriteAllText(token, new string('c', 64));
        iso.Set("NEXUS_MCP_TOKEN_FILE", token);
        if (antigravity) Directory.CreateDirectory(Path.Join(iso.Home, ".gemini", "config"));
        Directory.CreateDirectory(State);
        File.WriteAllText(Path.Join(State, "settings.json"), """{"wake_since": "2026-10-29T22:46:43Z"}""");
    }

    private JsonObject Spec() => JsonNode.Parse(File.ReadAllText(SpecPath))!.AsObject();
    private static string KeyOf(string exe) => Path.GetFileName(Path.GetDirectoryName(exe))!;

    [Fact]
    public void Fresh_install_places_the_engine_the_daemon_the_task_and_antigravity()
    {
        Sandbox();
        string package = Package("one");
        using var run = new Run();
        Install(package, run);

        string app = Path.Join(LocalAppData, "NexusRelay", "app");
        Assert.True(File.Exists(Path.Join(app, "get.ps1")) && File.Exists(Path.Join(app, "bin", "NexusRelay.exe")));  // the package became the app folder
        var spec = Spec();
        string exe = spec["exe"]!.ToString();
        Assert.StartsWith(Versions + "\\", exe, StringComparison.OrdinalIgnoreCase);
        string key = KeyOf(exe);
        Assert.Equal(key, File.ReadAllText(Path.Join(Versions, key, "publish.complete")));
        Assert.True(File.Exists(Path.Join(Versions, key, "engine.lock")));
        Assert.Equal(Port, spec["port"]!.GetValue<int>());
        string mailbox = Path.Join(iso.Home, ".nexus", "relay");
        Assert.Equal(Path.Join(mailbox, "daemon-restart.request"), spec["restart_request"]!.ToString());
        Assert.Equal(mailbox, spec["env"]!["NEXUS_RELAY_HOME"]!.ToString());
        Assert.Equal(app, spec["env"]!["NEXUS_RELAY_SOURCE_ROOT"]!.ToString());
        Assert.Equal("2026-10-29T22:46:43Z", spec["env"]!["NEXUS_RELAY_WAKE_SINCE"]!.ToString());  // ISO strings kept verbatim
        Assert.Null(spec["python"]);
        var settings = JsonNode.Parse(File.ReadAllText(Path.Join(State, "settings.json")))!;
        Assert.Equal(app, settings["installed_source"]!.ToString());
        Assert.Equal("native", settings["runtime"]!.ToString());
        Assert.Equal("2026-10-29T22:46:43Z", settings["wake_since"]!.ToString());


        Assert.Equal($"{RelayPaths.Version}|{exe}\r\n", File.ReadAllText(Path.Join(State, "engine.txt")));  // the versioned copy, never bin
        Assert.Equal(key, File.ReadAllText(Path.Join(State, "bin", "publish.complete")));
        Assert.Equal(0, Native.RunFile(Path.Join(State, "bin", "NexusRelay.exe"), ["--schemas"]).Code);
        Assert.False(File.Exists(Path.Join(State, "bin", "engine.lock")));
        Assert.Equal($"NexusRelay=Running {exe}", run.Tasks.Summary());
        Assert.Equal(key, File.ReadAllText(Path.Join(State, "installed.stamp")));  // a package without install.ps1: the key alone
        Assert.True(File.Exists(Path.Join(mailbox, "codex-projet", "AGENTS.md")) && File.Exists(Path.Join(mailbox, "NexusRelay", "CLAUDE.md")));

        string plugin = Path.Join(iso.Home, ".gemini", "config", "plugins", "nexus-relay");
        var mcp = JsonNode.Parse(File.ReadAllText(Path.Join(plugin, "mcp_config.json")))!["mcpServers"]!["nexus-relay"]!;
        Assert.Equal($"http://127.0.0.1:{Port}/mcp?agent=antigravity", mcp["serverUrl"]!.ToString());
        Assert.Equal("Bearer " + new string('c', 64), mcp["headers"]!["Authorization"]!.ToString());
        var sidecar = JsonNode.Parse(File.ReadAllText(Path.Join(plugin, "sidecars", "relay", "sidecar.json")))!;
        Assert.Equal(exe, sidecar["command"]!.ToString());
        Assert.Equal(["--sidecar"], sidecar["args"]!.AsArray().Select(a => a!.ToString()));
        var project = JsonNode.Parse(File.ReadAllText(Directory.EnumerateFiles(Path.Join(iso.Home, ".gemini", "config", "projects"), "*.json").Single()))!;
        Assert.Equal("NexusRelay", project["name"]!.ToString());
        var config = JsonNode.Parse(File.ReadAllText(Path.Join(iso.Home, ".gemini", "config", "config.json")))!;
        Assert.Equal(project["id"]!.ToString(), config["sidecars"]!["nexus-relay/relay"]!["projectId"]!.ToString());

        // Run again: nothing changes, the task is left as is, no extra engine folder.
        string specText = File.ReadAllText(SpecPath);
        run.Tasks.Log.Clear();
        Install(package, run);
        Assert.Equal(specText, File.ReadAllText(SpecPath));
        Assert.Empty(run.Tasks.Log);
        Assert.Equal([key], Directory.EnumerateDirectories(Versions).Select(Path.GetFileName));
    }

    [Fact]
    public void Register_sets_the_user_token_and_declares_the_chosen_clients_only()
    {
        Sandbox(antigravity: false);
        iso.Set("NEXUS_PACK", "1");
        iso.Set("NEXUS_PACK_TARGETS", "codex");
        Directory.CreateDirectory(Path.Join(iso.Home, ".claude"));
        Directory.CreateDirectory(iso.CodexHome);
        Directory.CreateDirectory(Path.Join(iso.Home, ".codex"));
        File.WriteAllText(Path.Join(iso.Home, ".claude.json"), """{"theme": "dark"}""");
        using var run = new Run();
        Install(Package("one"), run, register: true);
        Assert.Equal(new string('c', 64), run.UserVariables["NEXUS_MCP_TOKEN"]);
        Assert.Equal("codex", JsonNode.Parse(File.ReadAllText(Path.Join(State, "settings.json")))!["host"]!.ToString());  // only Codex chosen
        Assert.Equal("""{"theme": "dark"}""", File.ReadAllText(Path.Join(iso.Home, ".claude.json")).Trim());  // Claude Code untouched
        Assert.Contains("nexus-relay", File.ReadAllText(Path.Join(iso.CodexHome, "config.toml")), StringComparison.Ordinal);
        Assert.DoesNotContain(new string('c', 64), File.ReadAllText(Path.Join(iso.CodexHome, "config.toml")), StringComparison.Ordinal);
    }

    [Fact]
    public void Update_while_an_engine_runs_keeps_its_folder_until_it_stops()
    {
        Sandbox();
        using var run = new Run();
        Install(Package("one", "1"), run);
        string first = Spec()["exe"]!.ToString();
        var engine = StartEngine(first);  // the daemon of the installed version, holding its engine.lock
        try
        {
            Install(Package("two", "2"), run);
            string second = Spec()["exe"]!.ToString();
            Assert.NotEqual(KeyOf(first), KeyOf(second));
            Assert.True(File.Exists(first));  // still running: left whole
            Assert.Equal(KeyOf(second), File.ReadAllText(Path.Join(State, "bin", "publish.complete")));
            Assert.Equal($"NexusRelay=Running {second}", run.Tasks.Summary());  // the supervisor of the new version
            Assert.Contains("set NexusRelay", run.Tasks.Log);
            Assert.True(File.ReadAllText(Path.Join(State, "engine.txt")).Contains(KeyOf(second), StringComparison.Ordinal));
            Assert.DoesNotContain("\"isError\":true", Status(engine));  // the running engine keeps answering
        }
        finally { Stop(engine); }
        Install(Package("two", "2"), run);
        Assert.Equal([KeyOf(Spec()["exe"]!.ToString())], Directory.EnumerateDirectories(Versions).Select(Path.GetFileName));  // gone once stopped
    }

    [Theory]
    [InlineData("spec")]
    [InlineData("task")]
    [InlineData("antigravity")]
    [InlineData("hooks")]
    public void A_failed_update_puts_the_previous_installation_back(string failAt)
    {
        Sandbox();
        using var run = new Run();
        Install(Package("one", "1"), run);
        string app = Path.Join(LocalAppData, "NexusRelay", "app");
        File.WriteAllText(Path.Join(app, "mark-of-one.txt"), "one");
        string first = Spec()["exe"]!.ToString();
        string specText = File.ReadAllText(SpecPath), pointer = File.ReadAllText(Path.Join(State, "engine.txt"));
        string sidecarPath = Path.Join(iso.Home, ".gemini", "config", "plugins", "nexus-relay", "sidecars", "relay", "sidecar.json");
        string sidecarText = File.ReadAllText(sidecarPath);
        string tasks = run.Tasks.Summary();

        run.FailAt = failAt;
        var error = Assert.Throws<IOException>(() => new Installer(new Installer.Options { Source = Package("two", "2"), Mirror = true, Port = Port }, run.Host()).Run());
        Assert.Contains(failAt, error.Message, StringComparison.Ordinal);
        Assert.Equal(specText, File.ReadAllText(SpecPath));
        Assert.Equal(pointer, File.ReadAllText(Path.Join(State, "engine.txt")));
        Assert.Equal(sidecarText, File.ReadAllText(sidecarPath));
        Assert.Equal(KeyOf(first), File.ReadAllText(Path.Join(State, "bin", "publish.complete")));
        Assert.Equal(0, Native.RunFile(Path.Join(State, "bin", "NexusRelay.exe"), ["--schemas"]).Code);
        Assert.Equal(tasks, run.Tasks.Summary());
        Assert.True(File.Exists(Path.Join(app, "mark-of-one.txt")), run.Out + string.Join(", ", Directory.EnumerateFileSystemEntries(Path.Join(LocalAppData, "NexusRelay"))));  // the previous app folder is back
        Assert.False(File.Exists(Path.Join(app, "bin", "release.txt")) && File.ReadAllText(Path.Join(app, "bin", "release.txt")) == "2");
        Assert.True(File.Exists(first));
        Assert.Contains("WARNING: ", run.Out.ToString(), StringComparison.Ordinal);
        Assert.Empty(Directory.EnumerateDirectories(State, "bin.new-*"));
        Assert.DoesNotContain(Directory.EnumerateDirectories(Path.Join(LocalAppData, "NexusRelay")), d => Path.GetFileName(d).StartsWith("app.", StringComparison.Ordinal));

        // The next attempt succeeds and leaves no copy set aside; the app folder is the new package.
        run.FailAt = null;
        Install(Package("two", "2"), run);
        Assert.Equal("2", File.ReadAllText(Path.Join(app, "bin", "release.txt")));
        Assert.False(File.Exists(Path.Join(app, "mark-of-one.txt")));
        Assert.Equal(["app", "versions"], Directory.EnumerateDirectories(Path.Join(LocalAppData, "NexusRelay")).Select(Path.GetFileName).Order(StringComparer.Ordinal));
        Assert.NotEqual(KeyOf(first), KeyOf(Spec()["exe"]!.ToString()));
        Assert.Empty(Directory.EnumerateDirectories(State, "bin.old-*"));
    }

    [Fact]
    public void Installer_recovers_when_stable_fails_health_check_despite_matching_stamp()
    {
        Sandbox();
        string package = Package("one");
        string key = EngineFolders.Digest(Path.Join(package, "bin"));
        string stable = Path.Join(State, "bin");
        Directory.CreateDirectory(stable);
        CopyEngine(Path.Join(State, "bin.old-20261102000000"));  // a healthy copy set aside earlier
        // A corrupted stable copy carrying the exact stamp: not a program image, so judged unhealthy without being
        // started (no "16-bit application not supported" box).
        File.WriteAllText(Path.Join(stable, "NexusRelay.exe"), "invalid dummy non-PE");
        File.WriteAllText(Path.Join(stable, "publish.complete"), key);
        using var run = new Run();
        Install(package, run, mirror: false, promoteOnly: true);
        Assert.Contains("Promotion", run.Out.ToString(), StringComparison.Ordinal);
        Assert.Equal(0, Native.RunFile(Path.Join(stable, "NexusRelay.exe"), ["--schemas"]).Code);
        Assert.Empty(Directory.EnumerateDirectories(State, "bin.old-*"));  // validated: copies set aside removed
    }

    [Fact]
    public void Update_replaces_the_stable_copy_while_a_process_started_from_it_waits()
    {
        // 1.13.10: a Stop hook (--await-replies, up to 55 min) ran from bin and held bin\engine.lock; the next update
        // failed to rename bin aside and stopped before the pointer, the task and the clients. Here a 1.13.10-like
        // waiter runs from bin (its images mapped there) and holds bin\engine.lock: the update must still succeed.
        Sandbox();
        string bin = Path.Join(State, "bin");
        CopyEngine(bin);
        File.WriteAllText(Path.Join(bin, "engine.lock"), "");
        File.WriteAllText(Path.Join(bin, "publish.complete"), "0000000000000000");  // an older version
        string package = Package("one");
        var waiter = StartEngine(Path.Join(bin, "NexusRelay.exe"));
        var heldLock = new FileStream(Path.Join(bin, "engine.lock"), FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        try
        {
            Assert.ThrowsAny<IOException>(() => Directory.Move(bin, bin + ".probe"));  // precondition: Windows refuses the rename
            { using var once = new Run(); Install(package, once, mirror: false, promoteOnly: true); }
            Assert.Equal(EngineFolders.Digest(Path.Join(package, "bin")), File.ReadAllText(Path.Join(bin, "publish.complete")));
            Assert.Equal(0, Native.RunFile(Path.Join(bin, "NexusRelay.exe"), ["--schemas"]).Code);
            Assert.DoesNotContain("\"isError\":true", Status(waiter));  // its files, moved aside, stay whole
        }
        finally
        {
            heldLock.Dispose();
            Stop(waiter);
        }
        // Once the waiter is gone: the next run removes the old lock from bin and the copy set aside.
        { using var once = new Run(); Install(package, once, mirror: false, promoteOnly: true); }
        Assert.False(File.Exists(Path.Join(bin, "engine.lock")));
        Assert.Empty(Directory.EnumerateDirectories(State, "bin.old-*"));
    }
}
