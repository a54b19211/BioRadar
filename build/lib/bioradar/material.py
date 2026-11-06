"""Outbound material with provenance (v3 6.1): what a model or a service actually received, bound so it can be
read back exactly after cleaners change.

A Material is a string made of segments. A segment is either a slice [start, end) of one fixed *part* — a
version's clean text from Fabric's processing layer, an attachment's parsed text, the radar's rendering of
Fabric's registry diff — or literal radar text (joiners, headings, notes), kept as written. Windows taken for
reading, for a report or for a service are slices and joins of the document's Material, so every window keeps
its original range in its part, however many times it is cut again.

binding() is what the radar stores for each outbound material: the whole string's sha256 and length, and for
each segment its part identity (kind, source, record, version, processor version, the part's text_sha256),
range and the slice's sha256, or the literal. restore() turns a stored binding plus the stored string back
into a Material (checked); reread() rebuilds the string from the parts themselves and checks every slice
and the whole, so a stored excerpt is never taken on its own word."""

import hashlib
import json
import re


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}")


def tables_of(text: str) -> list:
    """The markdown tables of a part's text, in its own offsets: each table's header (its first row and the
    separator row under it, with the newline after them) and its data rows [start, end). Rows run while lines
    start with "|"; a row followed by a separator row starts a new table, so two tables written back to back
    never share a header."""
    lines, pos = [], 0
    for line in text.split("\n"):
        lines.append((pos, pos + len(line), line))
        pos += len(line) + 1

    def heads(i):
        return (
            i + 1 < len(lines)
            and lines[i][2].startswith("|")
            and lines[i + 1][2].startswith("|")
            and bool(_TABLE_RULE.match(lines[i + 1][2].strip()))
        )

    out, i = [], 0
    while i < len(lines):
        if not heads(i):
            i += 1
            continue
        header_start, header_end = lines[i][0], min(lines[i + 1][1] + 1, len(text))
        j = i + 2
        while j < len(lines) and lines[j][2].startswith("|") and not heads(j):
            j += 1
        if j > i + 2:
            out.append(
                {
                    "header_start": header_start,
                    "header_end": header_end,
                    "start": header_end,
                    "end": lines[j - 1][1],
                }
            )
        i = j
    return out


class MaterialError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class Material:
    """Segments [(text, part or None, start)]: part None is literal radar text (fixed templates and separators
    only, never source text); otherwise text is part_text[start:start + len(text)] of the part named by the
    identity dict. `locators` keeps, per part, Fabric's locators of that part's text (segments with their
    ranges, e.g. the JSON pointer of a registry field); a binding names those that overlap each slice."""

    def __init__(self, segments=(), locators=None, tables=None):
        self.segments = []
        self.locators = {key: list(value) for key, value in (locators or {}).items()}
        # per part: its markdown tables (tables_of), so a cut window still knows each row's table header
        self.tables = {key: list(value) for key, value in (tables or {}).items()}
        for text, part, start in segments:
            self._add(text, part, start)

    @classmethod
    def part(cls, text: str, identity: dict, locators=()) -> "Material":
        """A whole part. The identity names it exactly; its text_sha256 is the hash of this text."""
        part = {**identity, "text_sha256": sha256(text)}
        tables = tables_of(text) if "|" in text else []
        return cls(
            [(text, part, 0)],
            {part_key(part): [dict(loc) for loc in locators]},
            {part_key(part): tables} if tables else None,
        )

    @classmethod
    def literal(cls, text: str) -> "Material":
        return cls([(text, None, 0)])

    @classmethod
    def unbound(cls, text: str, reason: str) -> "Material":
        """Text whose source is not known (read before bindings existed): bound to nothing; reread fails."""
        return cls.part(text, {"kind": "unbound", "reason": reason})

    def _add(self, text, part, start):
        if not text:
            return
        if self.segments:
            last_text, last_part, last_start = self.segments[-1]
            if part is None and last_part is None:
                self.segments[-1] = (last_text + text, None, 0)
                return
            if part is not None and last_part == part and last_start + len(last_text) == start:
                self.segments[-1] = (last_text + text, part, last_start)
                return
        self.segments.append((text, part, start))

    def _merge(self, other):
        for key, value in other.locators.items():
            mine = self.locators.setdefault(key, [])
            mine += [loc for loc in value if loc not in mine]
        for key, value in other.tables.items():
            mine = self.tables.setdefault(key, [])
            mine += [t for t in value if t not in mine]

    @property
    def text(self) -> str:
        return "".join(text for text, _, _ in self.segments)

    def __len__(self):
        return sum(len(text) for text, _, _ in self.segments)

    def __add__(self, other: "Material") -> "Material":
        out = Material(self.segments + other.segments, self.locators, self.tables)
        out._merge(other)
        return out

    def slice(self, a: int, b: int) -> "Material":
        """The characters [a, b) of this material, each keeping where it came from."""
        out, pos = Material(locators=self.locators, tables=self.tables), 0
        for text, part, start in self.segments:
            lo, hi = max(a, pos), min(b, pos + len(text))
            if lo < hi:
                out._add(text[lo - pos : hi - pos], part, start + lo - pos if part is not None else 0)
            pos += len(text)
            if pos >= b:
                break
        return out

    @staticmethod
    def join(separator: str, items) -> "Material":
        out = Material()
        for i, item in enumerate(items):
            if i:
                out._add(separator, None, 0)
            for segment in item.segments:
                out._add(*segment)
            out._merge(item)
        return out

    def at(self, pos: int):
        """(part, offset in that part) of the display character at pos; (None, None) for literal text."""
        at = 0
        for text, part, start in self.segments:
            if at <= pos < at + len(text):
                return (part, start + pos - at) if part is not None else (None, None)
            at += len(text)
        return None, None

    def spans_of(self, part: dict, a: int, b: int) -> list:
        """The display spans [x, y) that hold the part's characters [a, b) in this material, in order."""
        out, at = [], 0
        for text, p, start in self.segments:
            if p is not None and p == part:
                lo, hi = max(a, start), min(b, start + len(text))
                if lo < hi:
                    out.append((at + lo - start, at + hi - start))
            at += len(text)
        return out

    def table_header(self, a: int, b: int):
        """For a display span starting inside a table's data rows: (display spans of that table's header in
        this material, whether the whole header is there). None when the span is not in a table's rows."""
        part, offset = self.at(a)
        if part is None:
            return None
        for t in self.tables.get(part_key(part), []):
            if t["start"] <= offset < t["end"]:
                spans = self.spans_of(part, t["header_start"], t["header_end"])
                return spans, sum(y - x for x, y in spans) == t["header_end"] - t["header_start"]
        return None

    def parts(self):
        """The distinct parts this material draws on, in order of first use."""
        seen = []
        for _, part, _ in self.segments:
            if part is not None and part not in seen:
                seen.append(part)
        return seen

    def binding(self) -> dict:
        segments = []
        for text, part, start in self.segments:
            if part is None:
                segments.append({"literal": text})
                continue
            end = start + len(text)
            segment = {"part": part, "start": start, "end": end, "sha256": sha256(text)}
            locators = [
                loc
                for loc in self.locators.get(part_key(part), [])
                if loc["start"] < end and loc["end"] > start
            ]
            if locators:
                segment["locators"] = locators
            tables = [
                t for t in self.tables.get(part_key(part), []) if t["header_start"] < end and t["end"] > start
            ]
            if tables:
                segment["tables"] = tables
            segments.append(segment)
        return {"sha256": sha256(self.text), "chars": len(self), "segments": segments}


