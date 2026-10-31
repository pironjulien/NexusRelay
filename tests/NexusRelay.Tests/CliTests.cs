using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace NexusRelay.Tests;

/// <summary>Windows' own SQLite, to write app database fixtures.</summary>
internal static partial class Sqlite
{
    [LibraryImport("winsqlite3.dll", EntryPoint = "sqlite3_open_v2", StringMarshalling = StringMarshalling.Utf8), UnmanagedCallConv(CallConvs = [typeof(CallConvCdecl)])] private static partial int Open(string path, out nint db, int flags, nint vfs);
    [LibraryImport("winsqlite3.dll", EntryPoint = "sqlite3_exec", StringMarshalling = StringMarshalling.Utf8), UnmanagedCallConv(CallConvs = [typeof(CallConvCdecl)])] private static partial int Exec(nint db, string sql, nint callback, nint arg, out nint error);
    [LibraryImport("winsqlite3.dll", EntryPoint = "sqlite3_close"), UnmanagedCallConv(CallConvs = [typeof(CallConvCdecl)])] private static partial int Close(nint db);
    public static string Q(string text) => "'" + text.Replace("'", "''") + "'";
    public static void Run(string path, string sql)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        Assert.Equal(0, Open(path, out nint db, 0x02 | 0x04, 0));
        try { Assert.Equal(0, Exec(db, sql, 0, 0, out _)); } finally { Close(db); }
    }
}

/// <summary>Command-line entry points of the engine: hooks, commit guard, app activity and history, quota, CDP, sidecar.</summary>
public sealed class CliTests : IDisposable
{
    private readonly Isolation iso = new();
    public void Dispose() => iso.Dispose();
    private const string CodexSchema = "CREATE TABLE threads(id TEXT,rollout_path TEXT,cwd TEXT,archived INTEGER); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);";

    [Fact]
    public void Edit_guard_begin_finish_and_notice_in_utf8()
    {
        Native.Git(iso.Root, "init", "-q");
        string target = Path.Join(iso.Root, "épreuve.py");
        var edit = new JsonObject { ["session_id"] = "root-a", ["tool_use_id"] = "appel-1", ["tool_name"] = "apply_patch", ["tool_input"] = new JsonObject { ["command"] = $"*** Add File: {target}" } };
        var begin = Native.Run(["--edit-guard", "codex"], edit.ToJsonString());
        Assert.Equal(0, begin.Code);
        Assert.Equal("", begin.Out);
        var box = iso.Box();
        var held = Reservations.Active(box);
        Assert.Single(held);
        Assert.Equal("root-a", held[0]["session"]?.ToString());
        Assert.Equal([target], (held[0]["paths"] as JsonArray)!.Select(p => p!.ToString()));
        Assert.Equal("{}", Native.Run(["--edit-guard", "codex", "--finish-tool"], edit.ToJsonString()).Out.Trim());
        Assert.Empty(Reservations.Active(box));
        var incoming = box.Post("codex", "claude", "Avis technique", "native-hooks", info: true);
        string prompt = new JsonObject { ["session_id"] = "root-a", ["hook_event_name"] = "UserPromptSubmit" }.ToJsonString();
        var notice = JsonNode.Parse(Native.Run(["--notice"], prompt).Out)!;
        Assert.Equal("UserPromptSubmit", notice["hookSpecificOutput"]?["hookEventName"]?.ToString());
        Assert.Contains("native-hooks", notice["hookSpecificOutput"]!["additionalContext"]!.ToString());
        Assert.False(box.IsRead(incoming));
        Assert.Equal("", Native.Run(["--notice"], prompt).Out);  // one notice per host conversation, no destructive read
    }

