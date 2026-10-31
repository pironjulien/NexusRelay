using System.Diagnostics;
using System.Text.Json.Nodes;

namespace NexusRelay.Tests;

/// <summary>Reviews checked against their files (citations, outdated state, rounds) and the host woken by its Stop hook.</summary>
public sealed class ReviewTests : IDisposable
{
    private readonly Isolation iso = new();
    private readonly string repo;
    public void Dispose() => iso.Dispose();

    private static JsonObject Tool(string name, JsonObject args, string agent) => McpServer.CallToolPayload(name, args, agent);

    public ReviewTests()
    {
        repo = Path.Join(iso.Root, "depot");
        Directory.CreateDirectory(Path.Join(repo, "src"));
        File.WriteAllLines(Path.Join(repo, "src", "a.cs"), Enumerable.Range(1, 10).Select(i => $"// ligne {i}"));
        Git("init", "-q");
        Git("add", "src/a.cs");
        Git("-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-q", "--no-verify", "-m", "fixture");
    }

    private void Git(params string[] args)
    {
        var start = new ProcessStartInfo("git") { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
        start.ArgumentList.Add("-C"); start.ArgumentList.Add(repo);
        foreach (var a in args) start.ArgumentList.Add(a);
        using var git = Process.Start(start)!;
        git.WaitForExit();
        Assert.True(git.ExitCode == 0, git.StandardError.ReadToEnd());
    }

    private (string Codex, string Agy) Review(string text, JsonObject? more = null)
    {
        var args = new JsonObject { ["to"] = "codex", ["text"] = text, ["topic"] = "revue-test", ["review"] = true, ["repo"] = repo };
        foreach (var (k, v) in more ?? new JsonObject()) args[k] = v?.DeepClone();
        var sent = Tool("send", args, "claude");
        Assert.Equal("success", sent["status"]?.ToString());
        var ids = sent["ids"]!.AsArray().Select(i => i!.ToString()).ToList();
        var to = ids.Select(i => iso.Box().Get(i)!.To).ToList();
        return (ids[to.IndexOf("codex")], ids[to.IndexOf("antigravity")]);
    }

    [Fact]
    public void Citations_to_a_missing_file_or_line_mark_the_review_answer()
    {
        var (codex, agy) = Review("Relis src/a.cs:3 et dis-moi.");
        var bad = Tool("reply", new() { ["id"] = codex, ["text"] = "src/a.cs:3 gravité haute : ok.\nsrc/a.cs:99 gravité basse.\nfantome.cs:1 gravité moyenne." }, "codex");
        Assert.Equal(ReviewChecks.CitationMissing, bad["quality"]?.ToString());
        Assert.Equal(2, bad["bad_citations"]!.AsArray().Count);
        var good = Tool("reply", new() { ["id"] = agy, ["text"] = "a.cs:5-7 gravité basse : nom à revoir." }, "antigravity");
        Assert.Null(good["quality"]);
        var waited = Tool("wait_reply", new() { ["id"] = new JsonArray(codex, agy), ["timeout_s"] = 1 }, "claude");
        Assert.Equal(ReviewChecks.CitationMissing, waited["replies"]![0]!["reply"]!["quality"]?.ToString());
        Assert.Null(waited["replies"]![0]!["stale"]);
    }

    [Fact]
    public void A_file_changed_before_the_answer_flags_the_review_as_stale()
    {
        var (codex, agy) = Review("Relis src/a.cs:2.");
        Assert.Null(Tool("track", new() { ["id"] = codex }, "claude")["stale"]);
        File.AppendAllText(Path.Join(repo, "src", "a.cs"), "// ajout\n");
        var pending = Tool("track", new() { ["id"] = codex }, "claude")["stale"]!;
        Assert.True(pending["pending"]!.GetValue<bool>());
        Assert.Equal("src/a.cs", pending["changed"]![0]!.ToString());
        Tool("reply", new() { ["id"] = codex, ["text"] = "rien d'important" }, "codex");
        Git("checkout", "--", "src/a.cs");
        var frozen = Tool("track", new() { ["id"] = codex }, "claude")["stale"]!;   // as of the answer, not of now
        Assert.Null(frozen["pending"]);
        Assert.Equal("src/a.cs", frozen["changed"]![0]!.ToString());
        Assert.Equal("src/a.cs", Tool("wait_reply", new() { ["id"] = codex, ["timeout_s"] = 1 }, "claude")["stale"]!["changed"]![0]!.ToString());
        Tool("reply", new() { ["id"] = agy, ["text"] = "rien d'important" }, "antigravity");
        Assert.Null(Tool("track", new() { ["id"] = agy }, "claude")["stale"]);
    }

    [Fact]
    public void Summary_of_a_round_separates_new_points_from_repeated_ones()
    {
        var (c1, a1) = Review("Relis src/a.cs:1.", new() { ["round"] = 1 });
        Tool("reply", new() { ["id"] = c1, ["text"] = "- src/a.cs:4 gravité haute : la boucle ne sort jamais quand la liste est vide." }, "codex");
        Tool("reply", new() { ["id"] = a1, ["text"] = "rien d'important" }, "antigravity");
        var (c2, _) = Review("Tour 2 de relecture : relis src/a.cs:1 après correction.");
        Assert.Equal(2, iso.Box().Get(c2)!.ExtraObj["round"]!.GetValue<int>());
        Tool("reply", new() { ["id"] = c2, ["text"] = "- src/a.cs:6 gravité haute : la boucle ne sort toujours jamais quand la liste est vide.\n- src/a.cs:9 gravité basse : commentaire obsolète sur le format des dates." }, "codex");
        var round = Tool("summary", new() { ["topic"] = "revue-test", ["round"] = 2 }, "claude")["round"]!;
        Assert.Equal(1, round["new_total"]!.GetValue<int>());
        Assert.Equal(1, round["repeated_total"]!.GetValue<int>());
        Assert.Equal(1, round["pending"]!.GetValue<int>());
        Assert.Contains("format des dates", round["replies"]![0]!["new"]![0]!.ToString());
    }

    [Fact]
    public void Stop_hook_wakes_the_host_with_the_answers_of_its_conversation()
    {
        string id = Tool("send", new() { ["to"] = "codex", ["text"] = "Question simple", ["topic"] = "reveil-test" }, "claude")["id"]!.ToString();
        string other = Tool("send", new() { ["to"] = "codex", ["text"] = "Autre conversation", ["topic"] = "reveil-test" }, "claude")["id"]!.ToString();
        string transcript = Path.Join(iso.Root, "conversation.jsonl");
        var use = new JsonObject { ["type"] = "assistant", ["message"] = new JsonObject { ["content"] = new JsonArray(new JsonObject { ["type"] = "tool_use", ["id"] = "toolu_1", ["name"] = "mcp__plugin_nexus-relay_nexus-relay__send" }) } };
        var result = new JsonObject { ["type"] = "user", ["message"] = new JsonObject { ["content"] = new JsonArray(new JsonObject { ["type"] = "tool_result", ["tool_use_id"] = "toolu_1", ["content"] = new JsonArray(new JsonObject { ["type"] = "text", ["text"] = $"{{\"id\": \"{id}\"}}" }) }) } };
        File.WriteAllLines(transcript, [use.ToJsonString(), result.ToJsonString()]);
        Assert.Equal([id], HostRewake.Conversation(transcript).Sent);
        Assert.Contains(id, HostRewake.Conversation(transcript).Seen);   // rendered as "id": "…" by send's result
        var tracked = new JsonObject { ["message"] = new JsonObject { ["content"] = new JsonArray(new JsonObject { ["type"] = "tool_result", ["tool_use_id"] = "toolu_2", ["content"] = $"{{\"replies\": [\"20261031T000000000-abcdef\"]}}" }) } };
        File.AppendAllLines(transcript, [tracked.ToJsonString()]);
        Assert.DoesNotContain("20261031T000000000-abcdef", HostRewake.Conversation(transcript).Seen);   // listed by track, never shown
        var readCall = new JsonObject { ["type"] = "assistant", ["message"] = new JsonObject { ["content"] = new JsonArray(new JsonObject { ["type"] = "tool_use", ["id"] = "toolu_3", ["name"] = "mcp__nexus-relay__read", ["input"] = new JsonObject { ["id"] = "20261031T000000000-abcdef" } }) } };
        var readFailed = new JsonObject { ["message"] = new JsonObject { ["content"] = new JsonArray(new JsonObject { ["type"] = "tool_result", ["tool_use_id"] = "toolu_3", ["is_error"] = true, ["content"] = "{\"id\": \"20261031T000000000-abcdef\"} connection refused" }) } };
        File.AppendAllLines(transcript, [readCall.ToJsonString(), readFailed.ToJsonString()]);
        Assert.DoesNotContain("20261031T000000000-abcdef", HostRewake.Conversation(transcript).Seen);   // asked for, never delivered
        File.AppendAllText(transcript, "{\"message\":{\"content\":[{\"type\":\"tool_result\",\"tool_use_id\":\"toolu_1\",\"con");
        Assert.Equal([id], HostRewake.Conversation(transcript).Sent);   // a line still being written is skipped
        File.WriteAllLines(transcript, [use.ToJsonString(), result.ToJsonString()]);

        var box = iso.Box();
        Tool("reply", new() { ["id"] = id, ["text"] = "Réponse du pair" }, "codex");
        box.MarkRead(box.Replies(id)[0]);   // listed by another conversation's inbox: this one has not seen it
        Tool("reply", new() { ["id"] = other, ["text"] = "Pas pour cette conversation" }, "codex");
        string stop = new JsonObject { ["session_id"] = "conv-1", ["transcript_path"] = transcript, ["hook_event_name"] = "Stop" }.ToJsonString();
        var woken = Native.Run(["--await-replies", "claude"], stop);
        Assert.Equal(2, woken.Code);
        Assert.Contains("Réponse du pair", woken.Err);
        Assert.DoesNotContain("Pas pour cette conversation", woken.Err);
        Assert.False(box.IsRead(box.Replies(other)[0]));
        Assert.Equal(0, Native.Run(["--await-replies", "claude"], stop).Code);   // already brought to this conversation
        Assert.Single(HostRewake.Pending(box, "claude", [id], new HashSet<string>()));
        Assert.Empty(HostRewake.Pending(box, "claude", [id], new HashSet<string> { box.Replies(id)[0].Id }));   // seen in the wake: nothing left to wait for
    }

    private string Transcript(string id)
    {
        string path = Path.Join(iso.Root, $"conversation-{id}.jsonl");
        var use = new JsonObject { ["message"] = new JsonObject { ["content"] = new JsonArray(new JsonObject { ["type"] = "tool_use", ["id"] = "toolu_x", ["name"] = "mcp__nexus-relay__send" }) } };
        var result = new JsonObject { ["message"] = new JsonObject { ["content"] = new JsonArray(new JsonObject { ["type"] = "tool_result", ["tool_use_id"] = "toolu_x", ["content"] = id }) } };
        File.WriteAllLines(path, [use.ToJsonString(), result.ToJsonString()]);
        return new JsonObject { ["session_id"] = "conv-" + id[^6..], ["transcript_path"] = path }.ToJsonString();
    }

    [Fact]
    public void A_request_closed_without_answer_wakes_the_host_once_and_an_info_answer_never()
    {
        var box = iso.Box();
        string halted = Tool("send", new() { ["to"] = "codex", ["text"] = "Arrêtée en route", ["topic"] = "reveil-test" }, "claude")["id"]!.ToString();
        box.Event(halted, "arrêt sur erreur", new JsonObject { ["reason"] = "tour arrêté sans réponse" });
        string stop = Transcript(halted);
        var first = Native.Run(["--await-replies", "claude"], stop);
        Assert.Equal(2, first.Code);
        Assert.Contains(halted, first.Err);
        Assert.Empty(HostRewake.Pending(box, "claude", [halted], new HashSet<string>()));
        Assert.Equal(0, Native.Run(["--await-replies", "claude"], stop).Code);

        string closing = Tool("send", new() { ["to"] = "codex", ["text"] = "Accord final ?", ["topic"] = "reveil-test" }, "claude")["id"]!.ToString();
        Tool("reply", new() { ["id"] = closing, ["text"] = "Accord, rien à ajouter", ["info"] = true }, "codex");
        Assert.Empty(HostRewake.Pending(box, "claude", [closing], new HashSet<string>()));
    }

    [Fact]
    public void The_repository_named_in_the_text_anchors_relative_citations()
    {
        var sent = Tool("send", new() { ["to"] = "codex", ["text"] = $"Relis le dépôt {repo}, surtout src/a.cs:2.", ["topic"] = "revue-test", ["review"] = true }, "claude");
        var snapshot = iso.Box().Get(sent["ids"]![0]!.ToString())!.ExtraObj["snapshot"]!;
        Assert.Equal(Path.GetFullPath(repo), snapshot["repo"]!.ToString(), ignoreCase: true);
        Assert.Single(snapshot["files"]!.AsObject());
    }

    [Fact]
    public void Citation_parser_keeps_files_and_skips_code_and_ports()
    {
        string spaced = Path.Join(iso.Root, "Mon Projet", "src", "b.cs");
        Directory.CreateDirectory(Path.GetDirectoryName(spaced)!);
        File.WriteAllLines(spaced, ["un", "deux"]);
        var found = ReviewChecks.Citations($"`response.status:500`, item.count:0, api.v1:8080, 12:30, {spaced}:2 et .gitignore:4, Dockerfile:3, README:9, src/a.cs:0")
            .Select(c => c.ToString()).ToList();
        Assert.Equal([$"{spaced}:2", ".gitignore:4", "Dockerfile:3", "README:9", "src/a.cs:0"], found);
        Assert.Empty(ReviewChecks.MissingCitations($"{spaced}:2 gravité basse", repo));
        var named = ReviewChecks.Citations(@"`C:\inexistant\Mon a.cs:3`").Single();   // explicit path kept as written
        Assert.Equal(@"C:\inexistant\Mon a.cs", named.Path);
        Assert.Empty(ReviewChecks.MissingCitations(@"`C:\inexistant\Mon a.cs:3`", repo));   // unchecked, not redirected to src/a.cs
        Assert.Equal($"{repo} a.cs", ReviewChecks.Citations($"`{repo} a.cs:3`").Single().Path);   // quoted: kept even after an existing folder
        Assert.Equal("a.cs", ReviewChecks.Citations($"Dépôt {repo}, voir a.cs:3").Single().Path);   // prose after a folder: its last word
        var bad = ReviewChecks.MissingCitations("src/a.cs:0 et Dockerfile:3", repo);
        Assert.Equal(2, bad.Count);
        Assert.Contains("commencent à 1", bad[0]);
        Assert.Contains("introuvable", bad[1]);
    }
}
