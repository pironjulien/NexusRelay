using System.Text.Json.Nodes;

namespace NexusRelay;

/// <summary>
/// A request is delivered while it is current, never later. 14/11: a Claude request due on 11/11 at 20:42 stayed unread
/// (Antigravity's sidecar was down), and when Antigravity came back three days later its sidecar handed it over in a new
/// conversation: Antigravity worked on an obsolete question. Past its due time, or older than 12 hours (the delay after
/// which an idle topic is archived), a request nobody received expires instead: marked read so that no wake and no
/// sidecar delivers it any more, an "expiré" event, and an incident the sender sees in status, track and wait_reply.
/// </summary>
public static class RelayExpiry
{
    public const string Kind = "expiré";
    public const double MaxAgeSeconds = 12 * 3600.0;

    /// <summary>Why this message must no longer be delivered at <paramref name="now"/>, or null while it is current.</summary>
    public static string? Reason(RelayMessage message, double now)
    {
        // Information is never woken; a reply to Claude goes back through its Stop hook, never through a wake.
        if (message.Info || message.To is not ("codex" or "antigravity" or "claude") || message.To == "claude" && message.ReplyTo is not null) return null;
        if (message.ExtraObj["due"]?.ToString() is string due && AppSessions.Epoch(due) is double dueAt && now > dueAt)
            return RelayPaths.Tr($"Expired: due {due} passed before delivery", $"Expirée : échéance {due} dépassée avant livraison");
        if (AppSessions.Epoch(message.Created) is double created && now - created > MaxAgeSeconds)
            return RelayPaths.Tr("Expired: not delivered within 12 hours", "Expirée : non livrée en 12 heures");
        return null;
    }

    public static JsonObject? Event(RelayBox box, RelayMessage message)
        => box.Events(message.Id).FirstOrDefault(e => e["kind"]?.ToString() == Kind);

    /// <summary>
    /// Expires the message if it is stale, unread and no delivery of it is in flight. The claim on the attempt the
    /// sidecar would use makes the expiry and a delivery by Antigravity's sidecar (another process) exclusive.
    /// True when the message is expired, now or before.
    /// </summary>
    public static bool Expire(RelayBox box, RelayMessage message, double? at = null)
    {
        if (Event(box, message) is not null) return true;
        if (Reason(message, at ?? Reservations.Now()) is not string reason || box.IsRead(message) || box.HasEvent(message.Id, McpServer.CancelledKind)) return false;
        if (box.Claims(message.Id).Any(c => RelayBox.ClaimLabel(c.Claim) is "submitting" or "uncertain" or "claimed")) return false;
        int attempt = NativeSidecar.Attempt(box, message);
        if (box.Claim(message.Id, attempt, "expiry")) box.SetClaim(message.Id, attempt, "expiry", "expired", new() { ["reason"] = reason });
        else if (box.ClaimOf(message.Id, attempt) is not JsonObject held || RelayBox.ClaimLabel(held) != "delivered") return false;
        box.Event(message.Id, Kind, new() { ["reason"] = reason });
        var ring = box.RingState(message.Id) ?? new();
        ring["incident"] = reason; ring["incident_at"] = RelayBox.NowIso(); ring["expired"] = true;
        box.SetRingState(message.Id, ring);
        box.MarkRead(message);
        Journal.Note(message.Sender, "expiration", message.To, "✗", null, reason, message.Id, null);
        return true;
    }

    /// <summary>Expires every stale request still waiting for one of the three agents, so status shows it even when no
    /// wake or sidecar looks at it any more. Returns how many expired now.</summary>
    public static int Sweep(RelayBox box)
    {
        double now = Reservations.Now(); int count = 0;
        foreach (string agent in new[] { "codex", "antigravity", "claude" })
            foreach (var message in box.Unread(agent))
                if (Reason(message, now) is not null && Event(box, message) is null && Expire(box, message, now)) count++;
        return count;
    }
}