    [Fact]
    public void Edit_guard_releases_a_file_outside_git_at_the_end_of_the_turn()
    {
        string target = Path.Join(iso.Root, "notes", "mémoire.md");
        Directory.CreateDirectory(Path.GetDirectoryName(target)!);
        File.WriteAllText(target, "avant");
        var edit = new JsonObject { ["session_id"] = "root-b", ["tool_use_id"] = "appel-2", ["tool_name"] = "Edit", ["tool_input"] = new JsonObject { ["file_path"] = target } };
        Assert.Equal("", Native.Run(["--edit-guard", "claude"], edit.ToJsonString()).Out);
        File.WriteAllText(target, "après");
        Native.Run(["--edit-guard", "claude", "--finish-tool"], edit.ToJsonString());
        var box = iso.Box();
        Assert.Single(Reservations.Active(box));  // still in the turn: other conversations keep waiting
        Native.Run(["--edit-guard", "claude", "--finish-session"], new JsonObject { ["session_id"] = "root-b" }.ToJsonString());
        Assert.Empty(Reservations.Active(box));  // no commit will ever come: the turn's end releases it
    }

    [Fact]
    public void Edit_guard_releases_a_git_ignored_file_at_the_end_of_the_turn_but_keeps_a_tracked_change()
    {
        Native.Git(iso.Root, "init", "-q");
        File.WriteAllText(Path.Join(iso.Root, ".gitignore"), "memoire/\n");
        string ignored = Path.Join(iso.Root, "memoire", "MEMORY.md"), tracked = Path.Join(iso.Root, "suivi.md");
        Directory.CreateDirectory(Path.GetDirectoryName(ignored)!);
        foreach (var (file, id) in new[] { (ignored, "appel-3"), (tracked, "appel-4") })
        {
            var edit = new JsonObject { ["session_id"] = "root-c", ["tool_use_id"] = id, ["tool_name"] = "Write", ["tool_input"] = new JsonObject { ["file_path"] = file } };
            Assert.Equal("", Native.Run(["--edit-guard", "claude"], edit.ToJsonString()).Out);
            File.WriteAllText(file, "contenu");
            Native.Run(["--edit-guard", "claude", "--finish-tool"], edit.ToJsonString());
        }
        var box = iso.Box();
        Native.Run(["--edit-guard", "claude", "--finish-session"], new JsonObject { ["session_id"] = "root-c" }.ToJsonString());
        var held = Reservations.Active(box);
        Assert.Equal([tracked], held.SelectMany(r => (r["paths"] as JsonArray)!.Select(p => p!.ToString())));  // waits for its commit
    }

    [Fact]
    public void Edit_guard_lets_another_conversation_edit_a_file_once_committed_without_the_post_commit_hook()
    {
        // 31/10: a repository cloned again had lost its post-commit hook; the committed files stayed reserved for 25 min.
        Native.Git(iso.Root, "init", "-q");
        Native.Git(iso.Root, "config", "user.email", "fixture@example.org");
        Native.Git(iso.Root, "config", "user.name", "Fixture");
        string target = Path.Join(iso.Root, "McpServer.cs");
        File.WriteAllText(target, "avant\n");
        Native.Git(iso.Root, "add", "McpServer.cs");
        Native.Git(iso.Root, "commit", "-q", "-m", "base");
        JsonObject Edit(string session, string id) => new() { ["session_id"] = session, ["tool_use_id"] = id, ["tool_name"] = "Edit", ["tool_input"] = new JsonObject { ["file_path"] = target } };
        Assert.Equal("", Native.Run(["--edit-guard", "claude"], Edit("root-a", "appel-5").ToJsonString()).Out);
        File.WriteAllText(target, "après\n");
        Native.Run(["--edit-guard", "claude", "--finish-tool"], Edit("root-a", "appel-5").ToJsonString());
        Assert.Contains("deny", Native.Run(["--edit-guard", "claude"], Edit("root-b", "appel-6").ToJsonString()).Out);  // uncommitted work
        Native.Git(iso.Root, "commit", "-q", "-am", "work");  // no hook, holder's turn still running
        Assert.Equal("", Native.Run(["--edit-guard", "claude"], Edit("root-b", "appel-7").ToJsonString()).Out);
        var held = Reservations.Active(iso.Box());
        Assert.Equal(["root-b"], held.Select(r => r["session"]!.ToString()));  // the committed one ended, the new edit holds it
    }

