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
        "text": record.get("clean_text") or "",
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

    def search(self, source, terms, *, is_demo, cutoff=None, cursor=None, limit=10, record_ids=()):
        """One page of records matching all terms. cutoff=None means now (current mode)."""
        params = {
            "source_id": source,
            "is_demo": str(is_demo).lower(),
            "mode": HISTORY if cutoff else CURRENT,
            "cutoff": (cutoff or datetime.now(UTC)).isoformat(),
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
