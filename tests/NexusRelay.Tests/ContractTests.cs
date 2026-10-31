using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Json.Schema;

namespace NexusRelay.Tests;

/// <summary>
/// Stable contracts of the engine. Golden/contracts.json and Golden/cards.json were recorded from the retired Python
/// engine (the proven oracle) on 30/10/2026: the native engine must keep answering exactly the same.
/// </summary>
public sealed class ContractTests : IDisposable
{
    private readonly Isolation iso = new();
    public void Dispose() => iso.Dispose();
    private static JsonObject Golden(string name) => JsonNode.Parse(File.ReadAllText(Paths.Golden(name)))!.AsObject();

    [Fact]
    public void Every_tool_schema_matches_the_recorded_oracle_and_is_valid_json_schema()
    {
        var native = (JsonArray)Native.Json("--schemas");
        var oracle = Golden("contracts.json")["tools"]!.AsObject();
        Assert.Equal(oracle.Select(p => p.Key).Order(), native.Select(t => t!["name"]!.ToString()).Order());
        foreach (var tool in native)
            foreach (string schema in new[] { "inputSchema", "outputSchema" })
            {
                Assert.True(JsonNode.DeepEquals(tool![schema], oracle[tool["name"]!.ToString()]![schema]), $"{tool["name"]} {schema}");
                if (tool[schema] is { } node) JsonSchema.FromText(node.ToJsonString());
            }
        var byName = native.ToDictionary(t => t!["name"]!.ToString());
        foreach (var (name, field, scalar, group) in new[] { ("send", "to", "codex", new[] { "codex", "antigravity" }), ("wait_reply", "id", "id-a", new[] { "id-a", "id-b" }) })
        {
            var schema = JsonSchema.FromText(byName[name]!["inputSchema"]!["properties"]![field]!.ToJsonString());
            bool Valid(string json) => schema.Evaluate(JsonDocument.Parse(json).RootElement).IsValid;
            Assert.True(Valid(JsonSerializer.Serialize(scalar)) && Valid(JsonSerializer.Serialize(group)));
            Assert.False(Valid("[1]") || Valid("17"));
        }
    }

    [Fact]
    public void Every_typed_tool_returns_data_valid_against_its_output_schema()
    {
        var box = iso.Box();
        var ask = box.Post("claude", "codex", "fixture café 😀", "typed-fixture");
        var answer = box.Post("codex", "claude", "fixture answer", "typed-fixture", replyTo: ask.Id);
        var blocked = box.Post("claude", "codex", "fixture still unavailable", "typed-fixture");
        box.SetRingState(blocked.Id, new() { ["agent"] = "codex", ["incident"] = "fixture unavailable", ["incident_at"] = blocked.Created });
        var calls = new (string Name, JsonObject Args)[]
        {
            ("inbox", new()), ("read", new() { ["id"] = answer.Id }), ("track", new() { ["id"] = ask.Id }), ("reservations", new()),
            ("search", new() { ["query"] = "café" }), ("summary", new() { ["topic"] = "typed-fixture" }), ("status", new()),
        };
        var results = Native.Stdio("claude", calls.Select(c => Native.Call(c.Name, c.Args)).ToArray());
        var schemas = ((JsonArray)Native.Json("--schemas")).ToDictionary(t => t!["name"]!.ToString());
        foreach (var ((name, _), response) in calls.Zip(results))
        {
            var data = Native.Data(response);
            Assert.True(data["status"]?.ToString() == "success", name + data.ToJsonString());
            var evaluation = JsonSchema.FromText(schemas[name]!["outputSchema"]!.ToJsonString()).Evaluate(JsonDocument.Parse(data.ToJsonString()).RootElement, new EvaluationOptions { OutputFormat = OutputFormat.List });
            Assert.True(evaluation.IsValid, name + " " + JsonSerializer.Serialize(evaluation));
        }
    }

    [Fact]
    public void Map_scripts_and_landmarks_match_the_proven_source()
    {
        var contracts = Native.Json("--ui-contracts");
        var oracle = Golden("contracts.json")["ui"]!;
        foreach (string key in new[] { "snapshot", "danger", "landmarks" }) Assert.True(JsonNode.DeepEquals(contracts[key], oracle[key]), key);
        foreach (var (app, spec) in oracle["apps"]!.AsObject())
            foreach (string key in new[] { "pages", "subpages" }) Assert.True(JsonNode.DeepEquals(contracts["apps"]![app]![key], spec![key]), $"{app} {key}");
    }

