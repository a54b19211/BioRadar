"""Shared stand-ins for the MechEvidence v2 tests: an analysis holding a lease, an authorization issued
for it, a Fabric whose source list and visible versions the test controls, and the client side of the
service protocol as MechEvidence's authorization.py performs it."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from fabric_receipts import sources_receipt
from fastapi.testclient import TestClient

from bioradar import db, service_api
from bioradar.adapters.fabric import FabricError
from bioradar.app import app
from bioradar.config import settings
from bioradar.research import analyses, attempts, lease

FIXTURES = Path(__file__).parent / "fixtures" / "mech-contract-v2"
EVALUATOR = json.loads((FIXTURES / "evaluator.json").read_text("utf-8"))["evaluator"]
KEY = "2c3c86c0de61aa1dc6e8b69499044d06165147c72289c528c8f4c601cc558daf"
TOKEN = "service-token-for-the-tests"
VERSION = "a" * 64


class Fab:
    """The Fabric the endpoints ask. `readable`: the sources it lists; `hidden`: versions not visible; `down`:
    it cannot answer."""

    readable = {"open_src"}
    hidden: set = set()
    down = False

    def __init__(self, _settings=None):
        pass

    def sources(self):
        if Fab.down:
            raise FabricError("fabric_http_503", "down")
        return sources_receipt(Fab.readable)

    def version_visible(self, source, record_id, version_id, *, is_demo, cutoff=None):
        if Fab.down:
            raise FabricError("fabric_http_503", "down")
        return version_id not in Fab.hidden


def reset_fabric():
    Fab.readable, Fab.hidden, Fab.down = {"open_src"}, set(), False


def service_client(monkeypatch):
    """A client of the radar's service endpoints (with the right token) and the Fabric double behind them."""
    reset_fabric()
    monkeypatch.setattr(settings(), "model_attempt_token", TOKEN)
    monkeypatch.setattr(service_api, "Fabric", Fab)
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client


def materials(sources=("open_src",)):
    return [
        {
            "ref": f"E{n}",
            "binding_sha256": hashlib.sha256(f"binding{n}".encode()).hexdigest(),
            "parts": [{"source": source, "record_id": f"R{n}", "version_id": VERSION}],
        }
        for n, source in enumerate(sources, 1)
    ]


def started(*, token="tok", max_attempts=4, seconds=600, sources=("open_src",), key=KEY, evaluator=EVALUATOR):
    """An analysis whose execution holds a lease (token) and an authorization issued for one evaluation of
    it. Returns (analysis id, authorization ref)."""
    row = analyses.create(question="机制证据怎样？", direction="science", is_demo=True, enqueue=False)
    if token is not None:
        with db.session() as s:
            lease.take([s.get(db.Analysis, row.id)], token, db.now(), lease.ANALYSIS_LEASE)
    run = SimpleNamespace(id=row.id, lease_token=token, cutoff=None, input={"is_demo": True})
    with db.session() as s:
        issued = attempts.issue(
            s,
            run=run,
            evaluation_key=key,
            evaluator=evaluator,
            materials=materials(sources),
            sources=set(sources),
            max_attempts=max_attempts,
            seconds=seconds,
        )
    return row.id, issued.ref


def attempt_body(ref, n, *, key=KEY, evaluator_id=None, stage="review", model=None, endpoint_id=None, **more):
    """model_attempt_authorization.v1 for the n-th request of an evaluation, as MechEvidence sends it."""
    config = EVALUATOR["model_config"]
    return {
        "contract_version": attempts.AUTHORIZE_V1,
        "service": "mechevidence",
        "authorization_ref": ref,
        "evaluation_key": key,
        "evaluator_id": evaluator_id or EVALUATOR["evaluator_id"],
        "attempt": {
            "id": f"{n:032x}",
            "stage": stage,
            "index": n,
            "request_body_sha256": hashlib.sha256(f"request {n}".encode()).hexdigest(),
            "model": model or config["model"],
            "endpoint_id": endpoint_id or config["endpoint_id"],
            **more,
        },
    }


def outcome_body(ref, decision, n, *, text="the raw answer", delivered="yes", status=200, error=None, **more):
    """model_attempt_outcome.v1 for the n-th request, as MechEvidence's settle() posts it."""
    digest = hashlib.sha256(text.encode()).hexdigest() if text is not None else None
    return {
        "contract_version": attempts.OUTCOME_V1,
        "service": "mechevidence",
        "authorization_ref": ref,
        "decision_id": decision["decision_id"],
        "attempt_id": decision["attempt_id"],
        "index": n,
        "index_in_call": 1,
        "delivered": delivered,
        "status": status,
        "error": error,
        "response_sha256": digest,
        "truncated": False,
        "response_text": text,
        **more,
    }


def acknowledged(ack, decision, body) -> bool:
    """MechEvidence's rule: the acknowledgement must echo the outcome version, recorded:true, the attempt id,
    the decision id and the response hash, each with the type it was sent with."""
    expected = {
        "contract_version": attempts.OUTCOME_V1,
        "recorded": True,
        "attempt_id": decision["attempt_id"],
        "decision_id": decision["decision_id"],
        "response_sha256": body["response_sha256"],
    }
    return isinstance(ack, dict) and all(
        key in ack and ack[key] == value and type(ack[key]) is type(value) for key, value in expected.items()
    )
