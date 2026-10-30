using System.Text;
using System.Text.Json.Nodes;
using Tomlyn.Model;

namespace NexusRelay.Tests;

/// <summary>Edit guard and MCP server declared without granting Codex trust nor touching unrelated or disabled entries.</summary>
public sealed class RegistrationTests : IDisposable
{
    private const string Exe = @"D:\fixture\.nexus\nexus-relay\bin\NexusRelay.exe";
    private const string Python = @"C:\relay\venv\Scripts\python.exe -I -B -m nexus_relay.edit_guard";
    private readonly Isolation iso = new();
    private readonly string home;
    private string codex;
    private string Backups => Path.Combine(iso.Root, "backups");

    public RegistrationTests()
    {
        home = Path.Combine(iso.Root, "profile");
        codex = Path.Combine(home, ".codex");
        iso.Set("CODEX_HOME", null);
        iso.Set("PATH", Environment.SystemDirectory);  // neither `claude` nor `codex`: never the real ones
    }
    public void Dispose() => iso.Dispose();

    private static void Write(string path, string text) { Directory.CreateDirectory(Path.GetDirectoryName(path)!); File.WriteAllText(path, text, new UTF8Encoding(false)); }
    private static void Write(string path, JsonNode node) => Write(path, node.ToJsonString());
    private static JsonObject Read(string path) => JsonNode.Parse(File.ReadAllText(path))!.AsObject();
    private static TomlTable Toml(string path) => Registration.ReadToml(File.ReadAllText(path));

    private static JsonObject Hooks(Func<string, string, string> command) => new()
    {
        ["hooks"] = new JsonObject(Registration.Events.Select(e => KeyValuePair.Create(e.Event, (JsonNode?)new JsonArray(new JsonObject
        {
            ["matcher"] = e.Event != "Stop" ? "apply_patch" : "", ["hooks"] = new JsonArray(new JsonObject { ["command"] = command(e.Event, e.Phase) }),
        })))),
    };
    private static JsonObject NativeHooks() => Hooks((_, phase) => Registration.Command(Exe, "codex", phase));

    private string CachedPlugin(JsonObject? hooks)
    {
        string manifest = Path.Combine(codex, "plugins", "cache", "nexus-plugins", "nexus-relay", "local", ".codex-plugin", "plugin.json");
        var data = new JsonObject { ["name"] = "nexus-relay", ["version"] = "1.10.0" };
        if (hooks is not null) data["hooks"] = hooks;
        Write(manifest, data);
        return manifest;
    }

    [Fact]
    public void Native_commands_are_recognized_without_quotes_unless_needed()
    {
        foreach (var (_, _, phase) in Registration.Events)
        {
            string command = Registration.Command(Exe, "codex", phase);
            Assert.DoesNotContain("\"", command);  // Antigravity runs it without a shell: quotes would reach the program
            Assert.True(Registration.OurHandler(new JsonObject { ["command"] = command }));
        }
        Assert.Equal("\"C:\\A B\\NexusRelay.exe\" --edit-guard codex --finish-tool", Registration.Command(@"C:\A B\NexusRelay.exe", "codex", "--finish-tool"));
        string manifest = Path.Combine(iso.Root, "plugin.json");
        Write(manifest, new JsonObject { ["name"] = "nexus-relay", ["hooks"] = NativeHooks() });
        Assert.All(Registration.Events, e => Assert.True(Registration.ManifestCarriesGuard(manifest, e.Event)));
        foreach (string legacy in new[] { Python, @"pwsh -NoProfile -File D:\fixture\.nexus\nexus-relay\hooks\invoke-hook.ps1 -Agent codex" })
        {
            Assert.True(Registration.OurHandler(new JsonObject { ["command"] = legacy }));  // replaced in place
            Write(manifest, new JsonObject { ["name"] = "nexus-relay", ["hooks"] = Hooks((_, phase) => legacy + " " + phase) });
            Assert.False(Registration.ManifestCarriesGuard(manifest));  // a retired engine no longer guards anything
        }
    }

