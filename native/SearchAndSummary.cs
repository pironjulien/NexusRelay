using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;

namespace NexusRelay;

public sealed record TopicSummaryData(
    List<RelayMessage> Messages,
    Dictionary<string, int> Participants,
    List<RelayMessage> Open,
    List<(RelayMessage Ask, RelayMessage Answer)> Answered);

public static partial class SearchAndSummary
{
    public const int SnippetRadius = 89;
    public const int PreviewMax = 233;

    [GeneratedRegex(@"""([^""]+)""|(\S+)", RegexOptions.Compiled)]
    private static partial Regex QueryTermsRegex();

    [GeneratedRegex(@"\s+", RegexOptions.Compiled)]
    private static partial Regex WhitespaceRegex();

    public static (string Folded, List<int> Where) Fold(string text)
    {
        var sb = new StringBuilder(text.Length);
        var where = new List<int>(text.Length);
        int index = 0;
        foreach (var rune in text.EnumerateRunes())
        {
            foreach (string lower in rune.ToString().Normalize(NormalizationForm.FormD).EnumerateRunes().Where(c => Rune.GetUnicodeCategory(c) != UnicodeCategory.NonSpacingMark).Select(c => Rune.ToLowerInvariant(c).ToString()))
            {
                sb.Append(lower);
                for (int i = 0; i < lower.Length; i++) where.Add(index);
            }
            index += rune.Utf16SequenceLength;
        }
        return (sb.ToString(), where);
    }

    public static string Bare(string text)
    {
        return Fold(text).Folded;
    }

    public static List<string> Terms(string query)
    {
        var list = new List<string>();
        foreach (Match m in QueryTermsRegex().Matches(query))
        {
            var raw = m.Groups[1].Success ? m.Groups[1].Value : m.Groups[2].Value;
            var (folded, _) = Fold(raw.Trim());
            if (!string.IsNullOrEmpty(folded))
                list.Add(folded);
        }
        return list;
    }

    private static string CollapseSpaces(string text)
    {
        var parts = WhitespaceRegex().Split(text.Trim()).Where(s => s.Length > 0);
        return string.Join(" ", parts);
    }

    public static string Snippet(string text, int start)
    {
        int lo = Math.Max(0, start - SnippetRadius);
        int hi = Math.Min(text.Length, start + SnippetRadius);
        if (lo > 0 && lo < text.Length && char.IsLowSurrogate(text[lo])) lo--;
        if (hi > 0 && hi < text.Length && char.IsLowSurrogate(text[hi])) hi++;
        var prefix = lo > 0 ? "…" : "";
        var suffix = hi < text.Length ? "…" : "";
        return prefix + CollapseSpaces(text[lo..hi]) + suffix;
    }

    public static string Preview(string text)
    {
        var collapsed = CollapseSpaces(text);
        int end = PreviewMax < collapsed.Length && char.IsLowSurrogate(collapsed[PreviewMax]) ? PreviewMax + 1 : PreviewMax;
        return collapsed.Length > PreviewMax
            ? collapsed[..end] + "…"
            : collapsed;
    }

    private static int CountOccurrences(string text, string pattern)
    {
        if (string.IsNullOrEmpty(pattern))
            return 0;
        int count = 0;
        int pos = 0;
        while ((pos = text.IndexOf(pattern, pos, StringComparison.Ordinal)) >= 0)
        {
            count++;
            pos += pattern.Length;
        }
        return count;
    }

    public static (int Total, List<(RelayMessage Message, int Score, string Extract)> Hits) SearchPage(
        RelayBox box,
        string query,
        string? topic = null,
        string? sender = null,
        int? limit = null)
    {
        var wanted = Terms(query);
        if (wanted.Count == 0)
        {
            throw new RelayError(RelayPaths.Tr(
                "empty search: give at least one word",
                "recherche vide : donne au moins un mot"));
        }

        List<RelayMessage> pool;
        if (!string.IsNullOrEmpty(topic))
            pool = box.TopicMessages(topic);
        else if (!string.IsNullOrEmpty(sender))
            pool = box.SentBy(sender);
        else
            pool = box.All();

        if (!string.IsNullOrEmpty(sender) && !string.IsNullOrEmpty(topic))
        {
            var normSender = RelayPaths.NormalizeParty(sender);
            pool = pool.Where(m => m.Sender == normSender).ToList();
        }

        var scored = new List<(int Score, string Id, string Name, RelayMessage Message)>();
        foreach (var msg in pool)
        {
            var plain = Bare(msg.Text);
            int totalScore = 0;
            bool allMatch = true;
            foreach (int c in wanted.Select(term => CountOccurrences(plain, term)))
            {
                if (c == 0)
                {
                    allMatch = false;
                    break;
                }
                totalScore += c;
            }
            if (allMatch)
            {
                var fileName = $"{msg.Id}.{msg.Sender}.{msg.To}.{msg.Topic}.json";
                scored.Add((totalScore, msg.Id, fileName, msg));
            }
        }

        scored.Sort((a, b) =>
        {
            int cmp = b.Score.CompareTo(a.Score);
            if (cmp != 0) return cmp;
            cmp = string.CompareOrdinal(b.Id, a.Id);
            if (cmp != 0) return cmp;
            return string.CompareOrdinal(b.Name, a.Name);
        });

        int take = limit.HasValue ? Math.Min(limit.Value, scored.Count) : scored.Count;
        var hits = new List<(RelayMessage Message, int Score, string Extract)>(take);
        for (int i = 0; i < take; i++)
        {
            var item = scored[i];
            var (folded, where) = Fold(item.Message.Text);
            int first = wanted.Select(term => folded.IndexOf(term, StringComparison.Ordinal)).Where(idx => idx >= 0).DefaultIfEmpty(int.MaxValue).Min();
            int origStart = (first < where.Count && where.Count > 0) ? where[first] : 0;
            hits.Add((item.Message, item.Score, Snippet(item.Message.Text, origStart)));
        }

        return (scored.Count, hits);
    }

    public static List<(RelayMessage Message, int Score, string Extract)> Search(
        RelayBox box,
        string query,
        string? topic = null,
        string? sender = null)
        => SearchPage(box, query, topic, sender).Hits;

    public static TopicSummaryData Summary(RelayBox box, string topic)
    {
        var messages = box.TopicMessages(topic).OrderBy(m => m.Id, StringComparer.Ordinal).ToList();
        if (messages.Count == 0)
        {
            throw new RelayError(RelayPaths.Tr(
                $"unknown topic “{topic}” (no message)",
                $"sujet « {topic} » inconnu (aucun message)"));
        }

        var participants = new Dictionary<string, int>(StringComparer.Ordinal);
        var replies = new Dictionary<string, List<RelayMessage>>(StringComparer.Ordinal);
        foreach (var m in messages)
        {
            participants[m.Sender] = participants.GetValueOrDefault(m.Sender, 0) + 1;
            if (!string.IsNullOrEmpty(m.ReplyTo))
            {
                if (!replies.TryGetValue(m.ReplyTo, out var list))
                {
                    list = [];
                    replies[m.ReplyTo] = list;
                }
                list.Add(m);
            }
        }

        var openAsks = new List<RelayMessage>();
        var answered = new List<(RelayMessage Ask, RelayMessage Answer)>();
        foreach (var ask in messages.Where(m => !m.Info && string.IsNullOrEmpty(m.ReplyTo)))
        {
            var own = replies.GetValueOrDefault(ask.Id)?.Where(r => r.Sender == ask.To).ToList() ?? [];
            if (own.Count > 0)
                answered.Add((ask, own[0]));
            else
                openAsks.Add(ask);
        }

        return new TopicSummaryData(messages, participants, openAsks, answered);
    }
}
