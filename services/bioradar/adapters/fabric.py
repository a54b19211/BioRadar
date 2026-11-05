"""Data-Fabric history API (徐子雯). The radar only reads fixed versions; it never writes Fabric tables."""

import json
import time
import uuid
from datetime import UTC, datetime

import httpx

from bioradar.config import Settings

CURRENT = "current"
HISTORY = "public_history_reconstruction"


# Fabric answers for a fixed version that cannot be read with this permission, scope and cutoff
NOT_VISIBLE = {"version_not_found", "observation_basis_not_visible", "graph_evidence_not_visible"}


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


def native_kind(record: dict):
    """(material kind, text level) from the source's own metadata, for literature records that come
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


# Registry fields Fabric already extracted from a trial record, in reading order. Each is shown with the
# JSON pointer it came from in the same fixed version, its value as stored (ACTUAL/ESTIMATED, false and
# missing kept as they are), so a quote from this block points back to the record, not to a summary.
REGISTRY_FIELDS = [
    ("trial_id", "登记号"),
    ("official_title", "正式题目"),
    ("status", "状态"),
    ("phase", "阶段"),
    ("study_type", "研究类型"),
    ("design_info", "设计"),
    ("enrollment", "入组人数"),
    ("enrollment_type", "入组人数类型"),
    ("start_date", "开始日期"),
    ("start_date_type", "开始日期类型"),
    ("primary_completion_date", "主要完成日期"),
    ("primary_completion_date_type", "主要完成日期类型"),
    ("completion_date", "完成日期"),
    ("completion_date_type", "完成日期类型"),
    ("conditions", "疾病"),
    ("interventions", "干预"),
    ("arm_groups", "分组"),
    ("lead_sponsor", "申办方"),
    ("primary_outcomes", "主要终点（方案）"),
    ("secondary_outcomes", "次要终点（方案）"),
    ("has_results", "登记中有结果表"),
    ("results_first_posted", "结果首次公布"),
    ("results_participant_flow", "结果：受试者流程"),
    ("results_baseline", "结果：基线"),
    ("results_outcome_measures", "结果：终点数据"),
    ("results_adverse_events", "结果：不良事件"),
]
REGISTRY_FIELD_CHARS = 3000  # one long results table must not push the protocol fields out


def _measurement(x: dict) -> str:
    """One value with whatever the registry gave around it: spread, or lower/upper limits, as named."""
    text = str(x.get("value"))
    if x.get("spread") not in (None, ""):
        text += f" (spread {x['spread']})"
    if x.get("lowerLimit") not in (None, "") or x.get("upperLimit") not in (None, ""):
        text += f" [{x.get('lowerLimit', '?')}, {x.get('upperLimit', '?')}]"
    return text


def outcome_lines(value, pointer="") -> list[str]:
    """ClinicalTrials results (resultsSection/outcomeMeasuresModule) as one compact line per measure,
    keeping the registry's own fields and words: type, title, time frame, unit, statistic (paramType),
    dispersion type (SD, 95% CI, range ... as written), the analysed counts with their units, and each
    class/category's group values with spread or lower/upper limits. Nothing is turned into "±" (Codex
    r41: a 95% CI lost its limits and its week-24 time frame). The full JSON stays in the record."""
    lines = []
    for i, m in enumerate((value or {}).get("outcomeMeasures", []) if isinstance(value, dict) else []):
        groups = {g.get("id"): g.get("title", g.get("id")) for g in m.get("groups", [])}
        denoms = [
            f"{d.get('units', '')}: "
            + "，".join(
                f"{groups.get(c.get('groupId'), c.get('groupId'))} {c.get('value')}"
                for c in d.get("counts", [])
            )
            for d in m.get("denoms", [])
        ]
        parts = []
        for c in m.get("classes", []):
            for cat in c.get("categories", []):
                values = "，".join(
                    f"{groups.get(x.get('groupId'), x.get('groupId'))} {_measurement(x)}"
                    for x in cat.get("measurements", [])
                )
                label = "／".join(t for t in (c.get("title"), cat.get("title")) if t)
                parts.append(f"{label}: {values}" if label else values)
        head = "；".join(
            f"{k} {m[k]}" for k in ("timeFrame", "unitOfMeasure", "paramType", "dispersionType") if m.get(k)
        )
        lines.append(
            f"终点 {m.get('type', '')} 「{m.get('title', '')}」（{pointer}/outcomeMeasures/{i}；{head}；"
            f"分析数 {'；'.join(denoms) or '（原件无）'}）: " + ("；".join(parts) or "（原件无数值）")
        )
    return lines


def registry_text(record: dict) -> str:
    """The trial's registry fields as lines "名称 field（JSON pointer）: value"; empty for other records."""
    facts, locators = record.get("facts") or {}, record.get("field_locators") or {}
    if record.get("record_kind") != "trial" or not facts:
        return ""
    lines = ["【登记字段：程序按原件 JSON 路径取出的值，同一固定版本】"]
    for key, label in REGISTRY_FIELDS:
        pointer = (locators.get(key) or {}).get("json_pointer", "")
        if key not in facts:
            value = "（原件无此字段）"
        elif key == "results_outcome_measures":
            # one compact line per measure below instead of the long JSON (kept in the record)
            lines.append(f"{label} {key}（{pointer}）: 见下列各终点")
            lines += [f"  {line}" for line in outcome_lines(facts[key], pointer)]
            continue
        else:
            value = json.dumps(facts[key], ensure_ascii=False, separators=(",", ":"))
            if len(value) > REGISTRY_FIELD_CHARS:
                # say that more exists, so a writer does not report the rest as missing
                value = (
                    value[:REGISTRY_FIELD_CHARS]
                    + f"…（此字段共 {len(value)} 字符，这里只取前 {REGISTRY_FIELD_CHARS}）"
                )
        lines.append(f"{label} {key}（{pointer}）: {value}")
    return "\n".join(lines)


