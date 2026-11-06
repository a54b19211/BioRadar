"""What of a record a model or service gets: the reading window (adopt), the report input (excerpt,
report_text) and the notice for table rows sent without their header. Material in, Material out: every
character keeps where it came from (bioradar.material)."""

import re

from bioradar.material import Material
from bioradar.text import squash

# Said at the head of a window that holds table rows without their table's header: a fixed
# template with the count only. Those values must not be read by column name.
TABLE_HEADER_OMITTED = (
    "【表头未提供（table_header_omitted）：本窗口有 {n} 处表格行没有带上所在表格的表头（列名），"
    "这些行的数值不能按列名解读】\n"
)


_OMITTED = re.compile(r"^【表头未提供（table_header_omitted）：本窗口有 (\d+) 处")


def table_header_omitted(text):
    """How many table rows of a window went without their header (0 when the window does not say so)."""
    m = _OMITTED.match(text or "")
    return int(m.group(1)) if m else 0


def with_omitted(window, omitted):
    return Material.literal(TABLE_HEADER_OMITTED.format(n=omitted)) + window if omitted else window


def hit_spans(material, positions):
    """Display spans of a verified search hit's passage in this material: by the hit's bound
    identity - version, processor version and artifact hash - and its range in that artifact, mapped through
    the part's segments (a heading or notice before the text only shifts the display). Without a range, the
    hit text stands in only if it occurs exactly once. Returns (spans, unresolved reasons)."""
    spans, unresolved = [], []
    for p in positions:
        start, end = (p.get("range") or [None, None])[:2]
        if start is not None and end is not None and p.get("text_sha256"):
            part = next(
                (
                    q
                    for q in material.parts()
                    if q.get("kind") == "clean_text"
                    and q.get("version_id") == p["version_id"]
                    and q.get("processor_version") == p.get("processor_version")
                    and q.get("text_sha256") == p["text_sha256"]
                ),
                None,
            )
            found = material.spans_of(part, start, end) if part else []
            if found:
                spans += found
            else:
                unresolved.append("hit_artifact_not_in_material")
            continue
        words = (p.get("text") or "").split()
        matches = list(re.finditer(r"\s*".join(map(re.escape, words)), material.text)) if words else []
        if len(matches) == 1:
            spans.append(matches[0].span())
        else:
            unresolved.append("hit_position_ambiguous" if matches else "hit_position_not_found")
    return spans, unresolved


def merge_spans(spans):
    """[a, b) spans sorted and merged where they touch or overlap, as [a, b] lists."""
    out = []
    for a, b in sorted([list(s) for s in spans]):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def adopt(material, terms, limit, hits=()):
    """Whole text when short; otherwise the lines of the verified search hits first (`hits`: display spans,
    see hit_spans), then the lines that mention the search terms, in original order. A table row comes with
    its table's header rows - real slices of the same part (Material.table_header), counted in the budget; a
    row kept without them (no room, or not in this material) is said at the head of the window
    (TABLE_HEADER_OMITTED). Material in, Material out: every line kept keeps where it came from."""
    text = material.text
    if len(text) <= limit:
        return material, "full"
    paragraphs = [m.span() for m in re.finditer(r"[^\n]+", text) if m.group().strip()]
    must = list(dict.fromkeys(i for a, b in hits for i, (x, y) in enumerate(paragraphs) if x < b and y > a))
    rest = sorted(
        (i for i in range(len(paragraphs)) if i not in set(must)),
        key=lambda i: (-sum(text[slice(*paragraphs[i])].count(t) for t in terms), i),
    )
    keep, headers, omitted = fit_lines(material, paragraphs, must, rest, limit)
    if not keep:
        return material.slice(0, limit), "selected"
    merged = merge_spans(sorted(set(keep) | set(headers)))
    return with_omitted(
        Material.join("\n…\n", [material.slice(a, b) for a, b in merged]), omitted
    ), "selected"