    [Fact]
    public void Codex_declares_without_inventing_trust_nor_touching_the_rest()
    {
        var foreign = new JsonObject { ["matcher"] = "^Bash$", ["hooks"] = new JsonArray(new JsonObject { ["type"] = "command", ["command"] = "echo moi" }) };
        Write(Path.Combine(codex, "hooks.json"), new JsonObject { ["hooks"] = new JsonObject { ["PreToolUse"] = new JsonArray(foreign.DeepClone()) } });
        const string original = "model = \"x\"\n\n[features]\nhooks = true\n";
        Write(Path.Combine(codex, "config.toml"), original);
        Assert.StartsWith("declared", Registration.CodexHooks(home, Exe, Backups));
        var groups = Read(Path.Combine(codex, "hooks.json"))["hooks"]!["PreToolUse"]!.AsArray();
        Assert.True(JsonNode.DeepEquals(groups[0], foreign));
        Assert.Equal("apply_patch", groups[1]!["matcher"]?.ToString());
        Assert.Equal(original, File.ReadAllText(Path.Combine(codex, "config.toml")));
        Assert.Contains("already declared", Registration.CodexHooks(home, Exe, Backups));
        var events = Read(Path.Combine(codex, "hooks.json"))["hooks"]!;
        Assert.Contains("--finish-tool", events["PostToolUse"]![0]!["hooks"]![0]!["command"]!.ToString());
        Assert.Contains("--finish-session", events["Stop"]![0]!["hooks"]![0]!["command"]!.ToString());
    }

    [Fact]
    public void Codex_retired_python_global_entries_are_replaced_by_the_engine()
    {
        Write(Path.Combine(codex, "hooks.json"), Hooks((_, phase) => Python + " codex " + phase));
        Assert.StartsWith("declared", Registration.CodexHooks(home, Exe, Backups));
        var events = Read(Path.Combine(codex, "hooks.json"))["hooks"]!.AsObject();
        Assert.All(events, e => Assert.Single(e.Value!.AsArray()));
        Assert.All(events, e => Assert.Contains("NexusRelay.exe --edit-guard codex", e.Value![0]!["hooks"]![0]!["command"]!.ToString()));
    }

    [Fact]
    public void Codex_personal_plugin_carrying_the_engine_removes_the_global_entry()
    {
        var foreign = new JsonObject { ["matcher"] = "^Bash$", ["hooks"] = new JsonArray(new JsonObject { ["type"] = "command", ["command"] = "echo moi" }) };
        Write(Path.Combine(codex, "hooks.json"), new JsonObject { ["hooks"] = new JsonObject { ["PreToolUse"] = new JsonArray(foreign.DeepClone()) } });
        Write(Path.Combine(codex, "config.toml"), "model = \"x\"\n");
        Assert.StartsWith("declared", Registration.CodexHooks(home, Exe, Backups));
        Write(Path.Combine(codex, "plugins", "nexus-relay", ".codex-plugin", "plugin.json"), new JsonObject { ["name"] = "nexus-relay", ["hooks"] = NativeHooks() });
        Assert.Contains("global entry removed", Registration.CodexHooks(home, Exe, Backups));
        Assert.True(JsonNode.DeepEquals(Read(Path.Combine(codex, "hooks.json"))["hooks"]!["PreToolUse"], new JsonArray(foreign.DeepClone())));
        var config = Toml(Path.Combine(codex, "config.toml"));
        Assert.Equal("x", config["model"]);
        Assert.False(config.ContainsKey("hooks"));
        Assert.Equal("carried by the nexus-relay plugin", Registration.CodexHooks(home, Exe, Backups));
    }

