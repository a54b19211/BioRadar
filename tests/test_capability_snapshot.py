"""The capability snapshot taken when an analysis begins is for planning and audit. It never authorises a
later send: each of these happens while the snapshot still says `readable: true` for the revoked source."""

import json

import httpx
import pytest
from fabric_receipts import sources_receipt
from test_send_recheck import PLAN, VALID, follow_up_plan, model, reading_call, stored

from bioradar.adapters.fabric import Fabric
from bioradar.config import settings
from bioradar.research.pipeline import Failed


def snapshotted_fabric(*, unreachable_after_snapshot=False):
    """A real Fabric client whose source list is controlled by the test; load_capabilities() has been called,
    so the snapshot says both sources are readable. `revoked` drops gone_src from the list from then on."""
    state = {"down": False}

    def handler(request):
        if request.url.path != "/api/history/sources":
            return httpx.Response(404, json={})
        if state["down"]:
            return httpx.Response(503, json={"error": {"code": "unavailable"}})
        return httpx.Response(
            200, json=sources_receipt(["open_src"] if fabric.revoked else ["open_src", "gone_src"])
        )

    client = httpx.Client(base_url="http://fabric.invalid", transport=httpx.MockTransport(handler))
    fabric = Fabric(settings(), client=client)
    fabric.revoked = False
    fabric.state = state
    assert fabric.load_capabilities().readable == {"open_src", "gone_src"}
    return fabric


def test_revoked_in_the_middle_of_an_analysis_the_snapshot_does_not_let_the_next_send_go(monkeypatch):
    fabric = snapshotted_fabric()
    sent = model(monkeypatch, fabric, [(200, VALID)])
    run, call = reading_call(fabric)
    fabric.revoked = True
    with pytest.raises(Failed) as blocked:
        call()
    assert blocked.value.code == "source_access_withdrawn" and sent == []
    assert "gone_src" in fabric.caps.readable  # what the snapshot still says is not what was asked


def test_revoked_before_a_format_retry_the_retry_is_not_sent(monkeypatch):
    fabric = snapshotted_fabric()
    sent = model(monkeypatch, fabric, [(200, "not json"), (200, VALID)], revoke_after=1)
    run, call = reading_call(fabric)
    with pytest.raises(Failed) as blocked:
        call()
    assert blocked.value.code == "source_access_withdrawn" and len(sent) == 1
    assert stored(run)[0] == [(1, "sent"), (2, "withheld")]
    assert "gone_src" in fabric.caps.readable


def test_the_earlier_report_of_a_revoked_source_is_not_used_whatever_the_snapshot_says(monkeypatch):
    fabric = snapshotted_fabric()
    sent = model(monkeypatch, fabric, [(200, PLAN)])
    run, ask_plan = follow_up_plan(fabric)
    fabric.revoked = True
    ask_plan()  # the plan goes on without the earlier report
    assert "PARENT_ONLY_FACT" not in json.dumps(sent, ensure_ascii=False)
    assert run.previous_report() is None and "gone_src" in fabric.caps.readable


def test_when_the_source_list_cannot_be_read_the_next_send_is_withheld_not_allowed_by_the_snapshot(
    monkeypatch,
):
    fabric = snapshotted_fabric()
    sent = model(monkeypatch, fabric, [(200, VALID)])
    run, call = reading_call(fabric)
    fabric.state["down"] = True
    with pytest.raises(Failed) as blocked:
        call()
    assert blocked.value.code == "source_access_unconfirmed" and sent == []
    assert stored(run)[0] == [(1, "withheld")]  # prepared, then withheld: the material did not go out
    assert fabric.caps is not None and "open_src" in fabric.caps.readable