    [Fact]
    public void Commit_guard_allows_siblings_and_rejects_other_chats()
    {
        string repository = Path.Join(iso.Root, "repo");
        Directory.CreateDirectory(repository);
        Native.Git(repository, "init", "-q");
        string target = Path.Join(repository, "a.py");
        File.WriteAllText(target, "change = 1\n");
        Native.Git(repository, "add", "a.py");
        var box = iso.Box();
        string heldId = Reservations.Reserve(box, "codex", [target], "parent's work", session: "root-a")["id"]!.ToString();
        var env = new Dictionary<string, string?> { ["NEXUS_RELAY_AGENT"] = "codex", ["CODEX_SESSION_ID"] = "root-a" };
        foreach (string child in new[] { "child-1", "child-2" })
        {
            var same = Native.Run(["--staged"], cwd: repository, env: new Dictionary<string, string?>(env) { ["CODEX_THREAD_ID"] = child });
            Assert.True(same.Code == 0, same.Err);
        }
        var other = Native.Run(["--staged"], cwd: repository, env: new Dictionary<string, string?>(env) { ["CODEX_SESSION_ID"] = "root-b", ["CODEX_THREAD_ID"] = "child-3" });
        Assert.Equal(1, other.Code);
        Assert.Contains("a.py", other.Err);
        Assert.Equal(heldId, Reservations.Active(box)[0]["id"]?.ToString());  // checking never releases or changes its owner
        var legacy = Native.Run(["--staged"], cwd: repository, env: new Dictionary<string, string?> { ["NEXUS_RELAY_AGENT"] = "codex", ["CODEX_THREAD_ID"] = "root-a" });
        Assert.Equal(0, legacy.Code);
    }

    [Fact]
    public void Git_commit_hooks_call_the_installed_engine_through_its_stable_path()
    {
        // The hooks installed by scripts/install_commit_guard.ps1, run by Git's own sh with HOME pointing at a fixture
        // profile whose ~/.nexus/nexus-relay/bin holds the engine.
        string bin = Path.Join(iso.Home, ".nexus", "nexus-relay", "bin");
        Directory.CreateDirectory(bin);
        foreach (string file in Directory.EnumerateFiles(AppContext.BaseDirectory, "*", SearchOption.TopDirectoryOnly))
            File.Copy(file, Path.Join(bin, Path.GetFileName(file)));
        string repository = Path.Join(iso.Root, "guarded");
        Directory.CreateDirectory(repository);
        Native.Git(repository, "init", "-q");
        Native.Git(repository, "config", "user.email", "fixture@example.org");
        Native.Git(repository, "config", "user.name", "Fixture");
        var install = Native.Pwsh(Path.Join(Paths.Repo(), "scripts", "install_commit_guard.ps1"), "-Root", repository);
        Assert.True(install.Code == 0, install.Err + install.Out);
        Assert.Contains("pre-commit", install.Out);
        string target = Path.Join(repository, "réservé.txt");
        File.WriteAllText(target, "x");
        var box = iso.Box();
        Reservations.Reserve(box, "antigravity", [target], "fixture held by another agent");
        Native.Git(repository, "add", "réservé.txt");
        var env = new Dictionary<string, string?> { ["HOME"] = iso.Home, ["NEXUS_RELAY_AGENT"] = "codex" };
        var refused = Native.RunFile("git", ["-C", repository, "commit", "-q", "-m", "fixture"], env: env);
        Assert.NotEqual(0, refused.Code);
        Reservations.Release(box, "antigravity");
        var accepted = Native.RunFile("git", ["-C", repository, "commit", "-q", "-m", "fixture"], env: env);
        Assert.True(accepted.Code == 0, accepted.Err);
    }

