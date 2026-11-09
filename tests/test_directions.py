"""A record's direction is the triage model's answer: left out of it, it is not filled in as public health.
Policy is a direction like the others: asked for, chosen by the triage and searched in its own scopes."""

import json

import pytest
from conftest import FakeFabric, record
from sqlalchemy import select
from test_access import admin_client, fabric_sources  # noqa: F401 - the fixture these tests take
from test_monitor import DAY

from bioradar import db
from bioradar.adapters import llm
from bioradar.research import analyses, monitor
from bioradar.research.run import search


def model_answers(monkeypatch, direction, topic="启衡新厂房", signal="capacity"):
    """The triage model's raw answer for the one record (a new topic at level important), without a direction
    when `direction` is None. It goes through the real call: the schema check and the one format retry."""
    item = {"index": 1, "new_topic": topic, "alert": "important", "signal": signal, "related": "related"}
    if direction:
        item["direction"] = direction
    content = json.dumps({"items": [item]}, ensure_ascii=False)
    answer = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {}}
    monkeypatch.setattr(llm, "post", lambda s, body, call, limit=None, before_send=None: answer)


def test_a_triage_answer_without_a_direction_is_a_failed_triage_never_public_health(monkeypatch):
    fabric = FakeFabric([record("T1", "启衡生物抗体原液厂房环评公示", "x", available=DAY.format(3))])
    model_answers(monkeypatch, None)  # both answers, the format retry's too, leave the direction out
    monitor.tick(is_demo=True, fabric=fabric)
    with db.session() as s:
        [row] = s.scalars(select(db.Observation)).all()
        assert (row.reason, row.relevance, row.direction, row.topic_id) == ("triage_failed", None, None, None)
        assert s.scalars(select(db.Topic)).all() == []
    # the retry path asks again, and what the model names this time is what is stored
    model_answers(monkeypatch, "business")
    monitor.tick(is_demo=True, fabric=fabric)
    with db.session() as s:
        [row] = s.scalars(select(db.Observation)).all()
        [topic] = s.scalars(select(db.Topic)).all()
        assert (row.direction, topic.direction) == ("business", "business")


def test_the_triage_can_file_a_record_under_policy(monkeypatch):
    fabric = FakeFabric([record("T1", "国家医保局发布谈判药品续约规则", "x", available=DAY.format(3))])
    model_answers(monkeypatch, "policy", topic="医保谈判药品续约规则", signal="policy")
    monitor.tick(is_demo=True, fabric=fabric)
    with db.session() as s:
        [row] = s.scalars(select(db.Observation)).all()
        [topic] = s.scalars(select(db.Topic)).all()
        assert (row.direction, topic.direction) == ("policy", "policy")


@pytest.fixture
def api(fabric_sources, monkeypatch):  # noqa: F811
    monkeypatch.setattr(analyses, "dispatch", lambda analysis_id: None)
    fabric_sources["value"] = ["pubmed"]
    return admin_client()


def test_policy_is_a_direction_the_api_lists_and_accepts_and_a_made_up_one_is_not(api):
    assert "policy" in api.get("/api/options").json()["directions"]
    ask = {"question": "新发的审评审批制度怎样影响同类药？", "sources": ["pubmed"]}
    response = api.post("/api/analyses", json={**ask, "direction": "policy"})
    assert response.status_code == 200
    assert analyses.get(response.json()["id"]).input["direction"] == "policy"
    assert api.post("/api/analyses", json={**ask, "direction": "regulation"}).status_code == 422


def test_every_direction_has_its_search_scopes_and_policy_reads_policy_and_pipeline():
    assert set(search.SEARCH_SCOPES) == analyses.DIRECTIONS  # none falls back to the default scope
    assert search.SEARCH_SCOPES["policy"] == ("policy", "pipeline")
