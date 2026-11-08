using System.Runtime.InteropServices;
using System.Runtime.InteropServices.Marshalling;

namespace NexusRelay;

// Source-generated COM interop ([GeneratedComInterface], ComWrappers): no runtime binder (`dynamic`), no built-in
// runtime callable wrapper ([ComImport]), so the engine stays trimmable and Native AOT compatible.

/// <summary>COM objects created through CoCreateInstance and wrapped by the source-generated ComWrappers.</summary>
internal static partial class ComObjects
{
    public static readonly Guid ApplicationActivationManager = new("45BA127D-10A8-46EA-8AB7-56EA9078943C");
    private static readonly Guid UnknownInterface = new("00000000-0000-0000-C000-000000000046");
    private static readonly StrategyBasedComWrappers Wrappers = new();

    [LibraryImport("ole32.dll")]
    private static partial int CoInitializeEx(nint reserved, uint model);

    [LibraryImport("ole32.dll")]
    private static partial int CoCreateInstance(in Guid classId, nint outer, uint context, in Guid interfaceId, out nint instance);

    /// <summary>The calling thread joins the multithreaded apartment (already initialized threads keep theirs).</summary>
    public static void EnterApartment() => _ = CoInitializeEx(0, 0 /* COINIT_MULTITHREADED */);

    /// <summary>A new instance of the class, as T (a [GeneratedComInterface]).</summary>
    public static T Create<T>(Guid classId, uint context) where T : class
    {
        EnterApartment();
        Marshal.ThrowExceptionForHR(CoCreateInstance(classId, 0, context, UnknownInterface, out var pointer));
        try { return (T)Wrappers.GetOrCreateObjectForComInstance(pointer, CreateObjectFlags.UniqueInstance); }
        finally { Marshal.Release(pointer); }
    }
}

/// <summary>Starts a packaged (Store) application by its AppUserModelID (shobjidl_core.h).</summary>
[GeneratedComInterface]
[Guid("2E941141-7F97-4756-BA1D-9DECDE894A3D")]
internal partial interface IApplicationActivationManager
{
    [PreserveSig]
    int ActivateApplication([MarshalAs(UnmanagedType.LPWStr)] string id, [MarshalAs(UnmanagedType.LPWStr)] string args, uint options, out uint pid);
}

/// <summary>
/// The Task Scheduler 2.0 API (taskschd.dll), as NexusApps' ScheduledTasks: no schtasks.exe, no PowerShell.
/// </summary>
internal static class TaskScheduler
{
    private static readonly Guid TaskSchedulerClass = new("0F87369F-A4E5-4CFC-BD3E-73E6154572DD");

    /// <summary>The root folder (\) of a connected Task Scheduler service.</summary>
    public static ITaskFolder Root()
    {
        var service = ComObjects.Create<ITaskService>(TaskSchedulerClass, 1 /* CLSCTX_INPROC_SERVER */);
        service.Connect(default, default, default, default);
        return service.GetFolder(@"\");
    }
}

/// <summary>
/// A VARIANT passed by value, always VT_EMPTY here (the defaults of Connect, RegisterTask and Run): 24 bytes on 64-bit
/// Windows. Its marshaller passes it as is, so the source generator handles it without disabling runtime marshalling.
/// </summary>
[NativeMarshalling(typeof(EmptyVariantMarshaller))]
[StructLayout(LayoutKind.Sequential)]
internal readonly struct EmptyVariant
{
    private readonly ushort type;
    private readonly ushort reserved1, reserved2, reserved3;
    private readonly nint value1, value2;
}

[CustomMarshaller(typeof(EmptyVariant), MarshalMode.Default, typeof(EmptyVariantMarshaller))]
internal static class EmptyVariantMarshaller
{
    public static EmptyVariant ConvertToUnmanaged(EmptyVariant managed) => managed;

    public static EmptyVariant ConvertToManaged(EmptyVariant unmanaged) => unmanaged;
}

// The three interfaces of taskschd.h used here, in their vtable order. They derive from IDispatch, whose four methods
// are declared as placeholders (never called) so that the slots after them line up.

[GeneratedComInterface]
[Guid("2FABA4C7-4DA9-4013-9697-20CC3FD40F85")]
internal partial interface ITaskService
{
    void DispatchGetTypeInfoCount();
    void DispatchGetTypeInfo();
    void DispatchGetIDsOfNames();
    void DispatchInvoke();
    ITaskFolder GetFolder([MarshalAs(UnmanagedType.BStr)] string path);
    void GetRunningTasks();
    void NewTask();
    void Connect(EmptyVariant serverName, EmptyVariant user, EmptyVariant domain, EmptyVariant password);
}

[GeneratedComInterface]
[Guid("8CFAC062-A080-4C15-9A88-AA7C2AF80DFC")]
internal partial interface ITaskFolder
{
    void DispatchGetTypeInfoCount();
    void DispatchGetTypeInfo();
    void DispatchGetIDsOfNames();
    void DispatchInvoke();
    void GetName();
    void GetPath();
    void GetSubFolder();
    void GetFolders();
    void CreateFolder();
    void DeleteFolder();
    [PreserveSig]
    int GetTask([MarshalAs(UnmanagedType.BStr)] string path, out IRegisteredTask? task);
    void GetTasks();
    [PreserveSig]
    int DeleteTask([MarshalAs(UnmanagedType.BStr)] string name, int flags);
    IRegisteredTask RegisterTask([MarshalAs(UnmanagedType.BStr)] string path, [MarshalAs(UnmanagedType.BStr)] string xml, int flags,
        EmptyVariant userId, EmptyVariant password, int logonType, EmptyVariant sddl);
}

[GeneratedComInterface]
[Guid("9C86F320-DEE3-4DD1-B972-A303F26B061E")]
internal partial interface IRegisteredTask
{
    void DispatchGetTypeInfoCount();
    void DispatchGetTypeInfo();
    void DispatchGetIDsOfNames();
    void DispatchInvoke();
    void GetName();
    void GetPath();
    int GetState();
    short GetEnabled();
    void SetEnabled(short enabled);
    nint Run(EmptyVariant parameters);
    void RunEx();
    void GetInstances();
    void GetLastRunTime();
    void GetLastTaskResult();
    void GetNumberOfMissedRuns();
    void GetNextRunTime();
    void GetDefinition();
    [return: MarshalAs(UnmanagedType.BStr)]
    string GetXml();
    void GetSecurityDescriptor();
    void SetSecurityDescriptor();
    void Stop(int flags);
}