    [Fact]
    public void Codex_installed_cache_removes_every_duplicate_and_remaps_trust()
    {
        codex = Path.Combine(iso.Root, "profil d'IA");
        iso.Set("CODEX_HOME", codex);
        CachedPlugin(NativeHooks());
        var own = new JsonObject { ["type"] = "command", ["command"] = Registration.Command(Exe, "codex") };
        var foreign = Enumerable.Range(0, 3).Select(i => new JsonObject { ["type"] = "command", ["command"] = $"echo étranger {i}" }).ToArray();
        var groups = new JsonArray(
            new JsonObject { ["matcher"] = "apply_patch", ["hooks"] = new JsonArray(own.DeepClone()) },
            new JsonObject { ["matcher"] = "^Bash$", ["hooks"] = new JsonArray(foreign[0].DeepClone(), own.DeepClone(), foreign[1].DeepClone()) },
            new JsonObject { ["matcher"] = "*", ["hooks"] = new JsonArray(foreign[2].DeepClone()) });
        var stop = new JsonArray(new JsonObject { ["hooks"] = new JsonArray(new JsonObject { ["type"] = "command", ["command"] = "echo stop" }) });
        string hooksPath = Path.Combine(codex, "hooks.json");
        Write(hooksPath, new JsonObject { ["hooks"] = new JsonObject { ["PreToolUse"] = groups.DeepClone(), ["Stop"] = stop.DeepClone() } });
        string prefix = hooksPath + ":pre_tool_use:";
        var state = new Dictionary<string, (string Hash, bool? Enabled)>();
        foreach (string key in new[] { "0:0", "1:0", "1:1", "1:2", "2:0" }) state[prefix + key] = ("sha256:" + key, key != "1:2");
        state["another-hook.json:stop:0:0"] = ("sha256:unrelated", null);
        var text = new StringBuilder("# garder ce commentaire\nmodel = \"x\"\n[plugins.\"nexus-relay@nexus-plugins\"]\nenabled = true\n");
        foreach (var (key, (hash, enabled)) in state)
        {
            text.Append($"\n[hooks.state.{System.Text.Json.JsonSerializer.Serialize(key)}]\ntrusted_hash = \"{hash}\"\n");  // quoted TOML keys too
            if (enabled is not null) text.Append($"enabled = {(enabled.Value ? "true" : "false")}\n");
        }
        Write(Path.Combine(codex, "config.toml"), text.ToString());
        Assert.Contains("global entry removed", Registration.CodexHooks(iso.Root, Exe, Backups));
        var expected = new JsonObject
        {
            ["PreToolUse"] = new JsonArray(new JsonObject { ["matcher"] = "^Bash$", ["hooks"] = new JsonArray(foreign[0].DeepClone(), foreign[1].DeepClone()) }, groups[2]!.DeepClone()),
            ["Stop"] = stop.DeepClone(),
        };
        Assert.True(JsonNode.DeepEquals(Read(hooksPath)["hooks"], expected), Read(hooksPath).ToJsonString());
        var config = Toml(Path.Combine(codex, "config.toml"));
        var trust = (TomlTable)((TomlTable)config["hooks"])["state"];
        string Hash(string key) => (string)((TomlTable)trust[key])["trusted_hash"];
        Assert.Equal(4, trust.Count);
        Assert.Equal("sha256:1:0", Hash(prefix + "0:0"));
        Assert.Equal("sha256:1:2", Hash(prefix + "0:1"));
        Assert.Equal(false, ((TomlTable)trust[prefix + "0:1"])["enabled"]);
        Assert.Equal("sha256:2:0", Hash(prefix + "1:0"));
        Assert.Equal("sha256:unrelated", Hash("another-hook.json:stop:0:0"));
        Assert.Equal(true, ((TomlTable)((TomlTable)config["plugins"])["nexus-relay@nexus-plugins"])["enabled"]);
        Assert.Contains("# garder ce commentaire", File.ReadAllText(Path.Combine(codex, "config.toml")));
        var before = (File.ReadAllBytes(hooksPath), File.ReadAllBytes(Path.Combine(codex, "config.toml")));
        Assert.Equal("carried by the nexus-relay plugin", Registration.CodexHooks(iso.Root, Exe, Backups));
        Assert.Equal(before.Item1, File.ReadAllBytes(hooksPath));
        Assert.Equal(before.Item2, File.ReadAllBytes(Path.Combine(codex, "config.toml")));
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void Codex_disabled_plugin_gets_no_global_guard(bool installed)
    {
        Directory.CreateDirectory(codex);
        Assert.StartsWith("declared", Registration.CodexHooks(home, Exe, Backups));
        string config = Path.Combine(codex, "config.toml");
        File.AppendAllText(config, "\n[plugins.\"nexus-relay@nexus-plugins\"]\nenabled = false\n");
        if (installed) CachedPlugin(NativeHooks());
        Assert.Contains("kept disabled", Registration.CodexHooks(home, Exe, Backups));
        Assert.Null(Read(Path.Combine(codex, "hooks.json"))["hooks"]?["PreToolUse"]);
        var parsed = Toml(config);
        Assert.Equal(false, ((TomlTable)((TomlTable)parsed["plugins"])["nexus-relay@nexus-plugins"])["enabled"]);
        Assert.False(parsed.ContainsKey("hooks"));
        Assert.Equal("kept disabled (nexus-relay plugin)", Registration.CodexHooks(home, Exe, Backups));
    }

    [Theory]
    [InlineData(false, true)]
    [InlineData(true, false)]
    public void Codex_orphaned_or_old_cache_does_not_remove_the_guard(bool registered, bool carries)
    {
        CachedPlugin(carries ? NativeHooks() : null);
        if (registered) Write(Path.Combine(codex, "config.toml"), "[plugins.\"nexus-relay@nexus-plugins\"]\nenabled = true\n");
        Assert.StartsWith("declared", Registration.CodexHooks(home, Exe, Backups));
        var hooks = Read(Path.Combine(codex, "hooks.json"))["hooks"]!["PreToolUse"]!.AsArray();
        Assert.Single(hooks);
        Assert.True(Registration.Ours(hooks[0]));
    }

    [Theory]
    [InlineData("^Bash$")]
    [InlineData("Stop")]
    public void Codex_cached_guard_on_another_tool_does_not_replace_apply_patch(string matcher)
    {
        var hooks = NativeHooks();
        hooks["hooks"]!["PreToolUse"]![0]!["matcher"] = matcher;
        CachedPlugin(hooks);
        Write(Path.Combine(codex, "config.toml"), "[plugins.\"nexus-relay@nexus-plugins\"]\nenabled = true\n");
        Assert.StartsWith("declared", Registration.CodexHooks(home, Exe, Backups));
        var groups = Read(Path.Combine(codex, "hooks.json"))["hooks"]!["PreToolUse"]!.AsArray();
        Assert.Single(groups);
        Assert.Equal("apply_patch", groups[0]!["matcher"]?.ToString());
    }

    [Fact]
    public void Codex_old_plugin_does_not_hide_missing_finalizers()
    {
        var hooks = NativeHooks();
        hooks["hooks"]!.AsObject().Remove("PostToolUse");
        hooks["hooks"]!.AsObject().Remove("Stop");
        CachedPlugin(hooks);
        const string original = "[plugins.\"nexus-relay@nexus-plugins\"]\nenabled = true\n";
        Write(Path.Combine(codex, "config.toml"), original);
        Assert.Contains("Codex review required", Registration.CodexHooks(home, Exe, Backups));
        Assert.Equal(["PostToolUse", "Stop"], Read(Path.Combine(codex, "hooks.json"))["hooks"]!.AsObject().Select(p => p.Key).Order());
        Assert.Equal(original, File.ReadAllText(Path.Combine(codex, "config.toml")));
    }

    [Fact]
    public void Codex_update_preserves_foreign_handlers_and_their_trust()
    {
        string path = Path.Combine(codex, "hooks.json");
        var own = new JsonObject { ["type"] = "command", ["command"] = Python + " codex --finish-tool" };
        var foreign = new JsonObject { ["type"] = "command", ["command"] = "echo preserve-me" };
        Write(path, new JsonObject { ["hooks"] = new JsonObject { ["PostToolUse"] = new JsonArray(new JsonObject { ["matcher"] = "other-tool", ["hooks"] = new JsonArray(own, foreign.DeepClone()) }) } });
        string prefix = path + ":post_tool_use:";
        Write(Path.Combine(codex, "config.toml"), $"[hooks.state.{System.Text.Json.JsonSerializer.Serialize(prefix + "0:1")}]\ntrusted_hash = \"sha256:foreign\"\nenabled = false\n");
        Registration.CodexHooks(home, Exe, Backups);
        var events = Read(path)["hooks"]!;
        Assert.True(JsonNode.DeepEquals(events["PostToolUse"]![0], new JsonObject { ["matcher"] = "other-tool", ["hooks"] = new JsonArray(foreign.DeepClone()) }));
        Assert.Equal("apply_patch", events["PostToolUse"]![1]!["matcher"]?.ToString());
        var trust = (TomlTable)((TomlTable)Toml(Path.Combine(codex, "config.toml"))["hooks"])["state"];
        Assert.Single(trust);
        Assert.Equal("sha256:foreign", ((TomlTable)trust[prefix + "0:0"])["trusted_hash"]);
        Assert.Equal(false, ((TomlTable)trust[prefix + "0:0"])["enabled"]);
    }

    [Fact]
    public void Antigravity_hook_added_next_to_the_others_and_disabled_choice_kept()
    {
        string path = Path.Combine(home, ".gemini", "config", "hooks.json");
        Write(path, """{"autre": {"Stop": []}}""");
        Assert.StartsWith("declared", Registration.AntigravityHooks(home, Exe, Backups));
        var data = Read(path);
        Assert.True(JsonNode.DeepEquals(data["autre"], new JsonObject { ["Stop"] = new JsonArray() }));
        var entry = data[Registration.AgyName]!;
        Assert.Equal(Exe + " --edit-guard antigravity", entry["PreToolUse"]![0]!["hooks"]![0]!["command"]?.ToString());
        Assert.Contains("--finish-tool", entry["PostToolUse"]![0]!["hooks"]![0]!["command"]!.ToString());
        Assert.Contains("--finish-session", entry["Stop"]![0]!["command"]!.ToString());
        Assert.Equal("already declared", Registration.AntigravityHooks(home, Exe, Backups));
        Write(path, new JsonObject { [Registration.AgyName] = new JsonObject { ["enabled"] = false, ["PreToolUse"] = new JsonArray() } });
        Registration.AntigravityHooks(home, Exe, Backups);
        Assert.False(Read(path)[Registration.AgyName]!["enabled"]!.GetValue<bool>());
    }

    // ---------- MCP clients ----------

    [Fact]
    public void Claude_code_by_its_file_without_writing_the_token()
    {
        Directory.CreateDirectory(Path.Combine(home, ".claude"));
        string config = Path.Combine(home, ".claude.json");
        Write(config, """{"mcpServers": {"autre": {"command": "x"}}, "theme": "dark"}""");
        Assert.StartsWith("declared in", Registration.ClaudeCode(home, 16181, Backups));
        var data = Read(config);
        Assert.Equal("http://127.0.0.1:16181/mcp?agent=claude", data["mcpServers"]!["nexus-relay"]!["url"]?.ToString());
        Assert.Equal("Bearer ${NEXUS_MCP_TOKEN}", data["mcpServers"]!["nexus-relay"]!["headers"]!["Authorization"]?.ToString());
        Assert.Equal("x", data["mcpServers"]!["autre"]!["command"]?.ToString());
        Assert.Equal("dark", data["theme"]?.ToString());
        Assert.Equal("already declared", Registration.ClaudeCode(home, 16181, Backups));
    }

    [Fact]
    public void Claude_code_user_entry_kept_plugin_recognized_and_absent()
    {
        Assert.Null(Registration.ClaudeCode(home, 16181, Backups));
        string config = Path.Combine(home, ".claude.json");
        const string mine = """{"mcpServers": {"nexus-relay": {"type": "http", "url": "https://ailleurs.example/mcp"}}}""";
        Write(config, mine);
        Assert.StartsWith("kept", Registration.ClaudeCode(home, 16181, Backups));
        Assert.Equal(mine, File.ReadAllText(config));
        File.Delete(config);
        Write(Path.Combine(home, ".claude", "settings.json"), """{"enabledPlugins": {"nexus-relay@nexus-plugins": true}}""");
        Assert.Equal(Registration.Plugin, Registration.ClaudeCode(home, 16181, Backups));
        Assert.False(File.Exists(config));
    }

    [Fact]
    public void Claude_code_through_its_command_and_its_failure()
    {
        string bin = Path.Combine(iso.Root, "bin");
        string log = Path.Combine(iso.Root, "claude-calls.txt");
        Write(Path.Combine(bin, "claude.cmd"), $"@echo %* >> \"{log}\"\r\n@exit /b 0\r\n");
        iso.Set("PATH", bin + ";" + Environment.SystemDirectory);
        Assert.Equal("declared (claude mcp add-json, user scope)", Registration.ClaudeCode(home, 16181, Backups));
        var calls = File.ReadAllLines(log);
        Assert.StartsWith("mcp remove nexus-relay --scope user", calls[0]);
        Assert.StartsWith("mcp add-json nexus-relay", calls[1]);
        Assert.Contains("127.0.0.1:16181/mcp?agent=claude", calls[1]);
        Assert.EndsWith("--scope user", calls[1].Trim());
        Write(Path.Combine(bin, "claude.cmd"), "@echo boom 1>&2\r\n@exit /b 1\r\n");
        Assert.Equal("failed: boom", Registration.ClaudeCode(home, 16181, Backups));
    }

    [Fact]
    public void Codex_table_added_read_back_and_replaced_in_place()
    {
        string config = Path.Combine(codex, "config.toml");
        Write(config, "model = \"x\"\n\n[mcp_servers.autre]\ncommand = \"y\"\n");
        Assert.StartsWith("declared in", Registration.Codex(home, 16181, Backups));
        var servers = (TomlTable)Toml(config)["mcp_servers"];
        var entry = (TomlTable)servers["nexus-relay"];
        Assert.Equal("http://127.0.0.1:16181/mcp?agent=codex", entry["url"]);
        Assert.Equal("NEXUS_MCP_TOKEN", entry["bearer_token_env_var"]);
        Assert.Equal("y", ((TomlTable)servers["autre"])["command"]);
        Assert.Equal("already declared", Registration.Codex(home, 16181, Backups));
        Assert.StartsWith("declared in", Registration.Codex(home, 16999, Backups));  // port changed: replaced in place
        Assert.StartsWith("http://127.0.0.1:16999/", (string)((TomlTable)((TomlTable)Toml(config)["mcp_servers"])["nexus-relay"])["url"]);
        Assert.Equal(1, File.ReadAllText(config).Split("[mcp_servers.nexus-relay]").Length - 1);
        Assert.NotEmpty(Directory.EnumerateFiles(Backups));
    }

    [Theory]
    [InlineData("nexus")]
    [InlineData("nexus-plugins")]
    public void Codex_plugin_already_enabled_and_absent(string catalog)
    {
        Assert.Null(Registration.Codex(home, 16181, Backups));
        Write(Path.Combine(codex, "config.toml"), $"[plugins.\"nexus-relay@{catalog}\"]\nenabled = true\n");
        Assert.Equal(Registration.Plugin, Registration.Codex(home, 16181, Backups));
        Assert.False(Toml(Path.Combine(codex, "config.toml")).ContainsKey("mcp_servers"));
    }

    private (int Code, string[] Lines) Cli(params string[] args)
    {
        iso.Set("USERPROFILE", home);
        var output = new StringWriter();
        var previous = Console.Out;
        Console.SetOut(output);
        try { int code = Registration.RunCli(args); return (code, output.ToString().Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)); }
        finally { Console.SetOut(previous); }
    }