    [Fact]
    public void History_registration_never_adopts_a_human_child_or_current_chat()
    {
        string documents = Path.Join(iso.Root, "Documents");
        iso.Set("NEXUS_RELAY_CODEX_WORKSPACE", documents);
        iso.Set("NEXUS_RELAY_CODEX_PROJECT", "Documents");
        iso.Set("NEXUS_RELAY_CODEX_PROJECT_ID", "official-fixture-id");
        iso.Set("CODEX_SESSION_ID", "parent");
        var box = iso.Box();
        var sql = new StringBuilder(CodexSchema);
        foreach (string chat in new[] { "relay", "human", "quoted", "child", "parent" })
        {
            var ask = box.Post("claude", "codex", "fixture", "ownership");
            string text = $"Nexus Relay : message d'un pair pour toi, read(id=\"{ask.Id}\").";
            if (chat == "quoted") text = "A user quote: " + text;
            string log = Path.Join(iso.Root, chat + ".jsonl");
            File.WriteAllText(log, new JsonObject { ["type"] = "response_item", ["payload"] = new JsonObject { ["type"] = "message", ["role"] = "user", ["content"] = new JsonArray(new JsonObject { ["type"] = "input_text", ["text"] = text }) } }.ToJsonString() + "\n");
            sql.Append($"INSERT INTO threads VALUES({Sqlite.Q(chat)},{Sqlite.Q(log)},{Sqlite.Q(documents)},1);");
            if (chat != "human") box.Event(ask.Id, "réveil", new() { ["status"] = "started", ["conversation"] = chat });
        }
        sql.Append("INSERT INTO thread_spawn_edges VALUES('relay','child');");
        Sqlite.Run(Path.Join(iso.CodexHome, "state_5.sqlite"), sql.ToString());
        var result = Native.Json("--register-known-relay-chats");
        Assert.Equal(["relay"], (result["registered"] as JsonArray)!.Select(n => n!.ToString()));
        var registry = box.State("codex-owned")!;
        Assert.Equal(["relay"], registry["threads"]!.AsObject().Select(p => p.Key));
        Assert.Equal("verified-relay-history", registry["threads"]!["relay"]!["origin"]?.ToString());
        Assert.Equal(0, Native.Json("--register-known-relay-chats")["count"]!.GetValue<int>());
    }

    private static JsonNode Activity() => Native.Json("--activity-status");

    [Fact]
    public void Codex_long_running_turn_remains_busy_until_durable_completion()
    {
        string log = Path.Join(iso.Root, "rollout.jsonl");
        File.WriteAllText(log, """{"timestamp": "2000-01-01T00:00:00Z", "payload": {"type": "task_started"}}""" + "\n");
        File.SetLastWriteTimeUtc(log, DateTime.UtcNow.AddHours(-2));
        Sqlite.Run(Path.Join(iso.CodexHome, "state_5.sqlite"), CodexSchema + $"INSERT INTO threads VALUES('long',{Sqlite.Q(log)},'workspace',0);");
        Assert.Equal("working", Activity()["codex"]?["status"]?.ToString());
        File.AppendAllText(log, """{"timestamp": "2000-01-01T02:00:00Z", "payload": {"type": "task_complete"}}""" + "\n");
        Assert.Equal("idle", Activity()["codex"]?["status"]?.ToString());
        File.Delete(log);
        Assert.Equal("unknown", Activity()["codex"]?["status"]?.ToString());
    }

    [Fact]
    public void Antigravity_old_incomplete_transcript_remains_busy_and_missing_is_unknown()
    {
        string root = Path.Join(iso.Gemini, "antigravity");
        const string conversation = "12345678-1234-1234-1234-123456789abc";
        Sqlite.Run(Path.Join(root, "conversations", conversation + ".db"), "CREATE TABLE steps(idx INTEGER,step_type INTEGER,step_payload BLOB); INSERT INTO steps VALUES(1,1,X'616374697665');");
        string transcript = Path.Join(root, "brain", conversation, ".system_generated", "logs", "transcript.jsonl");
        Directory.CreateDirectory(Path.GetDirectoryName(transcript)!);
        File.WriteAllText(transcript, """{"created_at": "2000-01-01T00:00:00Z", "type": "GENERIC", "status": "RUNNING"}""" + "\n");
        File.SetLastWriteTimeUtc(transcript, DateTime.UtcNow.AddHours(-2));
        Assert.Equal("working", Activity()["antigravity"]?["status"]?.ToString());
        File.AppendAllText(transcript, """{"created_at": "2000-01-01T02:00:00Z", "type": "PLANNER_RESPONSE", "status": "DONE"}""" + "\n");
        Assert.Equal("idle", Activity()["antigravity"]?["status"]?.ToString());
        File.Delete(transcript);
        Assert.Equal("unknown", Activity()["antigravity"]?["status"]?.ToString());
    }

