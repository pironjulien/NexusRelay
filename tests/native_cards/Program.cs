using NexusRelay;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text.Json.Nodes;

// JSON driver: real module + isolated files, and an optional fresh headless browser on a local fixture only.
Console.InputEncoding=new System.Text.UTF8Encoding(false);Console.OutputEncoding=new System.Text.UTF8Encoding(false);
string? line;while((line=Console.ReadLine()) is not null)
{
    try
    {
        var c=JsonNode.Parse(line)!.AsObject();var op=c["op"]!.ToString();JsonNode? result;
        string S(string name)=>c[name]?.ToString()??"";
        var card=c["card"] as JsonObject??new();var items=(c["items"] as JsonArray??[]).OfType<JsonObject>();
        result=op switch
        {
            "locator"=>NativeCards.Locator(c["item"]!.AsObject()),
            "template"=>JsonValue.Create(NativeCards.TemplateRegex(S("text"))),
            "mask"=>new JsonArray(items.Select(e=>(JsonNode?)new NativeCards.Known((c["labels"] as JsonArray??[]).Select(v=>v!.ToString())).Mask(e)).ToArray()),
            "collapse"=>new JsonArray(NativeCards.Collapse(items).Select(e=>(JsonNode?)e).ToArray()),
            "marks"=>NativeCards.LandmarkStatus(S("app"),items,c["context"] as JsonObject),
            "diff"=>NativeCards.Diff(c["before"]!.AsObject(),c["after"]!.AsObject()),
            "summary"=>JsonValue.Create(NativeCards.DiffSummary(c["diff"]!.AsObject())),
            "search"=>NativeCards.Search(card,S("app"),S("what"),c["limit"]?.GetValue<int>()??5),
            "where"=>NativeCards.Where(S("app"),S("what"),c["limit"]?.GetValue<int>()??5),
            "save"=>NativeCards.Save(card),
            "fresh"=>JsonValue.Create(NativeCards.Fresh(c["card"] as JsonObject,S("version"))),
            "overview"=>NativeCards.Overview(),
            "index"=>NativeCards.CodexIndex(S("path"),S("lang")),
            "browser"=>BrowserFixture.Run(S("path"),c),
            _=>throw new ArgumentException("Unknown fixture operation")
        };
        Console.WriteLine(new JsonObject{["result"]=result}.ToJsonString());
    }
    catch(Exception error){Console.WriteLine(new JsonObject{["error"]=error.GetType().Name,["message"]=error.Message}.ToJsonString());}
}

static class BrowserFixture
{
    public static JsonObject Run(string fixture,JsonObject configuration)
    {
        var chrome=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),"Google","Chrome","Application","chrome.exe");
        if(!File.Exists(chrome))throw new IOException("Chrome fixture executable unavailable");
        var listener=new TcpListener(IPAddress.Loopback,0);listener.Start();int port=((IPEndPoint)listener.LocalEndpoint).Port;listener.Stop();
        var profile=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"NexusRelay","card-contracts","browser",Guid.NewGuid().ToString("N"));Directory.CreateDirectory(profile);
        var start=new ProcessStartInfo(chrome){UseShellExecute=false,CreateNoWindow=true,WindowStyle=ProcessWindowStyle.Hidden};
        foreach(var arg in new[]{"--headless=new","--disable-gpu","--no-first-run","--no-default-browser-check","--disable-background-networking","--disable-component-update","--disable-sync","--disable-default-apps","--remote-debugging-address=127.0.0.1","--remote-debugging-port="+port,"--user-data-dir="+profile,new Uri(Path.GetFullPath(fixture)).AbsoluteUri})start.ArgumentList.Add(arg);
        using var process=Process.Start(start)??throw new IOException("Fixture browser did not start");
        try
        {
            using var http=new HttpClient{Timeout=TimeSpan.FromSeconds(1)};string? url=null;var until=DateTime.UtcNow.AddSeconds(15);
            while(DateTime.UtcNow<until&&url is null)
            {
                try{url=JsonNode.Parse(http.GetStringAsync($"http://127.0.0.1:{port}/json/list").GetAwaiter().GetResult())!.AsArray().OfType<JsonObject>().FirstOrDefault(t=>t["type"]?.ToString()=="page"&&t["url"]?.ToString().StartsWith("file:")==true)?["webSocketDebuggerUrl"]?.ToString();}catch(HttpRequestException){}catch(TaskCanceledException){}
                if(url is null)Thread.Sleep(100);
            }
            if(url is null)throw new IOException("Fixture browser endpoint missing");
            using var cdp=new CdpPage(url);while(cdp.Eval("document.readyState")?.ToString()!="complete")Thread.Sleep(20);
            var scenario=configuration["scenario"]?.ToString()??"normal";cdp.Eval("window.__scenario="+AppTransport.Quote(scenario));
            if(scenario=="draft")cdp.Eval("document.getElementById('draft').value='Synthetic unsent draft'");
            if(scenario=="running")cdp.Eval("(()=>{const b=document.createElement('button');b.setAttribute('aria-label','Stop execution');document.body.append(b)})()");
            var page=new ActualPage(cdp,scenario);var labels=new[]{"Accueil","Paramètres","Historique","Fichier","Lecture","Notifications","Aucun chat","Choisir modèle","Sécurité"};
            JsonObject? card=null;string? refusal=null;
            try{card=NativeCards.RunSurvey("antigravity","fixture-version",new JsonObject(),labels,page,()=>scenario=="user-return"&&(cdp.Eval("window.__events.length")?.GetValue<int>()??0)>0,ms=>Thread.Sleep(Math.Min(ms,30)));}
            catch(RelayError error){refusal=error.Message;}
            var events=cdp.Eval("window.__events")?.DeepClone();var view=cdp.Eval("({hash:location.hash,menu:document.getElementById('menu').hidden})");
            return new(){["card"]=card,["events"]=events,["view"]=view,["refusal"]=refusal};
        }
        finally{if(!process.HasExited){process.Kill(entireProcessTree:true);process.WaitForExit(5000);}}
    }
    private sealed class ActualPage(CdpPage page,string scenario):NativeCards.ICardPage
    {
        private bool broken;
        public JsonNode? Eval(string expression)
        {
            if(broken)throw new IOException("Fixture transport lost after the first gesture; outcome unknown");
            var value=page.Eval(expression);
            if(scenario=="transport-loss"&&expression.Contains("e.click()",StringComparison.Ordinal)&&AppTransport.Truth(value)){broken=true;throw new IOException("Fixture transport lost after gesture; outcome unknown");}
            return value;
        }
        public bool Press(string expression)=>page.Press(expression);
        public JsonObject Call(string method,JsonObject? arguments=null)=>page.Call(method,arguments);
        public void Escape()=>page.Escape();
    }
}
