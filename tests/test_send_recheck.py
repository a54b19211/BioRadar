"""Every real model HTTP request that carries the run's own evidence is preceded by a Fabric check of its
sources - the first, a resend after a refusal and the client's format retry - through the real llm client
(httpx.post replaced, nothing leaves the process)."""

import json

import httpx
import pytest
from sqlalchemy import select

from bioradar import db
from bioradar.adapters import llm
from bioradar.config import settings
from bioradar.research import analyses, schemas
from bioradar.research.pipeline import Failed, Run
from bioradar.research.run import submit

VALID = '{"relevant": false}'


class Listing:
    """Fabric's readable-source list; `revoke_after` sends later it no longer lists gone_src."""

    def __init__(self):
        self.reads, self.revoked = 0, False

    def sources(self):
        self.reads += 1
        return {
            "sources": [
                {"source_id": s} for s in (["open_src"] if self.revoked else ["open_src", "gone_src"])
            ]
        }


def model(monkeypatch, fabric, replies, revoke_after=None):
    """The model endpoint: one reply per request, (status, content); sources revoked after the n-th."""
    sent = []

    def post(url, headers, json, timeout):
        sent.append(json)
        status, content = replies[len(sent) - 1]
        if revoke_after is not None and len(sent) >= revoke_after:
            fabric.revoked = True
        body = {
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(llm.httpx, "post", post)
    monkeypatch.setattr(llm, "BACKOFF", (0,))
    return sent


def reading_call(fabric):
    """A run about to send one record's window to the reader, with its outbound record prepared."""
    row = analyses.create(question="撤权后的重试？", direction="science", is_demo=True, enqueue=False)
    run = Run(row, settings(), fabric=fabric)
    doc = {"source": "gone_src", "record_id": "R1", "version_id": "v" * 64}
    from bioradar.material import Material

    with db.session() as s:
        read_id = submit.sent(run, s, "read", "radar", "E1", doc, Material.literal("REVOKED_TEXT 原文"))

    def call():
        with submit.outbound(run, [read_id], model=True):
            return run.ask(
                "read",
                "读",
                {"material": "REVOKED_TEXT 原文"},
                schemas.Reading,
                carries={"gone_src", "open_src"},
            )

    return run, call


def stored(run):
    with db.session() as s:
        reads = [
            (r.attempt, r.status) for r in s.scalars(select(db.MaterialRead).order_by(db.MaterialRead.id))
        ]
        calls = list(s.scalars(select(db.LlmCall).where(db.LlmCall.analysis_id == run.id)))
        outputs = list(s.scalars(select(db.LlmOutput)))
        return (
            reads,
            [(c.requests, c.total_tokens, c.error, c.http) for c in calls],
            [o.content for o in outputs],
        )


def test_a_format_retry_after_the_source_was_revoked_is_not_sent(monkeypatch):
    fabric = Listing()
    sent = model(monkeypatch, fabric, [(200, "not json"), (200, VALID)], revoke_after=1)
    run, call = reading_call(fabric)
    with pytest.raises(Failed) as blocked:
        call()
    assert blocked.value.code == "source_access_withdrawn"
    assert len(sent) == 1 and fabric.reads == 2  # checked before each request; the second never went out
    reads, calls, outputs = stored(run)
    assert reads == [
        (1, "sent"),
        (2, "withheld"),
    ]  # what went out stays sent; only the blocked request is withheld
    assert calls == [
        (1, 15, "send_blocked", [200])
    ]  # the first request's count, usage and status as they were
    assert outputs == ["not json"]  # and its raw answer
    assert run.calls_used == 1


def test_a_resend_after_a_refusal_is_checked_too(monkeypatch):
    fabric = Listing()
    sent = model(monkeypatch, fabric, [(503, ""), (200, VALID)], revoke_after=1)
    run, call = reading_call(fabric)
    with pytest.raises(Failed):
        call()
    assert len(sent) == 1
    assert stored(run)[1] == [(1, 0, "send_blocked", [503])]  # a refusal has no usage


def test_without_a_revocation_the_format_retry_works_as_before(monkeypatch):
    fabric = Listing()
    sent = model(monkeypatch, fabric, [(200, "not json"), (200, VALID)])
    run, call = reading_call(fabric)
    reading = call()
    assert reading is not None and reading.relevant is False
    assert len(sent) == 2 and fabric.reads == 2
    assert "上面的JSON不合格" in json.dumps(sent[1], ensure_ascii=False)  # the retry tells the model why
    reads, calls, outputs = stored(run)
    assert reads == [(1, "sent")]
    assert calls == [(2, 30, None, [200, 200])]
    assert outputs == ["not json", VALID]
    assert run.calls_used == 2


def test_a_call_that_carries_no_evidence_asks_fabric_nothing(monkeypatch):
    fabric = Listing()
    model(monkeypatch, fabric, [(200, "not json"), (200, VALID)])
    row = analyses.create(question="没有证据的调用？", direction="science", is_demo=True, enqueue=False)
    run = Run(row, settings(), fabric=fabric)
    assert run.ask("plan", "计划", {"question": "?"}, schemas.Reading) is not None
    assert fabric.reads == 0


PLAN = schemas.Plan(
    checks=[schemas.PlanCheck(text="后续进展", required=True)],
    queries=[schemas.PlanQuery(terms=["XQ-17"], checks=[1])],
).model_dump_json()


def follow_up_plan(fabric):
    """A follow-up about to plan with its earlier report, whose evidence came from open_src and gone_src;
    the follow-up itself has read nothing yet."""
    from bioradar.research.run import plan

    parent = analyses.create(question="XQ-17 的进展？", direction="pipeline", is_demo=True, enqueue=False)
    with db.session() as s:
        row = s.get(db.Analysis, parent.id)
        row.state, row.completed_at = "completed", db.now()
        row.result = {"draft": "PARENT_ONLY_FACT 上一轮稿件[E2]。", "report_status": "PASS"}
        for ref, source in (("E1", "open_src"), ("E2", "gone_src")):
            s.add(
                db.Evidence(
                    analysis_id=parent.id,
                    ref=ref,
                    source=source,
                    record_id=f"{source}-1",
                    version_id="v" * 64,
                    title="t",
                    text="x",
                    meta={},
                )
            )
    child = analyses.create(
        question="XQ-17 后续应核实什么？",
        direction="pipeline",
        is_demo=True,
        parent_id=parent.id,
        enqueue=False,
    )
    run = Run(analyses.get(child.id), settings(), fabric=fabric)
    return run, lambda: plan.ask_plan(run)


def test_a_follow_ups_plan_retry_is_not_sent_once_an_earlier_rounds_source_is_revoked(monkeypatch):
    fabric = Listing()
    sent = model(monkeypatch, fabric, [(200, "not json"), (200, PLAN)], revoke_after=1)
    run, ask_plan = follow_up_plan(fabric)
    with pytest.raises(Failed) as blocked:
        ask_plan()
    assert blocked.value.code == "source_access_withdrawn"
    assert len(sent) == 1 and "PARENT_ONLY_FACT" in json.dumps(sent[0], ensure_ascii=False)
    assert stored(run)[1] == [(1, 15, "send_blocked", [200])]  # the request that went out, as it was
    assert run.calls_used == 1


def test_a_follow_ups_plan_retry_goes_out_while_the_earlier_rounds_sources_stay_readable(monkeypatch):
    fabric = Listing()
    sent = model(monkeypatch, fabric, [(200, "not json"), (200, PLAN)])
    run, ask_plan = follow_up_plan(fabric)
    model_plan, _ = ask_plan()
    assert model_plan.checks[0].text == "后续进展" and len(sent) == 2
    # the earlier report checked by the plan (previous_report), then before each of the two requests
    assert fabric.reads == 3 and stored(run)[1] == [(2, 30, None, [200, 200])]
