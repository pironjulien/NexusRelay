using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;

namespace NexusRelay;

// Same Windows byte-range token as NexusWindows/Python: offset 2^32, released by Windows on process death.
public sealed class ForegroundToken : IDisposable
{
    private readonly FileStream stream;
    private ForegroundToken(FileStream stream) { this.stream = stream; }
    public static ForegroundToken? Acquire(string purpose, double seconds = 3)
    {
        string path = Path.Join(RelayPaths.UserHome(), ".nexus", "foreground.lock"); Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var stream = new FileStream(path, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.ReadWrite | FileShare.Delete);
        var timer = Stopwatch.StartNew();
        while (true)
        {
            try
            {
                stream.Lock(1L << 32, 1);
                stream.Position = 0; stream.SetLength(0);
                stream.Write(System.Text.Encoding.UTF8.GetBytes(new JsonObject { ["owner"] = "NexusRelay", ["pid"] = Environment.ProcessId, ["since"] = Reservations.Now(), ["purpose"] = purpose }.ToJsonString())); stream.Flush();
                return new(stream);
            }
            catch (IOException) { if (timer.Elapsed.TotalSeconds >= seconds || AppTransport.EmergencyStop()) { stream.Dispose(); return null; } Thread.Sleep(50); }
        }
    }
    public void Dispose() { using (stream) { stream.Position = 0; stream.SetLength(0); stream.Flush(); stream.Unlock(1L << 32, 1); } }
}

