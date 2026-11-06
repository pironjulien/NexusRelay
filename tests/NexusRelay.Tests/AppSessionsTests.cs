using System.Text;
using System.Text.Json.Nodes;

namespace NexusRelay.Tests;

/// <summary>Turn detection on journals whose last turn or step is bigger than the read window (inlined images, 06/11).</summary>
public sealed class AppSessionsTests : IDisposable
{
    private readonly string dir = Directory.CreateTempSubdirectory("nexusrelay-sessions-").FullName;
    public void Dispose() => Directory.Delete(dir, true);

    private static string Line(string at, string type, string? padding = null)
    {
        var payload = new JsonObject { ["type"] = type };
        if (padding is not null) payload["output"] = padding;
        return new JsonObject { ["timestamp"] = at, ["type"] = "event_msg", ["payload"] = payload }.ToJsonString() + "\n";
    }
    private static double Epoch(string iso) => AppSessions.Epoch(iso)!.Value;

    private string Rollout(params string[] lines)
    {
        string path = Path.Join(dir, $"rollout-{Guid.NewGuid():N}.jsonl");
        using var writer = new StreamWriter(path, false, new UTF8Encoding(false));
        foreach (string line in lines) writer.Write(line);
        return path;
    }

    private static readonly string Huge = new('A', AppSessions.CodexTailCap + 1024 * 1024);

    [Fact]
    public void Codex_turn_longer_than_the_tail_cap_is_finished_after_task_complete()
    {
        string path = Rollout(
            Line("2026-11-06T10:00:00.000Z", "task_started"),
            Line("2026-11-06T10:00:01.000Z", "function_call_output", Huge),
            Line("2026-11-06T10:00:02.000Z", "agent_message"),
            Line("2026-11-06T10:00:03.000Z", "task_complete"));
        Assert.True(new FileInfo(path).Length > AppSessions.CodexTailCap);
        var turn = AppSessions.CodexTurn(path);
        Assert.True(turn.Finished, "A final task_complete ends the turn even when its task_started lies beyond the cap");
        Assert.Equal(Epoch("2026-11-06T10:00:03.000Z"), turn.At);
        Assert.Equal(Epoch("2026-11-06T10:00:03.000Z"), turn.Last);
    }

    [Fact]
    public void Codex_turn_longer_than_the_tail_cap_without_completion_is_still_working()
    {
        string path = Rollout(
            Line("2026-11-06T10:00:00.000Z", "task_started"),
            Line("2026-11-06T10:00:01.000Z", "function_call_output", Huge),
            Line("2026-11-06T10:00:02.000Z", "agent_message"));
        var turn = AppSessions.CodexTurn(path);
        Assert.False(turn.Finished);
        Assert.Equal(Epoch("2026-11-06T10:00:02.000Z"), turn.Last);
    }

    [Fact]
    public void Codex_capped_window_keeps_the_since_semantics()
    {
        string path = Rollout(
            Line("2026-11-06T10:00:00.000Z", "task_started"),
            Line("2026-11-06T10:00:01.000Z", "function_call_output", Huge),
            Line("2026-11-06T10:00:02.000Z", "turn_aborted"));
        // Woken just before the turn: the whole capped window is newer, so it is that turn and it ended.
        var woken = AppSessions.CodexTurn(path, Epoch("2026-11-06T09:59:58.000Z"));
        Assert.True(woken.Finished);
        Assert.Equal(Epoch("2026-11-06T10:00:02.000Z"), woken.At);
        // Woken after that turn: what the window shows predates the wake, the woken turn has not started.
        var later = AppSessions.CodexTurn(path, Epoch("2026-11-06T10:00:05.000Z"));
        Assert.False(later.Finished);
        Assert.Null(later.At);
        Assert.Null(later.Last);
    }

    [Fact]
    public void Last_line_is_read_whole_beyond_any_window()
    {
        string step = new JsonObject { ["type"] = "PLANNER_RESPONSE", ["status"] = "DONE", ["text"] = new string('B', 9 * 1024 * 1024) }.ToJsonString();
        string path = Rollout("{\"type\":\"USER_INPUT\"}\n", step + "\n");
        Assert.Equal(step, AppSessions.LastLine(path));
        string writing = Rollout("{\"type\":\"USER_INPUT\"}\n", "{\"type\":\"PLANNER");
        Assert.Null(AppSessions.LastLine(writing));
        Assert.Equal("{\"a\":1}", AppSessions.LastLine(Rollout("{\"a\":1}\n")));
        Assert.Null(AppSessions.LastLine(Rollout()));
    }
}