def fit_lines(material, paragraphs, must, rest, limit):
    """(lines kept, header rows kept with them, rows kept without their header) within `limit`, the hit
    lines first: each line with its table's header rows when they fit, a hit line alone when they do
    not."""
    keep, headers, size, omitted = [], [], 0, 0
    for i in must + rest:
        a, b = paragraphs[i]
        if any(x <= a and b <= y for x, y in headers):
            continue  # a header line already taken with its table
        found = material.table_header(a, b)
        header = [s for s in found[0] if s not in headers] if found and found[1] else []
        cost = b - a + 2 + sum(y - x + 2 for x, y in header)
        if size + cost <= limit:
            keep.append((a, b))
            headers += header
            size += cost
            omitted += bool(found and not found[1])
        elif i in must and size + b - a + 2 <= limit:
            keep.append((a, b))  # the hit's own line stays; its header did not fit
            size += b - a + 2
            omitted += bool(found)
    return keep, headers, omitted


EXCERPT_CHARACTERS = 4000


def excerpt(material, quotes, limit=EXCERPT_CHARACTERS):
    """Report input: windows around the quoted facts, so BioCheck sees the context of every cited fact.
    Every quote goes in first; the room left then widens each quote, shortest first, to its whole line
    (a registry field's title-only quote such as "Total Energy Expenditure" is useless without the values
    on that line), and what is still left widens the quotes not yet widened (first 100 before
    and 900 after, since values follow their titles). Nothing quoted is dropped for context.
    Material in, Material out: each window keeps its original range."""
    text = material.text
    if len(text) <= limit:
        return material
    spans = quote_spans(text, quotes)
    fixed, omitted = quoted_headers(material, spans, limit)
    windows = widened(text, spans, fixed, limit)
    if not windows:
        return material.slice(0, limit)
    merged = merge_spans(windows + fixed)
    return with_omitted(Material.join("\n…\n", [material.slice(a, b) for a, b in merged]), omitted)


def quote_spans(text, quotes):
    """Where each quote is; quotes were checked with whitespace removed, so they are located the same way."""
    spans = []
    for quote in quotes:
        hit = re.search(r"\s*".join(map(re.escape, squash(quote))), text) if squash(quote) else None
        if hit:
            spans.append([hit.start(), hit.end()])
    return spans


def covered(windows, fixed):
    """Characters the windows and the fixed header rows take together."""
    return sum(b - a for a, b in merge_spans(windows + fixed))


def quoted_headers(material, spans, limit):
    """(header rows, quoted rows without one): a quoted table row needs its table's header - real slices
    of the same part, taken right after the quotes and before any widening; one that does not fit or is
    not in this material is said."""
    fixed, omitted = [], 0
    windows = [list(s) for s in spans]
    for a, b in spans:
        found = material.table_header(a, b)
        if not found:
            continue
        header = [list(s) for s in found[0] if list(s) not in fixed]
        if found[1] and covered(windows + header, fixed) <= limit:
            fixed += header
        elif not found[1] or header:
            omitted += 1
    return fixed, omitted


def line_of(text, a, b):
    end = text.find("\n", b)
    return [text.rfind("\n", 0, a) + 1, len(text) if end < 0 else end]


def line_length(text, a, b):
    start, end = line_of(text, a, b)
    return end - start


def widen(windows, i, a, b):
    """The windows with window i widened to [a, b) - never narrower than what it already has."""
    return windows[:i] + [[min(a, windows[i][0]), max(b, windows[i][1])]] + windows[i + 1 :]


def widened(text, spans, fixed, limit):
    """The quote windows widened as far as the room left allows: a quote without a number first to the
    numbers after it, then each to its whole line (shortest first), then the rest by fixed margins."""
    windows = [list(s) for s in spans]
    done = set()
    # a quote with no number in it (a table or field title) gets the numbers after it first
    for i, (a, b) in enumerate(spans):
        if not re.search(r"\d", text[a:b]):
            trial = widen(windows, i, a, min(line_of(text, a, b)[1], b + 900))
            if covered(trial, fixed) <= limit:
                windows = trial
    for i in sorted(range(len(spans)), key=lambda i: line_length(text, *spans[i])):
        trial = widen(windows, i, *line_of(text, *spans[i]))
        if covered(trial, fixed) <= limit:
            windows, _ = trial, done.add(i)
    # values follow their titles in records and tables, so the first try reaches further forward
    for before, after in ((100, 900), (400, 400), (150, 150)):
        for i, (a, b) in enumerate(spans):
            if i in done:
                continue
            trial = widen(windows, i, max(0, a - before), min(len(text), b + after))
            if covered(trial, fixed) <= limit:
                windows, _ = trial, done.add(i)
    return windows


