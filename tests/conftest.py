"""Tests run on SQLite with fake Fabric, model and services; no network, no Docker.

The session owns one temporary directory (the SQLite file). It is removed when the session ends, whether the
tests passed, failed or were interrupted; a directory a killed process left behind is not touched here."""

import itertools
import os
import tempfile

import pytest

# the object also removes the directory by itself if the interpreter exits without running the hooks below
SESSION_TMP = tempfile.TemporaryDirectory(prefix="bioradar-tests-")
os.environ["BIORADAR_DATABASE_URL"] = "sqlite:///" + os.path.join(SESSION_TMP.name, "test.db")
os.environ["BIORADAR_LLM_BASE_URL"] = "http://model.invalid"
os.environ["BIORADAR_LLM_MODEL"] = "fake"
os.environ["BIORADAR_DEV_CREDENTIAL"] = "test-login"
os.environ["BIORADAR_SESSION_SECRET"] = "test-session-key"
os.environ["BIORADAR_PLANNER_URL"] = "http://planner.invalid"
os.environ["BIORADAR_BIOCHECK_URL"] = "http://biocheck.invalid"

from fabric_receipts import sources_receipt  # noqa: E402

from bioradar import db  # noqa: E402

SEQ = itertools.count(1)


def pytest_sessionfinish(session, exitstatus):
    """Close the connections, then delete this session's own directory (exactly the one it created)."""
    db.engine().dispose()
    SESSION_TMP.cleanup()


@pytest.fixture(autouse=True)
def fresh_db():
    db.Base.metadata.drop_all(db.engine())
    db.Base.metadata.create_all(db.engine())
    yield


CURRENT_PLANNER = {
    "status": "ok",
    "version": "0.2.0",
    "kinds": ["search", "read", "next_page", "fetch", "dataset", "analysis", "mechanism"],
    "channels": ["fabric_literal", "weknora_semantic", "gdelt_leads", "graph"],
}


@pytest.fixture(autouse=True)
def current_planner(monkeypatch):
    """/health answers as planner 0.2.0 does; each test stands in for /v1/decide itself."""
    from bioradar.adapters import services

    monkeypatch.setattr(services, "planner_health", lambda s: CURRENT_PLANNER)


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
    """A Fabric VersionRecord as the API returns it."""
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

    def __init__(self, records, clean=False, readable=()):
        """readable: further source ids Fabric lets the radar read, besides the sources of the records."""
        from bioradar.adapters.fabric import document, part
        from bioradar.material import Material

        self.extra_readable = set(readable)
        self.caps = None

        self.docs = [document(r) for r in records]
        for d in self.docs:
            d["read_at"] = {"mode": "current", "cutoff": "2026-11-01T00:00:00+00:00"}
        self.differences = {}  # version_id -> Fabric's difference facts for it, as a test sets them
        self.calls = []
        self.modes = []  # (read, mode) as the radar named the mode; None: it named none
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

    def search(
        self,
        source,
        terms,
        *,
        is_demo,
        cutoff=None,
        cursor=None,
        limit=10,
        record_ids=(),
        now=None,
        mode=None,
    ):
        self.calls.append(("search", source, tuple(terms), tuple(record_ids)))
        self.modes.append(("search", mode))
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

    def read_versions(self, source, refs, *, is_demo, cutoff=None, now=None, mode=None):
        self.calls.append(("read_versions", source, tuple(refs)))
        self.modes.append(("read_versions", mode))
        wanted = {v for _, v in refs}
        found = [d for d in self.docs if d["source"] == source and d["version_id"] in wanted]
        return found, sorted(wanted - {d["version_id"] for d in found})

    def version_differences(self, source, version_ids, *, is_demo, mode, cutoff):
        self.calls.append(("version_differences", source, tuple(version_ids), mode, cutoff))
        return {v: self.differences[v] for v in version_ids if v in self.differences}

    def sources(self):
        """Fabric's source receipt: every source this fake holds records of is readable."""
        return sources_receipt({d["source"] for d in self.docs} | self.extra_readable)

    def load_capabilities(self):
        from bioradar.adapters.capabilities import CapabilityError, SourceCapabilities
        from bioradar.adapters.fabric import FabricError

        try:
            self.caps = SourceCapabilities.parse(self.sources())
        except (FabricError, CapabilityError):
            self.caps = None
        return self.caps

    def head(self, source, *, is_demo):
        return max([0] + [d["seq"] for d in self.docs if d["source"] == source])

    def changes(self, source, *, is_demo, after=0, limit=100):
        items = [d for d in self.docs if d["source"] == source and d["seq"] > after][:limit]
        return items, max([after] + [d["seq"] for d in items])

    def fetch(self, source, query, **_):
        return []

    def part_texts(self, parts):
        """Fabric's full text of each bound part ({part_key: text}); a revoked version is left out and a
        tampered one reads as another text."""
        from bioradar.material import part_key

        out = {}
        for p in parts:
            doc = next(
                (
                    d
                    for d in self.docs
                    if (d["source"], d["record_id"], d["version_id"])
                    == (p["source"], p["record_id"], p["version_id"])
                ),
                None,
            )
            if doc is not None and doc["version_id"] not in self.revoked:
                out[part_key(p)] = doc["text"] + (" tampered" if doc["version_id"] in self.tampered else "")
        return out

    def version_visible(self, source, record_id, version_id, *, is_demo, cutoff=None, mode=None):
        """Whether the fixed version can be read now, or at the cutoff (not before it was available)."""
        from bioradar.research.monitor import parse_time

        self.modes.append(("version_visible", mode))

        doc = next(
            (
                d
                for d in self.docs
                if (d["source"], d["record_id"], d["version_id"]) == (source, record_id, version_id)
            ),
            None,
        )
        if doc is None or version_id in self.revoked:
            return False
        return cutoff is None or parse_time(doc["available_at"]) <= cutoff
