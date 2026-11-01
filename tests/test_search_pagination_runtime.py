"""Bounded source pages with actual PG recovery and synthetic source/model responses."""

import copy
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.adapters.fabric_monitor import search_analysis_records
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis_models import AnalysisCreate
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from bioradar.research.investigations.models import InvestigationCreate
from bioradar.research.investigations.query_policy import (
    BOUNDED_INITIAL_VERSION,
    PAGINATION_VERSION,
    _history,
    pagination_candidates,
    question_plan_payload,
    semantic_key,
    validate_initial_plans,
)
from bioradar.research.investigations.service import InvestigationService
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_engine_persistence import GRANT
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_graph import original_record
from test_initial_language_retrieval import context, fixed, plans, progress, save_empty
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_opus_source_adapters import original
from test_query_policy_runtime import expire

pytestmark = pytest.mark.ENGINE


def paged():
    data = fixed()
    data["query_policy"].update(
        initial_retrieval_version=BOUNDED_INITIAL_VERSION,
        pagination_version=PAGINATION_VERSION,
    )
    state, items = progress(), plans()[:1]
    action = context(data, state, items)["candidates"][0]
    query = save_empty(data, state, action)
    query.update(
        status="read",
        returned=1,
        has_more=True,
        pagination={"has_more": True, "next_cursor": "SYNTHETIC-page-2", "max_seq": 17, "page_number": 1},
    )
    return data, state, items, action, query


def test_new_initial_allocation_is_three_without_truncating_explicit_queries():
    data, *_ = paged()
    assert question_plan_payload(data)["max_queries"] == 3
    assert len(validate_initial_plans({"queries": plans()[:3]}, data)) == 3
    with pytest.raises(DomainError):
        validate_initial_plans({"queries": plans()[:4]}, data)
    assert len(validate_initial_plans(plans(), data)) == 6
    assert data["query_policy"]["action_limits"]["document_search"] == 8


def test_real_receipt_only_unlocks_exact_page_once_and_unknown_blocks_it():
    data, state, _, first, query = paged()
    actions = pagination_candidates(data, state, [])
    assert len(actions) == 1
    action = actions[0]
    args = action["arguments"]
    assert args["cursor"] == query["pagination"]["next_cursor"]
    assert args["previous_action_id"] == first["id"] and args["page_number"] == 2
    assert args["limit"] == first["arguments"]["limit"] and args["max_seq"] == 17
    assert _history(data, state)[0]["pagination"] == query["pagination"]
    changed = dict(args, previous_action_id="changed-label", page_number=7, index=99)
    assert semantic_key("document_search", args) == semantic_key("document_search", changed)
    unknown = copy.deepcopy(state)
    unknown["queries"][0]["status"] = "unconfirmed"
    assert pagination_candidates(data, unknown, []) == []
    receipt = save_empty(data, state, action)
    receipt["status"] = "failed"
    assert pagination_candidates(data, state, []) == []


def test_catalog_continues_after_evidence_and_preserves_source_binding():
    data, state, _, first, query = paged()
    first["arguments"].update(catalog_discovery=True, terms=[])
    first["arguments"]["plan"]["terms"] = []
    state["query_policy"]["decisions"][0]["action"] = copy.deepcopy(first)
    query["plan"]["terms"] = []
    state["evidence"] = [{"key": "SYNTHETIC-read-material"}]
    action = pagination_candidates(data, state, [])[0]
    assert action["arguments"]["catalog_discovery"] is True and action["arguments"]["terms"] == []
    altered = copy.deepcopy(state)
    altered["query_policy"]["decisions"][0]["action"]["arguments"]["limit"] = 3
    with pytest.raises(DomainError):
        pagination_candidates(data, altered, [])


@pytest.mark.parametrize("bad_sequence", [False, True])
def test_adapter_sends_one_frozen_page_and_rejects_changed_sequence(bad_sequence):
    requests = []

    def handler(request):
        requests.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "schema_version": "datafabric.history.collection.v1",
                "items": [original("papers")],
                "next_cursor": None,
                "coverage": {"max_seq": 18 if bad_sequence else 17},
            },
        )

    with httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(handler)) as client:
        reader = FabricHistoryReader(client, authorization_ref="SYNTHETIC")
        kwargs = dict(
            source_id="papers",
            is_demo=False,
            terms=[],
            catalog_discovery=True,
            limit=8,
            cutoff=datetime(2026, 10, 31, tzinfo=UTC),
            pagination=True,
            cursor="SYNTHETIC-page-2",
            max_seq=17,
        )
        if bad_sequence:
            with pytest.raises(DomainError):
                search_analysis_records(reader, **kwargs)
        else:
            result = search_analysis_records(reader, **kwargs)
            assert len(result["items"]) == 1 and result["coverage"]["max_seq"] == 17
    assert len(requests) == 1 and requests[0]["cursor"] == "SYNTHETIC-page-2"
    assert requests[0]["limit"] == "8" and "terms" not in requests[0]
    assert "as_of" not in requests[0] and "cutoff" in requests[0]