    [Fact]
    public void Quota_reads_the_selected_family_and_observation_time()
    {
        string path = Path.Join(iso.Root, "quota.json");
        iso.Set("NEXUS_RELAY_KEYPAD_QUOTA", path);
        var observed = DateTimeOffset.UtcNow;
        var data = new JsonObject
        {
            ["lu"] = observed.ToString("O"), ["groupeSelectionne"] = "gemini",
            ["groupes"] = new JsonArray(
                new JsonObject { ["groupe"] = "gemini", ["cinq"] = true, ["utilise"] = 0.8, ["fin"] = observed.AddHours(1).ToString("O") },
                new JsonObject { ["groupe"] = "gemini", ["cinq"] = false, ["utilise"] = 0.3, ["fin"] = null },
                new JsonObject { ["groupe"] = "argon", ["cinq"] = false, ["utilise"] = 1.0, ["fin"] = null }),
        };
        JsonNode Read() { File.WriteAllText(path, data.ToJsonString()); return Native.Json("--quota-status"); }
        var result = Read();
        Assert.Null(result["blocked_reason"]);
        Assert.Equal("gemini", result["usage"]?["selected_family"]?.ToString());
        Assert.Equal(data["lu"]!.ToString(), result["usage"]?["read_at"]?.ToString());
        data["groupeSelectionne"] = "argon";
        result = Read();
        Assert.Contains("argon", result["blocked_reason"]!.ToString());
        data["groupeSelectionne"] = null;
        Assert.Null(Read()["blocked_reason"]);
        data["groupeSelectionne"] = "gemini";
        data["groupes"]![0]!["utilise"] = 1;
        data["groupes"]![0]!["fin"] = observed.AddSeconds(-1).ToString("O");
        Assert.Null(Read()["blocked_reason"]);
        data["lu"] = observed.AddHours(-7).ToString("O");
        data["groupeSelectionne"] = "argon";
        result = Read();
        Assert.Equal("unknown", result["usage"]?["status"]?.ToString());
        Assert.Null(result["blocked_reason"]);
    }