    /// <summary>Card semantics: the recorded calls are replayed in order (storage included) against the engine.</summary>
    [Fact]
    public void Card_semantics_replay_the_recorded_oracle_calls()
    {
        var golden = Golden("cards.json");
        string state = Path.Join(iso.Root, "cards");
        Directory.CreateDirectory(state);
        iso.Set("NEXUS_RELAY_HOME", Path.Join(state, "relay"));
        foreach (var (name, data) in golden["files"]!.AsObject()) File.WriteAllBytes(Path.Combine(state, name), Convert.FromBase64String(data!.ToString()));
        string escaped = JsonSerializer.Serialize(state)[1..^1];
        int index = 0;
        bool diffChecked = false;
        foreach (var call in golden["calls"]!.AsArray())
        {
            var c = JsonNode.Parse(call!["args"]!.ToJsonString().Replace("{STATE}", escaped))!.AsObject();
            string S(string key) => c[key]?.ToString() ?? "";
            var items = (c["items"] as JsonArray ?? []).OfType<JsonObject>();
            var card = c["card"] as JsonObject ?? new();
            JsonObject answer;
            try
            {
                JsonNode? result = call["op"]!.ToString() switch
                {
                    "locator" => NativeCards.Locator(c["item"]!.AsObject()),
                    "mask" => new JsonArray(items.Select(e => (JsonNode?)new NativeCards.Known((c["labels"] as JsonArray ?? []).Select(v => v!.ToString())).Mask(e)).ToArray()),
                    "collapse" => new JsonArray(NativeCards.Collapse(items).Select(e => (JsonNode?)e).ToArray()),
                    "marks" => NativeCards.LandmarkStatus(S("app"), items, c["context"] as JsonObject),
                    "diff" => NativeCards.Diff(c["before"]!.AsObject(), c["after"]!.AsObject()),
                    "summary" => JsonValue.Create(NativeCards.DiffSummary(c["diff"]!.AsObject())),
                    "search" => NativeCards.Search(card, S("app"), S("what"), c["limit"]?.GetValue<int>() ?? 5),
                    "where" => NativeCards.Where(S("app"), S("what"), c["limit"]?.GetValue<int>() ?? 5),
                    "save" => NativeCards.Save(card),
                    "fresh" => JsonValue.Create(NativeCards.Fresh(c["card"] as JsonObject, S("version"))),
                    "index" => NativeCards.CodexIndex(S("path"), S("lang")),
                    var op => throw new InvalidOperationException("unknown recorded operation " + op),
                };
                answer = new JsonObject { ["result"] = result?.DeepClone() };
            }
            catch (Exception error) when (error is not InvalidOperationException) { answer = new JsonObject { ["error"] = error.GetType().Name, ["message"] = error.Message }; }
            var expected = JsonNode.Parse(call["answer"]!.ToJsonString().Replace("{STATE}", escaped))!;
            if (expected["error"] is not null) Assert.True(answer["error"] is not null, $"call {index} ({call["op"]}) should fail: {answer.ToJsonString()}");
            else Assert.True(JsonNode.DeepEquals(answer, expected), $"call {index} ({call["op"]})\nactual:   {answer.ToJsonString()}\nexpected: {expected.ToJsonString()}");
            if (!diffChecked && call["op"]!.ToString() == "save" && answer["result"]?["diff"] is { } diffPath)
            {
                // The stored difference of the first real change is the one the diff operation computes.
                var computed = golden["calls"]!.AsArray().First(x => x!["op"]!.ToString() == "diff")!["answer"]!["result"];
                Assert.True(JsonNode.DeepEquals(JsonNode.Parse(File.ReadAllText(diffPath.ToString())), computed));
                diffChecked = true;
            }
            index++;
        }
        Assert.Equal(38, index);
        Assert.True(diffChecked);
    }

    /// <summary>Electron app.asar: JSON header (pickle) then contents.</summary>
    public static string Asar(string path, IEnumerable<(string Name, byte[] Data)> files)
    {
        var tree = new JsonObject { ["files"] = new JsonObject() };
        using var blob = new MemoryStream();
        foreach (var (name, data) in files)
        {
            var node = tree;
            var parts = name.Trim('/').Split('/');
            foreach (string folder in parts[..^1])
            {
                var children = node["files"]!.AsObject();
                if (children[folder] is not JsonObject next) children[folder] = next = new JsonObject { ["files"] = new JsonObject() };
                node = next;
            }
            node["files"]![parts[^1]] = new JsonObject { ["size"] = data.Length, ["offset"] = blob.Length.ToString() };
            blob.Write(data);
        }
        byte[] header = Encoding.UTF8.GetBytes(tree.ToJsonString());
        var pickled = BitConverter.GetBytes(header.Length).Concat(header).ToArray();
        pickled = BitConverter.GetBytes(pickled.Length).Concat(pickled).ToArray();
        File.WriteAllBytes(path, [.. BitConverter.GetBytes(4), .. BitConverter.GetBytes(pickled.Length), .. pickled, .. blob.ToArray()]);
        return path;
    }

