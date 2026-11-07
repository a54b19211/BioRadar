"""Candidate groups: which of the materials the radar has seen may belong to one event, as a view over rows
that already exist (observations, the points of series_points, the evidence analyses read). There is no member
table: the analysis of a topic stores the member list of its display group in `analyses.result`
(docs/decisions/0012).

A group is the set of materials joined by an explicit basis (BASES) and by nothing else. A key match - the
same pathogen and region, seen within 30 days - joins nothing: it is a candidate link, listed apart, not
counted, and it never moves the group's start or end. A material may be a candidate of several groups; the
view names every group a material is involved in. Versions a Fabric backfill run created are background:
members, still readable, but not in the counts, the baseline or the first-seen time.

Everything is dated by when the radar could know it: a material by its `available_at`, a link by the later of
its two ends. A view is built from what was visible at its cutoff only, so a replay never shows a link that a
later material made."""

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from bioradar import db
from bioradar.config import Settings
from bioradar.research import topics
from bioradar.research.clock import utc

# what may confirm that two materials belong together; the order is the order of preference
BASES = ("same_record", "fabric_relation", "event_id", "text_reference")
RELATION_KINDS = ("reposts", "supplements", "corrects", "retracts")  # the Fabric relations that join
KEY_WINDOW = timedelta(days=30)  # how far apart a key match may be seen: a search range, not a merging rule
REGION_CLUES = 5  # the groups one material that names a place but no pathogen may be a candidate of
MAX_MATERIALS = 20000  # the newest materials of the 30 days a view reads; more is reported as truncated
PROVEN = ("checked", "carried")  # the states of a measurement dimension that make it known (domain.py)
CHUNK = 500


@dataclass(frozen=True)
class Material:
    """One seen version and what the grouping may use of it. Pathogen and region are known only from proven
    dimensions of a Fabric measurement; "" is not known."""

    version_id: str
    source: str
    record_id: str
    available_at: datetime
    activity_key: str
    background: bool = False
    independence: str = "unknown"  # "independent" only when Fabric says so
    pathogen: str = ""
    region: str = ""
    quote: str = ""  # the located sentence of the measurement they come from
    event_id: str = ""
    relations: tuple[tuple[str, str, str], ...] = ()  # (kind, target source, target record id)
    mentions: tuple[tuple[str, str], ...] = ()  # (source, record id) the text names outright


@dataclass(frozen=True)
class Link:
    """An explicit link of two materials, as old as the later of them."""

    a: str
    b: str
    basis: str
    at: datetime


@dataclass(frozen=True)
class Candidate:
    """Material `version_id` may belong to `group` because of `via`, a member of it: not a member."""

    version_id: str
    group: str
    via: str
    basis: str  # key_match | region_clue
    at: datetime


@dataclass
class Group:
    id: str
    kind: str  # event | unknown_cause (no member has both a pathogen and a region)
    members: list[Material]  # by availability; each joined by an explicit basis
    joined: dict[str, dict]  # version id -> basis, via, confirmed_at


def explicit_links(materials) -> list[Link]:
    """The links of these materials that have an explicit basis: versions of one record, one event id, a
    relation Fabric states, a record the text names outright."""
    ordered = sorted(materials, key=lambda m: (m.available_at, m.version_id))
    by_record, by_event = defaultdict(list), defaultdict(list)
    for m in ordered:
        by_record[(m.source, m.record_id)].append(m)
        if m.event_id:
            by_event[m.event_id].append(m)
    links = [
        Link(a.version_id, b.version_id, "same_record", b.available_at)
        for versions in by_record.values()
        for a, b in zip(versions, versions[1:])
    ]
    links += [
        Link(first.version_id, m.version_id, "event_id", m.available_at)
        for first, *rest in by_event.values()
        for m in rest
    ]
    for m in ordered:
        stated = [(source, record) for kind, source, record in m.relations if kind in RELATION_KINDS]
        for basis, refs in (("fabric_relation", stated), ("text_reference", m.mentions)):
            for ref in refs:
                target = by_record.get(ref, [None])[0]
                if target is not None and target.version_id != m.version_id:
                    at = max(m.available_at, target.available_at)
                    links.append(Link(m.version_id, target.version_id, basis, at))
    return links


