using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusRelay;

public static class NativeCards
{
    public static JsonObject Contracts()
    {
        using var stream = typeof(NativeCards).Assembly.GetManifestResourceStream("NexusRelay.UiContracts.json")!;
        return (JsonNode.Parse(stream) as JsonObject)!;
    }
    private static readonly JsonObject Ui = Contracts();
    private static readonly Regex Danger = new(Ui["danger"]!.ToString(), RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
    private static string Folder => Path.Combine(RelayPaths.RelayHome(), "v1", "cartes");
    private static string Label(JsonObject item) => item["aria"]?.ToString() is string aria && aria.Length > 0 ? aria : item["text"]?.ToString() ?? "";
    private static string Norm(string value) => Regex.Replace(string.Concat(value.Normalize(NormalizationForm.FormKD).ToLowerInvariant().Replace("ß","ss",StringComparison.Ordinal).Where(c=>CharUnicodeInfo.GetUnicodeCategory(c)!=UnicodeCategory.NonSpacingMark)), @"\s+", " ").Trim();
    private static void CheckApp(string app) { if (app is not ("codex" or "antigravity")) throw new RelayError("Expected app codex or antigravity"); }
    public static bool Complete(JsonObject? card) => card?["elements"] is JsonArray { Count: > 0 } && card["live"]?["status"]?.ToString() == "complet" && AppTransport.Truth(card["live"]?["view_restored"]);
    public static bool Fresh(JsonObject? card, string version) => card?["version"]?.ToString() == version && Complete(card);
    public static List<JsonObject> Cards(string app)
    {
        CheckApp(app);
        return Directory.Exists(Folder) ? Directory.EnumerateFiles(Folder, app + "-*.json").Where(p => !p.EndsWith(".diff.json", StringComparison.Ordinal) && !p.EndsWith(".partiel.json", StringComparison.Ordinal)).Select(RelayBox.ReadJson).OfType<JsonObject>().Where(c => c["elements"] is JsonArray && (c["app"] is null || c["app"]!.ToString() == app) && (c["live"] is null || Complete(c))).OrderByDescending(c => DateTimeOffset.TryParse(c["surveyed_at"]?.ToString(), CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var at) ? at : DateTimeOffset.MinValue).ThenByDescending(c => c["surveyed_at"]?.ToString(), StringComparer.Ordinal).ToList() : [];
    }
    public static bool Matches(JsonObject item, JsonObject rule)
    {
        foreach (string key in new[] { "role", "popup" }) if (rule[key] is JsonNode value && item[key]?.ToString() != value.ToString()) return false;
        foreach (string key in new[] { "role", "aria" }) if (rule[key + "_re"] is JsonNode expression && !Regex.IsMatch(item[key]?.ToString() ?? "", expression.ToString(), RegexOptions.None, TimeSpan.FromSeconds(1))) return false;
        if (rule["labels"] is JsonArray labels && !labels.Any(v => v?.ToString() == item["aria"]?.ToString() || v?.ToString() == item["text"]?.ToString())) return false;
        if (rule["in_menu"] is JsonNode menu && !(item["path"] as JsonArray ?? []).Any(p => p?.ToString().Contains(menu.ToString(), StringComparison.Ordinal) == true)) return false;
        return (rule["data"] as JsonObject ?? new()).All(kv => item["data"]?[kv.Key]?.ToString() == kv.Value?.ToString());
    }
    public static JsonObject Locator(JsonObject item)
    {
        var result = new JsonObject(); var data = item["data"] as JsonObject ?? new();
        foreach (string key in new[] { "data-testid", "data-sidebar-destination", "data-app-action-sidebar-thread-title" })
            if (data[key]?.ToString() is string stable && stable.Length > 0 && stable != "*") return new JsonObject { ["css"] = "[" + key + "=" + AppTransport.Quote(stable) + "]" };
        foreach (string key in data.Select(kv => kv.Key))
            if (data[key]?.ToString() is string value && value.Length > 0 && value != "*" && key is not ("data-color" or "data-variant" or "data-size" or "data-icon-size")) { result["css"] = "[" + key + "=" + AppTransport.Quote(value) + "]"; break; }
        if (item["role"] is JsonNode role) result["role"] = role.DeepClone();
        foreach (string key in new[] { "aria", "text" }) if (item[key]?.ToString() is string value && value.Length > 0 && value != "*") { result[Regex.IsMatch(value,@"\$?\{[^{}]*\}") ? key + "_re" : key] = Regex.IsMatch(value,@"\$?\{[^{}]*\}") ? TemplateRegex(value) : value; break; }
        return result;
    }
    public static string TemplateRegex(string value) => "^" + string.Join(".+", Regex.Split(value,@"\$?\{[^{}]*\}").Select(part => string.Concat(part.Select(c => ".*+?^${}()|[]\\/".Contains(c) ? "\\" + c : c.ToString())))) + "$";
    private static string Element(JsonObject locator) => """
        (()=>{const L=__LOCATOR__;const label=e=>(e.getAttribute('aria-label')||e.getAttribute('title')||'').trim();
        const text=e=>(e.innerText||'').trim().split('\n')[0].trim().slice(0,80);
        const matches=[...document.querySelectorAll(L.css||'*')].filter(e=>e.getClientRects().length&&(!L.role||(e.getAttribute('role')||{BUTTON:'button',A:'link'}[e.tagName]||'')===L.role)&&(!L.aria||label(e)===L.aria)&&(!L.text||text(e)===L.text));
        const exact=matches.filter(e=>(!L.aria_re||new RegExp(L.aria_re,'s').test(label(e).replace(/\u00a0/g,' ')))&&(!L.text_re||new RegExp(L.text_re,'s').test(text(e).replace(/\u00a0/g,' '))));
        return Number.isInteger(L.nth)?(exact[L.nth]||null):(exact.length===1?exact[0]:null)})()
        """.Replace("__LOCATOR__", locator.ToJsonString(), StringComparison.Ordinal);
    public static JsonArray Search(JsonObject card, string app, string what, int limit = 5)
    {
        CheckApp(app);
        string query = Norm(what); var elements = (card["elements"] as JsonArray ?? []).OfType<JsonObject>().ToList(); var rules = Ui["landmarks"]![app] as JsonObject ?? new();
        var scored = new List<(int Score, JsonObject Item)>();
        foreach (var item in elements)
        {
            int score;
            if (rules[what.Trim().ToUpperInvariant()] is JsonObject rule) score = Matches(item, rule) ? 100 : 0;
            else score = new[] { Label(item), item["text"]?.ToString() ?? "" }.Concat((item["data"] as JsonObject ?? new()).Select(kv => kv.Key + "=" + kv.Value)).Select(Norm).Select(f => f == query ? 100 : f.StartsWith(query, StringComparison.Ordinal) ? 60 : f.Contains(query, StringComparison.Ordinal) ? 30 : 0).Max();
            if (score > 0 && query.Length > 0) scored.Add((score - (item["path"] as JsonArray)?.Count ?? score, item));
        }
        return new(scored.OrderByDescending(x => x.Score).Take(limit).Select(pair =>
        {
            var item = pair.Item; var how = new JsonArray();
            foreach (string step in (item["path"] as JsonArray ?? []).Select(p => p?.ToString()).OfType<string>().Where(s => s != "vue de départ"))
            {
                string name = step.Contains(" « ", StringComparison.Ordinal) ? step.Split(" « ", 2)[1].TrimEnd(' ', '»') : step;
                var source = elements.FirstOrDefault(e => Label(e) == name && (step.StartsWith("page",StringComparison.Ordinal) ? e["popup"] is null || e["role"]?.ToString().StartsWith("menuitem",StringComparison.Ordinal)==true : e["popup"] is not null));
                how.Add(new JsonObject { ["step"] = step, ["gesture"] = step.StartsWith("sous-menu", StringComparison.Ordinal) ? RelayPaths.Tr("hover (Input.dispatchMouseEvent mouseMoved)","survol (Input.dispatchMouseEvent mouseMoved)") : RelayPaths.Tr("emulated click (cdp._press)","clic émulé (cdp._press)"), ["locator"] = source is null ? new JsonObject { ["text"] = name } : Locator(source) });
            }
            how.Add(new JsonObject { ["step"] = RelayPaths.Tr("element","élément"), ["locator"] = Locator(item) });
            return (JsonNode)new JsonObject { ["label"] = Label(item), ["role"] = item["role"]?.DeepClone(), ["path"] = string.Join(" > ", (item["path"] as JsonArray ?? []).Select(p => p?.ToString())), ["landmark"] = rules.FirstOrDefault(kv => kv.Value is JsonObject r && Matches(item, r)).Key, ["data"] = item["data"]?.DeepClone() ?? new JsonObject(), ["how"] = how };
        }).ToArray());
    }
    public static JsonObject Where(string app, string what, int limit = 5)
    {
        CheckApp(app);limit=Math.Clamp(limit,1,50);
        var cards = Cards(app); if (cards.Count == 0) return new() { ["status"] = "error", ["error"] = RelayPaths.Tr($"no map yet for {app}: run a survey",$"pas encore de carte pour {app} : lancer un relevé") };
        var latest = cards[0]; var hits = Search(latest, app, what,limit); var result = new JsonObject { ["status"] = "success", ["app"] = app, ["version"] = latest["version"]?.DeepClone(), ["surveyed_at"] = latest["surveyed_at"]?.DeepClone(), ["found"] = hits };
        if (hits.Count == 0) foreach (var older in cards.Skip(1)) if ((hits = Search(older, app, what,limit)).Count > 0) { result["found"] = hits; result["from_older_card"] = older["version"] + " (" + older["surveyed_at"] + RelayPaths.Tr("): not seen in the last survey",") : pas vu au dernier relevé"); break; }
        string query = Norm(what); var labels = new JsonArray((latest["index"] as JsonObject ?? new()).Where(kv => query.Length > 0 && (Norm(kv.Key).Contains(query, StringComparison.Ordinal) || Norm(kv.Value?.ToString() ?? "").Contains(query, StringComparison.Ordinal))).Take(limit).Select(kv => (JsonNode)new JsonObject { ["key"] = kv.Key, ["text"] = kv.Value?.DeepClone() }).ToArray());
        if (labels.Count > 0) result["labels"] = labels;
        if (((JsonArray)result["found"]!).Count == 0) result["note"] = RelayPaths.Tr("not seen in the survey","pas vu au relevé") + (labels.Count>0?RelayPaths.Tr("; label known to the app (index), page or menu not visited"," ; libellé connu de l'appli (index), page ou menu non parcourus"):"");
        return result;
    }
    public static List<JsonObject> Collapse(IEnumerable<JsonObject> elements)
    {
        var result=new List<JsonObject>();
        foreach(var group in elements.GroupBy(e=>new JsonArray(e["role"]?.DeepClone(),e["aria"]?.DeepClone(),e["data"]?.DeepClone(),e["region"]?.DeepClone(),e["popup"]?.DeepClone()).ToJsonString()))
        {
            var entries=group.ToList();if(entries.Count>3&&entries.Select(e=>e["text"]?.ToString()).Distinct().Count()>3){var first=entries[0].DeepClone().AsObject();first["text"]="";first["repeated"]=entries.Count;result.Add(first);}
            else result.AddRange(entries.DistinctBy(e=>e["text"]?.ToString()).Select(e=>e.DeepClone().AsObject()));
        }
        return result;
    }
    public static JsonObject LandmarkStatus(string app, IEnumerable<JsonObject> elements, JsonObject? context=null)
    {
        CheckApp(app);var items=elements.ToList();var result=new JsonObject();
        foreach(var pair in Ui["landmarks"]![app]!.AsObject())
        {
            var rule=pair.Value!.AsObject();var hits=items.Where(e=>Matches(e,rule)).ToList();
            if(hits.Count==0&&rule["needs"] is JsonValue need&&context is not null&&!AppTransport.Truth(context[need.ToString()])){result[pair.Key]=new JsonObject{["unverified"]=RelayPaths.Tr("unverifiable: no chat open at the start of the survey","non vérifiable : aucun chat ouvert au départ du relevé")};continue;}
            var empty=items.FirstOrDefault(e=>(rule["unless"] as JsonArray??[]).Any(label=>label?.ToString()==e["aria"]?.ToString()||label?.ToString()==e["text"]?.ToString()));
            if(hits.Count==0&&empty is not null){result[pair.Key]=new JsonObject{["unverified"]=RelayPaths.Tr($"unverifiable: “{Label(empty)}” ({WherePath(empty)})",$"non vérifiable : « {Label(empty)} » ({WherePath(empty)})")};continue;}
            var paths=hits.Select(WherePath).Distinct().OrderBy(p=>p.Count(c=>c=='>')).ThenBy(p=>!p.StartsWith("vue de départ",StringComparison.Ordinal)).ThenBy(p=>p,StringComparer.Ordinal).ToList();
            result[pair.Key]=hits.Count==0?null:new JsonObject{["path"]=paths[0],["paths"]=new JsonArray(paths.Select(p=>(JsonNode?)JsonValue.Create(p)).ToArray()),["label"]=Label(hits[0])};
        }
        return result;
    }
    private static string WherePath(JsonObject item)=>string.Join(" > ",(item["path"] as JsonArray??[]).Select(p=>p?.ToString()));
    private static string Identity(JsonObject item)=>new JsonArray(item["role"]?.DeepClone(),Label(item),new JsonArray((item["data"] as JsonObject??new()).Where(p=>p.Value?.ToString()!="*").Select(p=>p.Key).Order(StringComparer.Ordinal).Select(p=>(JsonNode?)JsonValue.Create(p)).ToArray())).ToJsonString();
    public static JsonObject Diff(JsonObject before,JsonObject after)
    {
        Dictionary<string,HashSet<string>> Index(JsonObject card)=>(card["elements"] as JsonArray??[]).OfType<JsonObject>().GroupBy(Identity).ToDictionary(g=>g.Key,g=>g.Select(e=>WherePath(e) is {Length:>0} p?p:"?").ToHashSet());
        string Describe(string identity){var id=JsonNode.Parse(identity)!.AsArray();var role=id[0]?.ToString();return RelayPaths.Tr($"{(string.IsNullOrEmpty(role)?"element":role)} \"{id[1]}\"",$"{(string.IsNullOrEmpty(role)?"élément":role)} « {id[1]} »");}
        bool Labeled(string identity)=>JsonNode.Parse(identity)![1]?.ToString().Length>0;
        JsonArray Strings(IEnumerable<string> values)=>new(values.Select(s=>(JsonNode?)JsonValue.Create(s)).ToArray());
        var old=Index(before);var current=Index(after);
        var added=current.Keys.Except(old.Keys).Where(Labeled).Select(k=>Describe(k)+" ("+string.Join(", ",current[k].Order(StringComparer.Ordinal))+")").Order(StringComparer.Ordinal);
        var removed=old.Keys.Except(current.Keys).Where(Labeled).Select(k=>Describe(k)+" ("+string.Join(", ",old[k].Order(StringComparer.Ordinal))+")").Order(StringComparer.Ordinal);
        var moved=old.Keys.Intersect(current.Keys).Where(k=>Labeled(k)&&old[k].Except(current[k]).Any()&&current[k].Except(old[k]).Any()).Select(k=>Describe(k)+" : "+string.Join(", ",old[k].Except(current[k]).Order(StringComparer.Ordinal))+" → "+string.Join(", ",current[k].Except(old[k]).Order(StringComparer.Ordinal))).Order(StringComparer.Ordinal);
        var marks=new List<string>();var oldMarks=before["landmarks"] as JsonObject??new();var newMarks=after["landmarks"] as JsonObject??new();
        foreach(var key in oldMarks.Select(p=>p.Key).Union(newMarks.Select(p=>p.Key)).Order(StringComparer.Ordinal))
        {
            var was=oldMarks[key] as JsonObject;var now=newMarks[key] as JsonObject;if(was?["path"] is null)was=null;
            if(now is not null&&now["path"] is null){if(was is not null)marks.Add(RelayPaths.Tr($"landmark {key} {now["unverified"]} (was: {was["path"]})",$"repère {key} {now["unverified"]} (était : {was["path"]})"));continue;}
            if(was is not null&&now is null)marks.Add(RelayPaths.Tr($"LANDMARK GONE {key} (was: {was["path"]})",$"REPÈRE DISPARU {key} (était : {was["path"]})"));
            else if(now is not null&&was is null)marks.Add(RelayPaths.Tr($"landmark {key} found: {now["path"]}",$"repère {key} trouvé : {now["path"]}"));
            else if(now is not null&&was is not null)
            {
                var a=(was["paths"] as JsonArray??new JsonArray(was["path"]!.DeepClone())).Select(p=>p!.ToString()).ToHashSet();var b=(now["paths"] as JsonArray??new JsonArray(now["path"]!.DeepClone())).Select(p=>p!.ToString()).ToHashSet();
                if(b.Except(a).Any())marks.Add(RelayPaths.Tr($"landmark {key} now also: {string.Join(", ",b.Except(a).Order(StringComparer.Ordinal))}",$"repère {key} maintenant aussi : {string.Join(", ",b.Except(a).Order(StringComparer.Ordinal))}"));
                if(a.Except(b).Any())marks.Add(RelayPaths.Tr($"landmark {key} no longer: {string.Join(", ",a.Except(b).Order(StringComparer.Ordinal))}",$"repère {key} n'est plus : {string.Join(", ",a.Except(b).Order(StringComparer.Ordinal))}"));
            }
        }
        var oldLabels=before["index"] as JsonObject??new();var newLabels=after["index"] as JsonObject??new();
        return new(){["from"]=$"{before["version"]} ({before["surveyed_at"]})",["to"]=$"{after["version"]} ({after["surveyed_at"]})",["landmarks"]=Strings(marks),["moved"]=Strings(moved),["added"]=Strings(added),["removed"]=Strings(removed),["labels"]=new JsonObject{["added"]=newLabels.Select(p=>p.Key).Except(oldLabels.Select(p=>p.Key)).Count(),["removed"]=Strings(oldLabels.Select(p=>p.Key).Except(newLabels.Select(p=>p.Key)).Order(StringComparer.Ordinal).Take(200)),["changed"]=Strings(oldLabels.Select(p=>p.Key).Intersect(newLabels.Select(p=>p.Key)).Where(k=>!JsonNode.DeepEquals(oldLabels[k],newLabels[k])).Order(StringComparer.Ordinal).Take(200).Select(k=>$"{k} : « {oldLabels[k]} » → « {newLabels[k]} »"))}};
    }
    public static string DiffSummary(JsonObject diff)
    {
        var lines=new List<string>{$"{diff["from"]} → {diff["to"]}"};var marks=(diff["landmarks"] as JsonArray??[]).Select(v=>v!.ToString()).ToList();lines.AddRange(marks.Count>0?marks:[RelayPaths.Tr("relay landmarks: unchanged","repères du relais : inchangés")]);
        lines.AddRange((diff["moved"] as JsonArray??[]).Take(20).Select(v=>RelayPaths.Tr("moved: ","déplacé : ")+v));var added=(diff["added"] as JsonArray)?.Count??0;var removed=(diff["removed"] as JsonArray)?.Count??0;lines.Add(RelayPaths.Tr($"{added} elements appeared, {removed} gone",$"{added} éléments apparus, {removed} disparus"));
        var labels=diff["labels"]!;var newCount=labels["added"]?.GetValue<int>()??0;var gone=(labels["removed"] as JsonArray)?.Count??0;var changed=(labels["changed"] as JsonArray)?.Count??0;lines.Add(RelayPaths.Tr($"labels: {newCount} new, {gone} removed, {changed} changed",$"libellés : {newCount} nouveaux, {gone} retirés, {changed} changés"));return string.Join('\n',lines);
    }
    public static JsonObject Save(JsonObject card)
    {
        var app=card["app"]?.ToString()??"";CheckApp(app);var version=card["version"]?.ToString()??throw new RelayError("Card version missing");var path=Path.Combine(Folder,app+"-"+Regex.Replace(version,@"[^\w.\-]","_")+".json");var partial=Path.ChangeExtension(path,".partiel.json");
        if(card["live"] is not null&&!Complete(card)){RelayBox.WriteState(partial,card);return new(){["card"]=partial,["partial"]=true};}
        var previous=Cards(app).FirstOrDefault(c=>c["version"]?.ToString()!=version);var result=new JsonObject{["card"]=path};
        if(previous is not null){var change=Diff(previous,card);var diffFile=Path.ChangeExtension(path,".diff.json");card["diff_from"]=app+"-"+Regex.Replace(previous["version"]!.ToString(),@"[^\w.\-]","_")+".json";RelayBox.WriteState(diffFile,change);result["diff"]=diffFile;result["summary"]=DiffSummary(change);result["alerts"]=new JsonArray(change["landmarks"]!.AsArray().Where(v=>v!.ToString().StartsWith("LANDMARK GONE",StringComparison.Ordinal)||v.ToString().StartsWith("REPÈRE DISPARU",StringComparison.Ordinal)).Select(v=>v!.DeepClone()).ToArray());}
        RelayBox.WriteState(path,card);if(File.Exists(partial))File.Delete(partial);return result;
    }
    public static JsonObject Overview()
    {
        var result=new JsonObject();foreach(var app in new[]{"codex","antigravity"}){var card=Cards(app).FirstOrDefault();if(card is null){result[app]=new JsonObject{["status"]=RelayPaths.Tr("no map yet","pas encore de carte")};continue;}var marks=card["landmarks"] as JsonObject??new();result[app]=new JsonObject{["version"]=card["version"]?.DeepClone(),["surveyed_at"]=card["surveyed_at"]?.DeepClone(),["elements"]=(card["elements"] as JsonArray)?.Count??0,["missing"]=new JsonArray(marks.Where(p=>p.Value is null).Select(p=>p.Key).Order(StringComparer.Ordinal).Select(p=>(JsonNode?)JsonValue.Create(p)).ToArray()),["unverified"]=new JsonArray(marks.Where(p=>p.Value is JsonObject value&&value["path"] is null).Select(p=>p.Key).Order(StringComparer.Ordinal).Select(p=>(JsonNode?)JsonValue.Create(p)).ToArray())};}return result;
    }
    public sealed class Known(IEnumerable<string> labels)
    {
        private readonly HashSet<string> exact = labels.Where(s => s.Length > 0 && !s.Contains('{')).Select(s => s.Replace('\u00a0', ' ').Trim()).ToHashSet(StringComparer.Ordinal);
        private readonly string[] templates = labels.Select(s=>s.Replace('\u00a0',' ').Trim()).Where(s=>Regex.IsMatch(s,@"\$?\{[^{}]*\}")&&Regex.Split(s,@"\$?\{[^{}]*\}").Sum(p=>p.Trim().Length)>=6).OrderByDescending(s=>Regex.Replace(s,@"\$?\{[^{}]*\}","").Length).ThenByDescending(s=>s,StringComparer.Ordinal).ToArray();
        public string Canon(string value)
        {
            string text = value.Replace('\u00a0', ' ').Trim(); if (text.Length == 0 || exact.Contains(text)) return text;
            if (text.Contains('@')) return "*";
            foreach (string template in templates)
            {
                var parts = Regex.Split(template, @"\$?\{[^{}]*\}"); if (parts.Sum(s => s.Trim().Length) < 6) continue;
                if (Regex.IsMatch(text, "^" + string.Join(".+", parts.Select(Regex.Escape)) + "$", RegexOptions.Singleline, TimeSpan.FromSeconds(1))) return template;
            }
            return "*";
        }
        public JsonObject Mask(JsonObject raw)
        {
            var item = (JsonObject)raw.DeepClone(); item["aria"] = item["aria"] is null?null:Canon(item["aria"]!.ToString()); item["text"] = item["text"] is null?null:Canon(item["text"]!.ToString());
            string region = item["region"]?.ToString() ?? ""; int split = region.IndexOf(':'); item["region"] = split >= 0&&split<region.Length-1?region[..split] + ":" + Canon(region[(split + 1)..]):region.TrimEnd(':');
            item["data"]??=new JsonObject();if (item["data"] is JsonObject data) foreach (string key in data.Select(kv => kv.Key).ToArray())
            { string value = data[key]?.ToString() ?? ""; bool structural = key is "data-testid" or "data-sidebar-destination" or "data-slot" or "data-variant" or "data-color" && Regex.IsMatch(value, @"^[A-Za-z0-9:_\- ]{1,60}$"); if (!structural && value is not ("" or "*" or "true" or "false") && !exact.Contains(value)) data[key] = "*"; }
            if (item["href"]?.ToString() is string href && href.StartsWith('/')) { var segments = Regex.Split(href,"[?#]")[0].Split('/', StringSplitOptions.RemoveEmptyEntries); item["href"] = "/" + string.Join("/", segments.Select((s, i) => i == 0 && (exact.Contains(s) || exact.Contains("/" + s)) ? s : "*")); }
            return item;
        }
    }
    private static string Unescape(string value) => Regex.Replace(value, @"\\(x[0-9A-Fa-f]{2}|u\{[0-9A-Fa-f]+\}|u[0-9A-Fa-f]{4}|.)", m => m.Groups[1].Value switch { "n" => "\n", "r" => "\r", "t" => "\t", "b" => "\b", "f" => "\f", "v" => "\v", "0" => "\0", string s when s.Length > 1 && s[0] is 'x' or 'u' => s.Contains('{') ? char.ConvertFromUtf32(Convert.ToInt32(s[2..^1], 16)) : ((char)Convert.ToInt32(s[1..], 16)).ToString(), string s => s }, RegexOptions.Singleline, TimeSpan.FromSeconds(2));
    public static JsonObject CodexIndex(string asar, string language)
    {
        using var stream = File.Open(asar, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete); using var reader = new BinaryReader(stream, Encoding.UTF8, leaveOpen: true);
        byte[] head = reader.ReadBytes(16); if (head.Length < 16) throw new IOException("Truncated ASAR header");
        uint headerSize = BitConverter.ToUInt32(head, 4), length = BitConverter.ToUInt32(head, 12); if (length > 32 * 1024 * 1024) throw new IOException("ASAR header too large");
        if(BitConverter.ToUInt32(head,0)!=4||headerSize<8||length>headerSize-8||8L+headerSize>stream.Length)throw new IOException("ASAR header bounds invalid");
        var jsonHeader=reader.ReadBytes((int)length);if(jsonHeader.Length!=length)throw new IOException("Truncated ASAR header");
        var tree = JsonNode.Parse(jsonHeader) as JsonObject ?? throw new IOException("ASAR header invalid"); var result = new JsonObject();
        void Walk(JsonObject node, string prefix,int depth=0)
        {
            if(depth>64)throw new IOException("ASAR hierarchy exceeds bound");
            foreach (var kv in (node["files"] as JsonObject ?? new()).OrderBy(p=>p.Key,StringComparer.Ordinal)) if (kv.Value is JsonObject child)
            {
                string name = prefix + "/" + kv.Key;
                if (child["files"] is JsonObject) { Walk(child, name,depth+1); continue; }
                bool locale = name.StartsWith("/webview/assets/", StringComparison.Ordinal) && kv.Key.StartsWith(language + "-", StringComparison.Ordinal) && name.EndsWith(".js", StringComparison.Ordinal);
                bool menu = name == "/native-menu-locales/" + language + ".json";
                if ((!locale && !menu) || child["offset"] is null) continue;
                if(!long.TryParse(child["offset"]!.ToString(),out long offset)||child["size"] is not JsonValue lengthNode||!lengthNode.TryGetValue<int>(out int size)||offset<0||size<0||size>16*1024*1024||offset>stream.Length-(8L+headerSize)||size>stream.Length-(8L+headerSize)-offset)throw new IOException("ASAR locale asset bounds invalid");
                stream.Position = 8L + headerSize + offset;var asset=reader.ReadBytes(size);if(asset.Length!=size)throw new IOException("Truncated ASAR locale asset");string source = Encoding.UTF8.GetString(asset);
                if (menu) foreach (var entry in JsonNode.Parse(source) as JsonObject ?? new()) if (entry.Value is JsonValue v && v.TryGetValue<string>(out string? text)) result["menu:" + entry.Key] = text;
                if (locale) foreach (Match match in Regex.Matches(source, "\"([A-Za-z][\\w.\\-]*)\":(?:`((?:[^`\\\\]|\\\\.)*)`|\"((?:[^\"\\\\]|\\\\.)*)\")", RegexOptions.None, TimeSpan.FromSeconds(2))) result[match.Groups[1].Value] = Unescape(match.Groups[2].Success ? match.Groups[2].Value : match.Groups[3].Value);
            }
        }
        Walk(tree, ""); return result;
    }
    public static JsonObject Survey(string app, Action? heartbeat = null)
    {
        CheckApp(app);
        if (!AppTransport.Locks[app].Wait(0)) return AppTransport.Result("deferred", "Wake or tidy-up holds the app port");
        try
        {
            if (AppTransport.UserPresent(app) || AppTransport.EmergencyStop() || AppLifecycle.Busy(app)) return AppTransport.Result("deferred", "App is in use or an agent is working");
            string? url = AppTransport.PageUrl(app), executable = AppTransport.Executable(app);
            if (url is null || executable is null) return AppTransport.Result("deferred", "App page or installation unavailable");
            using var page = new CdpPage(url);
            if (AppTransport.Truth(page.Eval(Ui["draft"]!.ToString())) || (page.Eval(Ui["layers"]!.ToString())?.GetValue<int>() ?? 0) != 0 || app=="antigravity"&&AppTransport.Truth(page.Eval(Ui["agy_running"]!.ToString()))) return AppTransport.Result("deferred", "Existing draft, open menu or running turn is preserved");
            string version = FileVersionInfo.GetVersionInfo(executable).ProductVersion ?? File.GetLastWriteTimeUtc(executable).ToString("O");
            JsonObject index; var known = new HashSet<string>();
            if (app == "codex") { string language = page.Eval("document.documentElement.lang")?.ToString() ?? "fr-FR"; index = CodexIndex(Path.Combine(Path.GetDirectoryName(executable)!, "resources", "app.asar"), language); }
            else
            {
                const string bundle = """
                    (async()=>{const sources=await Promise.all([...document.scripts].map(e=>e.src).filter(Boolean).map(u=>fetch(u).then(r=>r.text()).catch(()=>'')));const index={},known=new Set();
                    const decode=s=>s.replace(/\\(x[0-9A-Fa-f]{2}|u\{[0-9A-Fa-f]+\}|u[0-9A-Fa-f]{4}|.)/gs,(_,c)=>c[0]==='x'||c[0]==='u'?String.fromCodePoint(parseInt(c.slice(1).replace(/[{}]/g,''),16)):({n:'\n',t:'\t',r:'\r',b:'\b',f:'\f',v:'\v',0:'\0'}[c]??c));
                    for(const s of sources){for(const m of s.matchAll(/"((?:[^"\\\n]|\\.){2,120})"|`((?:[^`\\]|\\.){2,120})`/g))known.add(decode(m[1]??m[2]));
                    for(const m of s.matchAll(/(?:"aria-label"|ariaLabel|title|label|placeholder|tooltip|tooltipContent|children)\s*:\s*"((?:[^"\\\n]|\\.){2,80})"/g)){const t=decode(m[1]).trim();if(/[A-Za-zÀ-ÿ]/.test(t)&&!/[{}<>=;]|^\w+[-_]\w+$|^[a-z]+$/.test(t))index[t]=t;}}return {index,known:[...known]}})()
                    """;
                var labels = page.Eval(bundle) as JsonObject ?? new(); index = labels["index"] as JsonObject ?? new(); known.UnionWith((labels["known"] as JsonArray ?? []).Select(n => n?.ToString()).OfType<string>());
            }
            var card = RunSurvey(app,version,index,known,new CardPage(page),()=>{heartbeat?.Invoke();return AppTransport.UserPresent(app)||AppTransport.EmergencyStop()||AppLifecycle.Busy(app);},milliseconds=>{heartbeat?.Invoke();Thread.Sleep(milliseconds);heartbeat?.Invoke();});
            var saved=Save(card);var summaryMarks=new JsonObject();foreach(var pair in card["landmarks"]!.AsObject())summaryMarks[pair.Key]=pair.Value?["path"]?.DeepClone()??pair.Value?["unverified"]?.DeepClone();var summary=new JsonObject { ["status"]="success",["app"]=app,["version"]=version,["elements"]=card["elements"]!.AsArray().Count,["labels"]=index.Count,["live"]=card["live"]!.DeepClone(),["landmarks"]=summaryMarks };
            foreach(var entry in saved)summary[entry.Key]=entry.Value?.DeepClone();summary["path"]=saved["card"]?.DeepClone();return summary;
        }
        catch (Exception e) when (e is IOException or RelayError or System.Net.WebSockets.WebSocketException or OperationCanceledException or System.Text.Json.JsonException) { return AppTransport.Result("failed", e.Message); }
        finally { AppTransport.Locks[app].Release(); }
    }
    // The same survey engine accepts a fixture page; production always supplies the guarded CdpPage adapter.
    public interface ICardPage
    {
        JsonNode? Eval(string expression);
        bool Press(string expression);
        JsonObject Call(string method,JsonObject? arguments=null);
        void Escape();
    }
    private sealed class CardPage(CdpPage page):ICardPage
    {
        public JsonNode? Eval(string expression)=>page.Eval(expression);
        public bool Press(string expression)=>page.Press(expression);
        public JsonObject Call(string method,JsonObject? arguments=null)=>page.Call(method,arguments);
        public void Escape()=>page.Escape();
    }
    public static JsonObject RunSurvey(string app,string version,JsonObject index,IEnumerable<string> labels,ICardPage page,Func<bool> blocked,Action<int>? sleep=null)
    {
        CheckApp(app);if(blocked())throw new RelayError("App in use or emergency stop active; no gesture sent");
        if(AppTransport.Truth(page.Eval(Ui["draft"]!.ToString()))||(page.Eval(Ui["layers"]!.ToString())?.GetValue<int>()??0)!=0||app=="antigravity"&&AppTransport.Truth(page.Eval(Ui["agy_running"]!.ToString())))throw new RelayError("Existing draft, open layer or running turn is preserved");
        var known=labels.ToHashSet();var pause=sleep??Thread.Sleep;
            known.UnionWith(index.Select(kv => kv.Value?.ToString()).OfType<string>()); var mask = new Known(known);
            var spec = Ui["apps"]![app] as JsonObject ?? new(); var elements = new JsonArray(); var pages = new JsonArray(); var visitedMenus = new HashSet<string>();
            string viewScript = "(()=>({href:location.href,current:[...document.querySelectorAll('[aria-current=page]')].map(e=>{const data=Object.fromEntries([...e.attributes].filter(a=>['data-sidebar-destination','data-app-action-sidebar-thread-id'].includes(a.name)).map(a=>[a.name,a.value]));return Object.keys(data).length?{data}:{data,aria:(e.getAttribute('aria-label')||'').trim(),text:(e.innerText||'').trim().split('\\n')[0].trim().slice(0,80)}}),layers:"+Ui["layers"]+"}))()";
            var before = page.Eval(viewScript); bool restored = false; string outcome = "complet";
            void Guard() { if (blocked()) throw new RelayError("User returned or emergency stop active"); }
            JsonArray Raw(string scope = "") => page.Eval("(" + Ui["snapshot"] + ")(" + AppTransport.Quote(scope) + ")") as JsonArray ?? [];
            bool jsOpen=AppTransport.Truth(spec["js_open"]);
            bool Press(JsonObject item) { Guard();if(Danger.IsMatch(Label(item)))return false;var expression="(()=>{const e="+Element(Locator(item))+";return e&&!new RegExp("+AppTransport.Quote(Ui["danger"]!.ToString())+",'i').test((e.getAttribute('aria-label')||e.getAttribute('title')||e.innerText||'').trim())?e:null})()";return jsOpen?AppTransport.Truth(page.Eval("(()=>{const e="+expression+";if(!e)return false;e.click();return true})()")):page.Press(expression); }
            void Close(JsonObject? trigger=null)
            {
                for (int i = 0; i < 6; i++) { if ((page.Eval(Ui["layers"]!.ToString())?.GetValue<int>() ?? 0) == 0) return; Guard();if(i==4&&trigger is not null)Press(trigger);else if(jsOpen)page.Eval("(()=>{const t=document.activeElement||document.body;t.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',code:'Escape',bubbles:true,cancelable:true}));return 1})()");else page.Escape(); pause(500); }
                throw new RelayError("A menu did not close with Escape");
            }
            bool Trigger(JsonObject item) => item["popup"]?.ToString() is "menu" or "true" || spec["triggers"] is JsonObject triggers && triggers.Count > 0 && triggers.All(kv => item["data"]?[kv.Key]?.ToString() == kv.Value?.ToString());
            List<JsonObject> ReadPage(List<string> path)
            {
                Guard();if(spec["reveal"]?.ToString() is {Length:>0} reveal){var point=page.Eval("(()=>{const e="+Element(new JsonObject{["css"]=reveal,["nth"]=0})+";if(!e)return null;const r=e.getBoundingClientRect();return r.width?[r.x+r.width/2,r.y+r.height/2]:null})()");if(point is JsonArray xy){Guard();page.Call("Input.dispatchMouseEvent",new(){["type"]="mouseMoved",["x"]=xy[0]!.DeepClone(),["y"]=xy[1]!.DeepClone()});pause(300);}}
                var raw = Raw().OfType<JsonObject>().ToList();
                void Add(IEnumerable<JsonObject> items, List<string> location) { foreach (var masked in Collapse(items.Select(mask.Mask))) { masked["path"] = new JsonArray(location.Select(s => JsonValue.Create(s)).ToArray()); elements.Add(masked); } }
                Add(raw, path);
                foreach (var trigger in raw.Where(Trigger).Where(e => !Danger.IsMatch(Label(e))))
                {
                    string identity = Locator(mask.Mask(trigger)).ToJsonString(); if (visitedMenus.Count >= 60 || !visitedMenus.Add(identity)) continue;
                    try
                    {
                        if (!Press(trigger)) continue; pause(800); var entries = Raw("[role=menu], [role=listbox], [role=dialog]").OfType<JsonObject>().ToList(); var here = path.Append("menu « " + mask.Canon(Label(trigger)) + " »").ToList(); Add(entries, here);
                        if (app == "codex") foreach (var submenu in entries.Where(e => e["popup"] is not null && e["role"]?.ToString().StartsWith("menuitem", StringComparison.Ordinal) == true && !Danger.IsMatch(Label(e))))
                        {
                            Guard(); string expression = Element(Locator(submenu));
                            if (page.Eval("(()=>{const e=" + expression + ";if(!e)return null;const r=e.getBoundingClientRect();return r.width?[r.x+r.width/2,r.y+r.height/2]:null})()") is not JsonArray xy) continue;
                            page.Call("Input.dispatchMouseEvent", new() { ["type"] = "mouseMoved", ["x"] = xy[0]!.DeepClone(), ["y"] = xy[1]!.DeepClone() }); pause(800); Add(Raw("[role=menu]").OfType<JsonObject>(), here.Append("sous-menu « " + mask.Canon(Label(submenu)) + " »").ToList());
                        }
                    }
                    finally { Close(trigger); }
                }
                return raw;
            }
            bool Allowed(JsonObject item, string selector) => !Danger.IsMatch(Label(item)) && (item["href"] is null || item["href"]!.ToString() == "#" || item["href"]!.ToString().StartsWith('/')) && AppTransport.Truth(page.Eval("(()=>{const e=" + Element(Locator(item)) + ";if(!e)return false;const h=e.getAttribute('href');return e.matches(" + AppTransport.Quote(selector) + ")&&(!h||h==='#'||/^\\/(?!\\/)/.test(h))&&!new RegExp("+AppTransport.Quote(Ui["danger"]!.ToString())+",'i').test((e.getAttribute('aria-label')||e.getAttribute('title')||e.innerText||'').trim())})()"));
            try
            {
                var initial = ReadPage(["vue de départ"]); string nav = spec["pages"]!.ToString(); var queue = new Queue<(JsonObject Target, List<string> Path, string Selector)>(initial.Where(e => Allowed(e, nav)).Select(e => (e, new List<string>(), nav))); var seen = new HashSet<string>();
                // Settings is a navigation entry exposed from the Codex app menu, never an arbitrary menu action.
                if (app == "codex") foreach (var trigger in initial.Where(Trigger).Where(e => !Danger.IsMatch(Label(e))))
                {
                    try { if (!Press(trigger)) continue; pause(800); foreach (var settings in Raw("[role=menu]").OfType<JsonObject>().Where(e => (spec["menu_pages"] as JsonArray ?? []).Any(v => v?.ToString() == Label(e)))) queue.Enqueue((new JsonObject { ["via_menu"] = trigger.DeepClone(), ["entry"] = settings.DeepClone() }, [], "[role^=menuitem]")); }
                    finally { Close(); }
                }
                while (queue.Count > 0 && pages.Count < 40)
                {
                    var next = queue.Dequeue(); var target = next.Target; string key = target.ToJsonString() + string.Join("/", next.Path); if (!seen.Add(key)) continue; Guard();
                    if (target["via_menu"] is JsonObject via) { if (!Press(via)) continue; pause(800); target = (JsonObject)target["entry"]!.DeepClone(); }
                    if (!Allowed(target, next.Selector) || !Press(target)) { Close(); continue; }
                    pause(1200); var path = next.Path.Append("page « " + mask.Canon(Label(target)) + " »").ToList(); pages.Add(string.Join(" > ", path)); var raw = ReadPage(path);
                    if (next.Path.Count == 0) { string sub = spec["subpages"]!.ToString(); foreach (var item in raw.Where(e => Allowed(e, sub))) queue.Enqueue((item, path, sub)); }
                }
            }
            catch (Exception e) when (e is RelayError or IOException or System.Net.WebSockets.WebSocketException or OperationCanceledException or System.Text.Json.JsonException) { outcome = "interrompu : " + e.Message; }
            finally
            {
                if (!blocked())
                {
                    try
                    {
                        Close();
                        for (int i = 0; i < pages.Count + 4 && !JsonNode.DeepEquals(page.Eval(viewScript), before); i++)
                        {
                            Guard(); var current = page.Eval(viewScript);
                            if (current?["href"]?.ToString() != before?["href"]?.ToString()) page.Eval("(()=>{history.pushState(null,''," + AppTransport.Quote(before?["href"]?.ToString()) + ");dispatchEvent(new PopStateEvent('popstate',{state:null}));return true})()");
                            else
                            {
                                bool pressed = false;
                                foreach (var previous in (before?["current"] as JsonArray ?? []).OfType<JsonObject>().Reverse()) if (!((current?["current"] as JsonArray ?? []).Any(e => JsonNode.DeepEquals(e, previous)))) { if (Press(previous)) { pressed = true; break; } }
                                if (!pressed) { var back = Raw().OfType<JsonObject>().Where(e => e["role"]?.ToString() == "button" && Label(e) is "Retour" or "Back").ToList(); if (back.Count != 1 || !Press(back[0])) break; }
                            }
                            pause(1200);
                        }
                        restored = JsonNode.DeepEquals(page.Eval(viewScript), before);
                    }
                    catch (Exception e) { outcome = "interrompu : " + e.Message; }
                }
            }
            var live = new JsonObject { ["status"] = outcome, ["pages"] = pages, ["menus"] = visitedMenus.Count, ["view_restored"] = restored, ["chat_open"] = (before?["current"] as JsonArray ?? []).OfType<JsonObject>().Any(e=>e["data"]?["data-app-action-sidebar-thread-id"] is not null) };
            var marks = LandmarkStatus(app,elements.OfType<JsonObject>(),live);
            return new JsonObject { ["app"] = app, ["version"] = version, ["surveyed_at"] = RelayBox.NowIso(), ["elements"] = elements, ["index"] = index.DeepClone(), ["landmarks"] = marks, ["live"] = live };
    }
    public static void RefreshDue(RelayBox box, Action? heartbeat = null)
    {
        var state = box.State("native-cards-check") ?? new(); double now = Reservations.Now();
        foreach (string app in new[] { "codex", "antigravity" })
        {
            if (now - (state[app]?.GetValue<double>() ?? 0) < 1800) continue;
            string? exe = AppTransport.Executable(app); if (exe is null) continue;
            string? version = FileVersionInfo.GetVersionInfo(exe).ProductVersion; var last = Cards(app).FirstOrDefault();
            if (version is not null && Fresh(last,version)) { state[app] = now; continue; }
            heartbeat?.Invoke();var result = Survey(app,heartbeat); state[app] = result["live"]?["status"]?.ToString() == "complet" && AppTransport.Truth(result["live"]?["view_restored"]) ? now : now - 1500;
        }
        box.SetState("native-cards-check", state);
    }
}
