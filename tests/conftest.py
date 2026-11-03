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
    def __init__(self, records):
        from bioradar.adapters.fabric import document

        self.docs = [document(r) for r in records]
        self.calls = []

    def search(self, source, terms, *, is_demo, cutoff=None, cursor=None, limit=10, record_ids=()):
        self.calls.append(("search", source, tuple(terms), tuple(record_ids)))
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

    def head(self, source, *, is_demo):
        return max([0] + [d["seq"] for d in self.docs if d["source"] == source])

    def changes(self, source, *, is_demo, after=0, limit=100):
        items = [d for d in self.docs if d["source"] == source and d["seq"] > after][:limit]
        return items, max([after] + [d["seq"] for d in items])

    def fetch(self, source, query, **_):
        return []
