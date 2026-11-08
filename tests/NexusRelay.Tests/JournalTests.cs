using System.Text.Json.Nodes;

namespace NexusRelay.Tests;

/// <summary>
/// Common journal (NexusShared/journal/FORMAT.md v1): every relay action writes one line, so that the journal can no
/// longer go silent (it did from 30/10 to 08/11 after the move to the native engine). Never the text of a message.
/// </summary>
public sealed class JournalTests : IDisposable
{
    private readonly Isolation iso = new();
    public void Dispose() => iso.Dispose();

    private static JsonObject Tool(string name, JsonObject args, string agent) => McpServer.CallToolPayload(name, args, agent);

    private List<JsonObject> Lines(string agent)
    {
        string file = Path.Join(iso.Root, "journal", "relay", agent, $"{DateTime.UtcNow:yyyy-MM}.jsonl");
        return File.Exists(file) ? File.ReadAllLines(file).Select(l => (JsonObject)JsonNode.Parse(l)!).ToList() : [];
    }

    private static void AssertFormat(JsonObject line, string agent, string action, string issue)
    {
        Assert.Equal(1, line["v"]!.GetValue<int>());
        Assert.Equal(agent, line["agent"]!.ToString());
        Assert.Equal(RelayPaths.Version, line["outil"]!.ToString());
        Assert.Equal(action, line["action"]!.ToString());
        Assert.Equal(issue, line["issue"]!.ToString());
        Assert.True(line.ContainsKey("ms") && line.ContainsKey("erreur") && line.ContainsKey("appel") && line.ContainsKey("app"));
        Assert.True(line["ms"] is null || line["ms"]!.GetValue<int>() >= 0);
        Assert.True(DateTimeOffset.TryParse(line["ts"]!.ToString(), out _));
    }

    [Fact]
    public void Send_read_reply_wait_and_errors_each_write_one_common_line_without_message_text()
    {
        const string secret = "Texte secret du message";
        var sent = Tool("send", new() { ["to"] = "codex", ["text"] = secret, ["topic"] = "journal-test" }, "claude");
        string id = sent["id"]!.ToString();
        Tool("read", new() { ["id"] = id }, "codex");
        Tool("reply", new() { ["id"] = id, ["text"] = secret + " (réponse)" }, "codex");
        Tool("wait_reply", new() { ["id"] = id, ["timeout_s"] = 1 }, "claude");
        Tool("read", new() { ["id"] = "20261108T000000000-abcdef" }, "claude");

        var claude = Lines("claude"); var codex = Lines("codex");
        Assert.Equal(["send", "wait_reply", "read"], claude.Select(l => l["action"]!.ToString()));
        Assert.Equal(["read", "reply"], codex.Select(l => l["action"]!.ToString()));
        AssertFormat(claude[0], "claude", "send", "✓");
        Assert.Equal("codex", claude[0]["app"]!.ToString());
        Assert.Equal(id, claude[0]["appel"]!.ToString());
        AssertFormat(claude[1], "claude", "wait_reply", "✓");
        AssertFormat(claude[2], "claude", "read", "✗");
        Assert.Equal("message 20261108T000000000-abcdef introuvable", claude[2]["erreur"]!.ToString());
        AssertFormat(codex[1], "codex", "reply", "✓");
        Assert.Equal("claude", codex[1]["app"]!.ToString());
        Assert.Equal(id, codex[1]["appel"]!.ToString());
        Assert.DoesNotContain(claude.Concat(codex), l => l.ToJsonString().Contains("secret", StringComparison.OrdinalIgnoreCase));
    }

    [Fact]
    public void Wait_timeout_is_unknown_and_failed_wake_is_a_failure_with_its_short_cause()
    {
        var sent = Tool("send", new() { ["to"] = "antigravity", ["text"] = "Sans réponse", ["topic"] = "journal-test" }, "claude");
        Tool("wait_reply", new() { ["id"] = sent["id"]!.ToString(), ["timeout_s"] = 1 }, "claude");
        var wait = Lines("claude").Last();
        AssertFormat(wait, "claude", "wait_reply", "~");
        Assert.Equal("délai dépassé", wait["erreur"]!.ToString());

        iso.Set("NEXUS_RELAY_HOST", "claude");
        var box = new RelayBox();
        using var runtime = new RelayRuntime(box, (_, _, _) => AppTransport.Result("failed", "Codex ne répond pas (appli figée)"), () => { });
        var request = box.Post("claude", "codex", "Réveille-toi", "journal-wake");
        runtime.Recover(); runtime.Step("codex");
        var wake = Lines("claude").Last();
        AssertFormat(wake, "claude", "wake", "✗");
        Assert.Equal("codex", wake["app"]!.ToString());
        Assert.Equal(request.Id, wake["appel"]!.ToString());
        Assert.Equal("appli figée", wake["erreur"]!.ToString());
    }

    [Theory]
    [InlineData("Quota épuisé à 100 % used", "quota")]
    [InlineData("Request timed out after 30 s", "délai dépassé")]
    [InlineData("Erreur inattendue : « texte du pair »", "Erreur inattendue")]
    public void Causes_are_short_and_never_carry_screen_or_message_text(string reason, string expected)
        => Assert.Equal(expected, Journal.Cause(reason));
}