    [Fact]
    public void Pack_line_and_pack_scope()
    {
        iso.Set("NEXUS_PACK", "1");
        Directory.CreateDirectory(codex);
        var (code, lines) = Cli("--register-clients", "--backups", Backups, "--antigravity", "plugin installed in X");
        Assert.Equal(0, code);
        Assert.Equal("NEXUS_PACK_RESULT Claude Code=absent; Codex=declared; Antigravity=plugin", lines[^1]);

        File.Delete(Path.Combine(codex, "config.toml"));
        iso.Set("NEXUS_PACK_TARGETS", "codex, claude-desktop");
        Directory.CreateDirectory(Path.Combine(home, ".claude"));
        Write(Path.Combine(home, ".claude.json"), """{"theme": "dark"}""");
        (code, lines) = Cli("--register-clients", "--backups", Backups, "--antigravity", "plugin installed in X");
        Assert.Equal(0, code);
        Assert.Equal("NEXUS_PACK_RESULT Codex=declared", lines[^1]);
        Assert.Equal("""{"theme": "dark"}""", File.ReadAllText(Path.Combine(home, ".claude.json")));
        Assert.Contains("nexus-relay", File.ReadAllText(Path.Combine(codex, "config.toml")));
        (code, lines) = Cli("--register-hooks", "--backups", Backups, "--command", Exe);
        Assert.Equal(0, code);
        Assert.Equal(["Codex (edit guard): declared in " + Path.Combine(codex, "hooks.json") + "; Codex review required for new hooks"], lines);
        Assert.False(Directory.Exists(Path.Combine(home, ".gemini")));
    }
}
