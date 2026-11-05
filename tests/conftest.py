"""Tests run on SQLite with fake Fabric, model and services; no network, no Docker."""

import itertools
import os
import tempfile

import pytest

os.environ["BIORADAR_DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["BIORADAR_LLM_BASE_URL"] = "http://model.invalid"
os.environ["BIORADAR_LLM_MODEL"] = "fake"
os.environ["BIORADAR_DEV_CREDENTIAL"] = "test-login"
os.environ["BIORADAR_SESSION_SECRET"] = "test-session-key"
os.environ["BIORADAR_PLANNER_URL"] = "http://planner.invalid"
os.environ["BIORADAR_BIOCHECK_URL"] = "http://biocheck.invalid"

from bioradar import db  # noqa: E402

SEQ = itertools.count(1)


@pytest.fixture(autouse=True)
def fresh_db():
    db.Base.metadata.drop_all(db.engine())
    db.Base.metadata.create_all(db.engine())
    yield


def record(
    record_id,
    title,
    text,
    *,
    available="2026-09-27T08:00:00+08:00",
    kind="official_notice",
    relations=(),
    source="external_archive",
    version=None,
    unverified=False,
):
    """A Fabric HistoryRecord as the API returns it."""
    return {
        "source_id": source,
        "source_record_id": record_id,
        "version_id": version or ("v" + record_id).ljust(64, "0"),
        "raw_sha256": __import__("hashlib").sha256(text.encode()).hexdigest(),
        "is_demo": True,
        "record_kind": "document",
        "title": title,
        "source_url": None,
        "clean_text": text,
        "source_published_at": {"value": available[:10], "precision": "day"},
        "availability_proof": {
            "kind": "reviewed_archive",
            "available_at": {"value": available, "precision": "instant"},
        },
        "first_seen_at": available,
        "published_seq": next(SEQ),
        "document_evidence": {
            "material_kind": kind,
            "text_level": "full_text",
            "statement_status": "unverified_report" if unverified else "source_reported",
            "document_relations": [
                {"relation_type": r, "target_source_id": source, "target_source_record_id": t}
                for r, t in relations
            ],
        },
    }


class FakeFabric:
    """clean=True: each document's text is served as its clean_text artifact (test.clean.v11), the way
    attach_clean_text leaves real documents, and clean_texts() reads it back by version + processor; `revoked`
    and `tampered` (version ids) make that read refuse or return another text."""

    PROCESSOR = "test.clean.v11"

    def __init__(self, records, clean=False):
        from bioradar.adapters.fabric import document, part
        from bioradar.material import Material

        self.docs = [document(r) for r in records]
        self.calls = []
        self.revoked, self.tampered = set(), set()
        if clean:
            for d in self.docs:
                d["material"] = Material.part(
                    d["text"], part(d, "clean_text", processor_version=self.PROCESSOR)
                )
                d["text"] = d["material"].text
                d["clean_text_state"] = "processed"

    def clean_texts(self, source, version_ids, *, is_demo, mode, cutoff, processor_version=None):
        import hashlib

        from bioradar.adapters.fabric import FabricError

        self.calls.append(("clean_texts", source, tuple(version_ids)))
        out = {}
        for d in self.docs:
            if d["source"] != source or d["version_id"] not in version_ids:
                continue
            if d["version_id"] in self.revoked:
                raise FabricError("version_not_found", "not visible")
            text = d["text"] + (" tampered" if d["version_id"] in self.tampered else "")
            out[d["version_id"]] = {
                "state": "processed",  # as Fabric says it for a readable cleaned artifact
                "clean": {
                    "processor_version": processor_version or self.PROCESSOR,
                    "text": text,
                    "status": "parsed",
                    "text_sha256": hashlib.sha256(d["text"].encode()).hexdigest(),
                },
            }
        return out

    def search(self, source, terms, *, is_demo, cutoff=None, cursor=None, limit=10, record_ids=(), now=None):
        self.calls.append(("search", source, tuple(terms), tuple(record_ids)))
        self.times = getattr(self, "times", []) + [(tuple(terms), cursor, now)]
        found = [
            d
            for d in self.docs
            if d["source"] == source
            and (not record_ids or d["record_id"] in record_ids)
            and all(t in d["title"] + d["text"] for t in terms)
        ]
        if cutoff is not None:
            from bioradar.research.monitor import parse_time

            found = [d for d in found if parse_time(d["available_at"]) <= cutoff]
        start = int(cursor or 0)
        page = found[start : start + limit]
        return page, (str(start + limit) if start + limit < len(found) else None)

    def read_versions(self, source, refs, *, is_demo, cutoff=None, now=None):
        self.calls.append(("read_versions", source, tuple(refs)))
        wanted = {v for _, v in refs}
        found = [d for d in self.docs if d["source"] == source and d["version_id"] in wanted]
        return found, sorted(wanted - {d["version_id"] for d in found})

    def head(self, source, *, is_demo):
        return max([0] + [d["seq"] for d in self.docs if d["source"] == source])

    def changes(self, source, *, is_demo, after=0, limit=100):
        items = [d for d in self.docs if d["source"] == source and d["seq"] > after][:limit]
        return items, max([after] + [d["seq"] for d in items])

    def fetch(self, source, query, **_):
        return []


def biocheck_fake(answer=None, calls=None):
    """BioCheck /v2 stand-in for series-logic tests: every field of every item answered match by checker v6
    (answer(body) may alter the response). Real rule behaviour is tested against real BioCheck answers."""
    import copy

    import extraction_contract as ec

    def post(body):
        if calls is not None:
            calls.append(copy.deepcopy(body))
        items = [
            {
                "id": i["id"],
                "kind": "measurement",
                "status": "consistent",
                "fields": {
                    name: {"result": "match"} for name in ec.KIND_RESULTS[ec.CHECKER_V8]["measurement"]
                },
            }
            for i in body["items"]
        ]
        response = {
            "contract_version": ec.CONTRACT_V2,
            "checker_version": ec.CHECKER_V8,
            "subject": body["subject"],
            "items": items,
            "counts": {"consistent": len(items), "inconsistent": 0, "unchecked": 0},
            "llm_calls": [],
        }
        return answer(body, response) if answer else response

    return post
