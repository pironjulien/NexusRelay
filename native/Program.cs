using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;

namespace NexusRelay;

public static class Program
{
    public static async Task<int> Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);

        if (args.Length >= 1 && args[0] == "--settings") return RelaySettings.RunCli(args);
        if (args.Length >= 1 && args[0] is "--register-hooks" or "--register-clients") return Registration.RunCli(args);
        if (args.Contains("--router-status", StringComparer.Ordinal)) { Console.WriteLine(CodexRouter.Inspect().ToJsonString()); return 0; }
        if (args.Contains("--schemas", StringComparer.Ordinal)) { Console.WriteLine(McpServer.BuildToolsList().ToJsonString()); return 0; }
        if (args.Length == 1 && args[0] == "--ui-contracts") { Console.WriteLine(NativeCards.Contracts().ToJsonString()); return 0; }
        if (args.Length == 3 && args[0] == "--card-index") { Console.WriteLine(NativeCards.CodexIndex(args[1], args[2]).ToJsonString()); return 0; }
        if (args.Contains("--quota-status", StringComparer.Ordinal)) { Console.WriteLine(new JsonObject { ["usage"] = AppSessions.AntigravityUsage(new()), ["blocked_reason"] = AppSessions.AntigravityQuota(new()) }.ToJsonString()); return 0; }
        if (args.Length == 1 && args[0] == "--register-known-relay-chats") { Console.WriteLine(CodexSpace.RegisterHistory(new()).ToJsonString()); return 0; }
        if (args.Length == 1 && args[0] == "--activity-status") { Console.WriteLine(new JsonObject { ["codex"] = AppLifecycle.Activity("codex"), ["antigravity"] = AppLifecycle.Activity("antigravity") }.ToJsonString()); return 0; }
        if (args.Length is 2 or 3 && args[0] == "--recover-app") { string app = RelayPaths.NormalizeAgent(args[1]); if (app is not ("codex" or "antigravity") || args.Length == 3 && args[2] != "--force-frozen") return 2; Console.WriteLine(AppLifecycle.Ensure(app, args.Length == 3).ToJsonString()); return 0; }

        if (args.Contains("--edit-guard", StringComparer.Ordinal))
        {
            int idx = Array.IndexOf(args, "--edit-guard");
            var guardArgs = args.Skip(idx + 1).ToList();
            return EditGuard.RunCli(guardArgs);
        }

        if (args.Contains("--daemon", StringComparer.Ordinal))
        {
            return DaemonSupervisor.RunMain();
        }

        if (args.Contains("--sidecar", StringComparer.Ordinal)) return NativeSidecar.Run();
        if (args.Contains("--notice", StringComparer.Ordinal)) return NativeHooks.Notice();
        if (args.Contains("--staged", StringComparer.Ordinal) || args.Contains("--release-committed", StringComparer.Ordinal)) return NativeHooks.Commit(args);

        if (args.Contains("--inspect", StringComparer.Ordinal))
        {
            int idx = Array.IndexOf(args, "--inspect");
            string app = idx + 1 < args.Length ? RelayPaths.NormalizeAgent(args[idx + 1]) : "codex";
            if (app is not ("codex" or "antigravity")) return 2;
            Console.WriteLine(AppAutomation.Inspect(app).ToJsonString()); return 0;
        }

        if (args.Contains("--http", StringComparer.Ordinal))
        {
            int idx = Array.IndexOf(args, "--http");
            int port = 16181;
            if (idx + 1 < args.Length && int.TryParse(args[idx + 1], out int p))
                port = p;
            using var cts = new CancellationTokenSource();
            Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };
            using var fixtureRuntime = args.Contains("--protocol-fixture", StringComparer.Ordinal) ? new RelayRuntime(waker: (_, _, _) => AppTransport.Result("deferred", "isolated protocol fixture; app transport disabled"), tidy: () => { }) : null;
            await McpServer.RunHttpAsync(port, cts.Token, fixtureRuntime);
            return 0;
        }

        // Default or --mcp: stdio JSON-RPC 2.0
        string? agent = null;
        int agentIdx = Array.IndexOf(args, "--agent");
        if (agentIdx >= 0 && agentIdx + 1 < args.Length)
            agent = args[agentIdx + 1];

        using var stdioFixture = args.Contains("--protocol-fixture", StringComparer.Ordinal) ? new RelayRuntime(waker: (_, _, _) => AppTransport.Result("deferred", "isolated protocol fixture; app transport disabled"), tidy: () => { }) : null;
        return McpServer.RunStdio(agent, stdioFixture);
    }

}
