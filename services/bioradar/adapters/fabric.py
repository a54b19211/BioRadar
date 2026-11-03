"""Data-Fabric history API (徐子雯). The radar only reads fixed versions; it never writes Fabric tables."""

import json
import time
import uuid
from datetime import UTC, datetime

import httpx

from bioradar.config import Settings

CURRENT = "current"
HISTORY = "public_history_reconstruction"


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
        else:
            value = json.dumps(facts[key], ensure_ascii=False, separators=(",", ":"))
            if len(value) > REGISTRY_FIELD_CHARS:
                value = value[:REGISTRY_FIELD_CHARS] + "…（截断）"
        lines.append(f"{label} {key}（{pointer}）: {value}")
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
    }


class Fabric:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(base_url=settings.fabric_url, timeout=30)

    def _get(self, path, params, token=None):
        response = self.client.get(
            path,
            params=params,
            headers={"Authorization": f"Bearer {token or self.settings.fabric_read_token}"},
        )
        if response.status_code >= 400:
            try:
                error = response.json().get("error") or response.json().get("detail") or {}
            except ValueError:
                error = {}
            code = error.get("code") if isinstance(error, dict) else None
            raise FabricError(code or f"fabric_http_{response.status_code}", str(error)[:300])
        return response.json()

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
        return [document(item) for item in data["items"]], data.get("next_cursor")

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
