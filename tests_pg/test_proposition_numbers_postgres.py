"""Proposition numbers on a real PostgreSQL: two analyses of one topic that finish at the same moment never
take the same number, because the topic row is locked from reading the counter to the commit that raises it.

The code under test is propositions.number, in the transaction that stores the result."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from bioradar import db
from bioradar.research import propositions


def new_analysis(topic_id):
    with db.session() as s:
        investigation = db.Investigation(
            workspace_id="w", question="q", direction="public_health", is_demo=True, topic_id=topic_id
        )
        s.add(investigation)
        s.flush()
        row = db.Analysis(
            investigation_id=investigation.id,
            trigger="manual",
            method="planner",
            input={"question": "q"},
            input_hash=db.new_id(),
        )
        s.add(row)
        s.flush()
        return row.id


def result_of(*questions):
    judgments = [{"question": q, "conclusion": "待证实", "confidence": "possible"} for q in questions]
    return {"assessment": {"judgments": judgments}, "groups": {"display": "G-1"}}


def test_two_analyses_finishing_together_take_different_numbers():
    with db.session() as s:
        topic = db.Topic(
            workspace_id="w",
            title="t",
            direction="public_health",
            is_demo=True,
            first_seen_at=db.now(),
            last_activity_at=db.now(),
        )
        s.add(topic)
        s.flush()
        topic_id = topic.id
    first, second = new_analysis(topic_id), new_analysis(topic_id)
    holding, released = threading.Event(), threading.Event()
    finished = {}

    def slow(analysis_id):
        """Numbers its judgments and keeps its transaction open, as a result write that is still going on."""
        result = result_of("甲", "乙", "丙")
        with db.session() as s:
            propositions.number(s, s.get(db.Analysis, analysis_id), result)
            holding.set()
            released.wait(10)
        return result

    def quick(analysis_id):
        holding.wait(10)
        result = result_of("丁", "戊")
        with db.session() as s:
            propositions.number(s, s.get(db.Analysis, analysis_id), result)
        finished[analysis_id] = time.monotonic()
        return result

    with ThreadPoolExecutor(2) as pool:
        waiting = pool.submit(slow, first)
        blocked = pool.submit(quick, second)
        time.sleep(1)  # the second one is at the topic row, which the first still holds
        assert not blocked.done()
        released_at = time.monotonic()
        released.set()
        one, other = waiting.result(30), blocked.result(30)

    taken = [j["prop_id"] for r in (one, other) for j in r["assessment"]["judgments"]]
    assert len(taken) == len(set(taken)) == 5
    assert finished[second] >= released_at
    with db.session() as s:
        assert s.get(db.Topic, topic_id).next_prop_seq == 6
