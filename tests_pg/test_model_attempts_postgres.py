"""The reservation of a model request on a real PostgreSQL: two requests racing for the last place, or one
request sent twice, are decided by the row lock - exactly one gets the permission."""

import threading
from concurrent.futures import ThreadPoolExecutor

from mech_support import attempt_body, service_client, started

from bioradar import db
from bioradar.research import attempts

AUTHORIZE = "/internal/v1/model-attempts/authorize"


def race(calls):
    barrier = threading.Barrier(len(calls))

    def go(call):
        barrier.wait(10)
        return call()

    with ThreadPoolExecutor(len(calls)) as pool:
        return [f.result(60) for f in [pool.submit(go, call) for call in calls]]


def test_two_requests_for_the_last_place_get_one_permission(monkeypatch):
    client = service_client(monkeypatch)
    _, ref = started(max_attempts=2)
    assert client.post(AUTHORIZE, json=attempt_body(ref, 1)).status_code == 200
    answers = race(
        [
            lambda: client.post(AUTHORIZE, json=attempt_body(ref, 2)),
            lambda: client.post(AUTHORIZE, json=attempt_body(ref, 3, id="c" * 32)),
        ]
    )
    assert sorted(a.status_code for a in answers) == [200, 403]
    with db.session() as s:
        assert attempts.counts(s, ref)["prepared"] == 2  # the allowance, never more


def test_one_request_sent_twice_at_once_is_allowed_once(monkeypatch):
    client = service_client(monkeypatch)
    _, ref = started(max_attempts=4)
    answers = race([lambda: client.post(AUTHORIZE, json=attempt_body(ref, 1)) for _ in range(3)])
    assert sorted(a.status_code for a in answers) == [200, 403, 403]
    assert {a.json()["reason_code"] for a in answers if a.status_code == 403} <= {
        "attempt_already_decided",
        "index_not_increasing",
    }
    with db.session() as s:
        assert attempts.counts(s, ref) == {"prepared": 1, "settled": 0, "refused": 0}
