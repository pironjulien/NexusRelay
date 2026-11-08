using System.Globalization;
using System.Text;
using System.Text.Json.Nodes;

namespace NexusRelay;

/// <summary>
/// <c>NexusRelay.exe --install</c>: the whole installation and update, in the engine (no PowerShell logic). Idempotent:
/// run it again after an update. It is started by get.ps1 (a minimal starter that downloads and checks the release),
/// by <c>--update</c> (the daemon's own update) and by install.ps1 (a clone, after building the engine).
/// <list type="bullet">
/// <item>--mirror: the package becomes %LOCALAPPDATA%\NexusRelay\app (switched by one rename, put back on failure);</item>
/// <item>engine: immutable copy %LOCALAPPDATA%\NexusRelay\versions\&lt;fingerprint&gt; (never written again), stable
/// copy ~\.nexus\nexus-relay\bin for the edit hooks and commit guards (checked, switched, restored on failure),
/// ~\.nexus\nexus-relay\engine.txt for the Antigravity Marketplace launcher;</item>
/// <item>settings, token, host, daemon spec ~\.nexus\daemons\specs\nexus-relay.json, cooperative restart requests of the
/// daemon and the sidecar, scheduled task "NexusRelay" (Task Scheduler API) without the shared supervisor;</item>
/// <item>Antigravity plugin, sidecar and project; edit hooks of Codex and Antigravity;</item>
/// <item>--register (public install): user variable NEXUS_MCP_TOKEN (registry, HKCU) and the AI clients declared.</item>
/// </list>
/// A failure before the clients are declared puts back what this run changed: app folder, stable copy, files written
/// (spec, settings, pointer, plugin, sidecar, Antigravity configuration), scheduled task, and asks the daemon and the
/// sidecar to restart on the restored engine. Old engine folders are removed only once the run has succeeded.
/// </summary>
public sealed class Installer
{
    public sealed record Options
    {
        /// <summary>The installed source: plugin\antigravity, codex-projet, claude-projet, fr\ and, in a release, bin\.</summary>
        public required string Source { get; init; }
        public bool Mirror { get; init; }
        public bool Register { get; init; }
        public bool PrepareOnly { get; init; }
        public bool PromoteOnly { get; init; }
        public string? TokenFile { get; init; }
        public int Port { get; init; } = 16181;
    }

    /// <summary>What the installer touches outside its own files (replaced in tests).</summary>
    public sealed class Host
    {
        public ITaskStore Tasks { get; init; } = new WindowsTaskStore();
        public Func<string, string?> UserVariable { get; init; } = name => Environment.GetEnvironmentVariable(name, EnvironmentVariableTarget.User);
        public Action<string, string> SetUserVariable { get; init; } = (name, value) => Environment.SetEnvironmentVariable(name, value, EnvironmentVariableTarget.User);
        public Func<string, string?> OnPath { get; init; } = InstallNative.OnPath;
        public Func<string, bool> Healthy { get; init; } = InstallNative.Healthy;
        public Func<InstallNative.Images> Images { get; init; } = () => InstallNative.Images.Of();
        public string UserId { get; init; } = $"{Environment.UserDomainName}\\{Environment.UserName}";
        /// <summary>Called at each named step (tests make one fail).</summary>
        public Action<string> Step { get; init; } = _ => { };
        public TextWriter Out { get; init; } = Console.Out;
    }

    private readonly Options options;
    private readonly Host host;
    private readonly string home, state, backups, mailbox, local, root, versions;
    private readonly List<(string Path, byte[]? Before)> journal = [];
    private readonly HashSet<string> journaled = new(StringComparer.OrdinalIgnoreCase);
    private Action? undoApp, undoStable, undoTask;
    private bool specChanged, committed;
    private string? previousExe, previousSidecarExe;

    public Installer(Options options, Host? host = null)
    {
        this.options = options;
        this.host = host ?? new Host();
        home = RelayPaths.UserHome();
        state = Path.Join(home, ".nexus", "nexus-relay");
        backups = Path.Join(state, "backups");
        mailbox = Path.Join(home, ".nexus", "relay");
        local = Environment.GetEnvironmentVariable("LOCALAPPDATA") is { Length: > 0 } appData ? appData : Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        root = Environment.GetEnvironmentVariable("NEXUS_RELAY_ROOT") is { Length: > 0 } custom ? Path.GetFullPath(custom) : Path.Join(local, "NexusRelay");
        versions = Path.Join(local, "NexusRelay", "versions");
    }

    private static string Tr(string en, string fr) => InstallText.Tr(en, fr);
    private void Say(string line) => host.Out.WriteLine(line);
    private void Warn(string line) => host.Out.WriteLine("WARNING: " + line);

    /// <summary>Entry point of <c>--install</c>.</summary>
    public static int RunCli(string[] args)
    {
        string? Option(string name) { int i = Array.IndexOf(args, name); return i >= 0 && i + 1 < args.Length ? args[i + 1] : null; }
        string source = Option("--source") ?? Path.GetFullPath(Path.Join(AppContext.BaseDirectory, ".."));
        var options = new Options
        {
            Source = Path.GetFullPath(source),
            Mirror = args.Contains("--mirror", StringComparer.Ordinal),
            Register = args.Contains("--register", StringComparer.Ordinal),
            PrepareOnly = args.Contains("--prepare-only", StringComparer.Ordinal),
            PromoteOnly = args.Contains("--promote-only", StringComparer.Ordinal),
            TokenFile = Option("--token-file"),
        };
        try { return new Installer(options).Run(); }
        catch (Exception e) // Boundary: every failure is reported on stderr with exit code 1, never as a crash box.
        {
            Console.Error.WriteLine(Tr("NexusRelay installation failed: ", "Échec de l'installation de NexusRelay : ") + e.Message);
            return 1;
        }
    }

    /// <summary>Installs; returns the exit code (0 success). Throws after restoring what it changed.</summary>
    public int Run()
    {
        Directory.CreateDirectory(state);
        Directory.CreateDirectory(backups);
        Directory.CreateDirectory(mailbox);
        try { return Install(); }
        catch
        {
            Restore();
            throw;
        }
    }