    [Fact]
    public void Reads_localized_asar_index_with_unicode_and_escapes()
    {
        string asar = Asar(Path.Join(iso.Root, "app.asar"),
        [
            ("webview/assets/fr-FR-fixture.js", Encoding.Latin1.GetBytes("export default {\"archive\":\"Archiver\",\"mode\":`Changer de mode\\xA0: {mode}`,\"emoji\":\"\\uD83D\\uDE00\"}")),
            ("native-menu-locales/fr-FR.json", Encoding.UTF8.GetBytes("""{"quit": "Quitter"}""")),
        ]);
        var native = Native.Json("--card-index", asar, "fr-FR");
        Assert.Equal("Archiver", native["archive"]?.ToString());
        Assert.Equal("Changer de mode : {mode}", native["mode"]?.ToString());
        Assert.Equal("😀", native["emoji"]?.ToString());
        Assert.Equal("Quitter", native["menu:quit"]?.ToString());
    }

    [Fact]
    public void Where_reads_historical_complete_cards_without_the_application()
    {
        string folder = Path.Join(iso.Relay, "v1", "cartes");
        Directory.CreateDirectory(folder);
        var element = new JsonObject { ["role"] = "menuitem", ["aria"] = "Archiver", ["text"] = "", ["data"] = new JsonObject { ["data-testid"] = "archive-chat" }, ["path"] = new JsonArray("vue de départ", "menu « Actions du chat »") };
        JsonObject Card(string version, string at, JsonArray elements) => new() { ["app"] = "codex", ["version"] = version, ["surveyed_at"] = at, ["elements"] = elements, ["index"] = new JsonObject { ["archive"] = "Archiver" } };
        File.WriteAllText(Path.Join(folder, "codex-1.json"), Card("1", "2026-10-20T10:00:00Z", [element]).ToJsonString());
        File.WriteAllText(Path.Join(folder, "codex-2.json"), Card("2", "2026-10-21T10:00:00Z", []).ToJsonString());
        File.WriteAllText(Path.Join(folder, "codex-3.partiel.json"), Card("3", "2099-01-01T00:00:00Z", [element.DeepClone()]).ToJsonString());
        var data = Native.Data(Native.Stdio("claude", Native.Call("where", new() { ["app"] = "codex", ["what"] = "ARCHIVE" }))[0]);
        Assert.Equal("success", data["status"]?.ToString());
        Assert.Equal("2", data["version"]?.ToString());
        Assert.StartsWith("1 (", data["from_older_card"]!.ToString());
        var found = data["found"]![0]!;
        Assert.Equal("ARCHIVE", found["landmark"]?.ToString());
        Assert.EndsWith("menu « Actions du chat »", found["path"]!.ToString());
        Assert.Equal("[data-testid=\"archive-chat\"]", (found["how"] as JsonArray)![^1]!["locator"]?["css"]?.ToString());
    }

    // ---------- real DOM survey in a fresh, headless, local-only Chrome ----------

    private static string? Chrome()
    {
        foreach (var root in new[] { Environment.SpecialFolder.ProgramFiles, Environment.SpecialFolder.ProgramFilesX86, Environment.SpecialFolder.LocalApplicationData })
        {
            string path = Path.Join(Environment.GetFolderPath(root), "Google", "Chrome", "Application", "chrome.exe");
            if (File.Exists(path)) return path;
        }
        return null;
    }