# A trial registry's fields the writer gets whether or not a fact quoted them: what the trial
# is now and its primary results. Fabric's cleaner (v3 3.3) renders each field as a line
# "<名称> <field>:" with its nested values on the lines below as ASCII paths, so a field is the lines
# from its heading to the next.
REGISTRY_ESSENTIALS = (
    "status",
    "phase",
    "study_type",
    "enrollment",
    "enrollment_type",
    "start_date",
    "primary_completion_date",
    "primary_completion_date_type",
    "completion_date",
    "has_results",
)


RESULTS_FIELD = "results_outcome_measures"
FIELD_HEADING = re.compile(r"^[^\x00-\x7f]\S* ([a-z][a-z0-9_]*):", re.M)
MEASURE_LINE = re.compile(r"outcomeMeasures\[(\d+)\][ .]")
ESSENTIALS_SHARE = 0.6  # of the record's report characters; the rest is for the quoted passages


def lines_in(text, a, b):
    return [(a + m.start(), a + m.end()) for m in re.finditer(r"[^\n]+", text[a:b])]


def registry_lines(text):
    """The [start, end) of each line a writer must get from a trial record: the essential fields, and the
    primary end points' result lines (every outcomeMeasures[i] line of a measure typed PRIMARY)."""
    headings = list(FIELD_HEADING.finditer(text))
    fields = {
        h.group(1): (h.start(), headings[n + 1].start() if n + 1 < len(headings) else len(text))
        for n, h in enumerate(headings)
    }
    out = [span for name in REGISTRY_ESSENTIALS if name in fields for span in lines_in(text, *fields[name])]
    if RESULTS_FIELD in fields:
        results = lines_in(text, *fields[RESULTS_FIELD])
        measures = [(span, MEASURE_LINE.match(text, span[0])) for span in results]
        primary = {
            m.group(1)
            for (a, b), m in measures
            if m and text[m.end() - 1] == " " and re.search(r"\btype: PRIMARY\b", text[a:b])
        }
        out += [span for span, m in measures if m and m.group(1) in primary]
    return sorted(out)


def report_text(material, quotes, limit=EXCERPT_CHARACTERS):
    """A record's report input: for a trial registry record its essential fields and primary end points
    first (dropped from the end, with a note naming what was left out, when they do not fit), then the
    quoted passages in what is left; any other record goes to excerpt() unchanged. Material in and out."""
    text = material.text
    wanted = registry_lines(text) if len(text) > limit else []
    if not wanted:
        return excerpt(material, quotes, limit)
    kept, left_out, size = [], [], 0
    for a, b in wanted:
        if size + b - a + 1 <= limit * ESSENTIALS_SHARE:
            kept.append((a, b))
            size += b - a + 1
        else:
            heading = FIELD_HEADING.match(text, a)
            left_out.append(heading.group(1) if heading else text[a:b].split(" ", 1)[0])
    head = Material.join("\n", [material.slice(a, b) for a, b in kept])
    if left_out:
        names = "、".join(dict.fromkeys(left_out))
        head = head + Material.literal(f"\n（写稿输入放不下、未提供：{names}；原件里有）")
    rest, pos = [], 0
    for a, b in kept:
        rest.append(material.slice(pos, a))
        pos = b
    rest.append(material.slice(pos, len(text)))
    rest = Material.join("", rest)
    return head + Material.literal("\n…\n") + excerpt(rest, quotes, max(0, limit - len(head)))