# Sources whose attachment versions get their text from Fabric's processing layer (/derived-text).
ATTACHMENT_SOURCES = {"nmpa", "cde", "chictr"}


def attachment_block(doc: dict, found: dict) -> str:
    """The parsed text of an attachment, headed by what it is and where it came from, so a quote points back
    to the attachment version and its page."""
    return (
        f"【附件正文：{doc['title']}（附件版本 {doc['version_id'][:16]}，"
        f"所属页面 {doc.get('parent_record_id') or '?'}；"
        f"由 {found.get('parse_method') or '?'} 从原件解析，表格和扫描件的识别可能有误，以原件为准）】"
        + "\n\n"
        + (found.get("text") or "")
    )


# Sources whose versions Fabric's processing layer diffs field by field against the previous version.
REGISTRY_DIFF_SOURCES = {"clinicaltrials"}
REGISTRY_CHANGE_LINES = 12


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


def document(record: dict) -> dict:
    """The few fields the radar uses. Version identity stays exactly as Fabric returned it.

    `document_evidence` is Fabric's explicit material description: kind, text level, relations
    (corrects / retracts / supplements / reposts), same-origin grouping and information events
    (deletion, official denial, moderation, access failure)."""
    proof = record.get("availability_proof") or {}
    evidence = record.get("document_evidence") or {}
    native, level = native_kind(record) if not evidence else (None, None)
    kind = evidence.get("material_kind") or native or record.get("record_kind")
    independence = evidence.get("source_independence") or {}
    return {
        "source": record["source_id"],
        "record_id": record["source_record_id"],
        "version_id": record["version_id"],
        "is_demo": record["is_demo"],
        "title": record.get("title") or "",
        "url": record.get("source_url"),
        "text": "\n\n".join(t for t in (record.get("clean_text") or "", registry_text(record)) if t),
        "published_at": _time(record.get("source_published_at")) or _time(record.get("event_time")),
        "available_at": _time(proof.get("available_at")) or record.get("first_seen_at"),
        "material_kind": kind,
        "text_level": evidence.get("text_level") or level,
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
        "registry_diff": None,  # filled by Fabric.attach_registry_changes for REGISTRY_DIFF_SOURCES
        # an attachment version (Fabric legacy_site_crawler): its text comes from the processing layer
        "role": (record.get("facts") or {}).get("role"),
        "parent_record_id": (record.get("facts") or {}).get("parent_record_id"),
        "attachment_text": None,  # filled by Fabric.attach_attachment_text
        # program-derived text put into this document (registry diff block, parsed attachment text), with what
        # it was derived from, so the evidence record can point back to the inputs (Codex r58/r59)
        "derived": [],
    }


class Fabric:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(base_url=settings.fabric_url, timeout=30)
        # HTTP requests actually sent, per endpoint: counts only, no parameters, tokens or texts (Codex r44)
        self.requests = {}

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
                path, json=body, headers={"Authorization": f"Bearer {token or self.settings.fabric_read_token}"}
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
        """WeKnora recall verified by Fabric (POST /api/history/search). Every hit was checked at this mode and
        cutoff: source permission, the version visible then, and that the chunk is that version's fixed text.
        Returns (hits, summary): hits [{source, record_id, version_id, score, text, locators}] at the visible
        version; summary: excluded counts by reason, whether fewer than top_k came back, per-scope recall
        channels and index coverage. Missing hits do not mean the material does not exist."""
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
        self.attach_registry_changes(docs, is_demo=is_demo, mode=params["mode"], cutoff=params["cutoff"])
        self.attach_attachment_text(docs, is_demo=is_demo, mode=params["mode"], cutoff=params["cutoff"])
        return docs, data.get("next_cursor")

    def read_versions(self, source, refs, *, is_demo, cutoff=None, now=None):
        """Fixed versions [(record_id, version_id)] → (documents, versions no longer readable). Used where the
        radar owes work on one particular version (a deferred or failed triage): reading the record again
        would return its current head instead, and the owed version would never be done (Codex r62)."""
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
            docs.append(document(item))
        self.attach_registry_changes(docs, is_demo=is_demo, mode=mode, cutoff=at)
        self.attach_attachment_text(docs, is_demo=is_demo, mode=mode, cutoff=at)
        return docs, missing

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

    def attach_attachment_text(self, docs, *, is_demo, mode, cutoff):
        """Give each attachment version its parsed text. Not parsed yet, failed, or Fabric unable to answer:
        the text stays empty and `attachment_text` says why; the monitor decides what to do with it."""
        by_source = {}
        for d in docs:
            if d["source"] in ATTACHMENT_SOURCES and d.get("role") == "attachment":
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
                    if state.get("status") == "parsed" and (state.get("text") or "").strip():
                        d["text"] = attachment_block(d, state)
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
        block at the top of the text, and one event per material change. When Fabric cannot answer (an
        older Fabric without the endpoint, a timeout) the documents stay exactly as before, marked
        unavailable, so reading never fails because of the diff."""
        by_source = {}
        for d in docs:
            if d["source"] in REGISTRY_DIFF_SOURCES:
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
                        d["text"] = block + "\n\n" + d["text"]
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
        self.attach_registry_changes(items, is_demo=is_demo, mode=CURRENT, cutoff=now)
        self.attach_attachment_text(items, is_demo=is_demo, mode=CURRENT, cutoff=now)
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
