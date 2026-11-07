"""Data-Fabric history API (徐子雯). The radar only reads fixed versions; it never writes Fabric tables."""

import json
import time
import uuid
from datetime import UTC, datetime

import httpx

from bioradar import guard
from bioradar.adapters.capabilities import CapabilityError, SourceCapabilities
from bioradar.config import Settings
from bioradar.material import Material, part_key

CURRENT = "current"
HISTORY = "public_history_reconstruction"


# Fabric answers for a fixed version that cannot be read with this permission, scope and cutoff
# (source_not_available: the source as a whole is not readable for this reader now - a revocation)
NOT_VISIBLE = {
    "version_not_found",
    "observation_basis_not_visible",
    "graph_evidence_not_visible",
    "source_not_available",
}


class FabricError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _time(value):
    """TimeValue {value, precision} -> ISO text, or None."""
    return value.get("value") if isinstance(value, dict) else value


UNVERIFIED_KINDS = {"social_media", "repost"}
NOT_RESEARCH = ("editorial", "letter", "comment", "news", "erratum", "retraction")
RESEARCH = {
    "research-article",
    "journal article",
    "clinical trial",
    "randomized controlled trial",
    "case reports",
}


def legacy_literature_kind(record: dict):
    """COMPATIBILITY, temporary: until Fabric classifies literature records itself (its document_evidence),
    this keeps the radar's earlier promotion so such records still reach MechEvidence.
    (material kind, text level) from the source's own metadata, for literature records that come
    without document_evidence. Europe PMC keeps its publication types in pubTypeList and the read text
    is the abstract. Unknown stays None; nothing is promoted to full text."""
    try:
        raw = json.loads(record.get("raw_content") or "{}")
    except (TypeError, ValueError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    types = [str(t).lower() for t in ((raw.get("pubTypeList") or {}).get("pubType") or [])]
    kind = None
    if any("review" in t for t in types):
        kind = "review"
    elif any(any(n in t for n in NOT_RESEARCH) for t in types):
        kind = None
    elif any("conference" in t or "meeting" in t for t in types):
        kind = "conference_abstract"
    elif any(t in RESEARCH for t in types):
        kind = "primary_research"
    return kind, ("abstract" if raw.get("abstractText") else None)


def attachment_heading(doc: dict, found: dict) -> str:
    """The line put above an attachment's parsed text: what it is and where it came from, so a quote points
    back to the attachment version and its page. A fixed template filled with identifiers and the parse
    method only, never source text: it is a literal segment of what a model reads (v3 6.1)."""
    return (
        f"【附件正文（附件版本 {doc['version_id'][:16]}，"
        f"所属页面 {doc.get('parent_record_id') or '?'}；"
        f"由 {found.get('parse_method') or '?'} 从原件解析，表格和扫描件的识别可能有误，以原件为准）】\n\n"
    )


def part(doc: dict, kind: str, **identity) -> dict:
    """The identity of one part of a document's text (v3 6.1): what to read back, from which fixed version."""
    return {
        "kind": kind,
        "source": doc["source"],
        "record_id": doc["record_id"],
        "version_id": doc["version_id"],
        "raw_sha256": doc.get("raw_sha256"),
        "is_demo": doc["is_demo"],
        **identity,
    }


def locators(clean: dict) -> list:
    """Fabric's locators of a cleaned text: each segment's range with where it points in the original."""
    return [
        {"start": s["start"], "end": s["end"], **{k: v for k, v in s.items() if k not in ("start", "end")}}
        for s in clean.get("segments") or []
        if isinstance(s, dict) and "start" in s and "end" in s
    ]


# Put above a version's own projection text when the cleaning layer's text is not there: a fixed
# template filled with the state code only.
UNCLEANED = "【清洗层正文不可用（{state}）：以下是该版本自带的投影正文，未经清洗，可能不完整】\n\n"


def noticed(window, state):
    """A window as sent: when the version's cleaned text was not there, the notice heads the window itself,
    whatever part of the text it kept (a long text cut to paragraphs or quotes loses its first line)."""
    if state in (None, "processed"):
        return window
    notice = UNCLEANED.format(state=state)
    return window if window.text.startswith(notice) else Material.literal(notice) + window


REGISTRY_CHANGE_LINES = 12
# The radar's rendering of a registry diff is part of what a model reads: its version is in the binding, and
# a change to registry_change_block() needs a new one so earlier bindings still read back.
REGISTRY_DIFF_RENDERER = "bioradar.registry_change_block.v1"


def registry_diff_part(doc: dict, diff: dict) -> dict:
    return part(
        doc,
        "registry_diff",
        processor_version=diff.get("processor_version"),
        from_version_id=diff.get("from_version_id"),
        renderer=REGISTRY_DIFF_RENDERER,
    )


def registry_change_block(diff: dict, version_id: str) -> str:
    """Fabric's field-by-field registry diff against the previous fixed version, as lines at the top of the
    text: which field, its JSON pointer, old value -> new value. Both version ids are named, so a quote from
    this block points back to the two records. Material changes first; it restates the registry only."""
    if diff.get("state") != "processed" or not diff.get("changes"):
        return ""
    changes = sorted(diff["changes"], key=lambda c: (not c.get("material"), c.get("field") or ""))
    lines = [
        "【与上一版相比的登记变化：这是程序比对两个固定版本得出的派生说明，不是本版原文"
        + f"（上一版 {diff.get('from_version_id') or '?'}，本版 {version_id}，"
        + f"处理器 {diff.get('processor_version') or '?'}）。只复述登记值；登记状态改变不等于药物失败、"
        + "疗效或安全结论；要确认具体内容，读两版原件】"
    ]
    for c in changes[:REGISTRY_CHANGE_LINES]:
        lines.append(
            ("· " if c.get("material") else "· （小修订）") + str(c.get("description") or c.get("field"))
        )
    if len(changes) > REGISTRY_CHANGE_LINES:
        lines.append(f"……另有 {len(changes) - REGISTRY_CHANGE_LINES} 处字段变化，见两版原件")
    return "\n".join(lines)


def _material_kind(record: dict, evidence: dict) -> tuple:
    """(material kind, text level): Fabric's document_evidence when present, else the temporary
    compatibility fallback for literature records, else the record kind."""
    native, level = legacy_literature_kind(record) if not evidence else (None, None)
    kind = evidence.get("material_kind") or native or record.get("record_kind")
    return kind, evidence.get("text_level") or level


def document(record: dict) -> dict:
    """The few fields the radar uses. Version identity stays exactly as Fabric returned it.

    `document_evidence` is Fabric's explicit material description: kind, text level, relations
    (corrects / retracts / supplements / reposts), same-origin grouping and information events
    (deletion, official denial, moderation, access failure)."""
    proof = record.get("availability_proof") or {}
    evidence = record.get("document_evidence") or {}
    kind, level = _material_kind(record, evidence)
    independence = evidence.get("source_independence") or {}
    doc = {
        "source": record["source_id"],
        "record_id": record["source_record_id"],
        "version_id": record["version_id"],
        "raw_sha256": record.get("raw_sha256"),
        "is_demo": record["is_demo"],
        "title": record.get("title") or "",
        "url": record.get("source_url"),
        "published_at": _time(record.get("source_published_at")) or _time(record.get("event_time")),
        "available_at": _time(proof.get("available_at")) or record.get("first_seen_at"),
        "material_kind": kind,
        "text_level": level,
        "verified": evidence.get("statement_status") != "unverified_report" and kind not in UNVERIFIED_KINDS,
        "relations": [
            {
                "kind": r["relation_type"],
                "target_source": r.get("target_source_id") or record["source_id"],
                "target_record_id": r["target_source_record_id"],
                "target_version_id": r.get("target_version_id"),
            }
            for r in evidence.get("document_relations", [])
        ],
        "origin_group": independence.get("origin_group_id")
        if independence.get("status") == "same_origin"
        else None,
        "events": [
            {"kind": e["event_type"], "description": e["description"]}
            for e in evidence.get("information_events", [])
        ],
        "seq": record.get("published_seq"),
        # a trial's own registry dates and status, for routing first adoptions (None for other records)
        "registry": {
            "status": (record.get("facts") or {}).get("status"),
            "results_first_posted": (record.get("facts") or {}).get("results_first_posted"),
            "updated_at": _time(record.get("source_updated_at")),
        }
        if record.get("record_kind") == "trial"
        else None,
        "registry_diff": None,  # filled by Fabric.attach_registry_changes for sources that declare the diff
        # an attachment version (Fabric legacy_site_crawler): its text comes from the processing layer
        "role": (record.get("facts") or {}).get("role"),
        "parent_record_id": (record.get("facts") or {}).get("parent_record_id"),
        "attachment_text": None,  # filled by Fabric.attach_attachment_text
        # program-derived text put into this document (registry diff block, parsed attachment text), with what
        # it was derived from, so the evidence record can point back to the inputs
        "derived": [],
        # what the cleaning layer gave for this version (Fabric.attach_clean_text): processed, or why not;
        # and which of its texts was chosen: the current processor's, or the artifact a search hit named
        "clean_text_state": None,
        "clean_text_choice": None,
        "artifact_wanted": None,  # set by read_versions for a search hit: {processor_version, text_sha256}
    }
    # Until the cleaning layer's text is attached, the text is the version's own projection text. `material`
    # is the text with where each character came from (bioradar.material); `text` is always material.text.
    doc["material"] = Material.part(
        record.get("clean_text") or "",
        part(doc, "projection_text", parser_version=record.get("parser_version")),
    )
    doc["text"] = doc["material"].text
    return doc


def _aware(value):
    """A timezone-aware datetime from a datetime or an ISO string, else None (never raises)."""
    try:
        text = None if isinstance(value, datetime) else str(value).replace("Z", "+00:00")
        at = value if text is None else datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo is not None and at.utcoffset() is not None else None


def fixed_version_is(doc, source, record_id, version_id, is_demo, cutoff=None) -> bool:
    """Whether a document returned for a fixed-version read is exactly what was named: the
    same source, record and version, an explicit boolean demo kind equal to the one asked for, and a
    timezone-aware available_at no later than the read's bound - its cutoff, or now for a live read. Anything
    missing, mistyped or malformed is not a verified read."""
    if not isinstance(doc, dict):
        return False
    if (doc.get("source"), doc.get("record_id"), doc.get("version_id")) != (source, record_id, version_id):
        return False
    if not isinstance(doc.get("is_demo"), bool) or doc["is_demo"] is not bool(is_demo):
        return False
    at = _aware(doc.get("available_at"))
    bound = datetime.now(UTC) if cutoff is None else _aware(cutoff)
    return at is not None and bound is not None and at <= bound


class Fabric:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(
            base_url=settings.fabric_url, timeout=30, event_hooks={"request": [lambda request: guard.check()]}
        )
        # HTTP requests actually sent, per endpoint: counts only, no parameters, tokens or texts
        self.requests = {}
        # what Fabric declared when this analysis or monitoring round began (load_capabilities): planning
        # and audit only, never a permission. None: not read yet, or Fabric could not say (unknown)
        self.caps: SourceCapabilities | None = None

    def _get(self, path, params, token=None):
        endpoint = path.removeprefix("/api/history/").split("/")[0]
        count = self.requests.setdefault(endpoint, {"n": 0, "errors": 0})
        count["n"] += 1
        try:
            response = self.client.get(
                path,
                params=params,
                headers={"Authorization": f"Bearer {token or self.settings.fabric_read_token}"},
            )
        except httpx.HTTPError:
            count["errors"] += 1
            raise
        if response.status_code >= 400:
            count["errors"] += 1
            try:
                error = response.json().get("error") or response.json().get("detail") or {}
            except ValueError:
                error = {}
            code = error.get("code") if isinstance(error, dict) else None
            raise FabricError(code or f"fabric_http_{response.status_code}", str(error)[:300])
        return response.json()

    def _post(self, path, body, token=None):
        endpoint = path.removeprefix("/api/history/").split("/")[0]
        count = self.requests.setdefault(endpoint, {"n": 0, "errors": 0})
        count["n"] += 1
        try:
            response = self.client.post(
                path,
                json=body,
                headers={"Authorization": f"Bearer {token or self.settings.fabric_read_token}"},
            )
        except httpx.HTTPError:
            count["errors"] += 1
            raise
        if response.status_code >= 400:
            count["errors"] += 1
            try:
                error = response.json().get("error") or response.json().get("detail") or {}
            except ValueError:
                error = {}
            code = error.get("code") if isinstance(error, dict) else None
            raise FabricError(code or f"fabric_http_{response.status_code}", str(error)[:300])
        return response.json()

    def semantic_search(self, query, *, scopes, is_demo, cutoff=None, now=None, top_k=10):
        """WeKnora recall verified by Fabric (POST /api/history/search). Every hit was checked at this mode
        and cutoff: source permission, the version visible then, and that the chunk is that version's fixed
        text. Returns (hits, summary): hits [{source, record_id, version_id, score, text, locators}] at the
        visible version; summary: excluded counts by reason, whether fewer than top_k came back, per-scope
        recall channels and index coverage. Missing hits do not mean the material does not exist."""
        mode = HISTORY if cutoff else CURRENT
        data = self._post(
            "/api/history/search",
            {
                "query": query[:2000],
                "mode": mode,
                "cutoff": (cutoff or now or datetime.now(UTC)).isoformat(),
                "scopes": list(scopes),
                "is_demo": is_demo,
                "top_k": top_k,
            },
        )
        hits = [
            {
                "source": r["source_id"],
                "record_id": r["source_record_id"],
                "version_id": r["version_id"],
                "score": r.get("rerank_score", r.get("recall_score")),
                "text": r["text"],
                "locators": r.get("locators") or [],
                "range": [r.get("start"), r.get("end")],
                # the fixed artifact the hit was verified against (processor version, full-text sha256; during
                # a processor handover an earlier processor's): reading must read exactly that one
                "artifact": r.get("artifact") or {},
            }
            for r in data.get("results") or []
        ]
        execution = data.get("execution") or {}
        summary = {
            "excluded": (data.get("excluded") or {}).get("counts") or {},
            "insufficient": execution.get("insufficient"),
            "candidates": execution.get("candidates"),
            "rerank": (execution.get("rerank") or {}).get("state"),
            "recall": [
                {
                    "scopes": item.get("source_scopes"),
                    **{
                        k: (item.get("execution") or {}).get(k)
                        for k in ("keyword", "vector", "embedding", "degraded", "degraded_reasons")
                    },
                }
                for item in execution.get("recall") or []
            ],
            "coverage": data.get("coverage") or {},
        }
        return hits, summary

    def search(self, source, terms, *, is_demo, cutoff=None, cursor=None, limit=10, record_ids=(), now=None):
        """One page of records matching all terms. cutoff=None means current mode at `now` (default: this
        moment); Fabric binds a page cursor to that time, so later pages must pass the same `now`."""
        params = {
            "source_id": source,
            "is_demo": str(is_demo).lower(),
            "mode": HISTORY if cutoff else CURRENT,
            "cutoff": (cutoff or now or datetime.now(UTC)).isoformat(),
            "limit": limit,
        }
        if terms:
            params["terms"] = list(terms)[:3]
        if record_ids:
            params["source_record_ids"] = list(record_ids)
        if cursor:
            params["cursor"] = cursor
        data = self._get("/api/history/records", params)
        docs = [document(item) for item in data["items"]]
        self.complete(docs, is_demo=is_demo, mode=params["mode"], cutoff=params["cutoff"])
        return docs, data.get("next_cursor")

    def read_versions(self, source, refs, *, is_demo, cutoff=None, now=None, artifacts=None):
        """Fixed versions [(record_id, version_id)] → (documents, versions no longer readable). Used where the
        radar owes work on one particular version (a deferred or failed triage): reading the record again
        would return its current head instead, and the owed version would never be done.
        artifacts {version_id: {processor_version, text_sha256}}: read that cleaned artifact of the version
        (a search hit's), not whatever the current processor made of it."""
        mode = HISTORY if cutoff else CURRENT
        at = (cutoff or now or datetime.now(UTC)).isoformat()
        docs, missing = [], []
        for record_id, version_id in refs:
            try:
                item = self._get(
                    f"/api/history/versions/{version_id}",
                    {
                        "source_id": source,
                        "source_record_id": record_id,
                        "is_demo": str(is_demo).lower(),
                        "mode": mode,
                        "cutoff": at,
                    },
                )
            except FabricError as error:
                if error.code not in NOT_VISIBLE:
                    raise  # transient (503, network): the owed work stays owed and is retried next round
                missing.append(version_id)  # revoked, withdrawn from view, or not visible at this cutoff
                continue
            try:
                doc = document(item)
            except (KeyError, TypeError, ValueError, AttributeError):
                # a raw record that cannot be read as a HistoryRecord is never that version
                raise FabricError("version_identity_mismatch", f"{record_id}@{version_id[:12]}") from None
            if not fixed_version_is(doc, source, record_id, version_id, is_demo, cutoff):
                # the read named one version: anything else is never that version's text
                raise FabricError("version_identity_mismatch", f"{record_id}@{version_id[:12]}")
            doc["artifact_wanted"] = (artifacts or {}).get(version_id) or None
            docs.append(doc)
        self.complete(docs, is_demo=is_demo, mode=mode, cutoff=at)
        return docs, missing

    def version_visible(self, source, record_id, version_id, *, is_demo, cutoff=None) -> bool:
        """Whether this exact fixed version can be read now (at the cutoff, for a replay): Fabric's
        current answer for the identity named, nothing else. False: revoked, withdrawn from view or not
        visible then. Raises FabricError / httpx.HTTPError when Fabric cannot answer (not an answer of
        "no")."""
        at = (cutoff or datetime.now(UTC)).isoformat()
        try:
            item = self._get(
                f"/api/history/versions/{version_id}",
                {
                    "source_id": source,
                    "source_record_id": record_id,
                    "is_demo": str(is_demo).lower(),
                    "mode": HISTORY if cutoff else CURRENT,
                    "cutoff": at,
                },
            )
        except FabricError as error:
            if error.code in NOT_VISIBLE:
                return False
            raise
        try:
            return fixed_version_is(document(item), source, record_id, version_id, is_demo, cutoff)
        except (KeyError, TypeError, ValueError, AttributeError):
            return False  # a raw record that cannot be read as a HistoryRecord is never that version

    def registry_changes(self, source, version_ids, *, is_demo, mode, cutoff):
        """Fabric's registry diff of each version against its previous version: {version_id: {state, ...}}."""
        data = self._get(
            "/api/history/registry-changes",
            {
                "source_id": source,
                "is_demo": str(is_demo).lower(),
                "mode": mode,
                "cutoff": cutoff,
                "version_ids": list(version_ids),
            },
        )
        versions = data.get("versions") or {}
        for value in versions.values():
            value.setdefault("processor_version", data.get("processor_version"))
        return versions

    def derived_text(self, source, version_ids, *, is_demo, mode, cutoff):
        """Fabric's processing-layer text of each version (attachments):
        {version_id: {state, status, text, ...}}."""
        data = self._get(
            "/api/history/derived-text",
            {
                "source_id": source,
                "is_demo": str(is_demo).lower(),
                "mode": mode,
                "cutoff": cutoff,
                "version_ids": list(version_ids),
            },
        )
        versions = data.get("versions") or {}
        for value in versions.values():
            value.setdefault("processor_version", data.get("processor_version"))
        return versions

    def clean_texts(self, source, version_ids, *, is_demo, mode, cutoff, processor_version=None):
        """Fabric's cleaning-layer text of each version (v3 3.3):
        {version_id: {state, clean: {processor_version, text, text_sha256, segments, status, ...}}};
        processor_version names an earlier processor's text."""
        params = {
            "source_id": source,
            "is_demo": str(is_demo).lower(),
            "mode": mode,
            "cutoff": cutoff,
            "version_ids": list(version_ids),
        }
        if processor_version:
            params["processor_version"] = processor_version
        return self._get("/api/history/clean-text", params).get("versions") or {}

    def complete(self, docs, *, is_demo, mode, cutoff):
        """What the radar reads of each version: the cleaning layer's text (or the parsed attachment),
        with the registry diff block on top for trials."""
        self.attach_clean_text(docs, is_demo=is_demo, mode=mode, cutoff=cutoff)
        self.attach_registry_changes(docs, is_demo=is_demo, mode=mode, cutoff=cutoff)
        self.attach_attachment_text(docs, is_demo=is_demo, mode=mode, cutoff=cutoff)

    def attachment_kind(self, doc):
        """ "text": an attachment version of a source whose attachment text Fabric's processing layer parses
        (its capabilities say so). "unknown": an attachment while Fabric's capabilities are unknown - its text
        is not read, and it waits. None: not an attachment, or one of a source with no attachment text."""
        if doc.get("role") != "attachment":
            return None
        if self.caps is None:
            return "unknown"
        return "text" if self.caps.attachment_text(doc["source"]) else None

    def attach_clean_text(self, docs, *, is_demo, mode, cutoff):
        """The text a model reads is the cleaning layer's text of the version (v3 3.3; for a trial the summary
        and every registry field, untruncated): the artifact a search hit named when there is one (exactly
        that processor and text hash, never a newer one in its place), otherwise the current processor's. Not
        processed, or not that artifact any more: the version's own projection text stays, `clean_text_state`
        says why and the text level says it is not cleaned text (never counted as cleaned or as
        full text). Attachment versions get their parsed text from attach_attachment_text unless a hit named
        their artifact."""
        groups = {}
        for d in docs:
            wanted = d.get("artifact_wanted") or {}
            if self.attachment_kind(d) and not wanted:
                continue
            groups.setdefault((d["source"], wanted.get("processor_version")), []).append(d)
        for (source, processor), group in groups.items():
            for start in range(0, len(group), 100):
                page = group[start : start + 100]
                try:
                    found = self.clean_texts(
                        source,
                        [d["version_id"] for d in page],
                        is_demo=is_demo,
                        mode=mode,
                        cutoff=cutoff,
                        processor_version=processor,
                    )
                except (FabricError, httpx.HTTPError):
                    found = {}
                for d in page:
                    entry = found.get(d["version_id"]) or {"state": "unavailable"}
                    clean = entry.get("clean") or {}
                    wanted = d.get("artifact_wanted") or {}
                    if wanted and clean and clean.get("text_sha256") != wanted.get("text_sha256"):
                        clean = {"status": "artifact_changed"}
                    if clean.get("status") == "parsed" and (clean.get("text") or "").strip():
                        body = Material.part(
                            clean["text"],
                            part(d, "clean_text", processor_version=clean["processor_version"]),
                            locators(clean),
                        )
                        is_attachment = self.attachment_kind(d) == "text"
                        method = {"parse_method": clean.get("basis")}
                        heading = attachment_heading(d, method) if is_attachment else ""
                        d["material"] = Material.literal(heading) + body
                        d["text"] = d["material"].text
                        d["clean_text_state"] = "processed"
                        d["clean_text_choice"] = "search_hit" if wanted else "current"
                        d["clean_meta"] = {
                            "parse_method": clean.get("basis"),
                            "locator_kind": clean.get("locator_kind"),
                        }
                    else:
                        state = clean.get("status") or entry.get("state") or "unavailable"
                        d["clean_text_state"], d["text_level"] = state, "uncleaned_projection"
                        # said inside the text: each receiver (triage, reader, BioCheck, MechEvidence) gets it
                        d["material"] = Material.literal(UNCLEANED.format(state=state)) + d["material"]
                        d["text"] = d["material"].text

    def part_texts(self, parts):
        """Read back the full text of each bound part (bioradar.material.reread): {part_key: text}, at
        the bound identity: the clean text of the bound processor version, the projection text of the
        bound parser, the registry diff of the bound processor rendered by the bound renderer. A part
        Fabric no longer gives (revoked, withdrawn, another processor's output only), or one bound to
        nothing (unbound), is left out; reread reports it."""
        now = datetime.now(UTC).isoformat()
        out = {}
        for p in parts:
            key = part_key(p)
            if key in out or p.get("kind") not in ("clean_text", "projection_text", "registry_diff"):
                continue
            common = {"is_demo": p["is_demo"], "mode": CURRENT, "cutoff": now}
            try:
                if p["kind"] == "clean_text":
                    entry = (
                        self.clean_texts(
                            p["source"], [p["version_id"]], processor_version=p["processor_version"], **common
                        ).get(p["version_id"])
                        or {}
                    )
                    clean = entry.get("clean") or {}
                    if clean.get("processor_version") == p["processor_version"]:
                        out[key] = clean.get("text") or ""
                elif p["kind"] == "projection_text":
                    record = self._get(
                        f"/api/history/versions/{p['version_id']}",
                        {
                            "source_id": p["source"],
                            "source_record_id": p["record_id"],
                            "is_demo": str(p["is_demo"]).lower(),
                            "mode": CURRENT,
                            "cutoff": now,
                        },
                    )
                    if record.get("parser_version") == p["parser_version"]:
                        out[key] = record.get("clean_text") or ""
                elif p["kind"] == "registry_diff" and p["renderer"] == REGISTRY_DIFF_RENDERER:
                    found = self.registry_changes(p["source"], [p["version_id"]], **common)
                    diff = found.get(p["version_id"]) or {}
                    if (diff.get("processor_version"), diff.get("from_version_id")) == (
                        p["processor_version"],
                        p["from_version_id"],
                    ):
                        out[key] = registry_change_block(diff, p["version_id"])
            except FabricError as error:
                if error.code not in NOT_VISIBLE:
                    raise
        return out

    def attach_attachment_text(self, docs, *, is_demo, mode, cutoff):
        """Give each attachment version its parsed text. Not parsed yet, failed, or Fabric unable to answer:
        the text stays empty and `attachment_text` says why; the monitor decides what to do with it."""
        by_source = {}
        for d in docs:
            kind = self.attachment_kind(d)
            if kind == "unknown":
                d["attachment_text"] = {"state": "capability_unknown"}
            elif kind == "text":
                by_source.setdefault(d["source"], []).append(d)
        for source, group in by_source.items():
            for start in range(0, len(group), 100):
                page = group[start : start + 100]
                try:
                    found = self.derived_text(
                        source, [d["version_id"] for d in page], is_demo=is_demo, mode=mode, cutoff=cutoff
                    )
                except (FabricError, httpx.HTTPError):
                    found = {}
                for d in page:
                    state = found.get(d["version_id"]) or {"state": "unavailable"}
                    d["attachment_text"] = {k: v for k, v in state.items() if k != "text"}
                    wanted = d.get("artifact_wanted")
                    if wanted:
                        # a search hit named this attachment's artifact: attach_clean_text read exactly it, or
                        # said it is not there; the current processor's text never takes its place.
                        # Read, it keeps its inputs: the artifact and the parent pages it belongs to.
                        if d.get("clean_text_choice") == "search_hit":
                            d["derived"] = list(d.get("derived") or []) + [
                                {
                                    "kind": "attachment_text",
                                    "attachment_version_id": d["version_id"],
                                    "processor_version": wanted.get("processor_version"),
                                    "text_sha256": wanted.get("text_sha256"),
                                    **(d.get("clean_meta") or {}),
                                    "attachment_of": state.get("attachment_of") or [],
                                }
                            ]
                    elif state.get("status") == "parsed" and (state.get("text") or "").strip():
                        # the parsed text is the attachment version's clean text in Fabric's processing layer
                        d["material"] = Material.literal(attachment_heading(d, state)) + Material.part(
                            state["text"],
                            part(d, "clean_text", processor_version=state.get("processor_version")),
                            locators(state),
                        )
                        d["text"] = d["material"].text
                        d["clean_text_state"] = "processed"
                        d["derived"] = list(d.get("derived") or []) + [
                            {
                                "kind": "attachment_text",
                                "attachment_version_id": d["version_id"],
                                "processor_version": state.get("processor_version"),
                                "text_sha256": state.get("text_sha256"),
                                "parse_method": state.get("parse_method"),
                                "locator_kind": state.get("locator_kind"),
                                "attachment_of": state.get("attachment_of") or [],
                            }
                        ]

    def attach_registry_changes(self, docs, *, is_demo, mode, cutoff):
        """Put each trial version's diff against the previous version on the document: `registry_diff`, a
        block at the top of the text, and one event per material change. Only for the sources Fabric
        declares a registry diff for (capabilities). When Fabric cannot answer (an older Fabric without the
        endpoint, a timeout) the documents stay exactly as before, marked unavailable, so reading never
        fails because of the diff."""
        by_source = {}
        for d in docs:
            if self.caps is None and d.get("registry") is not None:
                # Fabric's capabilities are unknown: a trial gets no diff, and it says so
                d["registry_diff"] = {"state": "capability_unknown"}
            elif self.caps is not None and self.caps.registry_diff(d["source"]):
                by_source.setdefault(d["source"], []).append(d)
        for source, group in by_source.items():
            for start in range(0, len(group), 100):
                page = group[start : start + 100]
                try:
                    found = self.registry_changes(
                        source, [d["version_id"] for d in page], is_demo=is_demo, mode=mode, cutoff=cutoff
                    )
                except (FabricError, httpx.HTTPError):
                    found = {}
                for d in page:
                    diff = found.get(d["version_id"]) or {"state": "unavailable"}
                    d["registry_diff"] = diff
                    block = registry_change_block(diff, d["version_id"])
                    if block:
                        d["material"] = (
                            Material.part(block, registry_diff_part(d, diff))
                            + Material.literal("\n\n")
                            + d["material"]
                        )
                        d["text"] = d["material"].text
                        d["derived"] = list(d.get("derived") or []) + [
                            {
                                "kind": "registry_diff",
                                "processor_version": diff.get("processor_version"),
                                "from_version_id": diff.get("from_version_id"),
                                "to_version_id": d["version_id"],
                                "block_chars": len(block),
                                "fields": [
                                    {
                                        k: c.get(k)
                                        for k in ("field", "json_pointer", "change_kind", "material")
                                    }
                                    for c in diff.get("changes", [])
                                ],
                            }
                        ]
                        d["events"] = list(d["events"]) + [
                            {"kind": "registry_change", "description": c.get("description") or c["field"]}
                            for c in diff.get("changes", [])
                            if c.get("material")
                        ]

    def graph_resolve(self, doc, name, *, cutoff=None):
        """Tang's reviewed graph (read only): entity candidates for an exact name in one graph record, each
        with its identity status and the fixed record versions it was read from (node evidence)."""
        return self._get(
            f"/api/history/graph/{doc['version_id']}/resolve",
            {
                "source_id": doc["source"],
                "source_record_id": doc["record_id"],
                "is_demo": str(doc["is_demo"]).lower(),
                "mode": HISTORY if cutoff else CURRENT,
                "cutoff": (cutoff or datetime.now(UTC)).isoformat(),
                "name": name,
            },
        )

    def changes(self, source, *, is_demo, after=0, limit=100):
        data = self._get(
            "/api/history/changes",
            {"source_id": source, "is_demo": str(is_demo).lower(), "after": after, "limit": limit},
        )
        items = [document(item) for item in data["items"]]
        now = datetime.now(UTC).isoformat()
        self.complete(items, is_demo=is_demo, mode=CURRENT, cutoff=now)
        return items, max([after] + [item["seq"] or 0 for item in items])

    def head(self, source, *, is_demo):
        """The feed's current last sequence number."""
        data = self._get(
            "/api/history/changes",
            {"source_id": source, "is_demo": str(is_demo).lower(), "after": 0, "limit": 1},
        )
        return data.get("max_seq") or 0

    def source_health(self, source):
        """Fabric's own record of its crawl tasks for this source (state, last success/failure, scope)."""
        return self._get("/api/history/source-health", {"source_id": source})

    def latest(self, source, *, is_demo):
        """The newest record of a feed, or None."""
        head = self.head(source, is_demo=is_demo)
        if not head:
            return None
        items, _ = self.changes(source, is_demo=is_demo, after=head - 1, limit=1)
        return items[0] if items else None

    def sources(self):
        return self._get("/api/history/sources", {})

    def capabilities(self) -> SourceCapabilities:
        """One read of Fabric's source list, parsed. FabricError / httpx.HTTPError when it cannot be read,
        CapabilityError when it is not a receipt this client understands."""
        return SourceCapabilities.parse(self.sources())

    def load_capabilities(self) -> SourceCapabilities | None:
        """Read the capabilities once for the analysis or monitoring round that starts now and keep them for
        planning (what to fetch, which text to expect). Not read: None, and every capability is unknown (there
        is no built-in list to fall back to)."""
        try:
            self.caps = self.capabilities()
        except (FabricError, httpx.HTTPError, CapabilityError):
            self.caps = None
        return self.caps

    def fetch(self, source, query, *, max_records=10, deadline_seconds=60):
        """Ask Fabric to collect fresh public records, wait for the run, return the new documents."""
        response = self.client.post(
            "/api/history/fetch",
            json={
                "source_id": source,
                "query": query,
                "max_records": max_records,
                "deadline_seconds": deadline_seconds,
            },
            headers={
                "Authorization": f"Bearer {self.settings.fabric_admin_token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        if response.status_code >= 400:
            raise FabricError(f"fetch_http_{response.status_code}", response.text[:300])
        run = response.json()
        deadline = time.monotonic() + deadline_seconds + 30
        while run["state"] not in {"completed", "failed", "partial"} and time.monotonic() < deadline:
            time.sleep(2)
            run = self._get(f"/api/history/runs/{run['id']}", {}, self.settings.fabric_admin_token)
        if run["state"] == "failed":
            raise FabricError("fetch_failed", str(run.get("error"))[:300])
        data = self._get(
            "/api/history/records",
            {
                "source_id": source,
                "is_demo": "false",
                "mode": CURRENT,
                "cutoff": datetime.now(UTC).isoformat(),
                "run_id": run["id"],
                "limit": max_records,
            },
        )
        return [document(item) for item in data["items"]]

    def archive(self, body: dict):
        """Import a declared archive (scenario and history replay preparation only)."""
        response = self.client.post(
            "/api/history/archives",
            json=body,
            timeout=120,
            headers={
                "Authorization": f"Bearer {self.settings.fabric_admin_token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        if response.status_code >= 400:
            raise FabricError(f"archive_http_{response.status_code}", response.text[:500])
        return response.json()