    // ------------------------------------------------------------------------------------------------- the run

    private string source = "";

    private int Install()
    {
        source = options.Source;
        if (!Directory.Exists(source)) throw new DirectoryNotFoundException(Tr($"Source folder not found: {source}", $"Dossier source introuvable : {source}"));

        // Identity of the running daemon and sidecar, checked before anything changes: a process that is not the
        // installed engine is never asked to restart (nor stopped).
        string specPath = Path.Join(home, ".nexus", "daemons", "specs", "nexus-relay.json");
        var previousSpec = InstallFiles.ReadObject(specPath);
        previousExe = previousSpec?["exe"]?.ToString() ?? previousSpec?["python"]?.ToString();
        string sidecarPath = Path.Join(home, ".gemini", "config", "plugins", "nexus-relay", "sidecars", "relay", "sidecar.json");
        previousSidecarExe = InstallFiles.ReadObject(sidecarPath)?["command"]?.ToString();
        RestartOwner.Owner? daemonOwner = null, sidecarOwner = null;
        if (!options.PrepareOnly)
        {
            daemonOwner = RestartOwner.Find(mailbox, "daemon", previousExe, previousSpec?["port"]?.GetValue<int>() ?? options.Port);
            sidecarOwner = RestartOwner.Find(mailbox, "sidecar", previousSidecarExe);
        }

        if (options.Mirror && !options.PrepareOnly && !options.PromoteOnly) MirrorApp();
        host.Step("mirror");

        var (nativeKey, nativeExe) = InstallVersion();
        host.Step("version");
        if (options.PrepareOnly)
        {
            if (!host.Healthy(nativeExe)) throw new InvalidOperationException(Tr("Native candidate unreadable.", "Candidat natif illisible."));
            Say($"NEXUS_RELAY_NATIVE_CANDIDATE {nativeExe}");
            Say(Tr("Candidate prepared; spec, plugins and running processes unchanged.", "Candidat préparé ; spec, plugins et processus en service inchangés."));
            return 0;
        }

        string stable = Path.Join(state, "bin"), stableExe = Path.Join(stable, "NexusRelay.exe");
        Promote(nativeKey, Path.GetDirectoryName(nativeExe)!);
        host.Step("promote");
        if (options.PromoteOnly)
        {
            if (!StableValid(nativeKey)) throw new InvalidOperationException(Tr($"Promotion into '{stable}' failed or final health check invalid (--schemas); backups kept.", $"Promotion dans '{stable}' échouée ou santé finale invalide (--schemas) ; sauvegardes conservées."));
            CleanStableBackups();
            Say(Tr($"Promotion into {stable} validated (--schemas).", $"Promotion dans {stable} validée (--schemas)."));
            return 0;
        }

        string settingsPath = Path.Join(state, "settings.json");
        Directory.CreateDirectory(Path.Join(mailbox, "codex-projet"));  // folder of the relay's Codex project
        Directory.CreateDirectory(Path.Join(mailbox, "NexusRelay"));    // folder of Claude's relay session (its name is the project's)

        // Effective settings (this PC's file, then the source's; variables expanded): the daemon's own reading.
        var settings = RelaySettings.Load(RelaySettings.Files(source));
        bool french = (settings["lang"]?.ToString() ?? "").Trim().StartsWith("fr", StringComparison.OrdinalIgnoreCase);
        string Localized(string relative)
        {
            string fr = Path.Join(source, "fr", relative);
            return french && File.Exists(fr) ? fr : Path.Join(source, relative);
        }
        // Instructions of the relay chats, read by Codex and Claude in their folders.
        CopyFile(Localized(Path.Join("codex-projet", "AGENTS.md")), Path.Join(mailbox, "codex-projet", "AGENTS.md"));
        CopyFile(Localized(Path.Join("claude-projet", "CLAUDE.md")), Path.Join(mailbox, "NexusRelay", "CLAUDE.md"));
        // 1.12.11: resetting the relay session (1.12.10) stopped its process and so its mailbox: setting removed.
        DeleteFile(Path.Join(mailbox, "NexusRelay", ".claude", "settings.json"));

        // Shared token of the Nexus daemons: explicit file, else ~\.nexus\mcp-token.txt; reused, never regenerated. A
        // configured PC without --register never creates it (an installer run before the file syncs would make another).
        string legacyToken = Path.Join(state, "token.txt");
        bool IsLegacy(string f) => string.Equals(Path.GetFullPath(f), Path.GetFullPath(legacyToken), StringComparison.OrdinalIgnoreCase);
        var explicitToken = new[] { options.TokenFile, Environment.GetEnvironmentVariable("NEXUS_MCP_TOKEN_FILE"), settings["token_file"]?.ToString() }
            .Where(f => !string.IsNullOrWhiteSpace(f) && !IsLegacy(f!)).Select(f => f!).ToList();
        string? tokenFile = null;
        if (options.Register || explicitToken.Any(File.Exists))
        {
            tokenFile = SharedToken.ResolveFile(explicitToken, home, legacyToken);
            var localSettings = InstallFiles.ReadObject(settingsPath) ?? [];
            if (localSettings["token_file"]?.ToString() is { } old && IsLegacy(old))
            {
                localSettings.Remove("token_file");  // no relay-only token any more
                WriteFile(settingsPath, InstallFiles.ToJson(localSettings));
            }
        }

        // Scope of the Nexus pack: the AI apps the user chose; the others are never touched.
        var packTargets = Environment.GetEnvironmentVariable("NEXUS_PACK_TARGETS") is { } rawTargets
            ? rawTargets.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).ToList() : null;
        // Host (public install only, and only when not set yet: a user's choice stays).
        if (options.Register && string.IsNullOrEmpty(settings["host"]?.ToString()) && ChooseHost(packTargets) is { } chosen)
        {
            var localSettings = InstallFiles.ReadObject(settingsPath) ?? [];
            localSettings["host"] = chosen;
            WriteFile(settingsPath, InstallFiles.ToJson(localSettings));
            settings["host"] = chosen;
            Say(Tr($"Relay host: {chosen} (host setting in {settingsPath}).", $"Hôte du relais : {chosen} (réglage host de {settingsPath})."));
        }
        if (tokenFile is null || !File.Exists(tokenFile))
            throw new InvalidOperationException(Tr($"Token missing: set token_file in {settingsPath}, -TokenFile or NEXUS_MCP_TOKEN_FILE (or install with -Register).", $"Jeton absent : régler token_file dans {settingsPath}, -TokenFile ou NEXUS_MCP_TOKEN_FILE (ou installer avec -Register)."));
        string token = File.ReadAllText(tokenFile).Trim();
        host.Step("settings");