public static partial class AppLifecycle
{
    // Processes found for an app, disposed together.
    private sealed class ProcessList : List<Process>, IDisposable
    {
        public void Dispose() { foreach (var process in this) process.Dispose(); }
    }
    public static bool Busy(string app)
        => Activity(app)["status"]?.ToString() != "idle";
    public static JsonObject Activity(string app)
    {
        try
        {
            if (app == "codex")
            {
                if (!File.Exists(Path.Join(AppSessions.CodexHome, "state_5.sqlite"))) return AppTransport.Result("unknown", "Codex activity database unavailable");
                foreach (var chat in AppSessions.CodexChats().Where(c => !c.Archived))
                {
                    if (!File.Exists(chat.Path)) return AppTransport.Result("unknown", "An open Codex chat has no readable turn journal");
                    var turn = AppSessions.CodexTurn(chat.Path);
                    if (!turn.Finished) return AppTransport.Result(turn.At is null ? "unknown" : "working", "An open Codex turn has no durable completion; its age does not establish idleness");
                }
                return AppTransport.Result("idle", "All open Codex turns have durable completion records");
            }
            string conversations = Path.Join(RelayPaths.GeminiHome(), "antigravity", "conversations");
            if (!Directory.Exists(conversations)) return AppTransport.Result("unknown", "Antigravity conversation inventory unavailable");
            foreach (string id in Directory.EnumerateFiles(conversations, "*.db").Select(file => Path.GetFileNameWithoutExtension(file)).Where(id => RelayPaths.IsConversationId(id) && RelayPaths.ConversationAlive(id)))
            {
                var turn = AppSessions.AntigravityError(id) ?? AppSessions.AntigravityTurn(id);
                if (!turn.Finished) return AppTransport.Result(turn.At is null ? "unknown" : "working", "An Antigravity conversation has no durable completion; old timestamps do not establish idleness");
            }
            return AppTransport.Result("idle", "All live Antigravity conversations have durable completion records");
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or RelayError) { return AppTransport.Result("unknown", e.Message); }
    }
    public static double HungFor(string key, bool observed, RelayBox? box = null, double? now = null)
    {
        box ??= new(); double at = now ?? Reservations.Now();
        if (!observed)
        {
            if (box.State("hung-" + key) is not null) box.SetState("hung-" + key, new());
            return 0;
        }
        double since = box.State("hung-" + key)?["since"]?.GetValue<double>() ?? at;
        box.SetState("hung-" + key, new() { ["since"] = since }); return at - since;
    }
    public static bool InstalledExecutable(string app, string path)
    {
        string full = Path.GetFullPath(path);
        if (app == "codex") return full.StartsWith(Path.Join(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "WindowsApps", "OpenAI.Codex_"), StringComparison.OrdinalIgnoreCase) && full.EndsWith(@"\app\ChatGPT.exe", StringComparison.OrdinalIgnoreCase);
        return new[] { Path.Join(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "antigravity", "Antigravity.exe"), Path.Join(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Antigravity", "Antigravity.exe") }.Any(expected => full.Equals(expected, StringComparison.OrdinalIgnoreCase));
    }
    private static ProcessList Processes(string app)
    {
        var result = new ProcessList();
        foreach (var process in Process.GetProcessesByName(app == "codex" ? "ChatGPT" : "Antigravity"))
        {
            try { if (process.MainModule?.FileName is string path && InstalledExecutable(app, path)) { result.Add(process); continue; } }
            catch (Exception e) when (e is System.ComponentModel.Win32Exception or InvalidOperationException) { /* Inaccessible or exited process: not the installed app. */ }
            process.Dispose();
        }
        return result;
    }
    private static bool Running(string app) { using var processes = Processes(app); return processes.Count > 0; }
    public static bool HungWindow(string app)
    {
        using var processes = Processes(app); var ids = processes.Select(p => p.Id).ToHashSet(); bool hung = false;
        EnumWindows((hwnd, _) => { GetWindowThreadProcessId(hwnd, out uint pid); if (ids.Contains((int)pid) && IsWindowVisible(hwnd) && IsHungAppWindow(hwnd)) { hung = true; } return true; }, 0);
        return hung;
    }
    private static bool Responsive(string url) { try { using var page = new CdpPage(url); return page.Eval("1 + 1")?.ToString() == "2"; } catch (Exception e) when (e is IOException or RelayError or System.Net.WebSockets.WebSocketException or OperationCanceledException) { return false; } }
    public static string? RestartGuard(string app, bool frozen, bool present, bool busy, bool token)
        => present ? "Julien is using the app; restart postponed" : busy ? "An agent is working or activity is unknown; restart postponed" : !token ? "Foreground held by another agent; restart postponed" : null;
    public static string? PendingLaunch(JsonObject? state, bool pageReady)
        => !pageReady && state?["status"]?.ToString() is "submitting" or "uncertain" ? "Previous app launch is uncertain; no stop or duplicate submitted" : null;
    public static JsonObject Ensure(string app, bool forceFrozen = false)
    {
        if (AppTransport.EmergencyStop()) return AppTransport.Result("deferred", "Emergency stop active");
        bool running = Running(app), frozen = HungFor(app, running && HungWindow(app)) >= 120;
        string? url = AppTransport.PageUrl(app);
        if (url is not null && !frozen)
        {
            bool responsive = Responsive(url);
            if (responsive) { HungFor(app + "-page", false); new RelayBox().SetState("launch-" + app, new() { ["status"] = "ready", ["at"] = RelayBox.NowIso() }); return AppTransport.Result("ready"); }
            if (HungFor(app + "-page", true) < 120) return AppTransport.Result("deferred", "App page not responsive yet");
            frozen = true;
        }
        if (PendingLaunch(new RelayBox().State("launch-" + app), false) is string previousLaunch) return AppTransport.Result("deferred", previousLaunch);
        if (RestartGuard(app, frozen, AppTransport.UserPresent(app), running && Busy(app), true) is string reason) return AppTransport.Result("deferred", reason);
        string? exe = AppTransport.Executable(app);
        var package = app == "codex" ? CodexPackage() : null;
        if (exe is null && package is null) return AppTransport.Result("failed", $"{app} installation not found");
        using var token = ForegroundToken.Acquire("restart " + app + " with its debugging port");
        if (RestartGuard(app, frozen, AppTransport.UserPresent(app), Running(app) && Busy(app), token is not null) is string guard) return AppTransport.Result("deferred", guard);
        if (PendingLaunch(new RelayBox().State("launch-" + app), false) is string afterWait) return AppTransport.Result("deferred", afterWait);
        if ((url = AppTransport.PageUrl(app)) is not null && !frozen && Responsive(url)) return AppTransport.Result("ready");
        bool restarted = Running(app);
        if (restarted)
        {
            using (var processes = Processes(app))
            {
                var identities = processes.Select(p => (Process: p, Born: p.StartTime.ToUniversalTime().Ticks, Path: p.MainModule!.FileName)).ToList();
                var ids = identities.Select(i => i.Process.Id).ToHashSet(); var windows = new List<nint>();
                EnumWindows((hwnd, _) => { GetWindowThreadProcessId(hwnd, out uint pid); if (ids.Contains((int)pid) && IsWindowVisible(hwnd) && GetWindow(hwnd, 4) == 0) { windows.Add(hwnd); } return true; }, 0);
                foreach (nint window in windows)
                {
                    if (RestartGuard(app, frozen, AppTransport.UserPresent(app) || AppTransport.EmergencyStop(), Busy(app), true) is string changed) return AppTransport.Result("deferred", changed);
                    GetWindowThreadProcessId(window, out uint pid);
                    var identity = identities.FirstOrDefault(i => i.Process.Id == (int)pid);
                    if (identity.Process is null || identity.Process.HasExited || identity.Process.StartTime.ToUniversalTime().Ticks != identity.Born || identity.Process.MainModule?.FileName != identity.Path) continue;
                    PostMessageW(window, 0x0010, 0, 0); // WM_CLOSE: normal application shutdown; no dialog is accepted.
                }
                if (!AppTransport.Wait(() => !Running(app), 15))
                {
                    if (!forceFrozen || !frozen) return AppTransport.Result("deferred", "Normal close was not confirmed; possible user dialog is preserved. A missing port never authorizes forced termination");
                    foreach (var item in identities)
                    {
                        if (RestartGuard(app, frozen, AppTransport.UserPresent(app) || AppTransport.EmergencyStop(), Busy(app), true) is string changed) return AppTransport.Result("deferred", changed);
                        try { if (!item.Process.HasExited && item.Process.StartTime.ToUniversalTime().Ticks == item.Born && item.Process.MainModule?.FileName == item.Path && InstalledExecutable(app, item.Path)) item.Process.Kill(); }
                        catch (InvalidOperationException) { /* Exited meanwhile: nothing left to stop. */ }
                    }
                }
            }
            Thread.Sleep(2000);
        }
        if (RestartGuard(app, frozen, AppTransport.UserPresent(app) || AppTransport.EmergencyStop(), Running(app) && Busy(app), true) is string beforeLaunch) return AppTransport.Result("deferred", beforeLaunch);
        HungFor(app, false); HungFor(app + "-page", false);
        nint previous = GetForegroundWindow(); uint inputBefore = AppTransport.InputTick();
        var launchState = new RelayBox(); bool entered = false;
        void BeforeLaunch() { launchState.SetState("launch-" + app, new() { ["status"] = "submitting", ["at"] = RelayBox.NowIso() }); entered = true; }
        try
        {
            if (app == "codex" && package is not null)
            {
                var activation = (IAppActivation)new AppActivation();
                try { BeforeLaunch(); int hr = activation.ActivateApplication(package + "!App", $"--remote-debugging-port={AppTransport.Port(app)} --remote-debugging-address=127.0.0.1", 0, out _); Marshal.ThrowExceptionForHR(hr); }
                finally { Marshal.ReleaseComObject(activation); }
            }
            else DetachedProcess.Start(exe!, [$"--remote-debugging-port={AppTransport.Port(app)}", "--remote-debugging-address=127.0.0.1"], BeforeLaunch);
            launchState.SetState("launch-" + app, new() { ["status"] = "submitted", ["at"] = RelayBox.NowIso() });
        }
        catch (Exception e) when (entered) { launchState.SetState("launch-" + app, new() { ["status"] = "uncertain", ["at"] = RelayBox.NowIso(), ["reason"] = e.Message }); return AppTransport.Result("deferred", "App launch outcome uncertain; no automatic duplicate"); }
        // Restore focus only when activation took it. Never take it from another app opened by Julien meanwhile.
        var visible = Stopwatch.StartNew();
        while (visible.Elapsed.TotalSeconds < 15)
        {
            if (AppTransport.InputTick() != inputBefore || AppTransport.EmergencyStop()) break;
            var windows = new List<nint>();
            using (var processes = Processes(app))
            {
                var ids = processes.Select(p => p.Id).ToHashSet();
                EnumWindows((hwnd, _) => { GetWindowThreadProcessId(hwnd, out uint pid); if (ids.Contains((int)pid) && IsWindowVisible(hwnd)) { windows.Add(hwnd); } return true; }, 0);
            }
            if (windows.Count > 0)
            {
                nint taken = GetForegroundWindow();
                foreach (nint hwnd in windows)
                {
                    if (AppTransport.InputTick() != inputBefore || AppTransport.EmergencyStop()) break;
                    ShowWindowAsync(hwnd, 7);
                }
                if (AppTransport.InputTick() == inputBefore && !AppTransport.EmergencyStop() && windows.Contains(taken) && previous != 0) SetForegroundWindow(previous);
                break;
            }
            Thread.Sleep(100);
        }
        bool ready = AppTransport.Wait(() => AppTransport.PageUrl(app) is not null, 40);
        return new() { ["status"] = ready ? "ready" : "failed", ["reason"] = ready ? null : $"{app} port did not become available", ["launched"] = true, ["restarted"] = restarted, ["frozen"] = frozen };
    }
    public static JsonObject? MaintainAntigravity(RelayBox box)
    {
        double now = Reservations.Now(); var state = box.State("agy-port-maintenance") ?? new();
        if (now - (state["checked"]?.GetValue<double>() ?? 0) < 60) return null;
        state["checked"] = now; box.SetState("agy-port-maintenance", state);
        if (!Running("antigravity")) return null;
        bool noPort = AppTransport.PageUrl("antigravity") is null;
        bool frozen = HungFor("antigravity", HungWindow("antigravity")) >= 120;
        if (!frozen && HungFor("agy-port", noPort) < 300) return null;
        if (now - (state["restarted"]?.GetValue<double>() ?? 0) < 3600) return AppTransport.Result("deferred", "Antigravity restarted less than an hour ago");
        var result = Ensure("antigravity");
        if (AppTransport.Truth(result["launched"])) { state["restarted"] = now; HungFor("agy-port", false); }
        state["last"] = result.DeepClone(); box.SetState("agy-port-maintenance", state); return result;
    }
    private static string? CodexPackage()
    {
        var start = new ProcessStartInfo("powershell.exe") { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
        foreach (string arg in new[] { "-NoProfile", "-NonInteractive", "-Command", "(Get-AppxPackage OpenAI.Codex | Select-Object -First 1).PackageFamilyName" }) start.ArgumentList.Add(arg);
        using var process = Process.Start(start); if (process is null) return null;
        var result = process.StandardOutput.ReadToEndAsync(); _ = process.StandardError.ReadToEndAsync(); // Drained so a full stderr pipe cannot block the child.
        if (!process.WaitForExit(10_000)) { process.Kill(entireProcessTree: true); return null; }
        string family = result.GetAwaiter().GetResult().Trim(); return family.StartsWith("OpenAI.Codex_", StringComparison.Ordinal) ? family : null;
    }
    [ComImport, Guid("2E941141-7F97-4756-BA1D-9DECDE894A3D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)] private interface IAppActivation { [PreserveSig] int ActivateApplication([MarshalAs(UnmanagedType.LPWStr)] string id, [MarshalAs(UnmanagedType.LPWStr)] string args, uint options, out uint pid); }
    [ComImport, Guid("45BA127D-10A8-46EA-8AB7-56EA9078943C")] private class AppActivation { }
    private delegate bool EnumWindow(nint hwnd, nint parameter);
    // Kept as DllImport: LibraryImport cannot marshal the managed callback delegate.
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindow callback, nint parameter);
    [LibraryImport("user32.dll")] private static partial uint GetWindowThreadProcessId(nint hwnd, out uint pid);
    [LibraryImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static partial bool IsWindowVisible(nint hwnd);
    [LibraryImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static partial bool IsHungAppWindow(nint hwnd);
    [LibraryImport("user32.dll")] private static partial nint GetForegroundWindow();
    [LibraryImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static partial bool ShowWindowAsync(nint hwnd, int command);
    [LibraryImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static partial bool SetForegroundWindow(nint hwnd);
    [LibraryImport("user32.dll")] private static partial nint GetWindow(nint hwnd, uint command);
    [LibraryImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static partial bool PostMessageW(nint hwnd, uint message, nint wParam, nint lParam);
}
