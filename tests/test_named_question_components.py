"""Only declared exact question spans can split a composite object."""

import pytest
from bioradar.api.errors import DomainError
from bioradar.research.investigations.query_policy import (
    build_coverage_checks,
    named_object_context,
    question_named_objects,
)

pytestmark = pytest.mark.BOOT


def context(texts, *, components=True):
    question = "Compare ALPHA-17/RECEPTOR7."
    start, end = 8, len(question) - 1
    item = {"id": "pair", "name": question[start:end], "question_span": {"start": start, "end": end}}
    if components:
        item["components"] = [
            {"id": "a", "name": "ALPHA-17", "question_span": {"start": 8, "end": 16}},
            {"id": "b", "name": "RECEPTOR7", "question_span": {"start": 17, "end": 26}},
        ]
    fixed = {"question": question, "search_scope": ["pubmed"]}
    progress = {
        "query_policy": {"named_objects": [item]},
        "queries": [],
        "evidence": [{"key": str(i), "text": text} for i, text in enumerate(texts)],
    }
    candidates = []
    return named_object_context(fixed, progress, candidates, []), candidates


def test_two_declared_atoms_read_separately_do_not_claim_the_relationship():
    results, candidates = context(["ALPHA-17 was tested.", "RECEPTOR7 was measured."])
    assert [c["status"] for c in results] == ["read", "read"]
    assert all("question_relation" in c["reason"] for c in results)
    assert all(
        set(c) <= {"id", "name", "question_span", "status", "evidence_ids", "candidate_ids", "reason"}
        for c in results
    )
    assert candidates == []


def test_genuinely_unread_atom_gets_an_exact_original_term_query():
    results, candidates = context(["ALPHA-17 was tested."])
    assert [c["status"] for c in results] == ["read", "unread"]
    assert candidates[0]["arguments"]["terms"] == ["RECEPTOR7"]


def test_slash_identifier_is_not_split_without_explicit_components_and_invalid_spans_reject():
    results, candidates = context(["ALPHA-17 and RECEPTOR7"], components=False)
    assert results[0]["status"] == "unread"
    assert candidates[0]["arguments"]["terms"] == ["ALPHA-17/RECEPTOR7"]
    with pytest.raises(DomainError):
        question_named_objects(
            {"question": "Drug/Target"},
            [
                {
                    "id": "pair",
                    "name": "Drug/Target",
                    "question_span": {"start": 0, "end": 11},
                    "components": [
                        {"id": "d", "name": "Drug", "question_span": {"start": 0, "end": 4}},
                        {"id": "t", "name": "invented", "question_span": {"start": 5, "end": 11}},
                    ],
                }
            ],
        )


def test_atomic_read_status_does_not_close_the_relation_coverage_check():
    question = "DrugA/TargetB"
    fixed = {
        "question": question,
        "query_policy": {
            "effective": "query_planner",
            "research_contract_version": "bioradar-research-input-v1",
        },
    }
    progress = {
        "evidence": [{"key": "a"}, {"key": "b"}],
        "query_policy": {
            "named_objects": [
                {
                    "id": "pair",
                    "name": question,
                    "question_span": {"start": 0, "end": len(question)},
                    "components": [
                        {"id": "a", "name": "DrugA", "question_span": {"start": 0, "end": 5}},
                        {"id": "b", "name": "TargetB", "question_span": {"start": 6, "end": 13}},
                    ],
                }
            ]
        },
    }
    check = next(
        c for c in build_coverage_checks(fixed, progress) if c["id"].startswith("question_relation:")
    )
    assert check["status"] == "unknown" and check["evidence_ids"] == []
