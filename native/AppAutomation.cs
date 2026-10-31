using System.Text.Json.Nodes;

namespace NexusRelay;

public static class AppAutomation
{
    public const string Composer = "[...document.querySelectorAll('[contenteditable=true],textarea')].filter(e=>e.getClientRects().length).pop()";
    public const string ComposerText = "(()=>{const e=" + Composer + ";return e?(e.value??e.innerText):null})()";
    private const string View = "[...document.querySelectorAll('[aria-current=page]')].map(e=>(e.getAttribute('aria-label')||e.innerText).trim())";
    private const string Rows = """
        (()=>{const out=[];for(const r of document.querySelectorAll('[data-app-action-sidebar-thread-row]')){
        if(!r.getClientRects().length)continue;const key=Object.keys(r).find(k=>k.startsWith('__reactFiber'));
        let f=key?r[key]:null,id=null;for(let i=0;i<12&&f&&!id;i++){const p=f.memoizedProps||{};if(typeof p.conversationId==='string')id=p.conversationId;f=f.return;}
        if(id)out.push({row:r.getAttribute('data-app-action-sidebar-thread-id'),id,active:r.getAttribute('data-app-action-sidebar-thread-active')==='true',selected:r.getAttribute('data-app-action-sidebar-thread-selected')==='true'});}return out})()
        """;
    private const string CurrentProject = "[...document.querySelectorAll('button,[role=button]')].filter(e=>e.getClientRects().length).map(e=>e.getAttribute('aria-label')||'').find(s=>s.startsWith('Changer de projet : ')||s.startsWith('Change project: '))||''";
    private const string ModeButton = "[...document.querySelectorAll('button[aria-haspopup=menu]')].find(e=>e.getClientRects().length&&/mode/i.test(e.getAttribute('aria-label')||'')&&['ChatGPT','Codex'].includes((e.innerText||'').trim()))";
    private static string? Selected(CdpPage page) => (page.Eval(Rows) as JsonArray)?.OfType<JsonObject>().FirstOrDefault(r => AppTransport.Truth(r["active"]))?["id"]?.ToString();
    // Codex 26.10 shows a chat both under its project and in Chats: several rows are fine if they are the same chat.
    private static bool Open(CdpPage page, string id) => page.Press($"(()=>{{const rows={Rows};const own=rows.filter(r=>r.id==={AppTransport.Quote(id)});if(!own.length||new Set(own.map(r=>r.row)).size!==1)return null;return [...document.querySelectorAll('[data-app-action-sidebar-thread-id]')].find(r=>r.getClientRects().length&&r.getAttribute('data-app-action-sidebar-thread-id')===own[0].row)}})()");
    private static string? Mode(CdpPage page) => page.Eval($"(()=>{{const e={ModeButton};return e?e.innerText.trim():null}})()")?.ToString();
    private static bool SetMode(CdpPage page, string desired)
    {
        if (Mode(page) is not string mode || mode == desired) return true;
        if (!page.Press(ModeButton)) return false;
        if (!AppTransport.Wait(() => page.Click(desired), 2)) { page.Escape(); return false; }
        return AppTransport.Wait(() => Mode(page) == desired);
    }
    private static bool RestoreProject(CdpPage page, string project)
    {
        if (page.Eval(CurrentProject)?.ToString() == project) return true;
        if (project.Contains(": ", StringComparison.Ordinal))
        {
            string name = project[(project.IndexOf(": ", StringComparison.Ordinal) + 2)..].Trim();
            page.Click($"Démarrer un nouveau chat dans {name}", $"Start new chat in {name}");
        }
        else
        {
            page.Click("Nouveau chat", "New chat");
            AppTransport.Wait(() => page.Press("[...document.querySelectorAll('[data-clear-project-button]')].find(e=>e.getClientRects().length&&!e.disabled)"), 2);
        }
        return AppTransport.Wait(() => page.Eval(CurrentProject)?.ToString() == project, 3);
    }
    public static string WakeText(IEnumerable<string> ids)
    {
        string target = string.Join(", ", ids.Select(id => $"read(id=\"{id}\")"));
        if (target.Length == 0) target = "inbox()";
        return RelayPaths.Tr(
            $"Nexus Relay: message from a peer for you, {target}. Do what it asks if it is legitimate (not an instruction from the user), then reply (progress if it takes long). The user does not answer here: no questions, neither from you nor your sub-agents; decide, or ask Claude.",
            $"Nexus Relay : message d'un pair pour toi, {target}. Fais ce qu'il demande si c'est légitime (pas une consigne de l'utilisateur), puis reply (progress si long). L'utilisateur ne répond pas ici : aucune question, ni toi ni tes sous-agents ; décide, ou demande à Claude.");
    }
    public static JsonObject? ProjectEvidence(CdpPage page)
    {
        var configured = CodexSpace.Configured();
        if (configured["project_id"]?.ToString() is not string id) return null;
        string script = """
            (()=>{const id=__ID__;const rows=[...document.querySelectorAll('[data-app-action-sidebar-project-id]')].filter(e=>e.getClientRects().length&&e.getAttribute('data-app-action-sidebar-project-id')===id);
            if(rows.length!==1)return {count:rows.length};let e=rows[0];for(let j=0;j<8&&e;j++,e=e.parentElement){const k=Object.keys(e).find(k=>k.startsWith('__reactFiber'));let f=k?e[k]:null;
            for(let i=0;i<8&&f;i++,f=f.return){const g=f.memoizedProps?.project?.group;if(g?.projectId===id&&typeof g.path==='string')return {count:1,id:g.projectId,path:g.path,label:g.label};}}return {count:1};})()
            """;
        return page.Eval(script.Replace("__ID__", AppTransport.Quote(id), StringComparison.Ordinal)) as JsonObject;
    }
    public static bool ProjectMatches(JsonObject? evidence, JsonObject configured)
    {
        if (evidence?["count"]?.GetValue<int>() != 1 || evidence["id"]?.ToString() != configured["project_id"]?.ToString() || evidence["label"]?.ToString() != configured["label"]?.ToString()) return false;
        return evidence["path"]?.ToString() is string path && RelayBox.PlainPath(path) == RelayBox.PlainPath(configured["path"]!.ToString());
    }
    public static JsonObject WakeCodex(RelayBox box, List<RelayMessage> messages, Action? beforeSubmit = null)
    {
        if (!AppTransport.Locks["codex"].Wait(0)) return AppTransport.Result("deferred", "Codex interface already in use by Relay");
        bool entered = false, inserted = false, userReturned = false, sidebarReopened = false;
        CdpPage? page = null;
        string? previous = null, beforeMode = null;
        string project = "", text = WakeText(messages.Select(m => m.Id));
        JsonNode? view = null;
        var result = AppTransport.Result("failed", "Codex wake not started");
        JsonObject Outcome(string status, string? reason = null) => result = AppTransport.Result(status, reason);
        bool Present() => userReturned |= AppTransport.UserPresent("codex") || AppTransport.EmergencyStop();
        try
        {
            if (Present()) return Outcome("deferred", "Julien is using Codex or emergency stop active");
            if (!CodexRouter.Ensure()) return Outcome("deferred", "Codex router is not ready; wake postponed");
            var ready = AppTransport.EnsureApp("codex");
            if (ready["status"]?.ToString() != "ready") return result = ready;
            page = new CdpPage(AppTransport.PageUrl("codex") ?? throw new RelayError("Codex main page missing"));
            previous = Selected(page); view = page.Eval(View); beforeMode = Mode(page); project = page.Eval(CurrentProject)?.ToString() ?? "";
            if (!SetMode(page, "Codex")) return Outcome("failed", "Codex mode not selected");
            var space = CodexSpace.Configured();
            bool explicitSpace = space["explicit"]?.GetValue<bool>() ?? false;
            string? topic = messages.FirstOrDefault()?.Topic;
            string? existing = topic is null ? null : box.TopicState(topic, "codex")?["conversation"]?.ToString();
            AppChat? known = existing is null ? null : AppSessions.CodexChats().FirstOrDefault(c => c.Id == existing && !c.Archived);
            if (known is not null && CodexSpace.Owns(known, box) && RelayBox.PlainPath(known.Cwd) != RelayBox.PlainPath(space["path"]!.ToString())) known = null; // Explicit execution-space migration: keep the verified old chat, start only in the chosen workspace.
            if (known is not null)
            {
                if (!CodexSpace.Owns(known, box) || RelayBox.PlainPath(known.Cwd) != RelayBox.PlainPath(space["path"]!.ToString())) return Outcome("failed", "Stored topic chat has no verified ownership in the configured execution space; nothing sent");
                if (!AppSessions.CodexTurn(known.Path).Finished) return Outcome("deferred", "Topic chat is still working");
                if (!Open(page, known.Id) || !AppTransport.Wait(() => Selected(page) == known.Id)) return Outcome("deferred", "Topic chat is not visible; no duplicate created");
            }
            else
            {
                string label = CodexSpace.Configured()["label"]!.ToString();
                string labels = System.Text.Json.JsonSerializer.Serialize(new[] { $"Démarrer un nouveau chat dans {label}", $"Start new chat in {label}" });
                string relayButton = $"[...document.querySelectorAll('button,[role=button]')].find(e=>e.getClientRects().length&&{labels}.includes((e.getAttribute('aria-label')||'').trim()))";
                if (explicitSpace)
                {
                    if (ProjectEvidence(page)?["count"]?.GetValue<int>() == 0)
                    { // Codex 26.10 lists projects only on Home: leave Plugins/Space/Scheduled first, the project identity is then checked as before.
                        page.Press("[...document.querySelectorAll('[data-sidebar-destination=\"builtin:home\"]')].find(e=>e.getClientRects().length)");
                        AppTransport.Wait(() => ProjectEvidence(page)?["count"]?.GetValue<int>() > 0, 4);
                    }
                    if (!ProjectMatches(ProjectEvidence(page), space)) return Outcome("failed", "Configured project ID/path do not match the visible app project; nothing typed");
                    string container = "[...document.querySelectorAll('[data-sidebar-project-container-id]')].filter(e=>e.getClientRects().length&&!e.parentElement?.closest('[data-sidebar-project-container-id]')&&e.getAttribute('data-sidebar-project-container-id')===" + AppTransport.Quote("project:" + space["project_id"]) + ")";
                    relayButton = $"(()=>{{const roots={container};if(roots.length!==1)return null;const buttons=[...roots[0].querySelectorAll('button,[role=button]')].filter(e=>e.getClientRects().length&&{labels}.includes((e.getAttribute('aria-label')||'').trim()));return buttons.length===1?buttons[0]:null}})()";
                    string oldProject = project.Contains(": ", StringComparison.Ordinal) ? project[(project.IndexOf(": ", StringComparison.Ordinal) + 2)..].Trim() : "";
                    // Only a draft with text is the user's to keep; the empty draft a freshly started Codex opens is replaced
                    // by the verified project button below (its identity is then proven before anything is typed).
                    if (previous is null && oldProject == label && !string.IsNullOrWhiteSpace(page.Eval(ComposerText)?.ToString()))
                        return Outcome("deferred", "An existing draft has the same label; its project identity cannot be proven, so it is preserved");
                }
                if (!AppTransport.Wait(() => AppTransport.Truth(page.Eval($"!!({relayButton})")), 4))
                {
                    page.Press("[...document.querySelectorAll('[data-sidebar-destination=\"builtin:home\"]')].find(e=>e.getClientRects().length)");
                    sidebarReopened = page.Click("Afficher la barre latérale", "Show sidebar");
                }
                if (!AppTransport.Wait(() => AppTransport.Truth(page.Eval($"!!({relayButton})")), 2) || Present() || !(page.Press(relayButton) || page.Activate(relayButton)))
                    return Outcome("failed", $"Configured Codex project {CodexSpace.Configured()["label"]} missing: {AppSessions.RelayDirectory}");
                if (explicitSpace && !AppTransport.Wait(() => Selected(page) is null && (page.Eval(CurrentProject)?.ToString() ?? "").EndsWith(": " + label, StringComparison.Ordinal)))
                    return Outcome("failed", "New draft in the configured project was not confirmed; nothing typed");
            }
            bool ContextValid() => !explicitSpace || ProjectMatches(ProjectEvidence(page), space) && (known is not null ? Selected(page) == known.Id : Selected(page) is null && (page.Eval(CurrentProject)?.ToString() ?? "").EndsWith(": " + space["label"], StringComparison.Ordinal));
            if (!AppTransport.Wait(() => page.Eval(ComposerText) is not null)) return Outcome("failed", "Codex input field missing");
            if (!string.IsNullOrWhiteSpace(page.Eval(ComposerText)?.ToString())) return Outcome("deferred", "Codex draft is not empty; nothing overwritten");
            if (!ContextValid()) return Outcome("failed", "Project context changed before insertion; nothing typed");
            if (Present()) return Outcome("deferred", "Julien returned to Codex");
            page.Eval($"(()=>{{const e={Composer};e.focus();return true}})()");
            inserted = true;
            page.Call("Input.insertText", new() { ["text"] = text });
            if (!AppTransport.Wait(() => page.Eval(ComposerText)?.ToString()?.Trim() == text.Trim())) return Outcome("failed", "Inserted text was not verified");
            if (Present()) return Outcome("deferred", "Julien returned before send");
            if (page.Eval(ComposerText)?.ToString()?.Trim() != text.Trim()) { inserted = false; return Outcome("failed", "Draft changed before send"); }
            if (!ContextValid()) return Outcome("failed", "Project identity changed before submission; nothing sent");
            beforeSubmit?.Invoke();
            entered = true;
            page.Call("Input.dispatchKeyEvent", new() { ["type"] = "rawKeyDown", ["key"] = "Enter", ["code"] = "Enter", ["windowsVirtualKeyCode"] = 13 });
            page.Call("Input.dispatchKeyEvent", new() { ["type"] = "char", ["key"] = "Enter", ["text"] = "\r" });
            page.Call("Input.dispatchKeyEvent", new() { ["type"] = "keyUp", ["key"] = "Enter", ["code"] = "Enter", ["windowsVirtualKeyCode"] = 13 });
            bool sent = AppTransport.Wait(() => page.Eval(ComposerText)?.ToString()?.Trim() != text.Trim() && AppTransport.Truth(page.Eval($"document.body.innerText.includes({AppTransport.Quote(messages[0].Id)})")), known is null ? 6 : 20);
            result = AppTransport.Result(sent ? "started" : "uncertain", sent ? null : "Enter sent without confirmed effect; never retried automatically");
            result["via"] = "native-cdp";
            if (!Present())
            {
                AppTransport.Wait(() => Selected(page) is string selected && selected != previous, 15);
                string? conversation = known?.Id ?? Selected(page);
                if (conversation is not null && conversation != previous)
                {
                    result["conversation"] = conversation;
                    if (topic is not null)
                    {
                        box.SetTopicState(topic, "codex", new() { ["conversation"] = conversation, ["created"] = RelayBox.NowIso() });
                        if (sent) CodexSpace.Remember(topic, conversation, box);
                    }
                }
            }
            return result;
        }
        catch (Exception e) when (e is IOException or RelayError or System.Net.WebSockets.WebSocketException or OperationCanceledException)
        { result = AppTransport.Result(entered ? "uncertain" : "failed", e.Message); return result; }
        finally
        {
            if (page is not null)
            {
                try
                {
                    if (inserted && result["status"]?.ToString() != "started") page.Eval($"(()=>{{const e={Composer};if(e&&(e.value??e.innerText).trim()==={AppTransport.Quote(text.Trim())}){{e.focus();document.execCommand('selectAll');document.execCommand('delete');return true}}return false}})()");
                    if (!Present())
                    {
                        result["project_restored"] = RestoreProject(page, project);
                        if (previous is not null) result["view_restored"] = Open(page, previous) && AppTransport.Wait(() => Selected(page) == previous, 3);
                        else if (view is JsonArray labels)
                            foreach (string label in labels.Select(n => n?.ToString() ?? "").Reverse()) if (page.Click(label)) break;
                        if (beforeMode is not null) result["mode_restored"] = SetMode(page, beforeMode);
                        if (sidebarReopened) result["sidebar_restored"] = page.Click("Masquer la barre latérale", "Hide sidebar");
                    }
                    else result["restore_skipped"] = "Julien returned to Codex";
                }
                catch (Exception e) { result["restore_error"] = e.Message; } // Boundary: restoring the user's view is best effort and reported, never thrown from finally.
                page.Dispose();
            }
            AppTransport.Locks["codex"].Release();
        }
    }
    public static JsonObject Inspect(string app, string? what = null)
    {
        if (!AppTransport.Locks[app].Wait(0)) return AppTransport.Result("deferred", $"{app} interface busy");
        try
        {
            string? url = AppTransport.PageUrl(app);
            if (url is null) return AppTransport.Result("unavailable", $"{app} DevTools page not available on port {AppTransport.Port(app)}");
            using var page = new CdpPage(url);
            // Read roles and control labels only, never a conversation body or a draft value.
            const string controls = "[...document.querySelectorAll('button,[role=button],[role=menuitem],[role=tab],a[href],textarea,[contenteditable=true]')].filter(e=>e.getClientRects().length).map(e=>({role:e.getAttribute('role')||({BUTTON:'button',A:'link',TEXTAREA:'textbox'}[e.tagName])||(e.isContentEditable?'textbox':''),label:e.getAttribute('aria-label')||e.getAttribute('title')||(['button','menuitem','tab'].includes(e.getAttribute('role'))||e.tagName==='BUTTON'?(e.innerText||'').split('\\n')[0]:'')})).slice(0,500)";
            var items = page.Eval(controls) as JsonArray ?? [];
            if (!string.IsNullOrWhiteSpace(what)) items = new JsonArray(items.OfType<JsonObject>().Where(e => e["label"]?.ToString()?.Contains(what, StringComparison.OrdinalIgnoreCase) == true).Select(e => e.DeepClone()).ToArray());
            var result = new JsonObject { ["status"] = "success", ["app"] = app, ["observed_at"] = RelayBox.NowIso(), ["hits"] = items, ["port"] = AppTransport.Port(app), ["page"] = "current visible controls" };
            if (app == "codex")
            {
                result["mode"] = Mode(page);
                result["relay_project_visible"] = items.OfType<JsonObject>().Any(e => (e["label"]?.ToString() ?? "").Contains("nouveau chat dans " + CodexSpace.Configured()["label"], StringComparison.OrdinalIgnoreCase) || (e["label"]?.ToString() ?? "").Contains("new chat in " + CodexSpace.Configured()["label"], StringComparison.OrdinalIgnoreCase));
                result["composer_visible"] = page.Eval($"!!({Composer})");
                result["execution_space"] = CodexSpace.Configured();
                result["project_evidence"] = ProjectEvidence(page);
            }
            return result;
        }
        catch (Exception e) { Console.Error.WriteLine(e); return AppTransport.Result("failed", e.Message); } // Boundary: automation failures become a "failed" result.
        finally { AppTransport.Locks[app].Release(); }
    }
    public static JsonObject TidyCodex(RelayBox box)
    {
        if (AppTransport.UserPresent("codex") || AppTransport.EmergencyStop() || !AppTransport.Locks["codex"].Wait(0)) return AppTransport.Result("deferred", "Codex in use");
        try
        {
            var chats = AppSessions.CodexChats().Where(c => !c.Archived && CodexSpace.Owns(c, box)).ToList();
            if (chats.Count == 0) return AppTransport.Result("nothing");
            using var page = new CdpPage(AppTransport.PageUrl("codex") ?? throw new RelayError("Codex debug port unavailable"));
            int archived = 0;
            foreach (var chat in chats)
            {
                string? topic = box.State("codex-owned")?["threads"]?[chat.Id]?["topic"]?.ToString() ?? box.All().Select(m => m.Topic).Distinct().FirstOrDefault(t => box.TopicState(t, "codex")?["conversation"]?.ToString() == chat.Id);
                if (topic is null) continue; // Only conversations owned by this mailbox may be archived.
                bool Eligible() => (topic is null || box.TopicIdle(topic, Reservations.Now(), 12 * 3600)) && AppSessions.CodexTurn(chat.Path) is { Finished: true, At: not null } turn && Reservations.Now() - turn.At >= 10;
                if (!Eligible() || AppTransport.UserPresent("codex") || Selected(page) == chat.Id) continue;
                var rows = page.Eval(Rows) as JsonArray ?? [];
                if (rows.OfType<JsonObject>().Count(r => AppTransport.Truth(r["selected"])) > 1) return AppTransport.Result("deferred", "Multiple Codex chats selected");
                var own = rows.OfType<JsonObject>().Where(r => r["id"]?.ToString() == chat.Id).ToList();
                if (own.Count != 1) continue;
                string row = $"[...document.querySelectorAll('[data-app-action-sidebar-thread-id]')].find(e=>e.getClientRects().length&&e.getAttribute('data-app-action-sidebar-thread-id')==={AppTransport.Quote(own[0]["row"]?.ToString())})";
                if (!Eligible() || AppTransport.UserPresent("codex") || AppTransport.EmergencyStop()) continue;
                bool clicked = page.Press($"(()=>{{const row={row};return row&&[...row.querySelectorAll('button,[role=button]')].find(e=>['Archiver le chat','Archive chat'].includes(e.getAttribute('aria-label')))}})()");
                if (!clicked && page.Press($"(()=>{{const row={row};return row&&row.querySelector('button[aria-haspopup=menu]')}})()"))
                {
                    if (!Eligible() || AppTransport.UserPresent("codex") || AppTransport.EmergencyStop()) { page.Escape(); continue; }
                    clicked = page.Click("Archiver", "Archive");
                }
                if (clicked && AppTransport.Wait(() => AppSessions.CodexChats().Any(c => c.Id == chat.Id && c.Archived), 8)) archived++;
            }
            return new() { ["status"] = archived > 0 ? "archived" : "nothing", ["count"] = archived };
        }
        catch (Exception e) { Console.Error.WriteLine(e); return AppTransport.Result("failed", e.Message); } // Boundary: automation failures become a "failed" result.
        finally { AppTransport.Locks["codex"].Release(); }
    }
    public static bool AntigravityTidyReady(RelayBox box, string topic, bool retired, AppTurn turn, double now)
    {
        if (retired) return turn.Finished || turn.At is null || now - turn.At >= 7200;
        if (!box.TopicFinishedFor("antigravity", topic) || !box.TopicIdle(topic, now, 12 * 3600)) return false;
        return turn.At is not null && (now - turn.At >= 7200 || turn.Finished && now - turn.At >= 10);
    }
    private static string ArchiveAntigravity(CdpPage page, string id, Func<bool> eligible)
    {
        if (!eligible() || AppTransport.UserPresent("antigravity") || AppTransport.EmergencyStop()) return "deferred";
        string rows = $"[...document.querySelectorAll('[data-testid=conversation-row-sidebar]')].filter(e=>e.getAttribute('data-cascade-id')==={AppTransport.Quote(id)})";
        if (page.Eval($"(()=>{{const rows={rows};if(rows.length!==1)return null;rows[0].scrollIntoView({{block:'nearest'}});const r=rows[0].getBoundingClientRect();return r.width?[r.x+r.width/2,r.y+r.height/2]:null}})()") is not JsonArray xy) return "absent";
        page.Call("Input.dispatchMouseEvent", new() { ["type"] = "mouseMoved", ["x"] = xy[0]!.DeepClone(), ["y"] = xy[1]!.DeepClone() });
        string button = $"(()=>{{const rows={rows};return rows.length===1?rows[0].querySelector('[data-testid=conversation-archive-button]'):null}})()";
        if (!AppTransport.Wait(() => AppTransport.Truth(page.Eval($"!!({button})")), 2) || !eligible() || AppTransport.UserPresent("antigravity") || AppTransport.EmergencyStop()) return "deferred";
        return page.Press(button) && AppTransport.Wait(() => !RelayPaths.ConversationAlive(id), 8) ? "archived" : "failed";
    }
    public static JsonObject TidyAntigravity(RelayBox box)
    {
        if (AppTransport.UserPresent("antigravity") || AppTransport.EmergencyStop() || !AppTransport.Locks["antigravity"].Wait(0)) return AppTransport.Result("deferred", "Antigravity in use");
        try
        {
            var topics = box.All().Select(m => m.Topic).Concat(Directory.Exists(Path.Join(box.Root, "topics")) ? Directory.EnumerateDirectories(Path.Join(box.Root, "topics")).Select(Path.GetFileName).OfType<string>() : []).Distinct().ToList();
            if (topics.Count == 0) return AppTransport.Result("nothing");
            string? url = AppTransport.PageUrl("antigravity");
            if (url is null) return AppTransport.Result("deferred", "Antigravity debug port unavailable");
            using var page = new CdpPage(url); int archived = 0;
            foreach (string topic in topics)
            {
                string? id = box.TopicState(topic, "agy")?["conversation"]?.ToString();
                var retired = box.TopicState(topic, "agy_retired")?["conversations"] as JsonArray ?? [];
                var left = new JsonArray();
                foreach (string old in retired.Select(n => n?.ToString()).OfType<string>().Distinct().Where(o => RelayPaths.IsConversationId(o) && RelayPaths.ConversationAlive(o)))
                {
                    if (old == id) { left.Add(old); continue; }
                    bool Ready() => AntigravityTidyReady(box, topic, true, AppSessions.AntigravityTurn(old), Reservations.Now());
                    if (ArchiveAntigravity(page, old, Ready) == "archived") archived++;
                    else left.Add(old);
                }
                if (retired.Count > 0) box.SetTopicState(topic, "agy_retired", new() { ["conversations"] = left });
                if (id is null || !RelayPaths.IsConversationId(id) || !RelayPaths.ConversationAlive(id)) continue;
                if (box.TopicState(topic, "agy_archived")?["conversation"]?.ToString() == id) continue;
                bool Eligible() => AntigravityTidyReady(box, topic, false, AppSessions.AntigravityTurn(id), Reservations.Now());
                if (!Eligible()) continue;
                string outcome = ArchiveAntigravity(page, id, Eligible);
                if (outcome == "archived") { archived++; box.SetTopicState(topic, "agy_archived", new() { ["conversation"] = id, ["at"] = RelayBox.NowIso(), ["status"] = "archived" }); }
            }
            page.Call("Input.dispatchMouseEvent", new() { ["type"] = "mouseMoved", ["x"] = 0, ["y"] = 0 });
            return new() { ["status"] = archived > 0 ? "archived" : "nothing", ["count"] = archived };
        }
        catch (Exception e) { Console.Error.WriteLine(e); return AppTransport.Result("failed", e.Message); } // Boundary: automation failures become a "failed" result.
        finally { AppTransport.Locks["antigravity"].Release(); }
    }
}