@pytest.mark.parametrize("recover_received", [False, True])
def test_native_executor_reads_next_page_and_received_restart_does_not_repeat_it(
    runtime,
    database,
    recover_received,
):
    directory = os.environ.get("BIORADAR_QUERY_PLANNER_TEST_PROJECT_DIR")
    if not directory:
        pytest.fail("native query-planner directory is required")
    make, actor, _, source, model, settings, _ = runtime
    settings.query_planner_project_dir = Path(directory)
    settings.analysis_query_policy = "query_planner"
    settings.analysis_v2_records_per_query = 2
    source.records = [
        original_record("synthetic-page-" + str(i), {"body": f"SYNTHETIC membrane observation {i}."})
        for i in range(3)
    ]
    _, inquiry = InvestigationService(make().engine).create(
        actor,
        InvestigationCreate(
            title="SYNTHETIC pages", question="SYNTHETIC membrane observations?", is_demo=True
        ),
        "pages-inquiry",
    )

    def model_reply(payload, result):
        if payload["task"] == "question_plan":
            assert payload["max_queries"] == 3
            result["queries"] = [{"source_id": "fixture-source", "terms": ["SYNTHETIC"], "reason": "literal"}]
        elif payload["task"] == "read_plan":
            result["next_queries"], result["gaps"] = [], []

    model.transform = model_reply
    requests = []

    def source_reply(request):
        if request.url.path != "/api/history/records":
            return source.handler(request)
        args = dict(request.url.params)
        requests.append(args)
        assert args["limit"] == "2" and request.url.params.get_list("terms") == ["SYNTHETIC"]
        cursor = args.get("cursor")
        assert cursor in {None, "SYNTHETIC-page-2"}
        return httpx.Response(
            200,
            json={
                "schema_version": "datafabric.history.collection.v1",
                "items": source.records[:2] if cursor is None else source.records[2:],
                "next_cursor": "SYNTHETIC-page-2" if cursor is None else None,
                "coverage": {"max_seq": 17, "as_of": args["cutoff"]},
            },
        )

    accepted = make().start(
        actor,
        inquiry["id"],
        AnalysisCreate(expected_revision=1, auto_retrieve=True),
        "pages-analysis",
    )[1]
    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(source_reply)) as client:
        service = make()
        service.reader = FabricHistoryReader(client, authorization_ref=GRANT, max_seconds=90)
        if recover_received:
            checkpoint = service._checkpoint

            def exit_after_page(identifier, token, state, **kwargs):
                checkpoint(identifier, token, state, **kwargs)
                if any(
                    q.get("pagination", {}).get("page_number") == 2 and q["status"] == "read"
                    for q in state["queries"]
                ):
                    raise SQLAlchemyError("SYNTHETIC exit after page originals committed")

            service._checkpoint = exit_after_page
            with pytest.raises(RuntimeError):
                service.execute(accepted["id"], accepted["input_hash"])
            assert len(requests) == 2
            with Session(database) as session:
                row = session.get(InvestigationAnalysisRecord, accepted["id"])
                page = next(
                    q for q in row.progress["queries"] if q.get("pagination", {}).get("page_number") == 2
                )
                assert page["records"] == source.records[2:] and page["pagination"]["max_seq"] == 17
            expire(database, accepted["id"])
            service = make()
            service.reader = FabricHistoryReader(client, authorization_ref=GRANT, max_seconds=90)
            service.recover()
        try:
            service.execute(accepted["id"], accepted["input_hash"])
        except RuntimeError:
            with Session(database) as session:
                saved = session.get(InvestigationAnalysisRecord, accepted["id"])
                pytest.fail(str(saved.progress.get("execution_diagnostic", saved.error_code)))
    result = make().get(actor, inquiry["id"], accepted["id"])
    assert result.state in {"completed", "needs_review"}, result.error_code
    from tests.query_policy_assertions import assert_query_policy

    assert_query_policy(result, expected_policy="query_planner")
    pages = [q for q in result.queries if q.pagination is not None]
    assert len(pages) == len(requests) == 2
    assert pages[0].pagination.has_more and not pages[1].pagination.has_more
    assert all(q.cost["tool_calls"] == 1 for q in pages)
    assert len(result.evidence) == 3
    with Session(database) as session:
        saved = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert sum(len(q["records"]) for q in saved.progress["queries"] if q.get("pagination")) == 3
        assert (
            saved.fixed_input["query_policy"]["action_limits"]["document_search"]
            == settings.analysis_v2_action_limits["document_search"]
        )
        assert saved.fixed_input["query_policy"]["initial_retrieval_version"] == BOUNDED_INITIAL_VERSION
    posts = len(model.requests)
    make().execute(accepted["id"], accepted["input_hash"])
    assert len(model.requests) == posts and len(requests) == 2
