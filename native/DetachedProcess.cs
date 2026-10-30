using System.Diagnostics;
using System.Text;

namespace NexusRelay;

// User apps are launched by Explorer itself. No service-child fallback or parent-process impersonation.
public static class DetachedProcess
{
    public static string QuoteArgument(string value)
    {
        var result = new StringBuilder("\""); int slashes = 0;
        foreach (char c in value)
        {
            if (c == '\\') { slashes++; continue; }
            result.Append('\\', c == '"' ? 2 * slashes + 1 : slashes); result.Append(c); slashes = 0;
        }
        result.Append('\\', 2 * slashes); return result.Append('"').ToString();
    }
    public static void Start(string executable, IEnumerable<string> arguments, Action? entered = null, int show = 7)
        => UserApplication.Start(new ProcessStartInfo(executable) { Arguments = string.Join(" ", arguments.Select(QuoteArgument)), WorkingDirectory = Path.GetDirectoryName(executable) ?? "" }, entered ?? (() => { }), show);
}
