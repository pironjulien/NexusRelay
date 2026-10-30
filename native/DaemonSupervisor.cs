using System.Diagnostics;
using System.Text;
using System.Text.Json.Nodes;

namespace NexusRelay;

public static class DaemonSupervisor
{
    public const double MinUpSeconds = 10.0;
    public const double MaxPauseSeconds = 60.0;
    public const long LogMaxBytes = 5 * 1024 * 1024;

    public static string SpecPath()
        => Path.Combine(RelayPaths.UserHome(), ".nexus", "daemons", "specs", "nexus-relay.json");

    public static string StateDir()
        => Path.Combine(RelayPaths.UserHome(), ".nexus", "nexus-relay");

    public static (string Executable, List<string> Args, Dictionary<string, string> Env) ParseCommand(JsonObject spec)
    {
        var exe = spec["exe"]?.GetValue<string>()
            ?? spec["executable"]?.GetValue<string>()
            ?? spec["python"]?.GetValue<string>()
            ?? throw new RelayError("missing executable/python in spec");
        var port = spec["port"]?.GetValue<int>() ?? throw new RelayError("missing port in spec");

        var args = new List<string>();
        if (spec["args"] is JsonArray arr)
        {
            foreach (var item in arr)
            {
                if (item is not null)
                    args.Add(item.ToString());
            }
        }
        args.Add("--http");
        args.Add(port.ToString());

        var env = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (System.Collections.DictionaryEntry entry in Environment.GetEnvironmentVariables())
        {
            if (entry.Key is string k && entry.Value is string v)
                env[k] = v;
        }
        if (spec["env"] is JsonObject envObj)
        {
            foreach (var kv in envObj)
            {
                if (kv.Value is not null)
                    env[kv.Key] = kv.Value.ToString();
            }
        }
        return (exe, args, env);
    }

    public static FileStream? AcquireLock(string lockPath)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(lockPath)!);
        FileStream? fs = null;
        try
        {
            fs = new FileStream(lockPath, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.ReadWrite);
            fs.Lock(0, 1);
            return fs;
        }
        catch
        {
            fs?.Dispose();
            return null;
        }
    }

    public static void RotateLogIfNeeded(string logPath)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(logPath)!);
        if (File.Exists(logPath))
        {
            try
            {
                var info = new FileInfo(logPath);
                if (info.Length > LogMaxBytes)
                {
                    File.Move(logPath, logPath + ".1", overwrite: true);
                }
            }
            catch
            {
                // Ignore rotation failure
            }
        }
    }

    public static string RequestPath(string specFile)
    {
        try
        {
            if (File.Exists(specFile))
            {
                var spec = JsonNode.Parse(File.ReadAllText(specFile, Encoding.UTF8)) as JsonObject;
                var declared = spec?["restart_request"]?.GetValue<string>();
                if (!string.IsNullOrWhiteSpace(declared))
                    return declared;
            }
        }
        catch
        {
            // Fallback to default
        }
        return Path.Combine(RelayPaths.RelayHome(), "daemon-restart.request");
    }

    public static string? TakeRequest(string? requestPath = null)
    {
        requestPath ??= Path.Combine(RelayPaths.RelayHome(), "daemon-restart.request");
        var taken = requestPath + ".lancement";
        try
        {
            File.Move(requestPath, taken, overwrite: true);
            return taken;
        }
        catch (FileNotFoundException)
        {
            return null;
        }
        catch (DirectoryNotFoundException)
        {
            return null;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"nexus-relay daemon : demande de relance non prise : {ex.Message}");
            return null;
        }
    }

    public static void Settle(string? takenPath, bool launched)
    {
        if (takenPath is null)
            return;
        try
        {
            if (launched)
            {
                if (File.Exists(takenPath))
                    File.Delete(takenPath);
            }
            else
            {
                var dir = Path.GetDirectoryName(takenPath)!;
                RelayPaths.GiveBack(takenPath, Path.Combine(dir, "daemon-restart.request"));
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"nexus-relay daemon : demande de relance non réglée : {ex.Message}");
        }
    }

    public static void Run(
        string? specFile = null,
        Action<double>? sleep = null,
        Func<double>? clock = null,
        int? rounds = null)
    {
        specFile ??= SpecPath();
        sleep ??= s => Thread.Sleep((int)(s * 1000));
        clock ??= () => Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency;

        double pause = 0.0;
        int done = 0;
        while (rounds is null || done < rounds.Value)
        {
            done++;
            var taken = TakeRequest(RequestPath(specFile));
            string exe;
            List<string> args;
            Dictionary<string, string> env;
            try
            {
                var specNode = JsonNode.Parse(File.ReadAllText(specFile, Encoding.UTF8)) as JsonObject
                    ?? throw new RelayError("invalid spec JSON");
                (exe, args, env) = ParseCommand(specNode);
            }
            catch (Exception ex)
            {
                Settle(taken, launched: false);
                Console.Error.WriteLine($"nexus-relay daemon : spec illisible ({specFile}) : {ex.Message}");
                pause = Math.Min(MaxPauseSeconds, Math.Max(2.0, pause * 2.0));
                sleep(pause);
                continue;
            }

            double started = clock();
            var logPath = Path.Combine(StateDir(), "daemon.log");
            RotateLogIfNeeded(logPath);

            int exitCode;
            try
            {
                var psi = new ProcessStartInfo(exe)
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardInput = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                };
                foreach (var a in args)
                    psi.ArgumentList.Add(a);
                foreach (var kv in env)
                    psi.Environment[kv.Key] = kv.Value;

                using var proc = Process.Start(psi) ?? throw new RelayError("failed to start daemon process");
                proc.StandardInput.Close();
                Settle(taken, launched: true);

                using var logStream = new FileStream(logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
                using var logWriter = TextWriter.Synchronized(new StreamWriter(logStream, new UTF8Encoding(false)) { AutoFlush = true });
                proc.OutputDataReceived += (_, e) => { if (e.Data is not null) logWriter.WriteLine(e.Data); };
                proc.ErrorDataReceived += (_, e) => { if (e.Data is not null) logWriter.WriteLine(e.Data); };
                proc.BeginOutputReadLine();
                proc.BeginErrorReadLine();
                proc.WaitForExit();
                exitCode = proc.ExitCode;
            }
            catch
            {
                Settle(taken, launched: false);
                throw;
            }

            double up = clock() - started;
            if (exitCode == 0 && up >= MinUpSeconds)
                pause = 0.0;
            else
                pause = Math.Min(MaxPauseSeconds, Math.Max(2.0, pause * 2.0));
            sleep(pause > 0 ? pause : 0.5);
        }
    }

    public static int RunMain()
    {
        using var held = AcquireLock(Path.Combine(StateDir(), "daemon.lock"));
        if (held is null)
        {
            Console.Error.WriteLine("nexus-relay daemon : déjà lancé");
            return 0;
        }
        Run();
        return 0;
    }
}
