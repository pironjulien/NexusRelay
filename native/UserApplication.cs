using System.Diagnostics;
using System.Runtime.InteropServices;

namespace NexusRelay;

public sealed class UserApplicationUnavailableException(string message, Exception? inner = null) : InvalidOperationException(message, inner);

// Explorer's existing desktop broker owns user apps; Process.Start/Shell.Application are not a job boundary.
// Shared NexusWindows protocol, qualified under KILL_ON_JOB_CLOSE.
// https://devblogs.microsoft.com/oldnewthing/20131118-00/?p=2643
public static unsafe class UserApplication
{
    [DllImport("user32.dll")] private static extern nint GetShellWindow();
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(nint hwnd, out uint pid);
    private static nint* Table(nint value) => *(nint**)value;
    private static void Check(int result) { if (result < 0) Marshal.ThrowExceptionForHR(result); }
    private static void Release(nint value) { if (value != 0) Marshal.Release(value); }
    private static void Release(object? value) { if (value is not null && Marshal.IsComObject(value)) Marshal.ReleaseComObject(value); }
    public static void Start(ProcessStartInfo info, Action started, int show = 0)
    {
        if (string.IsNullOrWhiteSpace(info.FileName) || info.ArgumentList.Count != 0 || info.RedirectStandardInput || info.RedirectStandardOutput || info.RedirectStandardError)
            throw new UserApplicationUnavailableException("Desktop broker requires an executable, argument string and no inherited protocol pipes; nothing launched");
        object application;
        try { application = DesktopApplication(); }
        catch (Exception error) { throw new UserApplicationUnavailableException("Interactive Explorer broker unavailable; nothing launched", error); }
        try { started(); ((dynamic)application).ShellExecute(info.FileName, info.Arguments, info.WorkingDirectory, "open", show); }
        finally { Release(application); }
    }
    private static object DesktopApplication()
    {
        object? windows = null, desktop = null, folder = null;
        nint unknown = 0, provider = 0, browser = 0, view = 0, dispatch = 0;
        try
        {
            var shell = GetShellWindow();
            if (shell == 0 || GetWindowThreadProcessId(shell, out uint shellPid) == 0 || shellPid == 0) throw new IOException("No interactive desktop shell");
            windows = Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("9BA05972-F6A8-11CF-A442-00A0C90A8F39"), true)!) ?? throw new IOException("ShellWindows unavailable");
            object location = 0, unused = Type.Missing;
            desktop = ((dynamic)windows).FindWindowSW(ref location, ref unused, 8, out int desktopHwnd, 1);
            if (desktop is null || GetWindowThreadProcessId((nint)(uint)desktopHwnd, out uint desktopPid) == 0 || desktopPid != shellPid) throw new IOException("Desktop object is not owned by Explorer");
            unknown = Marshal.GetIUnknownForObject(desktop);
            var serviceProvider = new Guid("6D5140C1-7436-11CE-8034-00AA006009FA"); Check(Marshal.QueryInterface(unknown, in serviceProvider, out provider));
            var topBrowser = new Guid("4C96BE40-915C-11CF-99D3-00AA004AE837"); var browserInterface = new Guid("000214E2-0000-0000-C000-000000000046");
            Check(((delegate* unmanaged[Stdcall]<nint, Guid*, Guid*, nint*, int>)Table(provider)[3])(provider, &topBrowser, &browserInterface, &browser));
            Check(((delegate* unmanaged[Stdcall]<nint, nint*, int>)Table(browser)[15])(browser, &view));
            var dispatchInterface = new Guid("00020400-0000-0000-C000-000000000046");
            Check(((delegate* unmanaged[Stdcall]<nint, uint, Guid*, nint*, int>)Table(view)[15])(view, 0, &dispatchInterface, &dispatch));
            folder = Marshal.GetObjectForIUnknown(dispatch);
            return ((dynamic)folder).Application ?? throw new IOException("Desktop Shell dispatch missing");
        }
        finally { Release(folder); Release(dispatch); Release(view); Release(browser); Release(provider); Release(unknown); Release(desktop); Release(windows); }
    }
}