def of(doc: dict) -> Material:
    """A document's material (bioradar.adapters.fabric.document builds it); a document put together without
    one, e.g. a test fixture, is unbound text and claims no provenance."""
    return doc.get("material") or Material.unbound(doc.get("text") or "", "no_material")


def restore(text: str, binding: dict) -> Material:
    """The Material of a stored string from its stored binding; refuses a string it does not describe."""
    if sha256(text) != binding.get("sha256") or len(text) != binding.get("chars"):
        raise MaterialError("material_changed", "stored text does not match its binding")
    out, pos = Material(), 0
    for segment in binding["segments"]:
        if "literal" in segment:
            piece, part, start = segment["literal"], None, 0
        else:
            part, start = segment["part"], segment["start"]
            piece = text[pos : pos + segment["end"] - start]
            if sha256(piece) != segment["sha256"]:
                raise MaterialError("material_changed", "a segment does not match its binding")
        if text[pos : pos + len(piece)] != piece:
            raise MaterialError("material_changed", "a literal does not match its binding")
        out._add(piece, part, start)
        if part is not None and segment.get("locators"):
            out._merge(Material(locators={part_key(part): segment["locators"]}))
        if part is not None and segment.get("tables"):
            out._merge(Material(tables={part_key(part): segment["tables"]}))
        pos += len(piece)
    if pos != len(text):
        raise MaterialError("material_changed", "the binding does not cover the text")
    return out


def part_key(part: dict) -> str:
    return json.dumps(part, sort_keys=True, ensure_ascii=False)


def reread(binding: dict, resolve) -> str:
    """Rebuild a bound material from its parts and check it. resolve(parts) -> {part_key: full part text}
    (Fabric reads at the bound identities; a part it cannot give, e.g. revoked, is simply absent). Raises
    MaterialError: part_unreadable, part_changed (the part's text is not the bound text any more) or
    material_changed (a slice or the whole differs)."""
    parts = [s["part"] for s in binding["segments"] if "part" in s]
    texts = resolve(parts) if parts else {}
    out = []
    for segment in binding["segments"]:
        if "literal" in segment:
            out.append(segment["literal"])
            continue
        part = segment["part"]
        full = texts.get(part_key(part))
        if full is None:
            raise MaterialError("part_unreadable", part.get("kind", ""))
        if sha256(full) != part["text_sha256"]:
            raise MaterialError("part_changed", part.get("kind", ""))
        piece = full[segment["start"] : segment["end"]]
        if sha256(piece) != segment["sha256"]:
            raise MaterialError("material_changed", "slice")
        out.append(piece)
    text = "".join(out)
    if sha256(text) != binding["sha256"]:
        raise MaterialError("material_changed", "whole")
    return text
