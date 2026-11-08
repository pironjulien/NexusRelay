using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace NexusRelay;

/// <summary>
/// Source-generated JSON metadata for the few values the engine serializes directly (JSON or TOML string literals,
/// label lists injected into scripts): no reflection, Native AOT compatible. Everything else goes through JsonNode.
/// </summary>
[JsonSerializable(typeof(string))]
[JsonSerializable(typeof(string[]))]
internal sealed partial class RelayJsonContext : JsonSerializerContext
{
    /// <summary>Non-ASCII and HTML characters kept as they are (TOML strings written for people to read).</summary>
    public static RelayJsonContext Relaxed { get; } = new(new JsonSerializerOptions { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping });
}
