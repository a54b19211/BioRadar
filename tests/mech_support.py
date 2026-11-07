"""Shared stand-ins for the MechEvidence v2 tests: an analysis holding a lease, an authorization issued
for it, a Fabric whose source list and visible versions the test controls, and the client side of the
service protocol as MechEvidence's authorization.py performs it."""

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from fabric_receipts import sources_receipt
from fastapi.testclient import TestClient

from bioradar import db, service_api
from bioradar.adapters import services
from bioradar.adapters.fabric import FabricError
from bioradar.adapters.services import ServiceError
from bioradar.app import app
from bioradar.config import settings
from bioradar.mech import contract
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


# ---- a stand-in MechEvidence that speaks the service side of the protocol against the radar's endpoints ----

DECOMPOSE = '{"claims":[{"text":"A changes M","kind":"causal"}],"joins":[]}'
REVIEW = '{"observations":[]}'
PROVIDER_USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


class FakeMech:
    """MechEvidence v2 as the radar sees it: GET /v2/evaluator, POST /v2/evaluate. Each evaluation makes
    two model "requests" (decompose, review); before each it asks the radar's authorize endpoint and sends
    only on a decision that echoes its attempt, after each it posts the outcome and believes it only on an
    acknowledgement that echoes every field - the rules of MechEvidence's authorization.py. Its receipt is
    the committed fixture receipt re-made for the request it was given. `edit` may damage it; `between`
    runs after request n."""

    def __init__(self, client, evaluator=EVALUATOR, usage=PROVIDER_USAGE):
        self.client, self.evaluator, self.usage = client, evaluator, usage
        self.requests, self.edit, self.between = [], (lambda receipt: receipt), (lambda n: None)
        self.swap_before_answering = None  # an evaluator that replaces the current one when a request arrives
        self.extra_attempt = False  # asks for a third permission and never reports it
        self.counter = 0

    def evaluator_answer(self, settings):
        return {
            "contract_version": contract.CONTRACT_V2,
            "evaluator": self.evaluator,
            "authorization": {"mode": "per_attempt_trusted_endpoint", "configured": True},
        }

    def evaluate(self, settings, request):
        self.requests.append(request)
        if self.swap_before_answering is not None:
            self.evaluator, self.swap_before_answering = self.swap_before_answering, None
        if request.get("expected_evaluator_id") != self.evaluator["evaluator_id"]:
            body = {"error_code": "evaluator_changed", "evaluator": self.evaluator, "llm_calls": []}
            raise ServiceError("mechevidence", "evaluator_changed", "", (), body)
        key = contract.reuse_key(request, self.evaluator)
        calls = []
        for index, stage in enumerate(
            ("decompose", "review") + (("review",) if self.extra_attempt else ()), 1
        ):
            call = self.attempt(request, key, stage, index, calls, last=self.extra_attempt and index == 3)
            if call is not None:
                calls.append(call)
            self.between(index)
        return self.edit(self.receipt(request, key, calls))

    def attempt(self, request, key, stage, index, done, last):
        self.counter += 1
        config = self.evaluator["model_config"]
        attempt_id = uuid.uuid4().hex
        body = {
            "contract_version": attempts.AUTHORIZE_V1,
            "service": "mechevidence",
            "authorization_ref": request["authorization_ref"],
            "evaluation_key": key,
            "evaluator_id": self.evaluator["evaluator_id"],
            "attempt": {
                "id": attempt_id,
                "stage": stage,
                "index": index,
                "request_body_sha256": hashlib.sha256(f"{attempt_id}".encode()).hexdigest(),
                "model": config["model"],
                "endpoint_id": config["endpoint_id"],
            },
        }
        answer = self.client.post("/internal/v1/model-attempts/authorize", json=body)
        decision = answer.json()
        echoed = (decision.get("attempt_id"), decision.get("evaluation_key"), decision.get("evaluator_id"))
        if (
            answer.status_code != 200
            or decision.get("valid") is not True
            or echoed != (attempt_id, key, self.evaluator["evaluator_id"])
        ):
            error = ServiceError("mechevidence", "authorization_blocked", "", done, {"llm_calls": done})
            raise error
        if last:
            return None  # permission taken, request "sent", nothing ever reported
        text = DECOMPOSE if stage == "decompose" else REVIEW
        outcome = outcome_body(request["authorization_ref"], decision, index, text=text)
        ack = self.client.post(f"/internal/v1/model-attempts/{attempt_id}/outcome", json=outcome)
        if ack.status_code != 200 or not acknowledged(ack.json(), decision, outcome):
            raise ServiceError("mechevidence", "authorization_blocked", "outcome not recorded", done, {})
        return {
            "stage": stage,
            "model": "stub",
            "finish_reason": "stop",
            "usage": self.usage,
            "seconds": 0,
            "error": None,
            "requests": 1,
            "outputs": [
                {
                    "attempt": 1,
                    "index": index,
                    "attempt_id": attempt_id,
                    "decision_id": decision["decision_id"],
                    "delivered": "yes",
                    "status": 200,
                    "error": None,
                    "content": text,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "truncated": False,
                }
            ],
        }

    def receipt(self, request, key, calls):
        first = request["materials"][0]["id"]
        template = json.dumps(json.loads((FIXTURES / "receipt.json").read_text("utf-8")))
        receipt = json.loads(
            template.replace('"E1"', json.dumps(first))
            .replace("E1.", first + ".")
            .replace("text:E1", "text:" + first)
        )
        receipt.update(
            evaluator=json.loads(json.dumps(self.evaluator)),
            reuse_key=key,
            scope=contract.normal_scope(request["scope"]),
            materials=contract.material_echo(request),
            llm_calls=calls,
        )
        return receipt


def install(monkeypatch, mech):
    """The radar's calls to MechEvidence go to the stand-in."""
    monkeypatch.setattr(services, "mechevidence_evaluator", mech.evaluator_answer)
    monkeypatch.setattr(services, "mechevidence_v2", mech.evaluate)
