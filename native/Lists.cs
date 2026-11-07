namespace NexusRelay;

/// <summary>Fixed name lists, allocated once rather than at every call (CA1861).</summary>
internal static class Lists
{
    internal static readonly string[] CodexSessionFolders = ["sessions", "archived_sessions"];
    internal static readonly string[] RateLimitWindows = ["primary", "secondary"];
    internal static readonly string[] CodexThreadVariables = ["CODEX_SESSION_ID", "CODEX_THREAD_ID"];
    internal static readonly string[] AnswerMarks = ["state", "quality"];
    internal static readonly string[] MessageExtras = ["round", "bad_citations", "stale"];
    internal static readonly string[] AttachmentFields = ["name", "mime", "size", "path"];
    internal static readonly string[] Peers = ["codex", "antigravity"];
    internal static readonly string[] AgentApiNames = ["agentapi.exe", "agentapi.bat", "agentapi.cmd"];
}