def components(materials, links) -> list[list[Material]]:
    parent = {m.version_id: m.version_id for m in materials}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for link in links:
        a, b = find(link.a), find(link.b)
        if a != b:
            parent[max(a, b)] = min(a, b)
    parts = defaultdict(list)
    for m in materials:
        parts[find(m.version_id)].append(m)
    return list(parts.values())


def build_groups(materials, links) -> list[Group]:
    """The groups: the connected parts of the explicit links, every material in exactly one. A member's basis
    is its earliest link; the first material of a group is its own start (the same record as itself) and needs
    none. The id is stable: the first material's source and record."""
    earliest = {}
    for link in sorted(links, key=lambda x: (x.at, BASES.index(x.basis), x.a, x.b)):
        earliest.setdefault(link.a, (link.basis, link.b, link.at))
        earliest.setdefault(link.b, (link.basis, link.a, link.at))
    groups = []
    for part in components(materials, links):
        members = sorted(part, key=lambda m: (m.available_at, m.version_id))
        first = members[0]
        joined = {}
        for m in members:
            basis, via, at = ("same_record", None, m.available_at) if m is first else earliest[m.version_id]
            joined[m.version_id] = {"basis": basis, "via": via, "confirmed_at": at.isoformat()}
        kind = "event" if any(m.pathogen and m.region for m in members) else "unknown_cause"
        digest = hashlib.sha256(f"{first.source}\x1f{first.record_id}".encode()).hexdigest()[:8]
        groups.append(Group(f"G-{digest}", kind, members, joined))
    return groups


def candidate_links(materials, group_of) -> list[Candidate]:
    """What only a key makes of two materials of different groups. Pathogen and region both known and equal:
    a candidate of the other group. A place but no pathogen, and the located sentence: a candidate of the
    groups of that place, at most REGION_CLUES, the nearest in time first. Only groups with a pathogen and
    a region of their own are candidates of (a lead of unknown cause is not an existing group), and only
    materials seen within KEY_WINDOW of each other. A background material can be what a new one is a
    candidate of, never a candidate itself."""
    by_key, by_region = defaultdict(list), defaultdict(list)
    for m in materials:
        if m.region:
            by_region[m.region].append(m)
            if m.pathogen:
                by_key[(m.pathogen, m.region)].append(m)
    found = []
    for m in materials:
        if m.background or not m.region:
            continue
        if m.pathogen:
            pool, basis, cap = by_key[(m.pathogen, m.region)], "key_match", None
        elif m.quote:
            pool, basis, cap = by_region[m.region], "region_clue", REGION_CLUES
        else:
            continue
        own, nearest = group_of[m.version_id], {}
        for gap, _, n in sorted((abs(n.available_at - m.available_at), n.version_id, n) for n in pool):
            target = group_of[n.version_id]
            if gap <= KEY_WINDOW and target.kind == "event" and target is not own:
                nearest.setdefault(target.id, (gap, n))
        ranked = sorted(nearest.items(), key=lambda item: (item[1][0], item[0]))[:cap]
        found += [
            Candidate(m.version_id, gid, n.version_id, basis, max(m.available_at, n.available_at))
            for gid, (_, n) in ranked
        ]
    return found


def _day(at: datetime) -> str:
    return at.astimezone(UTC).date().isoformat()


def _counted(group: Group) -> list[Material]:
    return [m for m in group.members if not m.background]


def _row(m: Material) -> dict:
    return {
        "version_id": m.version_id,
        "source": m.source,
        "record_id": m.record_id,
        "available_at": m.available_at.isoformat(),
        "background": m.background,
    }


def _summary(group: Group, candidate_times: list[datetime]) -> dict:
    """What orders groups (task book 05 2.2): the deduplicated groups among the confirmed members, the
    earliest first seen of the visible materials (members and candidates, background left out), the stable id.
    Start and end are the confirmed members' alone: a candidate does not extend the group."""
    live = [m.available_at for m in _counted(group)]
    seen = live + candidate_times
    return {
        "id": group.id,
        "kind": group.kind,
        "confirmed_groups": len({m.activity_key for m in _counted(group)}),
        "first_seen_at": min(seen).isoformat() if seen else None,
        "start": min(live).isoformat() if live else None,
        "end": max(live).isoformat() if live else None,
    }


