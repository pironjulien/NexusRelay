using System.Collections.Concurrent;
using System.Text.Json.Nodes;

namespace NexusRelay;

public sealed class RpcRequest : IDisposable
{
    private static readonly AsyncLocal<RpcRequest?> Local = new();
    private static readonly ConcurrentDictionary<string, CancellationTokenSource> Running = new();
    private readonly CancellationTokenSource cancelled = new();
    private readonly RpcRequest? previous;
    private readonly string key;
    private readonly Func<bool>? disconnected;
    private readonly Action<double, double>? progress;
    public static RpcRequest? Current => Local.Value;
    public RpcRequest(string scope, JsonNode id, Func<bool>? disconnected = null, Action<double, double>? progress = null)
    {
        key = scope + "|" + id.ToJsonString(); this.disconnected = disconnected; this.progress = progress;
        if (!Running.TryAdd(key, cancelled)) throw new RelayError("Request ID already active in this session");
        previous = Local.Value; Local.Value = this;
    }
    public static void Cancel(string scope, JsonNode? id) { if (id is not null && Running.TryGetValue(scope + "|" + id.ToJsonString(), out var source)) source.Cancel(); }
    public void Check() { if (disconnected?.Invoke() == true) cancelled.Cancel(); cancelled.Token.ThrowIfCancellationRequested(); }
    public void Progress(double elapsed, double total) { Check(); progress?.Invoke(elapsed, total); }
    public void Dispose() { Local.Value = previous; Running.TryRemove(key, out _); cancelled.Dispose(); }
}