    [Fact]
    public void User_application_survives_caller_job_with_negative_control()
    {
        string output = Path.Join(Paths.Scratch, "..", "lifetime");
        var build = Native.RunFile("dotnet", ["publish", Path.Join(Paths.Repo(), "tests", "native_lifetime", "LifetimeFixture.csproj"), "-c", "Release", "-o", output, "--nologo", "-v", "q"], timeoutS: 180);
        Assert.True(build.Code == 0, build.Out + build.Err);
        var result = Native.RunFile(Path.Join(output, "LifetimeFixture.exe"), ["--test"], timeoutS: 45);
        Assert.True(result.Code == 0, result.Out + result.Err);
        var lines = result.Out.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).Select(l => JsonNode.Parse(l)!).ToList();
        Assert.Equal(2, lines.Count);
        foreach (var (line, broker) in lines.Zip(new[] { false, true }))
        {
            Assert.True(line["ok"]!.GetValue<bool>());
            Assert.Equal(broker, line["broker"]!.GetValue<bool>());
            Assert.True(line["caller_in_job"]!.GetValue<bool>());
            Assert.Equal(!broker, line["child_in_caller_job"]!.GetValue<bool>());
            Assert.Equal(broker, line["survived_job_close"]!.GetValue<bool>());
            Assert.Equal(broker, line["child_normal_exit"]!.GetValue<bool>());
        }
        Assert.False(Directory.Exists(Path.Join(Paths.Repo(), "tests", "native_lifetime", "bin")));
        Assert.False(Directory.Exists(Path.Join(Paths.Repo(), "tests", "native_lifetime", "obj")));
    }

    [Fact]
    public void Cdp_reads_multiple_responses_and_real_controls()
    {
        using var server = new FakeDebugger();
        var response = Native.Run(["--inspect", "codex"], env: new Dictionary<string, string?> { ["NEXUS_RELAY_CODEX_PORT"] = server.Port.ToString() });
        var data = JsonNode.Parse(response.Out)!;
        Assert.True(data["status"]?.ToString() == "success", response.Out + response.Err);
        Assert.True(data["relay_project_visible"]!.GetValue<bool>());
        Assert.Equal("Codex", data["mode"]?.ToString());
        Assert.True(data["composer_visible"]!.GetValue<bool>());
    }

    [Fact]
    public void Sidecar_delivers_by_official_adapter_and_reuses_topic()
    {
        var box = iso.Box();
        var info = box.Post("claude", "antigravity", "Context only", "native-delivery", info: true);
        var first = box.Post("claude", "antigravity", "Technical fixture", "native-delivery");
        string calls = Path.Join(iso.Root, "calls.jsonl");
        const string conversation = "12345678-1234-1234-1234-123456789abc";
        string folder = Path.Join(iso.Gemini, "antigravity", "conversations");
        Directory.CreateDirectory(folder);
        File.WriteAllText(Path.Join(folder, conversation + ".db"), "");
        string script = Path.Join(iso.Root, "fake_agentapi.ps1");
        File.WriteAllText(script, """
            $line = ConvertTo-Json -Compress -InputObject @($args)
            [IO.File]::AppendAllText($env:TEST_CALLS, $line + "`n", [Text.UTF8Encoding]::new($false))

            """ + "'" + new JsonObject { ["response"] = new JsonObject { ["newConversation"] = new JsonObject { ["conversationId"] = conversation } } }.ToJsonString() + "'\n", new UTF8Encoding(true));
        var start = Native.Start(Native.Exe, ["--sidecar"], env: new Dictionary<string, string?>
        {
            ["NEXUS_RELAY_AGENTAPI"] = new JsonArray("pwsh", "-NoProfile", "-NonInteractive", "-File", script).ToJsonString(),
            ["TEST_CALLS"] = calls,
        });
        using var sidecar = Process.Start(start)!;
        try
        {
            Wait.Until(() => box.HasEvent(first.Id, "livré"), 20, "first delivery");
            var second = box.Post("claude", "antigravity", "Next fixture", "native-delivery");
            Wait.Until(() => box.HasEvent(second.Id, "livré"), 20, "second delivery");
            var requests = File.ReadAllLines(calls).Where(l => l.Length > 0).Select(l => JsonNode.Parse(l)!.AsArray().Select(n => n!.ToString()).ToList()).ToList();
            Assert.Equal(["new-conversation", "send-message"], requests.Select(r => r[0]));
            Assert.Contains(conversation, requests[1]);
            Assert.Contains(first.Id, requests[0][^1]);
            Assert.Contains(second.Id, requests[1][^1]);
            Assert.Contains($"Pour info (sans réponse) : read(id=\"{info.Id}\")", requests[0][^1]);
            Assert.True(box.HasEvent(info.Id, "livré"));
            Assert.Equal(conversation, box.TopicState("native-delivery", "agy")?["conversation"]?.ToString());
        }
        finally
        {
            if (!sidecar.HasExited) sidecar.Kill(true);
            sidecar.WaitForExit(10_000);
        }
    }

    /// <summary>Minimal Chrome debugger: /json/list, then a WebSocket answering Runtime.evaluate.</summary>
    private sealed class FakeDebugger : IDisposable
    {
        private readonly TcpListener listener = new(IPAddress.Loopback, 0);
        private readonly CancellationTokenSource stop = new();
        public int Port { get; }
        public FakeDebugger()
        {
            listener.Start();
            Port = ((IPEndPoint)listener.LocalEndpoint).Port;
            _ = Task.Run(async () =>
            {
                while (!stop.IsCancellationRequested)
                {
                    TcpClient client;
                    try { client = await listener.AcceptTcpClientAsync(stop.Token); } catch (Exception e) when (e is OperationCanceledException or ObjectDisposedException or SocketException) { return; }
                    _ = Task.Run(() => { using (client) { try { Serve(client); } catch (Exception e) when (e is IOException or SocketException or ObjectDisposedException or EndOfStreamException) { /* The test closed the connection. */ } } });
                }
            });
        }

        private static string ReadHeaders(Stream stream)
        {
            var bytes = new List<byte>();
            while (bytes.Count < 4 || !(bytes[^4] == 13 && bytes[^3] == 10 && bytes[^2] == 13 && bytes[^1] == 10))
            {
                int b = stream.ReadByte();
                if (b < 0) throw new EndOfStreamException();
                bytes.Add((byte)b);
            }
            return Encoding.ASCII.GetString(bytes.ToArray());
        }

        private static byte[] Exact(Stream stream, int size) { var buffer = new byte[size]; stream.ReadExactly(buffer); return buffer; }

        private void Serve(TcpClient client)
        {
            var stream = client.GetStream();
            while (true)
            {
                string headers = ReadHeaders(stream);
                string path = headers.Split(' ')[1];
                if (path == "/json/list")
                {
                    byte[] body = Encoding.UTF8.GetBytes(new JsonArray(new JsonObject { ["type"] = "page", ["url"] = "app://-/index.html", ["webSocketDebuggerUrl"] = $"ws://127.0.0.1:{Port}/debugger" }).ToJsonString());
                    stream.Write(Encoding.ASCII.GetBytes($"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {body.Length}\r\n\r\n"));
                    stream.Write(body);
                    continue;
                }
                string key = headers.Split("\r\n").First(l => l.StartsWith("Sec-WebSocket-Key:", StringComparison.OrdinalIgnoreCase))[18..].Trim();
                string accept = Convert.ToBase64String(SHA1.HashData(Encoding.ASCII.GetBytes(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")));
                stream.Write(Encoding.ASCII.GetBytes($"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"));
                while (true)
                {
                    var header = Exact(stream, 2);
                    if ((header[0] & 15) == 8) return;
                    long size = header[1] & 127;
                    if (size == 126) { var s = Exact(stream, 2); size = s[0] << 8 | s[1]; }
                    else if (size == 127) { var s = Exact(stream, 8); size = 0; foreach (byte b in s) size = size << 8 | b; }
                    var mask = (header[1] & 128) != 0 ? Exact(stream, 4) : new byte[4];
                    var raw = Exact(stream, (int)size);
                    for (int i = 0; i < raw.Length; i++) raw[i] ^= mask[i % 4];
                    var request = JsonNode.Parse(raw)!;
                    string expression = request["params"]?["expression"]?.ToString() ?? "";
                    JsonNode? value = expression.StartsWith("[...document.querySelectorAll('button,[role=button],[role=menuitem]", StringComparison.Ordinal)
                        ? new JsonArray(new JsonObject { ["role"] = "button", ["label"] = "Nouveau chat" }, new JsonObject { ["role"] = "button", ["label"] = "Démarrer un nouveau chat dans Nexus Relay" })
                        : expression.Contains("return e?e.innerText.trim():null", StringComparison.Ordinal) ? JsonValue.Create("Codex") : JsonValue.Create(true);
                    byte[] data = Encoding.UTF8.GetBytes(new JsonObject { ["id"] = request["id"]?.DeepClone(), ["result"] = new JsonObject { ["result"] = new JsonObject { ["value"] = value } } }.ToJsonString());
                    var frame = new List<byte> { 0x81 };
                    if (data.Length < 126) frame.Add((byte)data.Length); else { frame.Add(126); frame.Add((byte)(data.Length >> 8)); frame.Add((byte)data.Length); }
                    frame.AddRange(data);
                    stream.Write(frame.ToArray());
                }
            }
        }

        public void Dispose() { stop.Cancel(); listener.Stop(); }
    }
}
