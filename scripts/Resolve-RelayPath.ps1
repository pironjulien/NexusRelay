function Resolve-RelayPath {
    param([Parameter(Mandatory=$true)][string]$Path)
    if (-not ('NexusRelayPhysicalPath' -as [type])) {
        Add-Type @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
public static class NexusRelayPhysicalPath {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern SafeFileHandle CreateFileW(string path, uint access, uint share, IntPtr security, uint mode, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern uint GetFinalPathNameByHandleW(SafeFileHandle file, StringBuilder path, uint size, uint flags);
    public static string Resolve(string path) {
        using (var file = CreateFileW(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {
            if (file.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
            var result = new StringBuilder(32768);
            uint count = GetFinalPathNameByHandleW(file, result, (uint)result.Capacity, 0);
            if (count == 0 || count >= result.Capacity) throw new Win32Exception(Marshal.GetLastWin32Error());
            string value = result.ToString();
            if (value.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) return @"\\" + value.Substring(8);
            return value.StartsWith(@"\\?\", StringComparison.OrdinalIgnoreCase) ? value.Substring(4) : value;
        }
    }
}
'@
    }
    [NexusRelayPhysicalPath]::Resolve([IO.Path]::GetFullPath($Path))
}