        // --- Shared daemon: one nexus-relay server for every session of Claude, Codex and Antigravity.
        var daemonEnv = new JsonObject { ["NEXUS_MCP_TOKEN_FILE"] = Path.GetFullPath(tokenFile) };
        string Joined(JsonNode? node, string separator) => node is JsonArray list ? string.Join(separator, list.Select(v => v?.ToString())) : node?.ToString() ?? "";
        if (settings["vaults"] is { } vaults) daemonEnv["NEXUS_RELAY_VAULTS"] = Joined(vaults, Path.PathSeparator.ToString());
        if (settings["codex_router"] is { } router) daemonEnv["NEXUS_RELAY_CODEX_ROUTER"] = router.ToString();
        if (settings["codex_router_start"] is { } starts) daemonEnv["NEXUS_RELAY_CODEX_ROUTER_START"] = Joined(starts, ";");
        if (settings["host"] is { } relayHost) daemonEnv["NEXUS_RELAY_HOST"] = relayHost.ToString();
        if (settings["lang"] is { } lang) daemonEnv["NEXUS_RELAY_LANG"] = lang.ToString();
        foreach (var (key, variable) in new[] { ("codex_workspace", "NEXUS_RELAY_CODEX_WORKSPACE"), ("codex_project", "NEXUS_RELAY_CODEX_PROJECT"), ("codex_project_id", "NEXUS_RELAY_CODEX_PROJECT_ID"), ("wake_since", "NEXUS_RELAY_WAKE_SINCE") })
            if (settings[key] is { } value) daemonEnv[variable] = value.ToString();
        // Pinned mailbox: the daemon reads the one this installer writes to, whatever its launcher's environment.
        daemonEnv["NEXUS_RELAY_HOME"] = mailbox;
        daemonEnv["NEXUS_RELAY_SOURCE_ROOT"] = source;
        RegisterKnownChats(daemonEnv);

        // restart_request: the restart request the daemon follows. A supervisor takes it just before starting the
        // daemon (before reading the spec again): that start satisfies it, the new daemon does not restart at once.
        string daemonRequest = Path.Join(mailbox, "daemon-restart.request");
        var spec = new JsonObject { ["port"] = options.Port, ["exe"] = nativeExe, ["args"] = new JsonArray(), ["env"] = daemonEnv, ["restart_request"] = daemonRequest };
        specChanged = !JsonNode.DeepEquals(InstallFiles.ReadObject(specPath), spec);
        if (specChanged) WriteFile(specPath, InstallFiles.ToJson(spec));
        var currentSettings = InstallFiles.ReadObject(settingsPath) ?? [];
        currentSettings["runtime"] = "native";
        currentSettings["installed_source"] = source;
        currentSettings["native_root"] = Path.GetDirectoryName(versions);
        WriteFile(settingsPath, InstallFiles.ToJson(currentSettings));
        host.Step("spec");

        // Engine of this PC for the Antigravity Marketplace launcher (plugin\antigravity\scripts\start.cmd):
        // engine.txt (version|path of the versioned copy, never bin, which a promotion renames).
        Snapshot(Path.Join(state, "engine.txt"));
        StdioBridge.WritePointer(nativeExe);

        // New code: restart request to the daemon, never a forced stop. It restarts only if its code changed, once no
        // call is running, and its supervisor starts it again. Same for Antigravity's sidecar, between two deliveries.
        string fingerprint = $"sha256:{InstallFiles.Sha256(RelayPaths.CodeFile(nativeExe)).ToLowerInvariant()} ";
        bool forced = specChanged || ReadText(daemonRequest).StartsWith("force", StringComparison.Ordinal);
        RestartOwner.Request(daemonRequest, daemonOwner, forced ? "force" : "code", fingerprint);
        RestartOwner.Request(Path.Join(mailbox, "sidecar-restart.request"), sidecarOwner, specChanged ? "force" : "code", fingerprint);
        host.Step("restart");

        // Without the shared Nexus supervisor: logon task "NexusRelay" running the relay's small supervisor
        // (NexusRelay.exe --daemon: reads the spec, starts the daemon again when it stops).
        string? previousTask = host.Tasks.Xml(RelayTask.Name);
        bool taskRan = host.Tasks.State(RelayTask.Name) == "Running";
        bool shared = host.Tasks.Xml(RelayTask.SharedSupervisor) is not null;
        undoTask = () =>
        {
            if (previousTask is null || shared || string.Equals(TaskXml.Command(previousTask), TaskXml.Command(host.Tasks.Xml(RelayTask.Name)), StringComparison.Ordinal)) return;
            host.Tasks.Register(RelayTask.Name, previousTask);
            if (host.Tasks.State(RelayTask.Name) == "Running") host.Tasks.Stop(RelayTask.Name);
            if (taskRan) host.Tasks.Run(RelayTask.Name);
        };
        foreach (string line in RelayTask.Sync(host.Tasks, nativeExe, state, shared, host.UserId)) Say(line);
        host.Step("task");

        string agyResult = Antigravity(settings, french, nativeExe, token, packTargets);
        host.Step("antigravity");

        // --- Edit hook of Codex and Antigravity (the engine by its stable path): refuses a file reserved by another
        //     agent, reserves it otherwise. Claude Code has it through the nexus-relay plugin.
        if (Registration.RegisterHooks(home, stableExe, backups) != 0) throw new InvalidOperationException(Tr("Edit hook incomplete (see above).", "Hook d'édition incomplet (voir ci-dessus)."));
        // Old hook relays (PowerShell to Python or native): no declaration calls them any more.
        InstallFiles.TryDeleteTree(Path.Join(state, "hooks"));
        host.Step("hooks");

