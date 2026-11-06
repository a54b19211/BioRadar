"""Request accounting: a call record's explicit request count is kept, 0 included; only a record that does not
say defaults to 1. The budget (Run.calls_used) counts the same way."""

import pytest
from sqlalchemy import select

from bioradar import db
from bioradar.config import settings
from bioradar.research import analyses
from bioradar.research.pipeline import Failed, Run


def run_():
    row = analyses.create(question="请求怎么记账？", direction="science", is_demo=True, enqueue=False)
    return Run(row, settings(), fabric=None)


def logged(run):
    with db.session() as s:
        rows = s.scalars(select(db.LlmCall).where(db.LlmCall.analysis_id == run.id).order_by(db.LlmCall.id))
        return [(r.stage, r.requests, r.total_tokens, r.error) for r in rows]


@pytest.mark.parametrize("given,counted", [(None, 1), (0, 0), (1, 1), (2, 2)])
def test_an_explicit_request_count_is_kept_and_only_a_missing_one_is_1(given, counted):
    run = run_()
    call = {"stage": "mechevidence", "usage": {}} | ({} if given is None else {"requests": given})
    run.log([call], "mechevidence")
    assert logged(run) == [("mechevidence", counted, 0, None)] and run.calls_used == counted
    assert db.request_count(call) == counted


def test_an_explicit_total_of_zero_tokens_stays_zero():
    assert db.total_tokens({"total_tokens": 0}) == 0
    assert db.total_tokens({"prompt_tokens": 3, "completion_tokens": 2}) == 5  # no total given: its parts


def test_a_call_blocked_before_its_first_request_counts_zero_and_one_blocked_after_it_counts_one():
    run = run_()
    with pytest.raises(Failed):
        run.settle(
            {
                "stage": "read",
                "usage": {},
                "requests": 0,
                "error": "send_blocked",
                "blocked": "source_access_withdrawn",
            }
        )
    assert logged(run) == [("read", 0, 0, "send_blocked")] and run.calls_used == 0
    with pytest.raises(Failed):  # the first request went out before the revocation: it stays counted
        run.settle(
            {
                "stage": "read",
                "usage": {"total_tokens": 15},
                "requests": 1,
                "error": "send_blocked",
                "blocked": "source_access_withdrawn",
            }
        )
    assert logged(run)[-1] == ("read", 1, 15, "send_blocked") and run.calls_used == 1