    private JsonObject Browser(string scenario = "normal")
    {
        string chrome = Chrome() ?? throw new InvalidOperationException("Chrome is required for the card survey tests");
        int port = Native.FreePort();
        string profile = Path.Join(iso.Root, "browser", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(profile);
        var start = new ProcessStartInfo(chrome) { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden };
        // Chromium's Windows sandbox needs the real OS profile identity; its data still goes only to --user-data-dir.
        start.Environment["USERPROFILE"] = Isolation.RealProfile;
        foreach (string arg in new[] { "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-default-apps", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=" + port, "--user-data-dir=" + profile, new Uri(Paths.Fixture("cards.html")).AbsoluteUri })
            start.ArgumentList.Add(arg);
        using var process = Process.Start(start)!;
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(1) };
            string? url = null; var until = DateTime.UtcNow.AddSeconds(20);
            while (DateTime.UtcNow < until && url is null)
            {
                try { url = JsonNode.Parse(http.GetStringAsync($"http://127.0.0.1:{port}/json/list").GetAwaiter().GetResult())!.AsArray().OfType<JsonObject>().FirstOrDefault(t => t["type"]?.ToString() == "page" && t["url"]?.ToString().StartsWith("file:") == true)?["webSocketDebuggerUrl"]?.ToString(); }
                catch (Exception e) when (e is HttpRequestException or TaskCanceledException) { /* Debugger not up yet: retried until the deadline. */ }
                if (url is null) Thread.Sleep(100);
            }
            Assert.NotNull(url);
            using var cdp = new CdpPage(url!);
            // The target can appear in /json/list before its first navigation commits. Its initial empty document
            // already reports readyState=complete, so wait for the actual fixture and its handlers, not that state alone.
            until = DateTime.UtcNow.AddSeconds(20);
            while (!AppTransport.Truth(cdp.Eval("document.readyState === 'complete' && !!document.getElementById('draft') && Array.isArray(window.__events)")))
            {
                Assert.True(DateTime.UtcNow < until, "Chrome did not load the card survey fixture within 20 seconds");
                Thread.Sleep(20);
            }
            cdp.Eval("window.__scenario=" + AppTransport.Quote(scenario));
            if (scenario == "draft") cdp.Eval("document.getElementById('draft').value='Synthetic unsent draft'");
            if (scenario == "running") cdp.Eval("(()=>{const b=document.createElement('button');b.setAttribute('aria-label','Stop execution');document.body.append(b)})()");
            var page = new ActualPage(cdp, scenario);
            string[] labels = ["Accueil", "Paramètres", "Historique", "Fichier", "Lecture", "Notifications", "Aucun chat", "Choisir modèle", "Sécurité"];
            JsonObject? card = null; string? refusal = null;
            try { card = NativeCards.RunSurvey("antigravity", "fixture-version", new JsonObject(), labels, page, () => scenario == "user-return" && (cdp.Eval("window.__events.length")?.GetValue<int>() ?? 0) > 0, ms => Thread.Sleep(Math.Min(ms, 30))); }
            catch (RelayError error) { refusal = error.Message; }
            return new() { ["card"] = card, ["events"] = cdp.Eval("window.__events")?.DeepClone(), ["view"] = cdp.Eval("({hash:location.hash,menu:document.getElementById('menu').hidden})")?.DeepClone(), ["refusal"] = refusal };
        }
        finally { if (!process.HasExited) { process.Kill(entireProcessTree: true); process.WaitForExit(5000); } }
    }

    private sealed class ActualPage(CdpPage page, string scenario) : NativeCards.ICardPage
    {
        private bool broken;
        public JsonNode? Eval(string expression)
        {
            if (broken) throw new IOException("Fixture transport lost after the first gesture; outcome unknown");
            var value = page.Eval(expression);
            if (scenario == "transport-loss" && expression.Contains("e.click()", StringComparison.Ordinal) && AppTransport.Truth(value)) { broken = true; throw new IOException("Fixture transport lost after gesture; outcome unknown"); }
            return value;
        }
        public bool Press(string expression) => page.Press(expression);
        public JsonObject Call(string method, JsonObject? arguments = null) => page.Call(method, arguments);
        public void Escape() => page.Escape();
    }

    private static List<string> Events(JsonObject result) => (result["events"] as JsonArray ?? []).Select(e => e!.ToString()).ToList();

    [Fact]
    public void Survey_walks_real_menus_pages_subpages_and_restores_the_view_without_clicking_actions()
    {
        var result = Browser();
        Assert.Equal("complet", result["card"]?["live"]?["status"]?.ToString());
        Assert.True(result["card"]!["live"]!["view_restored"]!.GetValue<bool>());
        var events = Events(result);
        Assert.Contains("navigate:settings", events);
        Assert.Contains("navigate:notifications", events);
        Assert.DoesNotContain(events, e => e.StartsWith("FORBIDDEN", StringComparison.Ordinal));
        Assert.True(JsonNode.DeepEquals(result["view"], new JsonObject { ["hash"] = "", ["menu"] = true }));
    }

    [Fact]
    public void Survey_stops_when_the_user_returns_and_keeps_partial_evidence()
    {
        var result = Browser("user-return");
        Assert.Equal(["menu"], Events(result));
        Assert.False(result["card"]!["live"]!["view_restored"]!.GetValue<bool>());
        Assert.StartsWith("interrompu", result["card"]!["live"]!["status"]!.ToString());
        Assert.False(NativeCards.Fresh(result["card"] as JsonObject, "fixture-version"));
    }

    [Fact]
    public void Survey_transport_loss_after_a_gesture_stays_unknown_and_is_never_replayed()
    {
        var result = Browser("transport-loss");
        Assert.Equal(["menu"], Events(result));
        Assert.Contains("unknown", result["card"]!["live"]!["status"]!.ToString());
        Assert.False(result["card"]!["live"]!["view_restored"]!.GetValue<bool>());
    }

    [Theory]
    [InlineData("draft")]
    [InlineData("running")]
    public void Survey_refuses_before_any_gesture_with_a_draft_or_a_running_turn(string scenario)
    {
        var result = Browser(scenario);
        Assert.Null(result["card"]);
        Assert.False(string.IsNullOrEmpty(result["refusal"]?.ToString()));
        Assert.Empty(Events(result));
    }
}