        // Committed: the old engine folders go once no engine uses them; the previous stable copy and app folder too.
        CleanStableBackups();
        EngineFolders.RemoveStale(versions, nativeKey, host.Images());
        committed = true;
        CommitApp();
        string install = Path.Join(source, "install.ps1");
        File.WriteAllText(Path.Join(state, "installed.stamp"), File.Exists(install) ? $"{nativeKey} {InstallFiles.Sha256(install)}" : nativeKey, InstallFiles.Utf8);

        // --- Public install: token in the user variable NEXUS_MCP_TOKEN (read by Claude Code and Codex, never written
        //     in their files), then the server declared to the clients found.
        bool pack = Environment.GetEnvironmentVariable("NEXUS_PACK") == "1";
        if (options.Register)
        {
            if (host.UserVariable("NEXUS_MCP_TOKEN") != token)
            {
                host.SetUserVariable("NEXUS_MCP_TOKEN", token);
                Say(Tr("User variable NEXUS_MCP_TOKEN set (restart Claude Code and Codex to see it).", "Variable utilisateur NEXUS_MCP_TOKEN posée (redémarrer Claude Code et Codex pour la voir)."));
            }
            Environment.SetEnvironmentVariable("NEXUS_MCP_TOKEN", token);
            host.Out.Flush();
            if (Registration.RegisterClients(home, options.Port, backups, agyResult.Length > 0 ? agyResult : null) != 0)
                throw new InvalidOperationException(Tr("Declaration to the clients incomplete (see above).", "Déclaration aux clients incomplète (voir ci-dessus)."));
        }
        // Nexus pack: the NEXUS_PACK_RESULT line stays the last one.
        if (options.Register && pack) return 0;
        Say(Tr("NexusRelay installed successfully.", "NexusRelay installé avec succès."));
        if (options.Register)
        {
            Say("");
            Say(Tr("Last steps:", "Dernières étapes :"));
            Say(Tr("  1. Restart Claude Code and Codex (quit them completely) so they see NEXUS_MCP_TOKEN.", "  1. Redémarrer Claude Code et Codex (les quitter complètement) pour qu'ils voient NEXUS_MCP_TOKEN."));
            Say(Tr(@"  2. Codex app, once: Add a project, name ""NexusRelay"", folder %USERPROFILE%\.nexus\relay\codex-projet.", @"  2. Appli Codex, une fois : Ajouter un projet, nom « NexusRelay », dossier %USERPROFILE%\.nexus\relay\codex-projet."));
            Say(Tr(@"  3. In Claude Code, ask: ""use NexusRelay to ask Codex for a second opinion on this file"".", "  3. Dans Claude Code, demander : « utilise NexusRelay pour demander à Codex un second avis sur ce fichier »."));
        }
        return 0;
    }

    private string? ChooseHost(List<string>? packTargets)
    {
        var agentOf = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase) { ["claude-code"] = "claude", ["claude"] = "claude", ["codex"] = "codex", ["antigravity"] = "antigravity" };
        string? forced = Environment.GetEnvironmentVariable("NEXUS_PACK_HOST")?.Trim();
        if (!string.IsNullOrEmpty(forced) && agentOf.TryGetValue(forced, out string? agent)) return agent;
        var present = new (string Key, bool Found)[]
        {
            ("claude-code", host.OnPath("claude") is not null || Directory.Exists(Path.Join(home, ".claude"))),
            ("codex", host.OnPath("codex") is not null || Directory.Exists(Path.Join(home, ".codex"))),
            ("antigravity", Directory.Exists(Path.Join(home, ".gemini"))),
        };
        // 1.12.2: with two AI apps or more, all equal ("all"); a single host is never chosen for the user.
        var agents = present.Where(p => p.Found && (packTargets is null || packTargets.Contains(p.Key))).Select(p => agentOf[p.Key]).ToList();
        return agents.Count >= 2 ? "all" : agents.FirstOrDefault();
    }

    private void RegisterKnownChats(JsonObject daemonEnv)
    {
        string[] keys = ["NEXUS_RELAY_HOME", "NEXUS_RELAY_CODEX_WORKSPACE", "NEXUS_RELAY_CODEX_PROJECT", "NEXUS_RELAY_CODEX_PROJECT_ID"];
        var prior = keys.ToDictionary(k => k, Environment.GetEnvironmentVariable);
        try
        {
            foreach (string key in keys) Environment.SetEnvironmentVariable(key, daemonEnv[key]?.ToString());
            CodexSpace.RegisterHistory(new RelayBox(Path.Join(mailbox, "v1")));
        }
        catch (Exception e) when (e is not OutOfMemoryException)
        {
            throw new InvalidOperationException(Tr("Checking the earlier Relay chats failed; none is claimed by its folder alone.", "Vérification des anciens chats Relay en échec ; aucune appropriation par dossier seul.") + " " + e.Message, e);
        }
        finally { foreach (var (key, value) in prior) Environment.SetEnvironmentVariable(key, value); }
    }

    // ------------------------------------------------------------------------------------------------- app folder

    private string app => Path.Join(root, "app");

    /// <summary>The package becomes the app folder: copied beside it, then switched by renames (an older version's files
    /// that no longer exist are gone with it). Nothing runs from app; a folder Windows refuses to rename is mirrored in place.</summary>
    private void MirrorApp()
    {
        string package = Path.GetFullPath(source).TrimEnd('\\');
        if (string.Equals(package, app.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase)) return;
        string pid = Environment.ProcessId.ToString(CultureInfo.InvariantCulture);
        string fresh = $"{app}.new-{pid}", old = $"{app}.old-{pid}";
        // Copies left by an interrupted run ("app.*" would also match app itself in Windows' wildcard rules).
        foreach (string leftover in Directory.Exists(root) ? Directory.EnumerateDirectories(root).Where(d => Path.GetFileName(d).StartsWith("app.new-", StringComparison.OrdinalIgnoreCase) || Path.GetFileName(d).StartsWith("app.old-", StringComparison.OrdinalIgnoreCase)) : [])
            if (!Running(leftover)) InstallFiles.TryDeleteTree(leftover);
        InstallFiles.TryDeleteTree(fresh);
        InstallFiles.CopyTree(package, fresh, new HashSet<string>(StringComparer.OrdinalIgnoreCase) { ".git" });
        InstallFiles.Unblock(fresh);
        if (!Directory.Exists(app))
        {
            InstallFiles.MoveFolder(fresh, app);
            undoApp = () => { if (Directory.Exists(app)) InstallFiles.MoveFolder(app, fresh); InstallFiles.TryDeleteTree(fresh); };
        }
        else
        {
            bool moved = false;
            try { InstallFiles.MoveFolder(app, old); moved = true; }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException) { /* a file of app stays open */ }
            if (moved)
            {
                InstallFiles.MoveFolder(fresh, app);
                undoApp = () =>
                {
                    if (Directory.Exists(app)) InstallFiles.MoveFolder(app, fresh);
                    InstallFiles.MoveFolder(old, app);
                    InstallFiles.TryDeleteTree(fresh);
                };
            }
            else
            {
                // A file of app stays open: a copy of it is kept aside, then app is mirrored in place (what robocopy
                // /MIR did); a failure mirrors the copy back.
                InstallFiles.CopyTree(app, old);
                MirrorInPlace(fresh, app);
                InstallFiles.TryDeleteTree(fresh);
                undoApp = () => { MirrorInPlace(old, app); InstallFiles.TryDeleteTree(old); };
            }
        }
        source = app;
        Say(Tr($"NexusRelay copied into {app}.", $"NexusRelay copié dans {app}."));
    }

    /// <summary>Makes a folder equal to another file by file: copies every file, removes those it does not have.</summary>
    private static void MirrorInPlace(string from, string to)
    {
        InstallFiles.CopyTree(from, to);
        foreach (string file in Directory.EnumerateFiles(to, "*", SearchOption.AllDirectories).ToList())
        {
            if (File.Exists(Path.Join(from, Path.GetRelativePath(to, file)))) continue;
            try { File.Delete(file); }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException) { /* held: next time */ }
        }
    }

    private void CommitApp()
    {
        string pid = Environment.ProcessId.ToString(CultureInfo.InvariantCulture);
        InstallFiles.TryDeleteTree($"{app}.old-{pid}");
    }

    private static bool Running(string folder) => EngineFolders.InUse(folder, InstallNative.Images.Of());

    // ------------------------------------------------------------------------------------------------- engine

    /// <summary>
    /// The immutable versioned copy of the engine: the folder this engine already runs from when install.ps1 built it
    /// there, else bin\ of the source (a release) or this engine's own folder, copied once under its fingerprint.
    /// </summary>
    private (string Key, string Exe) InstallVersion()
    {
        string self = Path.GetFullPath(AppContext.BaseDirectory).TrimEnd('\\');
        string? built = string.Equals(Path.GetDirectoryName(self), Path.GetFullPath(versions).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase)
            && File.Exists(Path.Join(self, "publish.complete")) ? self : null;
        string key, folder;
        if (built is not null && !File.Exists(Path.Join(source, "bin", "NexusRelay.exe")))
        {
            key = Path.GetFileName(built);
            folder = built;
        }
        else
        {
            string payload = File.Exists(Path.Join(source, "bin", "NexusRelay.exe")) ? Path.Join(source, "bin") : self;
            if (!File.Exists(Path.Join(payload, "NexusRelay.exe")))
                throw new FileNotFoundException(Tr($"Native engine missing: no bin\\NexusRelay.exe in {source}.", $"Moteur natif absent : pas de bin\\NexusRelay.exe dans {source}."));
            key = EngineFolders.Digest(payload);
            folder = Path.Join(versions, key);
            if (!File.Exists(Path.Join(folder, "publish.complete")))
            {
                string partial = folder + ".partial";
                InstallFiles.TryDeleteTree(partial);
                InstallFiles.CopyTree(payload, partial);
                File.Delete(Path.Join(partial, "publish.complete"));
                File.Delete(Path.Join(partial, EngineLock.FileName));
                File.WriteAllText(Path.Join(partial, EngineLock.FileName), "", InstallFiles.Utf8);
                if (Directory.Exists(folder) && !EngineFolders.RemoveIfUnused(folder, host.Images()))
                    throw new IOException(Tr($"{folder} is incomplete and in use: run the installation again.", $"{folder} est incomplet et en cours d'utilisation : relancer l'installation."));
                InstallFiles.MoveFolder(partial, folder);
                File.WriteAllText(Path.Join(folder, "publish.complete"), key, InstallFiles.Utf8);
            }
        }
        // Usage lock (EngineLock.cs): each engine started from a versioned copy holds its engine.lock for its whole
        // life, which keeps the folder from the cleanup of old versions. Never in bin.
        string engineLock = Path.Join(folder, EngineLock.FileName);
        if (!File.Exists(engineLock)) File.WriteAllText(engineLock, "", InstallFiles.Utf8);
        return (key, InstallNative.PhysicalPath(Path.Join(folder, "NexusRelay.exe")));
    }

    private bool StableValid(string key)
    {
        string stable = Path.Join(state, "bin"), stamp = Path.Join(stable, "publish.complete");
        return File.Exists(stamp) && File.ReadAllText(stamp) == key && host.Healthy(Path.Join(stable, "NexusRelay.exe"));
    }

    /// <summary>
    /// Stable path of the engine (~\.nexus\nexus-relay\bin, edit hooks and commit guards): a copy of the version in
    /// service, replaced without breaking a guard that runs: full copy beside it, checked, then switched by protected
    /// renames, restored on failure; an old folder still used by a hook goes at a later install.
    /// </summary>
    private void Promote(string key, string installedDir)
    {
        string stable = Path.Join(state, "bin"), stableExe = Path.Join(stable, "NexusRelay.exe"), stamp = Path.Join(stable, "publish.complete");
        string pid = Environment.ProcessId.ToString(CultureInfo.InvariantCulture);
        // Temporary folders left by an interrupted install (dead PIDs only).
        foreach (string dir in Directory.EnumerateDirectories(state, "bin.new-*"))
        {
            if (int.TryParse(Path.GetFileName(dir)["bin.new-".Length..], NumberStyles.None, CultureInfo.InvariantCulture, out int other) && other != Environment.ProcessId && !RelayBox.PidAlive(other))
                InstallFiles.TryDeleteTree(dir);
        }
        // bin missing (interrupted after renaming it aside): the newest copy set aside comes back first.
        if (!Directory.Exists(stable) && Directory.EnumerateDirectories(state, "bin.old-*").OrderByDescending(Directory.GetLastWriteTimeUtc).FirstOrDefault() is { } latest)
        {
            try { Directory.Move(latest, stable); } catch (IOException) { /* stays aside: a fresh copy is made */ }
        }
        // Health even when the stamp matches: an earlier stop between switch and check may have left a broken binary.
        if (File.Exists(stamp) && File.ReadAllText(stamp) == key && File.Exists(stableExe))
        {
            if (host.Healthy(stableExe)) return;
            Warn(Tr($"The engine in '{stable}' carries the stamp but failed its health check (--schemas). Reinstalling.", $"Le moteur dans '{stable}' porte le stamp mais a échoué au contrôle de santé (--schemas). Réinstallation requise."));
        }

        string fresh = Path.Join(state, $"bin.new-{pid}");
        InstallFiles.TryDeleteTree(fresh);
        InstallFiles.CopyTree(installedDir, fresh);
        // Never validate a promotion before its health check; the usage lock protects versioned copies only (bin is
        // replaced at each update, nothing may hold a file in it).
        File.Delete(Path.Join(fresh, "publish.complete"));
        File.Delete(Path.Join(fresh, EngineLock.FileName));
        if (!host.Healthy(Path.Join(fresh, "NexusRelay.exe")))
        {
            InstallFiles.TryDeleteTree(fresh);
            throw new InvalidOperationException(Tr($"Stable candidate '{fresh}' invalid (--schemas).", $"Candidat stable '{fresh}' invalide (--schemas)."));
        }

        var link = new DirectoryInfo(stable);
        string? old = null, junction = null;
        bool inPlace = false;
        var left = new List<string>();
        if (link.Exists && link.LinkTarget is { } target)
        {
            junction = target;
            Directory.Delete(stable, false);  // old junction: the link only
        }
        else if (link.Exists)
        {
            old = Path.Join(state, $"bin.old-{DateTime.Now.ToString("yyyyMMddHHmmss", CultureInfo.InvariantCulture)}-{pid}");
            bool moved = false;
            for (int attempt = 0; attempt < 20 && !moved; attempt++)
            {
                try { Directory.Move(stable, old); moved = true; }
                catch (Exception e) when (e is IOException or UnauthorizedAccessException) { Thread.Sleep(250); }
            }
            if (!moved)
            {
                // A process started from bin (a hook of an engine before 1.13.11, which ran from there for up to 55 min
                // and held bin\engine.lock): Windows never renames a folder where a file is open or mapped, but moves
                // each file of a running program. The content goes aside file by file; a file still held stays (the
                // old engine.lock only), the new files take the place of the others.
                left = InstallFiles.MoveContent(stable, old);
                if (left.Any(l => File.Exists(Path.Join(fresh, l)) || Directory.Exists(Path.Join(fresh, l))))
                {
                    InstallFiles.MoveContent(old, stable);
                    InstallFiles.TryDeleteTree(old);
                    InstallFiles.TryDeleteTree(fresh);
                    throw new IOException(Tr($"{stable} is in use ({string.Join(", ", left)}): run the installation again.", $"{stable} est en cours d'utilisation ({string.Join(", ", left)}) : relancer l'installation."));
                }
                inPlace = true;
            }
        }

        void Undo()
        {
            if (inPlace)
            {
                // File by file: remove what was placed (not the files still held), then bring everything back.
                foreach (var item in new DirectoryInfo(stable).EnumerateFileSystemInfos())
                {
                    if (left.Contains(item.Name)) continue;
                    if (item is DirectoryInfo) InstallFiles.TryDeleteTree(item.FullName);
                    else { try { File.Delete(item.FullName); } catch (Exception e) when (e is IOException or UnauthorizedAccessException) { /* held */ } }
                }
                var back = InstallFiles.MoveContent(old!, stable);
                if (back.Count > 0) Warn(Tr($"Automatic restore failed: {string.Join(", ", back)} left in {old}.", $"Échec critique de restauration automatique : {string.Join(", ", back)} resté(s) dans {old}."));
                else { InstallFiles.TryDeleteTree(old!); Warn(Tr($"Previous version restored in {stable}.", $"Restauration automatique réussie dans {stable}.")); }
                return;
            }
            if (Directory.Exists(stable)) InstallFiles.TryDeleteTree(stable);
            if (old is not null && Directory.Exists(old))
            {
                try { InstallFiles.MoveFolder(old, stable); Warn(Tr($"Previous version restored from {old}.", $"Restauration automatique réussie depuis {old}.")); }
                catch (IOException e) { Warn(Tr($"Automatic restore failed: {old} could not be restored as {stable} ({e.Message}).", $"Échec critique de restauration automatique : {old} n'a pas pu être restauré en {stable} ({e.Message}).")); }
            }
            else if (junction is not null && !Directory.Exists(stable))
            {
                try { Directory.CreateSymbolicLink(stable, junction); Warn(Tr($"Link to {junction} restored.", $"Lien restauré vers {junction}.")); }
                catch (IOException e) { Warn(Tr($"Could not restore the link to {junction} ({e.Message}).", $"Échec de restauration du lien vers {junction} ({e.Message}).")); }
            }
        }

        try
        {
            if (inPlace)
            {
                var stuck = InstallFiles.MoveContent(fresh, stable);
                if (stuck.Count > 0) throw new IOException(Tr($"Candidate files not placed in {stable}: {string.Join(", ", stuck)}.", $"Fichiers du candidat non placés dans {stable} : {string.Join(", ", stuck)}."));
                InstallFiles.TryDeleteTree(fresh);
            }
            else InstallFiles.MoveFolder(fresh, stable);
            if (!host.Healthy(stableExe))
                throw new InvalidOperationException(Tr($"The engine in '{stable}' failed its health check (--schemas).", $"Le moteur dans '{stable}' a échoué au contrôle de santé (--schemas)."));
            File.WriteAllText(stamp, key, InstallFiles.Utf8);  // stamp only after the health check
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or InvalidOperationException)
        {
            Warn(Tr($"Switch to {stable} failed: {e.Message}. Restoring the previous version...", $"Échec lors de la bascule vers {stable} : {e.Message}. Tentative de restauration de la version précédente..."));
            Undo();
            InstallFiles.TryDeleteTree(fresh);
            throw;
        }
        undoStable = Undo;
    }

    /// <summary>Copies set aside by earlier promotions: removed unless an engine still runs from them, once the stable
    /// copy is valid; the engine.lock a 1.13.10 bin carried goes once no old hook holds it.</summary>
    private void CleanStableBackups()
    {
        string legacyLock = Path.Join(state, "bin", EngineLock.FileName);
        try { File.Delete(legacyLock); } catch (Exception e) when (e is IOException or UnauthorizedAccessException) { /* still held */ }
        var images = host.Images();
        foreach (string old in Directory.EnumerateDirectories(state, "bin.old-*")) EngineFolders.RemoveIfUnused(old, images);
    }

    // ------------------------------------------------------------------------------------------------- Antigravity

    /// <summary>Antigravity: plugin ~\.gemini\config\plugins\nexus-relay (MCP server by URL, skill, permanent sidecar
    /// "nexus-relay/relay"), its activation in config.json, in the Antigravity project "NexusRelay". Returns the line
    /// given to the clients' report ("" when Antigravity is outside the scope or absent).</summary>
    private string Antigravity(JsonObject settings, bool french, string nativeExe, string token, List<string>? packTargets)
    {
        string agyRoot = Path.Join(home, ".gemini", "config");
        if (packTargets is not null && !packTargets.Contains("antigravity"))
        {
            Say(Tr("Antigravity: outside the chosen scope (NEXUS_PACK_TARGETS), left untouched.", "Antigravity : hors du périmètre choisi (NEXUS_PACK_TARGETS), non touché."));
            return "";
        }
        if (!Directory.Exists(agyRoot))
        {
            Warn(Tr("Antigravity is not on this PC: plugin not installed.", "Antigravity absent de ce PC : plugin non installé."));
            return "";
        }
        string plugin = Path.Join(agyRoot, "plugins", "nexus-relay");
        int copied = CopyChanged(Path.Join(source, "plugin", "antigravity"), plugin);
        if (french && Directory.Exists(Path.Join(source, "fr", "plugin", "antigravity")))
            copied += CopyChanged(Path.Join(source, "fr", "plugin", "antigravity"), plugin);  // French copy on top
        // Files the plugin no longer ships, written by an earlier install (scripts\bridge.ps1, the PowerShell bridge
        // replaced by NexusRelay.exe --bridge in 1.13.8); the rest of the folder is never touched.
        foreach (string retired in new[] { Path.Join("scripts", "bridge.ps1") })
        {
            string stale = Path.Join(plugin, retired);
            if (File.Exists(stale)) { DeleteFile(stale); copied++; }
        }
        var mcp = new JsonObject
        {
            ["mcpServers"] = new JsonObject
            {
                ["nexus-relay"] = new JsonObject
                {
                    ["serverUrl"] = $"http://127.0.0.1:{options.Port.ToString(CultureInfo.InvariantCulture)}/mcp?agent=antigravity",
                    ["headers"] = new JsonObject { ["Authorization"] = $"Bearer {token}" },
                },
            },
        };
        if (WriteJsonIfChanged(Path.Join(plugin, "mcp_config.json"), mcp)) copied++;
        var sidecar = new JsonObject
        {
            ["display_name"] = "NexusRelay",
            ["description"] = french
                ? "Livre à Antigravity les messages de Claude (boîte ~\\.nexus\\relay) par agentapi, une conversation par sujet dans le projet NexusRelay ; une livraison incertaine n'est jamais rejouée."
                : "Delivers the Relay messages (mailbox ~\\.nexus\\relay) to Antigravity through agentapi, one conversation per topic in the NexusRelay project; an uncertain delivery is never replayed.",
            ["command"] = nativeExe,
            ["args"] = new JsonArray("--sidecar"),
            ["env"] = new JsonObject
            {
                ["NEXUS_RELAY_HOME"] = mailbox,
                ["NEXUS_RELAY_HOST"] = settings["host"]?.ToString() ?? "",
                ["NEXUS_RELAY_WAKE_SINCE"] = settings["wake_since"]?.ToString() ?? "",
                ["NEXUS_RELAY_SOURCE_ROOT"] = source,
            },
            ["restart_policy"] = "always",
        };
        if (WriteJsonIfChanged(Path.Join(plugin, "sidecars", "relay", "sidecar.json"), sidecar)) copied++;
        Say(copied > 0
            ? Tr($"Antigravity: nexus-relay plugin installed ({copied} file(s)) in {plugin}.", $"Antigravity : plugin nexus-relay installé ({copied} fichier(s)) dans {plugin}.")
            : Tr($"Antigravity: nexus-relay plugin already up to date in {plugin}.", $"Antigravity : plugin nexus-relay déjà à jour dans {plugin}."));

        // Project where agentapi creates the conversations: "NexusRelay", apart from the user's, on an empty folder
        // outside any synchronized folder; created here when missing. A "Nexus Relay" project from before 1.13.2 is
        // kept as is (same id, conversations kept): its file is Antigravity's, never renamed here.
        string[] names = ["NexusRelay", "Nexus Relay"];
        string relayFolder = Path.Join(mailbox, "agy-projet");
        Directory.CreateDirectory(relayFolder);
        string projects = Path.Join(agyRoot, "projects");
        Directory.CreateDirectory(projects);
        string? projectId = null;
        int rank = names.Length;
        foreach (string file in Directory.EnumerateFiles(projects, "*.json"))
        {
            var candidate = InstallFiles.ReadObject(file);
            int at = Array.IndexOf(names, candidate?["name"]?.ToString());
            if (candidate?["id"]?.ToString() is { Length: > 0 } id && at >= 0 && at < rank) { projectId = id; rank = at; }
        }
        if (projectId is null)
        {
            projectId = Guid.NewGuid().ToString();
            string full = Path.GetFullPath(relayFolder);
            string uri = "file:///" + char.ToLowerInvariant(full[0]) + "%3A" + full[2..].Replace('\\', '/');
            var definition = new JsonObject
            {
                ["id"] = projectId, ["name"] = names[0],
                ["projectResources"] = new JsonObject { ["resources"] = new JsonArray(new JsonObject { ["folderUri"] = uri }) },
                ["permissionGrants"] = new JsonObject { ["permissionGrants"] = new JsonObject() }, ["settings"] = new JsonObject(), ["isWorkspaceOnly"] = false,
            };
            WriteFile(Path.Join(projects, $"{projectId}.json"), InstallFiles.ToJson(definition));
            Say(Tr($"Antigravity: project \"{names[0]}\" created ({relayFolder}).", $"Antigravity : projet « {names[0]} » créé ({relayFolder})."));
        }
        // Sidecar enabled in config.json; nothing else touched.
        string configPath = Path.Join(agyRoot, "config.json");
        var config = InstallFiles.ReadObject(configPath) ?? [];
        var activation = AntigravityActivation.Apply(config, projectId);
        if (activation.Disabled) Say(Tr("Antigravity: the choice to disable the plugin or the sidecar is kept.", "Antigravity : choix de désactivation du plugin ou du sidecar conservé."));
        else if (activation.Changed)
        {
            if (File.Exists(configPath))
            {
                string backup = Path.Join(backups, $"antigravity-config-{DateTime.Now.ToString("yyyyMMdd-HHmmss", CultureInfo.InvariantCulture)}.json");
                File.Copy(configPath, backup, overwrite: true);
                Say(Tr($"  backup copy: {backup}", $"  copie de sauvegarde : {backup}"));
            }
            WriteFile(configPath, InstallFiles.ToJson(activation.Config));
            Say(Tr($"Antigravity: nexus-relay plugin and nexus-relay/relay sidecar enabled (project {projectId}), hot-reloaded by Antigravity.", $"Antigravity : plugin et sidecar nexus-relay/relay activés (projet {projectId}), rechargés à chaud par Antigravity."));
        }
        else Say(Tr($"Antigravity: nexus-relay plugin and nexus-relay/relay sidecar already enabled (project {projectId}).", $"Antigravity : plugin et sidecar nexus-relay/relay déjà activés (projet {projectId})."));
        return $"plugin installed in {plugin}";
    }

    // ------------------------------------------------------------------------------------------------- journal

    private void Snapshot(string path)
    {
        if (!journaled.Add(path)) return;
        journal.Add((path, File.Exists(path) ? File.ReadAllBytes(path) : null));
    }

    private void WriteFile(string path, string text)
    {
        Snapshot(path);
        InstallFiles.WriteAtomic(path, text);
    }

    private void CopyFile(string from, string to)
    {
        Snapshot(to);
        Directory.CreateDirectory(Path.GetDirectoryName(to)!);
        File.Copy(from, to, overwrite: true);
    }

    private void DeleteFile(string path)
    {
        if (!File.Exists(path)) return;
        Snapshot(path);
        File.Delete(path);
    }

    private bool WriteJsonIfChanged(string path, JsonObject data)
    {
        if (JsonNode.DeepEquals(InstallFiles.ReadObject(path), data)) return false;
        WriteFile(path, InstallFiles.ToJson(data));
        return true;
    }

    /// <summary>Copies the files of a tree whose content differs; returns how many were copied.</summary>
    private int CopyChanged(string from, string to)
    {
        int copied = 0;
        foreach (string file in Directory.EnumerateFiles(from, "*", SearchOption.AllDirectories))
        {
            string target = Path.Join(to, Path.GetRelativePath(from, file));
            if (File.Exists(target) && InstallFiles.Sha256(target) == InstallFiles.Sha256(file)) continue;
            CopyFile(file, target);
            copied++;
        }
        return copied;
    }

    private static string ReadText(string path)
    {
        try { return File.Exists(path) ? File.ReadAllText(path, Encoding.UTF8) : ""; }
        catch (IOException) { return ""; }
    }

    /// <summary>Puts back what this run changed, newest first; each part on its own, so one failure leaves the others.</summary>
    private void Restore()
    {
        if (committed) return;  // the engine is in place: a failure to declare the clients leaves it
        void Try(string what, Action action)
        {
            try { action(); }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException or InvalidOperationException or System.Runtime.InteropServices.COMException)
            {
                Warn(Tr($"Restore of {what} incomplete: {e.Message}", $"Restauration de {what} incomplète : {e.Message}"));
            }
        }
        if (undoTask is { } task) Try("the scheduled task", task);
        for (int i = journal.Count - 1; i >= 0; i--)
        {
            var (path, before) = journal[i];
            Try(path, () =>
            {
                if (before is null) { if (File.Exists(path)) File.Delete(path); return; }
                string temporary = $"{path}.{Environment.ProcessId.ToString(CultureInfo.InvariantCulture)}.restore";
                File.WriteAllBytes(temporary, before);
                File.Move(temporary, path, overwrite: true);
            });
        }
        if (undoStable is { } stable) Try(Path.Join(state, "bin"), stable);
        if (undoApp is { } appUndo) Try(app, appUndo);
        // The daemon and the sidecar may have been asked to restart on the new engine: they restart on the restored one.
        if (specChanged && previousExe is not null) Try("the daemon restart", () => File.WriteAllText(Path.Join(mailbox, "daemon-restart.request"), "force restore", InstallFiles.Utf8));
        if (previousSidecarExe is not null && journaled.Contains(Path.Join(home, ".gemini", "config", "plugins", "nexus-relay", "sidecars", "relay", "sidecar.json")))
            Try("the sidecar restart", () => File.WriteAllText(Path.Join(mailbox, "sidecar-restart.request"), "force restore", InstallFiles.Utf8));
        if (journal.Count > 0 || undoStable is not null || undoApp is not null) Warn(Tr("Installation failed: the previous installation was put back.", "Installation échouée : l'installation précédente a été remise en place."));
    }
}