def _activity(group: Group, received: set[str], cutoff: datetime) -> dict:
    """The group's information activity on the cutoff's day against the radar's activity baseline (the rule
    the topic monitor uses: topics.previous_days, topics.baseline): the deduplicated groups (activity key) of
    its confirmed members, how many of them are proven independent and how many are not known to be. Reposts
    and Fabric's same-origin records are one group. Activity, not case incidence."""
    today = _day(cutoff)
    per_day = defaultdict(dict)  # day -> {activity key: proven independent}
    for m in _counted(group):
        day = per_day[_day(m.available_at)]
        day[m.activity_key] = day.get(m.activity_key, False) or m.independence == "independent"
    keys = per_day.get(today, {})
    proven = sum(keys.values())
    out = {"day": today, "groups": len(keys), "independent": proven, "unknown": len(keys) - proven}
    out["baseline"] = None
    before = topics.previous_days(received, today)
    if before is not None:
        mean, spread, threshold = topics.baseline([len(per_day.get(d, ())) for d in before])
        out["baseline"] = {
            "days": before,
            "mean": round(mean, 2),
            "sd": round(spread, 2),
            "threshold": round(threshold, 2),
            "above": len(keys) > threshold,
        }
    return out


def _candidate_row(c: Candidate, by_id, group_of) -> dict:
    return {
        **_row(by_id[c.version_id]),
        "basis": c.basis,
        "via": c.via,
        "candidate_at": c.at.isoformat(),
        "member_of": group_of[c.version_id].id,
    }


def _shared(shown: Group, mine, group_of, of_material) -> tuple[dict, set[str]]:
    """({version id: every group the material is involved in}, those groups' ids) for the materials of the
    display group - its members and its candidates - that are involved in more than one: a material has its
    own group and is a candidate of each group a key put it in."""
    shared = {}
    for version_id in [m.version_id for m in shown.members] + [c.version_id for c in mine]:
        roles = [{"group": group_of[version_id].id, "role": "confirmed"}]
        roles += [{"group": c.group, "role": "candidate", "basis": c.basis} for c in of_material[version_id]]
        if len(roles) > 1:
            shared[version_id] = roles
    return shared, {r["group"] for roles in shared.values() for r in roles} - {shown.id}


def view(materials, *, cutoff: datetime, anchors) -> dict | None:
    """The display group as of the cutoff: the group of the latest of the `anchors` (version ids) that is not
    background, with its members (explicit basis, when it was confirmed), its candidates (the key that made
    them, not members), its counts and baseline; `shared`: every material of it that is involved in more
    than one group, with all of them; `others`: those groups' ordering fields. None when the cutoff sees no
    anchor."""
    visible = [m for m in materials if m.available_at <= cutoff]
    live = [m for m in visible if m.version_id in anchors and not m.background]
    if not live:
        return None
    anchor = max(live, key=lambda m: (m.available_at, m.version_id))
    groups = build_groups(visible, explicit_links(visible))
    group_of = {m.version_id: g for g in groups for m in g.members}
    by_id = {m.version_id: m for m in visible}
    to_group, of_material = defaultdict(list), defaultdict(list)
    for c in candidate_links(visible, group_of):
        to_group[c.group].append(c)
        of_material[c.version_id].append(c)
    shown = group_of[anchor.version_id]
    shared, involved = _shared(shown, to_group[shown.id], group_of, of_material)

    def seen(gid):  # when the candidates of a group, background left out, were first seen
        return [by_id[c.version_id].available_at for c in to_group[gid] if not by_id[c.version_id].background]

    received = {_day(m.available_at) for m in visible if not m.background}
    return {
        "cutoff": cutoff.isoformat(),
        "display": shown.id,
        "group": {
            **_summary(shown, seen(shown.id)),
            "members": [{**_row(m), **shown.joined[m.version_id]} for m in shown.members],
            "candidates": [_candidate_row(c, by_id, group_of) for c in to_group[shown.id]],
            "activity": _activity(shown, received, cutoff),
        },
        "shared": shared,
        "others": [_summary(g, seen(g.id)) for g in sorted(groups, key=lambda g: g.id) if g.id in involved],
    }


def _chunks(values):
    values = list(values)
    return [values[i : i + CHUNK] for i in range(0, len(values), CHUNK)]


def _keys(session, s: Settings, ids, *, is_demo: bool) -> dict:
    """version id -> (pathogen, region, quote) from the series points of its Fabric measurements. A dimension
    is known only when proven and when the version's points agree on it; the coverage dimension is the only
    place the radar holds a region."""
    seen = defaultdict(lambda: {"disease": set(), "coverage": set()})
    quotes = {}
    for chunk in _chunks(ids):
        points = session.scalars(
            select(db.SeriesPoint)
            .where(
                db.SeriesPoint.workspace_id == s.workspace_id,
                db.SeriesPoint.is_demo == is_demo,
                db.SeriesPoint.version_id.in_(chunk),
            )
            .order_by(db.SeriesPoint.id)
        )
        for p in points:
            quotes.setdefault(p.version_id, p.quote)
            known = seen[p.version_id]
            for d in p.dims:
                if d.get("kind") in known and d.get("state") in PROVEN and d.get("norm"):
                    known[d["kind"]].add(d["norm"])

    def one(values):
        return next(iter(values)) if len(values) == 1 else ""

    return {v: (one(f["disease"]), one(f["coverage"]), quotes[v]) for v, f in seen.items()}


def _relations(session, ids, cutoff: datetime) -> dict:
    """version id -> the relations Fabric stated for it, as an analysis recorded them when it read it. Only
    what analyses had recorded by the cutoff: a later reading does not write back into an earlier moment."""
    found = defaultdict(set)
    for chunk in _chunks(ids):
        query = (
            select(db.Evidence.version_id, db.Evidence.meta)
            .join(db.Analysis, db.Analysis.id == db.Evidence.analysis_id)
            .where(db.Evidence.version_id.in_(chunk), db.Analysis.created_at <= cutoff)
        )
        for version_id, meta in session.execute(query):
            for r in (meta or {}).get("relations") or []:
                found[version_id].add((r["kind"], r["target_source"], r["target_record_id"]))
    return found


def load(session, s: Settings, *, is_demo: bool, cutoff: datetime, background) -> tuple[list[Material], bool]:
    """(the materials of the 30 days up to the cutoff, whether there were more than MAX_MATERIALS - the oldest
    are left out). A repost's relation comes from its activity key, the others from the evidence read."""
    rows = list(
        session.scalars(
            select(db.Observation)
            .where(
                db.Observation.workspace_id == s.workspace_id,
                db.Observation.is_demo == is_demo,
                db.Observation.available_at <= cutoff,
                db.Observation.available_at >= cutoff - KEY_WINDOW,
            )
            .order_by(db.Observation.available_at.desc(), db.Observation.id.desc())
            .limit(MAX_MATERIALS + 1)
        )
    )
    truncated, rows = len(rows) > MAX_MATERIALS, rows[:MAX_MATERIALS]
    ids = [o.version_id for o in rows]
    keys, relations = _keys(session, s, ids, is_demo=is_demo), _relations(session, ids, cutoff)
    materials = []
    for o in rows:
        own = f"record:{o.source}:{o.record_id}"
        key = o.activity_key or own
        stated = set(relations.get(o.version_id, ()))
        if key != own and key.startswith("record:"):
            _, source, record = key.split(":", 2)
            stated.add(("reposts", source, record))
        pathogen, region, quote = keys.get(o.version_id, ("", "", ""))
        materials.append(
            Material(
                version_id=o.version_id,
                source=o.source,
                record_id=o.record_id,
                available_at=utc(o.available_at),
                activity_key=key,
                background=o.version_id in background,
                pathogen=pathogen,
                region=region,
                quote=quote,
                relations=tuple(sorted(stated)),
            )
        )
    return materials, truncated


def for_topic(session, s: Settings, topic: db.Topic, *, cutoff: datetime, background) -> dict | None:
    """The display group of a topic as of the cutoff: the group of the topic's latest material that is not
    background. None when the topic has none in the 30 days before the cutoff."""
    materials, truncated = load(session, s, is_demo=topic.is_demo, cutoff=cutoff, background=background)
    own = select(db.Observation.version_id).where(db.Observation.topic_id == topic.id)
    anchors = set(session.scalars(own))
    found = view(materials, cutoff=cutoff, anchors=anchors)
    return None if found is None else {**found, "truncated": truncated}
